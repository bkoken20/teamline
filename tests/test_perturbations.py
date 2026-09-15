"""Re-derive every "this check can fail" claim in docs/FIX_LOG.md.

For each entry in perturbations.py: break the code exactly as described, run the suite, and require
that the NAMED check goes red. A perturbation that leaves the suite green means either the fix is no
longer load-bearing or the check has quietly become a tautology. Either way you want to know, and
you should not have to be watching to find out.

    python tests/test_perturbations.py            # all of them (minutes: the e2e ones are ~30 s each)
    python tests/test_perturbations.py A4 A5      # only the ids that start with these
    python tests/test_perturbations.py --unit     # only the fast ones

Every file it touches is restored in a `finally`, and the restore is VERIFIED by comparing bytes
before exit. If a restore ever fails the run stops immediately and says which file is dirty, because
leaving a deliberately broken source behind would be far worse than the check it was proving.
"""
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, HERE)
from perturbations import PERTURBATIONS                                   # noqa: E402

SUITES = {"unit": os.path.join(HERE, "test_switchboard.py"),
          "e2e": os.path.join(HERE, "test_switchboard_e2e.py")}


def read(path):
    with open(path, "rb") as fh:
        return fh.read()


def write(path, data):
    with open(path, "wb") as fh:
        fh.write(data)


def run_suite(which):
    """Return the suite's stdout. Its exit code is not the signal here -- we want to know whether ONE
    named check failed, and a suite can be red for an unrelated reason."""
    p = subprocess.run([sys.executable, SUITES[which]], capture_output=True, text=True,
                       encoding="utf-8", errors="replace", cwd=ROOT, timeout=300)
    return (p.stdout or "") + (p.stderr or "")


def failed_checks(out):
    return [ln.split("FAIL", 1)[1].strip() for ln in out.splitlines() if ln.strip().startswith("FAIL")]


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    only_unit = "--unit" in sys.argv
    todo = [p for p in PERTURBATIONS
            if (not args or any(p["id"].startswith(a) for a in args))
            and (not only_unit or p["suite"] == "unit")]

    print("PERTURBATION RUN -- %d of %d claims\n" % (len(todo), len(PERTURBATIONS)))
    bad = []
    for p in todo:
        path = os.path.join(ROOT, p["file"])
        original = read(path)
        try:
            src = original.decode("utf-8")
            if src.count(p["find"]) == 0:
                bad.append((p["id"], "the code to break is not there any more -- the fix was changed "
                                     "or removed, and this claim no longer describes the repository"))
                print("  STALE  %-12s %s" % (p["id"], p["must_fail"][:70]))
                continue
            if not p.get("all_occurrences") and src.count(p["find"]) != 1:
                bad.append((p["id"], "matches %d places; a perturbation must be exact"
                            % src.count(p["find"])))
                print("  AMBIG  %-12s %s" % (p["id"], p["must_fail"][:70]))
                continue
            write(path, src.replace(p["find"], p["repl"]).encode("utf-8"))
            out = run_suite(p["suite"])
            fails = failed_checks(out)
            fired = [f for f in fails if p["must_fail"] in f]
            if fired:
                print("  FIRES  %-12s %s" % (p["id"], p["must_fail"][:70]))
            elif "  PASS" not in out:
                # The suite produced no checks at all, so the perturbation broke the code outright --
                # an import error, a syntax error -- rather than changing behaviour. That says
                # nothing about whether the check can fail, and reporting it as "did not fail" would
                # imply a tautology that has not been demonstrated. Caught the first time this ran:
                # one spec produced `str + list` through operator precedence.
                bad.append((p["id"], "the perturbation BROKE the suite instead of changing behaviour "
                                     "-- it never ran, so this claim is untested. Fix the spec. Tail: %s"
                            % out.strip().splitlines()[-1][:120] if out.strip() else "no output"))
                print("  BROKE  %-12s %s" % (p["id"], p["must_fail"][:70]))
            else:
                bad.append((p["id"], "the check did NOT fail. Either the fix is no longer "
                                     "load-bearing, or the check cannot fail. Other checks that did "
                                     "fail: %s" % (fails[:3] or "none")))
                print("  SILENT %-12s %s" % (p["id"], p["must_fail"][:70]))
        finally:
            write(path, original)
            if read(path) != original:                     # never leave a broken source behind
                print("\nSTOP: %s could not be restored. Fix it before anything else." % p["file"])
                return 3

    print()
    if bad:
        print("%d claim(s) did not hold:\n" % len(bad))
        for i, why in bad:
            print("  %s: %s" % (i, why))
        return 1
    print("all %d claims hold: every fix is load-bearing and every check can fail." % len(todo))
    return 0


if __name__ == "__main__":
    sys.exit(main())
