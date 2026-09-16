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
import io
import os
import re
import subprocess
import sys
import tempfile
import time
import uuid

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


def run_suite(which, needle=""):
    """Return the suite's stdout. Its exit code is not the signal here -- we want to know whether ONE
    named check failed, and a suite can be red for an unrelated reason.

    `needle` supplies the de-identification scan's list for this run. The list lives OUTSIDE the
    repository (that was R-12), so a claim about that scan has to hand it one -- and the needle is
    generated fresh per run rather than written down here, because a needle spelled in this file
    would already be in the tree the scan walks, and the check would go red with the perturbation
    doing nothing at all. A claim that fires either way proves nothing.

    EVERY run gets one, not only the claims that plant it. Two checks print NOT RUN without a list,
    and a claim naming a check that did not run reads as "the check cannot fail" -- which is how this
    runner started exiting 1 on a fresh clone the moment the list moved out of the repository. That
    is V-1's failure exactly: the advertised command broken for every reader but us. A needle that is
    in no file and in no commit lets both checks run and measure honestly."""
    env, tmp = dict(os.environ), ""
    if needle:
        fd, tmp = tempfile.mkstemp(prefix="deid-", suffix=".txt")
        with io.open(fd, "w", encoding="utf-8") as fh:
            fh.write(needle + "\n")
        env["TEAMLINE_DEID_LIST"] = tmp
    try:
        p = subprocess.run([sys.executable, SUITES[which]], capture_output=True, text=True,
                           encoding="utf-8", errors="replace", cwd=ROOT, timeout=300, env=env)
        return (p.stdout or "") + (p.stderr or "")
    finally:
        if tmp:
            os.unlink(tmp)


def failed_checks(out):
    return [ln.split("FAIL", 1)[1].strip() for ln in out.splitlines() if ln.strip().startswith("FAIL")]


def ran(out, name):
    """Did the named check RUN at all -- either way round?

    A perturbation can stop the suite part way through: enough checks run to produce PASS lines, so
    the run does not look broken, but the one being pinned is never reached. Reported as "the check
    did NOT fail", that reads as `this check cannot fail` -- the most expensive wrong conclusion this
    runner can draw, because it is the exact sentence it exists to catch honestly. So ask whether the
    check appeared at all before judging what it did."""
    return any(name in ln for ln in out.splitlines()
               if ln.strip().startswith(("PASS", "FAIL")))


def check_count():
    """How many ck( assertions the two suites hold. Counted, never written down."""
    here = os.path.dirname(os.path.abspath(__file__))
    return sum(len(re.findall(r"^\s*ck\(", io.open(os.path.join(here, f), encoding="utf-8").read(), re.M))
               for f in sorted(os.listdir(here))
               if f.startswith("test_") and f.endswith(".py") and f != "test_perturbations.py")


def scope_line(pinned, total=None):
    """The verdict's scope sentence, in one place.

    It exists as a function because a check asserts on what this runner PRINTS. That check used to
    grep this file for the words of an overstatement it had already been fixed to stop making, which
    caught one spelling and let every synonym past. `--scope` prints this line without running a
    single claim, so the assertion can be made against the real output for the price of a subprocess.
    """
    total = check_count() if total is None else total
    return ("Scope: %d of the %d checks in the two suites are pinned this way. The rest are present "
            "but UNPROVEN -- no one has shown they can fail." % (pinned, total))


def main():
    if "--scope" in sys.argv:
        print(scope_line(len(PERTURBATIONS)))
        return 0
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
            # Every run gets a one-off needle so the two list-dependent checks actually run; a claim
            # that wants it planted as well writes `{NEEDLE}` into its payload.
            #
            # `{DATE}` and `{CLOCK}` are filled the same way and for a sharper reason: a claim that
            # proves the dated-comment check can fail has to PLANT a date, and this file is inside the
            # tree that check walks. Spelled here, it would make the check red for ever and the claim
            # would fire with the perturbation doing nothing. They are formatted from the clock, so
            # no date and no time appears in this file at all.
            needle = "zz-" + uuid.uuid4().hex[:12]
            payload = (p["repl"].replace("{NEEDLE}", needle)
                       .replace("{DATE}", time.strftime("%Y-%m-%d"))
                       .replace("{CLOCK}", time.strftime("%H:%M"))
                       # `{ADDR}` is a private-range address, formatted rather than written, for the
                       # same reason: spelled here it would sit in the tree the address check walks.
                       .replace("{ADDR}", "%d.%d.%d.%d" % (10, 0, 0, 2))
                       # `{MISSING}` is the name of a document that is not in the repository. Written
                       # here it would BE a dangling reference, in a file the check reads.
                       # Note the extension is a separate slot: written as one literal, this line
                       # would itself read as a reference to a document that does not exist.
                       .replace("{MISSING}", "NOT_A_REAL_DOCUMENT_%s.%s" % (needle[3:9], "md"))
                       # `{LINES}` is a count far enough from any real one to fail a 20% band. A
                       # claim about self-counting prose cannot carry the figures, or it goes stale
                       # every time the thing it counts changes size.
                       .replace("{LINES}", "99,000"))
            write(path, src.replace(p["find"], payload).encode("utf-8"))
            out = run_suite(p["suite"], needle)
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
            elif not ran(out, p["must_fail"]):
                # The suite ran, and this check is not in its output at all -- so the perturbation
                # stopped the run before reaching it. It says NOTHING about whether the check can
                # fail, and the branch below would have called it SILENT, which reads as a tautology
                # that was never demonstrated.
                bad.append((p["id"], "the suite stopped before this check ran, so the claim is "
                                     "UNTESTED -- not silent. Narrow the perturbation. Checks that "
                                     "did fail: %s" % (fails[:2] or "none")))
                print("  NORUN  %-12s %s" % (p["id"], p["must_fail"][:70]))
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
    # State the SCOPE with the verdict. This line used to read "every fix is load-bearing and every
    # check can fail", which claimed the whole suite. It pins the claims made in the fix log; the
    # other checks are present but unproven -- nobody has shown they can fail, which is exactly the
    # state a check that cannot fail hides in. Naming the fraction is the honest version, and it is
    # computed rather than written down so it cannot drift.
    print("all %d claims in the fix log hold: each names a check that goes RED when its fix is undone."
          % len(todo))
    print(scope_line(len(todo)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
