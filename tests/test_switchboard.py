"""TEAMLINE SWITCHBOARD -- red-first tests for the extension directory, concurrent calls, hygiene.

Contract: docs/PROTOCOL.md. session-chosen team-prefixed ext; `now`
with age, model line wins < 30 min; BUSY(turn) from `running`, rings to running sessions still
queue behind the turn (never blocked); cap 6 concurrent calls INTO beta (operator 09-08); 2 h STALE / 24 h retire /
10 min GONE / 7-day voicemail hold; re-registration replaces GONE/STALE, refused for LIVE; register-time
directory snapshot, then pull only; ledger is the source of truth. v1 (teamline_state) keeps running.
Run: python tests/test_switchboard.py
"""
import asyncio
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "teamline"))
# The suite defines the teams it exercises, so a fresh clone runs green with no setup. The library
# ships with a different default; check 14 asserts that separately.
os.environ.setdefault("TEAMLINE_TEAMS", "alpha,beta,gamma")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
FAILS = []
H, M = 3600.0, 60.0


def ck(name, cond, detail=""):
    print("  %s  %s%s" % ("PASS" if cond else "FAIL", name, "" if cond else "\n        [%s]" % (detail,)))
    if not cond:
        FAILS.append(name)


class Clock:
    def __init__(self):
        self.t = 100_000.0

    def __call__(self):
        return self.t


# The library ships UNCAPPED; a cap is a deployment choice. The suite configures one so that the cap
# behaviour is still exercised, and check 14 asserts the shipped default separately.
CAP_INTO_TEST = {"beta": 6}


def fresh(tmp, clk=None, cap_into=None):
    import switchboard as SB
    clk = clk or Clock()
    sb = SB.Switchboard(ledger_path=os.path.join(tmp, "sb.jsonl"), calls_dir=os.path.join(tmp, "calls"), now=clk,
                        cap_into=dict(CAP_INTO_TEST if cap_into is None else cap_into))
    return SB, sb, clk


def dirmap(sb):
    return {e["ext"]: e for e in sb.directory()}


def main():
    print("TEST -- switchboard: extensions, directory, concurrent calls, hygiene\n")
    try:
        import switchboard as SB  # noqa
    except ImportError as e:
        ck("switchboard imports", False, "no switchboard exists -- the line still connects teams, not sessions (%s)" % e)
        return finish()
    tmp = tempfile.mkdtemp(prefix="sb_")
    SB, sb, clk = fresh(tmp)

    # ---- 1. registration + naming
    r = sb.register("beta", "deep-work", now="deep work on the parser", session_id="sess-1")
    ck("register returns team/ext and a directory snapshot",
       r["ext"] == "beta/deep-work" and isinstance(r.get("directory"), list), r)
    r2 = sb.register("alpha", "docs-writer", now="writing the docs", feed=True)
    ck("an extension registers with a feed and no session id", r2["ext"] == "alpha/docs-writer", r2)
    try:
        sb.register("alpha", "beta/x", now="n", feed=True)
        ck("an ext may not carry another team's prefix", False, "no error")
    except SB.SwitchError:
        ck("an ext may not carry another team's prefix", True)
    try:
        sb.register("beta", "deep-work", now="n", session_id="sess-9")
        ck("re-registering a LIVE ext is refused (two sessions never share a name)", False, "no error")
    except SB.SwitchError as e:
        ck("re-registering a LIVE ext is refused (two sessions never share a name)", "LIVE" in str(e), e)
    try:
        sb.register("beta", "Bad Name!", now="n", session_id="s")
        ck("ext names are [a-z0-9-]{1,32}", False, "no error")
    except SB.SwitchError:
        ck("ext names are [a-z0-9-]{1,32}", True)

    # ---- 2. directory: now + age, states
    clk.t += 12 * M
    dd = dirmap(sb)["beta/deep-work"]
    ck("directory shows now WITH its age (load-bearing: a now-line with no age cannot be judged)",
       dd["now"] == "deep work on the parser" and abs(dd["now_age_s"] - 12 * M) < 1, dd)
    ck("directory shows state IDLE, team, last_seen age",
       dd["state"] == "IDLE" and dd["team"] == "beta" and "last_seen_age_s" in dd, dd)
    sb.set_now("beta", "deep-work", "still on the parser")
    dd = dirmap(sb)["beta/deep-work"]
    ck("sw_now refreshes the line and resets its age", dd["now"].startswith("still on the parser") and dd["now_age_s"] < 1, dd)
    sb.set_now_derived("beta", "deep-work", "goal: parser")
    ck("model's now wins while younger than 30 min", dirmap(sb)["beta/deep-work"]["now"].startswith("still on the parser"))
    clk.t += 31 * M
    dd = dirmap(sb)["beta/deep-work"]
    ck("after 30 min the derived line shows, tagged derived", dd["now"] == "goal: parser" and dd["now_source"] == "derived", dd)
    sb.set_running("beta", {"sess-1": True})
    dd = dirmap(sb)["beta/deep-work"]
    ck("running=true from session.list shows as BUSY(kind=turn)", dd["state"] == "BUSY" and dd["busy_kind"] == "turn", dd)

    # ---- 3. calls: a ring to a running session is queued behind the turn, not blocked
    r = sb.call("alpha", "docs-writer", "beta/deep-work", "subject", "have you got a moment?")
    ck("a ring to a BUSY(turn) ext is NOT refused: RINGING with the busy kind attached",
       r["state"] == "RINGING" and r["peer_busy_kind"] == "turn" and r["call_id"], r)
    ev = sb.pending_for("beta/deep-work")
    ck("the ring is pending for the callee ext, lane=steer while running, TEAMLINE prefix",
       len(ev) == 1 and ev[0]["kind"] == "ring" and ev[0]["lane"] == "steer" and ev[0]["text"].startswith("TEAMLINE"), ev)
    sb.set_running("beta", {"sess-1": False})
    ck("busy(turn) clears when running=false and the ring is still up", dirmap(sb)["beta/deep-work"]["state"] == "RINGING")
    try:
        sb.call("alpha", "docs-writer", "beta/deep-work", "x", "y")
        ck("an ext holds at most one call", False, "no error")
    except SB.SwitchError:
        ck("an ext holds at most one call", True)
    cid1 = r["call_id"]
    sb.answer("beta", "deep-work")
    sb.say("beta", "deep-work", "yes: three of them")
    ev = sb.pending_for("alpha/docs-writer")
    ck("in-call lines route to the caller ext only", any(e["kind"] == "say" and "three of them" in e["text"] for e in ev), ev)
    ck("the caller's own line is not echoed back to it",
       sb.say("alpha", "docs-writer", "thanks") and not any("thanks" in e["text"] for e in sb.pending_for("alpha/docs-writer")))

    # ---- 4. concurrency: second pair in parallel, cap 6 into beta
    # The cap is PER TEAM, counted across every ringing/open call whose callee is on that team.
    # 3 was one deployment's answer on how many simultaneous wakes a session could absorb; it was
    # later raised to 6 on the same grounds -- each wake is answerable, so the cap is comfort not capacity
    # ("increase to 6, each can answer, simultaneously, no problem") -- one live lane each.
    sb.register("beta", "spec-owner", now="spec package", session_id="sess-2")
    sb.register("alpha", "spec-review", now="reviewing the spec", feed=True)
    r = sb.call("alpha", "spec-review", "beta/spec-owner", "ruler", "ready?")
    ck("a second call runs concurrently with the first", r["state"] == "RINGING" and len(sb.active_calls()) == 2, sb.active_calls())
    for i in range(3, 9):
        sb.register("beta", "ext-%d" % i, now="n", session_id="sess-%d" % i)
        sb.register("alpha", "q-%d" % i, now="n", feed=True)
    rs = [sb.call("alpha", "q-%d" % i, "beta/ext-%d" % i, "s", "o") for i in range(3, 8)]
    ck("SIX concurrent calls into beta are allowed (2 already open + 4 more all RINGING)",
       all(x["state"] == "RINGING" for x in rs[:4]) and len(sb.active_calls()) == 6,
       [x["state"] for x in rs] + [len(sb.active_calls())])
    r7 = rs[4]
    ck("the SEVENTH is CAPPED with the cap stated, voicemail offered (per team, not per session)",
       r7["state"] == "CAPPED" and r7["cap"] == 6 and r7["voicemail_offered"], r7)
    # ext-8 is registered but in no call: at cap 6 the earlier ones are all busy, so use a free lane
    r5 = sb.call("beta", "ext-8", "alpha/q-8", "s", "o")
    ck("outbound from beta is uncapped (a 7th call OUT while 6 are in)", r5["state"] == "RINGING", r5)
    sb.decline("alpha", "q-8", "later")
    for i in range(3, 8):
        try:
            sb.decline("beta", "ext-%d" % i, "later")
        except SB.SwitchError:
            pass
    sb.decline("beta", "spec-owner", "later")   # ext-3..7 already declined above; leaves only cid1 open

    # ---- 5. voicemail per extension
    sb.register("beta", "vm-target", now="n", session_id="sess-vm")
    sb.busy("beta", "vm-target", True, "walk", "walk running")
    r = sb.leave("alpha", "docs-writer", "beta/vm-target", "ping when the walk ends")
    ck("voicemail is addressed to an ext and held while it is BUSY", r["queued"] and not sb.pending_for("beta/vm-target"), r)
    sb.busy("beta", "vm-target", False)
    ev = sb.pending_for("beta/vm-target")
    ck("voicemail is released as lane=queue when the ext goes IDLE", len(ev) == 1 and ev[0]["lane"] == "queue", ev)
    sb.mark_delivered(ev[0]["id"], "sess-vm")

    # ---- 6. hygiene
    clk.t += 2 * H + 1
    dd = dirmap(sb)["beta/vm-target"]
    ck("idle > 2 h shows STALE, still listed", dd["hygiene"] == "STALE", dd)
    r = sb.call("alpha", "q-5", "beta/vm-target", "s", "o")
    ck("a ring to a STALE ext is allowed and the caller is warned", r["state"] == "RINGING" and r.get("warning"), r)
    sb.decline("beta", "vm-target", "busy")
    live = {"sess-1": False, "sess-2": False, "sess-3": False, "sess-4": False, "sess-5": False}   # sess-vm gone
    sb.set_running("beta", live)
    dd = dirmap(sb)["beta/vm-target"]
    ck("a session missing from session.list is GONE at once", dd["hygiene"] == "GONE", dd)
    r = sb.call("alpha", "q-5", "beta/vm-target", "s", "o")
    ck("a ring to a GONE ext is refused with voicemail offered", r["state"] == "GONE" and r["voicemail_offered"], r)
    del live["sess-1"]                                                                            # deep-work gone mid-call
    sb.set_running("beta", live)
    ck("the peer of an ACTIVE call going GONE ends it as peer_lost with a transcript",
       any(x["event"] == "peer_lost" and x["call_id"] == cid1 for x in sb.ledger_rows())
       and cid1 not in {c["call_id"] for c in sb.active_calls()}
       and os.path.exists(os.path.join(tmp, "calls", cid1 + ".md")), [x["event"] for x in sb.ledger_rows()[-4:]])
    r = sb.register("beta", "vm-target", now="back", session_id="sess-vm2")
    ck("re-registering a GONE ext replaces it (old binding retired, reason replaced)",
       r["ext"] == "beta/vm-target" and any(x["event"] == "retired" and x.get("reason") == "replaced" for x in sb.ledger_rows()), r)
    sb.set_running("beta", {"sess-2": False})                                                  # vm2, ext-3.. gone
    clk.t += 11 * M
    sb.tick()
    exts = {e["ext"] for e in sb.directory()}
    ck("GONE > 10 min is retired from the directory (tick)", "beta/vm-target" not in exts and "beta/ext-3" not in exts, exts)
    sb.leave("alpha", "q-5", "beta/vm-target", "held voicemail")
    sb.register("beta", "vm-target", now="again", session_id="sess-vm3")
    sb.set_running("beta", {"sess-2": False, "sess-vm3": False})
    ck("voicemail left for a retired ext is held and re-attached on re-registration within 7 days",
       any(e["kind"] == "voicemail" and "held voicemail" in e["text"] for e in sb.pending_for("beta/vm-target")), sb.pending_for("beta/vm-target"))
    clk.t += 24 * H + 1
    sb.touch("alpha/q-5")                     # keep one ext alive for the operator-retire check
    sb.tick()
    ck("idle > 24 h is retired", "beta/vm-target" not in {e["ext"] for e in sb.directory()})
    r = sb.operator_retire("alpha/q-5")
    ck("operator retire is a ledger row tagged operator",
       r["ok"] and any(x["event"] == "retired" and x.get("by") == "operator" for x in sb.ledger_rows()))

    # ---- 7. ledger is the source of truth
    # THE STATE HAS TO EXIST BEFORE REPLAYING IT MEANS ANYTHING. Everything registered above has been
    # retired by this point -- the 24-hour tick, the operator retire, and the 11-minute GONE sweep
    # between them empty the directory -- so these two checks used to compare `set()` with `set()` and
    # run `all([])` over `[]`. Both passed, on nothing. That is the suite's HEADLINE property, the one
    # the README sells: restart the broker and the ledger rebuilds it.
    #
    # So the state is built here rather than inherited: lanes on two teams, an open call, and held
    # voicemail, all committed to the same ledger the replay reads.
    sb.register("beta", "replay-a", now="working", session_id="sess-ra")
    sb.register("beta", "replay-b", now="working", session_id="sess-rb")
    sb.register("alpha", "replay-c", now="holding", feed=True, sid="s-rc")
    rr = sb.call("alpha", "replay-c", "beta/replay-a", "replay subject", "replay opening")
    sb.answer("beta", "replay-a")
    sb.leave("alpha", "replay-c", "beta/replay-b", "a message that must survive a restart")
    live_exts = {e["ext"] for e in sb.directory()}
    ck("the replay fixture is NOT empty -- these checks compared set() with set() before",
       len(live_exts) >= 3 and len(sb.active_calls()) >= 1,
       (sorted(live_exts), len(sb.active_calls())))

    SB2, sb2, _ = fresh(tmp, clk)
    ck("a restart rebuilds the directory and active calls from the ledger",
       {e["ext"] for e in sb2.directory()} == {e["ext"] for e in sb.directory()}
       and len(sb2.active_calls()) == len(sb.active_calls()), ({e["ext"] for e in sb2.directory()}, {e["ext"] for e in sb.directory()}))
    ck("...and the rebuilt call is the same call, in the same state, with its subject",
       [(c["call_id"], c["state"], c["subject"]) for c in sb2.active_calls()]
       == [(c["call_id"], c["state"], c["subject"]) for c in sb.active_calls()],
       (sb2.active_calls(), sb.active_calls()))
    # `pending_for` on a lane that does not exist returns nothing rather than raising, which is what
    # lets this check FAIL instead of crashing when the fixture above is missing. That distinction is
    # not cosmetic: a check that raises takes the whole suite down before the later ones run, and a
    # perturbation aimed at it then reports "the claim is untested" rather than proving anything.
    ck("...and an undelivered message is still owed after the restart",
       [e["text"] for e in sb2.pending_for("beta/replay-b")]
       == [e["text"] for e in sb.pending_for("beta/replay-b")] != [],
       (sb2.pending_for("beta/replay-b"), sb.pending_for("beta/replay-b")))
    # The silence window starts again at the restart: a replayed lane reads LIVE for those 90 seconds
    # because nothing has been silent yet from the new process's point of view. Asserting GONE at the
    # instant of replay fails for that reason and not because presence was rebuilt -- measured, and
    # worth the two lines, because "a socket that is gone is gone" is true only after the window.
    clk.t += 100
    sb2.tick()
    _alpha2 = [e for e in sb2.directory() if e["team"] == "alpha"]
    ck("alpha feed presence is NOT rebuilt (a socket that is gone is gone, once the window passes)",
       _alpha2 and all(e["hygiene"] == "GONE" for e in _alpha2), _alpha2)

    # ---- 7b. hook-derived now (operator: sessions must update when a new task starts)
    SBh, sh, ch = fresh(tempfile.mkdtemp(prefix="sb_"))
    sh.register("alpha", "hooked", now="model line", feed=True, sid="session-abc-42")
    ck("a feed extension can bind the harness session id (sid) for hook lookups",
       sh.ext_by_sid("session-abc-42") == "alpha/hooked", sh.ext_by_sid("session-abc-42"))
    ch.t += 5 * M
    sh.set_now_derived_by_sid("session-abc-42", "Ring beta for a real test call")
    dd = dirmap(sh)["alpha/hooked"]
    ck("a derived line NEWER than the model line shows at once (a new task is newer information)",
       dd["now"] == "Ring beta for a real test call" and dd["now_source"] == "derived", dd)
    sh.set_now_derived_by_sid("local_session-abc-42", "prefixed id from the app")
    ck("a `local_`-prefixed app id matches the bare id bound at feed time (suffix match either way)",
       dirmap(sh)["alpha/hooked"]["now"] == "prefixed id from the app")
    sh.register("beta", "hooked-s", now="model", session_id="session-host-77")
    ch.t += 1                                    # derived must be NEWER than the register-time model line
    sh.set_now_derived_by_sid("session-host-77", "derived via the host session id")
    ck("an ext registered by session_id is found by /hook/now through that id (with sid null, the hook never matched)",
       dirmap(sh)["beta/hooked-s"]["now"] == "derived via the host session id", dirmap(sh)["beta/hooked-s"])
    sh.set_now("alpha", "hooked", "model refreshed")
    ck("a model line newer than the derived one wins again", dirmap(sh)["alpha/hooked"]["now"] == "model refreshed")
    try:
        sh.set_now_derived_by_sid("unknown-sid", "x")
        ck("an unknown sid is refused, not silently dropped", False, "no error")
    except SBh.SwitchError:
        ck("an unknown sid is refused, not silently dropped", True)

    # ---- 9. etiquette: the ring timer holds while the callee is mid-turn;
    #         the caller learns "delivered, peer busy"; in-call silence nudges; orphan cap
    SBe, se, ce = fresh(tempfile.mkdtemp(prefix="sb_"))
    se.register("beta", "slow", now="n", session_id="s-slow")
    se.register("alpha", "q", now="n", feed=True)
    se.set_running("beta", {"s-slow": True})
    r = se.call("alpha", "q", "beta/slow", "s", "o")
    ring = [e for e in se.pending_for("beta/slow") if e["kind"] == "ring"][0]
    se.mark_delivered(ring["id"], "s-slow")
    ev = [e for e in se.pending_for("alpha/q") if e["kind"] == "ring_delivered"]
    ck("the caller gets a machine 'ring delivered to the peer host, peer mid-turn' signal (no model action)",
       len(ev) == 1 and "mid-turn" in ev[0]["text"], se.pending_for("alpha/q"))
    ce.t += 200
    se.tick()
    ck("the 90 s ring timer HOLDS while the callee is mid-turn (running=true): still RINGING after 200 s",
       dirmap(se)["beta/slow"]["state"] == "RINGING", dirmap(se)["beta/slow"])
    se.set_running("beta", {"s-slow": False})
    ce.t += 60
    se.tick()
    ck("once idle, the timer runs: 60 s of idle ringing is not yet a timeout", dirmap(se)["beta/slow"]["state"] == "RINGING")
    ce.t += 31
    se.tick()
    ck("91 s of idle ringing times out (the first-response bound stays 90 s)",
       any(x["event"] == "ring_timeout" and x["call_id"] == r["call_id"] for x in se.ledger_rows()))
    se.set_running("beta", {"s-slow": False})
    r2 = se.call("alpha", "q", "beta/slow", "s2", "o")
    se.answer("beta", "slow")
    for x in ("beta/slow", "alpha/q"):
        for e in se.pending_for(x):
            se.mark_delivered(e["id"], "x")
    ce.t += 5 * M + 1
    se.tick()
    n_callee = [e for e in se.pending_for("beta/slow") if e["kind"] == "nudge"]
    n_caller = [e for e in se.pending_for("alpha/q") if e["kind"] == "nudge"]
    ck("5 min of in-call silence nudges BOTH sides (callee: still there?; caller: peer silent 5 min), call stays open",
       len(n_callee) == 1 and len(n_caller) == 1 and "5 min" in n_caller[0]["text"] and dirmap(se)["beta/slow"]["state"] == "IN_CALL",
       (n_callee, n_caller))
    ce.t += 60
    se.tick()
    ck("no second nudge inside the next 5 min", len([e for e in se.pending_for("beta/slow") if e["kind"] == "nudge"]) == 1)
    se.say("beta", "slow", "still working, hang on")
    ce.t += 4 * M + 30
    se.tick()
    ck("a line resets the silence clock (no nudge 4.5 min after the last line)",
       len([e for e in se.pending_for("beta/slow") if e["kind"] == "nudge"]) == 1)
    ce.t += 2 * H + 1
    se.tick()
    ck("an open call older than 2 h is expired (call_expired, transcript written) -- the orphan cap",
       any(x["event"] == "call_expired" and x["call_id"] == r2["call_id"] for x in se.ledger_rows())
       and dirmap(se)["beta/slow"]["state"] == "IDLE", [x["event"] for x in se.ledger_rows()[-3:]])
    SBe2, se2, _ = fresh(os.path.dirname(se.ledger_path), ce)
    live_open = sorted(e["id"] for e in se.pending_for("alpha/q") if e["kind"] not in ("ring_delivered", "nudge"))
    ck("replay reproduces the pending ids deterministically (no duplicate after a restart; signals of ended calls purged)",
       sorted(e["id"] for e in se2.pending_for("alpha/q")) == live_open
       and not any(e["kind"] in ("ring_delivered", "nudge") for e in se2.pending_for("alpha/q")),
       (len(se2.pending_for("alpha/q")), len(live_open)))

    SBr, sr_, cr = fresh(tempfile.mkdtemp(prefix="sb_"))
    sr_.register("beta", "p", now="n", session_id="s-p"); sr_.register("alpha", "c", now="n", feed=True)
    sr_.set_running("beta", {"s-p": False})
    rc = sr_.call("alpha", "c", "beta/p", "s", "o")
    sr_.mark_delivered([e for e in sr_.pending_for("beta/p") if e["kind"] == "ring"][0]["id"], "s-p")
    sr_.answer("beta", "p"); sr_.hangup("alpha", "c", "done")
    for x in ("beta/p", "alpha/c"):
        for e in sr_.pending_for(x):
            if e["kind"] != "ring_delivered":            # live: the signal was never delivered before the restart
                sr_.mark_delivered(e["id"], "x")
    SBr2, sr2, _ = fresh(os.path.dirname(sr_.ledger_path), cr)
    ck("replay does NOT resurrect a ring_delivered signal for a call that has since ended (seen live: two stale pushes)",
       not any(e["kind"] in ("ring_delivered", "nudge") for e in sr2.pending_for("alpha/c")), sr2.pending_for("alpha/c"))

    # ---- 9b. names come from the directory, never from memory: an invented session name reaches nobody
    try:
        se.call("alpha", "q", "beta/slo", "s", "o")
        ck("a call to a non-existent extension is refused AND the refusal names the closest real ones", False, "no error")
    except SBe.SwitchError as e:
        ck("a call to a non-existent extension is refused AND the refusal names the closest real ones",
           "beta/slow" in str(e) and "sw_directory" in str(e), e)

    # ---- 10. the receipt-line and refused-call rules, enforced by the broker rather than by etiquette
    SBx, sx, cx = fresh(tempfile.mkdtemp(prefix="sb_"))
    sx.register("beta", "a", now="n", session_id="s-a"); sx.register("alpha", "b", now="n", feed=True)
    sx.register("beta", "c", now="n", session_id="s-c")
    sx.set_running("beta", {"s-a": False, "s-c": False})
    r = sx.call("alpha", "b", "beta/a", "subj", "open")
    sx.answer("beta", "a")
    ev = [e for e in sx.pending_for("alpha/b") if e["kind"] == "say"]
    ck("rule 7 (ring half): answering sends the caller a RECEIPT line automatically ('received, working on it')",
       len(ev) == 1 and "working" in ev[0]["text"].lower() and "beta/a" in ev[0]["text"], ev)
    sx.answer  # noqa
    r2 = sx.call("beta", "c", "beta/a", "second subj", "second opening")
    ck("rule 9: a call refused because the peer is IN_CALL becomes an ADDRESSED voicemail with the same subject + opening",
       r2["state"] == "IN_CALL" and r2.get("voicemail_queued") and any(v["to"] == "beta/a" and "second subj" in v["text"]
       and "second opening" in v["text"] for v in sx._held), r2)
    sx.hangup("alpha", "b", "done")                     # free b (one call per ext)
    sx.busy("beta", "c", True, "walk", "walking")
    r3 = sx.call("alpha", "b", "beta/c", "s3", "o3")
    ck("rule 9 covers BUSY too: voicemail queued, no ring", r3["state"] == "BUSY" and r3.get("voicemail_queued"), r3)
    sx.busy("beta", "c", False)
    ck("...and it is delivered when the peer goes IDLE, addressed to it",
       any(e["kind"] == "voicemail" and "s3" in e["text"] for e in sx.pending_for("beta/c")), sx.pending_for("beta/c"))

    # ---- 11. voicemail counts per extension in the directory: how many messages each lane is holding
    SBv, sv, cv = fresh(tempfile.mkdtemp(prefix="sb_"))
    sv.register("beta", "t", now="n", session_id="s-t"); sv.register("alpha", "u", now="n", feed=True)
    sv.set_running("beta", {"s-t": False})
    sv.busy("beta", "t", True, "walk", "w")
    sv.leave("alpha", "u", "beta/t", "vm one"); sv.leave("alpha", "u", "beta/t", "vm two")
    d = dirmap(sv)["beta/t"]
    ck("directory shows voicemail HELD (not yet deliverable) per extension", d.get("voicemail_held") == 2 and d.get("pending") == 0, d)
    sv.busy("beta", "t", False)
    d = dirmap(sv)["beta/t"]
    ck("...and PENDING (released, awaiting delivery) once the ext is IDLE", d.get("voicemail_held") == 0 and d.get("pending") == 2, d)
    for e in sv.pending_for("beta/t"):
        sv.mark_delivered(e["id"], "s-t")
    ck("...and zero after delivery", dirmap(sv)["beta/t"]["pending"] == 0)

    # ---- 12. now-line cap 200: 120 truncated real now-lines
    SBn, sn, _ = fresh(tempfile.mkdtemp(prefix="sb_"))
    long_now = "L" * 180
    sn.register("beta", "n", now=long_now, session_id="s-n")
    ck("a 180-char now line is kept whole (cap raised 120 -> 200)", dirmap(sn)["beta/n"]["now"] == long_now, len(dirmap(sn)["beta/n"]["now"]))
    sn.set_now("beta", "n", "M" * 250)
    ck("a 250-char line is cut at 200", len(dirmap(sn)["beta/n"]["now"]) == 200)

    # ---- 13. voicemail across a RETIRED lane -- the two facts the no-standby-holder decision
    #          rests on (docs/PROTOCOL.md 2, "why there is no standby holder").
    #          13a is why hand registration is SAFE: an off box costs a delay, not a message.
    #          13b is why a standby holder on the broker's own host would be UNSAFE as the broker stands.
    SBv, sv2, cv = fresh(tempfile.mkdtemp(prefix="sb_"))
    seen = []
    sv2.on_event(lambda e: seen.append(e))
    sv2.register("alpha", "d", now="up", session_id="s-d", feed=True, sid="s-d")
    sv2.register("beta", "d", now="up", session_id="s-sd", feed=True, sid="s-sd")
    sv2.feed("alpha", "d", False)              # the box is switched off
    cv.t += 11 * M
    ck("a lane whose box went off is retired", "alpha/d" in sv2.tick()["retired"])
    try:
        sv2.call("beta", "d", "alpha/d", "s", "o")
        ck("a call to a retired lane is refused", False, "the call was accepted")
    except SBv.SwitchError:
        ck("a call to a retired lane is refused", True)
    seen.clear()
    r = sv2.leave("beta", "d", "alpha/d", "the message that matters")
    ck("voicemail to a retired lane is accepted and held", r["queued"] and r["peer_state"] == "RETIRED"
       and not [e for e in seen if e["to"] == "alpha/d"], (r, len(seen)))
    seen.clear()
    cv.t += 12 * H
    sv2.register("alpha", "d", now="back", session_id="s-d2", feed=True, sid="s-d2")
    ck("...and released when the session re-registers by hand (option 1 is safe)",
       [e for e in seen if e["to"] == "alpha/d" and "the message that matters" in e["text"]],
       [e["text"][:60] for e in seen])

    # 13b. THE HAZARD: register() releases held voicemail to WHOEVER registers the lane. A standby
    # holder covering a retired lane therefore DRAINS that lane's mailbox into its own feed and the
    # real session gets nothing. If this check ever goes red, someone made register() park-safe --
    # revisit the decision in docs/PROTOCOL.md 2 rather than deleting the check.
    SBh, sh, ch = fresh(tempfile.mkdtemp(prefix="sb_"))
    got = []
    sh.on_event(lambda e: got.append(e))
    sh.register("alpha", "h", now="up", session_id="s-h", feed=True, sid="s-h")
    sh.register("beta", "h", now="up", session_id="s-sh", feed=True, sid="s-sh")
    sh.feed("alpha", "h", False)
    ch.t += 11 * M
    sh.tick()
    sh.leave("beta", "h", "alpha/h", "swallowed?")
    got.clear()
    sh.register("alpha", "h", now="held by a standby", session_id=None, feed=True, sid=None)
    drained = [e for e in got if e["to"] == "alpha/h" and "swallowed?" in e["text"]]
    ck("a session-less standby registering a retired lane DRAINS its voicemail (documented hazard)",
       len(drained) == 1, [e["text"][:60] for e in got])
    got.clear()
    ch.t += 12 * H
    sh.register("alpha", "h", now="the real session", session_id="s-h2", feed=True, sid="s-h2")
    ck("...and the real session then receives nothing (delivered once, to the wrong holder)",
       not [e for e in got if "swallowed?" in e["text"]], [e["text"][:60] for e in got])

    # ---- 14. a THIRD team, gamma: nothing may be special about the first two
    SBc, sc, cc = fresh(tempfile.mkdtemp(prefix="sb_"))
    ck("'gamma' is a known team", "gamma" in SBc.TEAMS, SBc.TEAMS)
    r = sc.register("gamma", "review", now="reviewing the spec", session_id="cx-1")
    ck("a gamma session registers and lands in the directory as its own team",
       r["ext"] == "gamma/review" and dirmap(sc)["gamma/review"]["team"] == "gamma", r)
    sc.register("alpha", "q", now="n", feed=True)
    r = sc.call("gamma", "review", "alpha/q", "subj", "open")
    ck("gamma can ring alpha", r["state"] == "RINGING" and r["call_id"], r)
    sc.answer("alpha", "q")
    sc.say("alpha", "q", "line to gamma")
    ck("a line reaches the gamma ext", any("line to gamma" in e["text"] for e in sc.pending_for("gamma/review")), sc.pending_for("gamma/review"))
    sc.hangup("gamma", "review", "done")
    ck("gamma is UNCAPPED under this deployment's config (only beta carries a cap)",
       sc.cap_into.get("gamma") is None and sc.cap_into.get("beta") == CAP_INTO_TEST["beta"], sc.cap_into)
    # ...and the SHIPPED default caps nothing at all: a cap is a deployment decision, never a
    # property baked into the library. Perturb CAP_INTO_DEFAULT to see this one fire.
    SBd, sd, _ = fresh(tempfile.mkdtemp(prefix="sb_"), cap_into={})
    ck("the shipped default caps NO team (a cap is configuration, not a constant)",
       sd.cap_into == {} and SBd.CAP_INTO_DEFAULT == {}, (sd.cap_into, SBd.CAP_INTO_DEFAULT))
    try:
        sc.register("gamma", "alpha/x", now="n", session_id="c")
        ck("a gamma ext may not carry another team's prefix", False, "no error")
    except SBc.SwitchError:
        ck("a gamma ext may not carry another team's prefix", True)
    # SECURITY, and it must hold at BOTH layers. The end-to-end suite pins the broker's refusal as
    # non-enumerating ("the refusal does NOT disclose which teams exist"); this one used to assert the
    # OPPOSITE of the state machine -- that its message "names the real ones". Both were green, so the
    # policy held only because team_of() happens to check the header before any Switchboard method
    # runs. A caller reaching the state machine by any other route got the whole list.
    try:
        sc.register("nosuchteam", "x", now="n", session_id="c")
        ck("an unknown team is refused, and the error names the team the CALLER sent", False, "no error")
    except SBc.SwitchError as e:
        ck("an unknown team is refused, and the error names the team the CALLER sent",
           "nosuchteam" in str(e), e)
        # Scan the message with the caller's OWN token removed: echoing back what the caller sent
        # discloses nothing, and without this the check goes red for an input like "alphax" that
        # merely contains a real name. The check has to fail on disclosure, not on the fixture.
        rest = str(e).replace("nosuchteam", "")
        ck("...and the state machine's refusal does NOT enumerate the real teams, same as the broker's",
           not any(t in rest for t in SBc.TEAMS), rest)

    # ---- a feed marked down by SILENCE must recover when its holder speaks again.
    # tick() presumes a watcher dead after feed_gone_s of quiet, which is right. But the holder's
    # socket may be perfectly healthy -- a long turn, a stall, a paused process -- and when it
    # resumes keepalives NOTHING restored the lane: set_running_ext() only refreshed last_seen.
    # The lane then sat feed-down while connected, went GONE, and was retired, and its holder was
    # never told because a session with no line cannot be rung.
    SBs, ss, cs = fresh(tempfile.mkdtemp(prefix="sb_"))
    ss.register("beta", "paused", now="n", session_id="sess-P", feed=True)
    ck("an acking feed starts LIVE", dirmap(ss)["beta/paused"]["hygiene"] == "LIVE", dirmap(ss)["beta/paused"])
    cs.t += 91                                     # past feed_gone_s (90) with no keepalive
    out = ss.tick()
    ck("silence past the window marks the feed down", "beta/paused" in out.get("feed_silent", []), out)
    cs.t += 91                                     # ...and past the window again, so it reads GONE
    ck("...which reads GONE once the window has run out", dirmap(ss)["beta/paused"]["hygiene"] == "GONE",
       dirmap(ss)["beta/paused"])
    ss.set_running_ext("beta/paused", True)        # the holder resumes keepalives on the SAME socket
    ck("a keepalive on the same socket brings the lane back",
       dirmap(ss)["beta/paused"]["hygiene"] == "LIVE", dirmap(ss)["beta/paused"])
    # ...but a keepalive must NOT overrule the HOST. A watcher is a separate process from the session
    # it delivers to: its socket being alive says nothing about whether the session still exists. When
    # the host reports the session absent that is evidence, and a ping from the watcher must not wash
    # it away.
    ss.set_running("beta", {})                     # the host lists no sessions: this one is absent
    ck("a session the host reports absent is GONE", dirmap(ss)["beta/paused"]["hygiene"] == "GONE",
       dirmap(ss)["beta/paused"])
    ss.set_running_ext("beta/paused", True)        # its watcher keeps pinging regardless
    ck("...and its watcher's keepalive does NOT resurrect it",
       dirmap(ss)["beta/paused"]["hygiene"] == "GONE", dirmap(ss)["beta/paused"])
    # THE ORDERING THAT BROKE THE FIRST FIX. Above, the socket was never down, so the restore had
    # nothing to fire on. Reverse it -- host says absent, THEN the socket stalls, THEN a ping -- and
    # a restore that clears "gone" without knowing WHY it was set washes the host's evidence away.
    ss.register("beta", "both", now="n", session_id="sess-B2", feed=True)
    ss.set_running("beta", {})                     # 1. the host reports every beta session absent
    cs.t += 91
    ss.tick()                                      # 2. the socket goes quiet as well
    cs.t += 91
    ss.set_running_ext("beta/both", True)          # 3. and then the watcher speaks again
    ck("a ping cannot withdraw the HOST's evidence, whichever failure came first",
       dirmap(ss)["beta/both"]["hygiene"] == "GONE", dirmap(ss)["beta/both"])

    # ...and NEITHER CAN A DROP. The check above covers a frame arriving. The other way a lane's
    # state is recomputed is its socket going down, and that branch waits the silence window out
    # using `gone_since` without asking why it was set -- so a lane the host had already reported
    # absent read LIVE again for the whole window. Silence is not evidence; evidence does not become
    # silence because a second thing failed afterwards.
    SBd, sd, cd = fresh(tempfile.mkdtemp(prefix="sb_"))
    sd.register("beta", "watched", now="n", session_id="host-W", feed=True, sid="s-w")
    sd.set_running("beta", {})                     # 1. the host reports the session absent
    was = sd.entry("beta", "watched")["hygiene"]
    cd.t += 10
    sd.feed("beta", "watched", False)              # 2. and THEN the socket drops
    cd.t += 30                                     # 3. well inside the silence window
    ck("a socket drop cannot withdraw the HOST's evidence either",
       was == "GONE" and sd.entry("beta", "watched")["hygiene"] == "GONE",
       (was, sd.entry("beta", "watched")["hygiene"], sd._ext["beta/watched"].get("gone_reason")))

    # ---- when a call ENDS, its transient signals must stop being deliverable.
    # Replay already drops ring_delivered and nudge rows belonging to ended calls -- the live path
    # had no equivalent, so a signal queued while the call was open was still delivered afterwards,
    # waking a session about a conversation that had finished.
    SBd, sd, cd = fresh(tempfile.mkdtemp(prefix="sb_"))
    sd.register("alpha", "caller", now="n", feed=True)
    sd.register("beta", "callee", now="n", session_id="sess-EC", feed=True)
    rc = sd.call("alpha", "caller", "beta/callee", "subject", "opening")
    sd.answer("beta", "callee")
    for ev in sd.pending_for("alpha/caller"):      # the caller drains what it has so far
        sd.mark_delivered(ev["id"], "x")
    cd.t += 6 * M
    # One tick does both: 5 minutes of silence nudges the parties, and the callee's watcher having
    # gone quiet past feed_gone_s ends the call as peer_lost. So the nudge is queued and the call it
    # belongs to is over, in that order -- which is precisely the state replay knows to clean up and
    # the live path did not.
    sd.tick()
    ck("silence nudges the parties, and the call then ends as its peer is lost",
       sd._calls[rc["call_id"]]["state"] == "ENDED", sd._calls[rc["call_id"]]["state"])
    left = [e for e in sd.pending_for("alpha/caller") + sd.pending_for("beta/callee")
            if e.get("call_id") == rc["call_id"] and e["kind"] in ("nudge", "ring_delivered")]
    ck("...and none of that call's transient signals are still deliverable", not left,
       [(e["kind"], e["text"][:40]) for e in left])
    # ...while what was SAID is not transient. A party that had not yet read the last line must
    # still receive it after the call ends, or ending a call would swallow its tail.
    SBk, sk, ck2 = fresh(tempfile.mkdtemp(prefix="sb_"))
    sk.register("alpha", "one", now="n", feed=True)
    sk.register("alpha", "two", now="n", feed=True)
    rk = sk.call("alpha", "one", "alpha/two", "s", "o")
    sk.answer("alpha", "two")
    sk.say("alpha", "two", "the last thing said")
    sk.hangup("alpha", "two", "done")
    ck("a line already said survives the call ending -- only machine signals are dropped",
       any("the last thing said" in e["text"] for e in sk.pending_for("alpha/one")),
       [(e["kind"], e["text"][:40]) for e in sk.pending_for("alpha/one")])

    # ---- a call that goes quiet AGAIN must nudge again.
    # The nudge id is "{call_id}-nudge-{n}-{who}" and a `say` resets the counter to 0. So after any
    # line, the next silent stretch regenerated n=1 -- an id already delivered -- and _emit dropped
    # it in silence. The parties then sat for a second five minutes with nothing, and only the
    # 10-minute mark produced a new id. A nudge that silently does not fire is worse than no nudge:
    # the whole point is to break a stall that neither side has noticed.
    SBn2, sn2, cn2 = fresh(tempfile.mkdtemp(prefix="sb_"))
    sn2.register("alpha", "aa", now="n", feed=True)      # no session_id: the feed-silence rule, which
    sn2.register("alpha", "bb", now="n", feed=True)      # needs one, cannot end the call underneath us
    rn = sn2.call("alpha", "aa", "alpha/bb", "s", "o")
    sn2.answer("alpha", "bb")
    cn2.t += 6 * M
    sn2.tick()
    first = [e for e in sn2.pending_for("alpha/aa") if e["kind"] == "nudge"]
    ck("a silent call nudges the first time", len(first) == 1, first)
    for ev in sn2.pending_for("alpha/aa") + sn2.pending_for("alpha/bb"):
        sn2.mark_delivered(ev["id"], "x")
    sn2.say("alpha", "bb", "still here")                 # the stall breaks, the counter resets
    for ev in sn2.pending_for("alpha/aa"):
        sn2.mark_delivered(ev["id"], "x")
    cn2.t += 6 * M
    sn2.tick()
    second = [e for e in sn2.pending_for("alpha/aa") if e["kind"] == "nudge"]
    ck("...and nudges AGAIN when it goes quiet a second time", len(second) == 1, second)

    # ---- a ring must not hold a caller's lane forever.
    # The 90 s answer timer counts only the CALLEE'S IDLE time, deliberately: a session mid-turn
    # cannot see a ring, so holding the timer is right. But the hold had no outer bound, and
    # CALL_CAP_S only ever applied to a call that was already open. A callee whose watcher keeps
    # pinging "still running" therefore pinned the CALLER's extension indefinitely -- and a lane
    # holds one call at a time, so the caller could place no other call, with no way to withdraw.
    SBr, sr, cr = fresh(tempfile.mkdtemp(prefix="sb_"))
    sr.register("alpha", "ringer", now="n", feed=True)
    sr.register("beta", "midturn", now="n", session_id="sess-MT", feed=True)
    sr.set_running("beta", {"sess-MT": True})
    rr = sr.call("alpha", "ringer", "beta/midturn", "s", "o")
    for _ in range(int(SBr.CALL_CAP_S / 25) + 10):        # the watcher keeps pinging, mid-turn throughout
        cr.t += 25
        sr.set_running_ext("beta/midturn", True)
        sr.tick()
        if sr._calls[rr["call_id"]]["state"] != "RINGING":
            break
    held = sr._calls[rr["call_id"]]
    ck("a ring held by a permanently mid-turn callee still ends at the call cap",
       held["state"] == "ENDED", (held["state"], "%.1f h" % ((cr.t - held["started"]) / 3600.0)))
    ck("...which frees the caller's lane to be used again",
       sr.call("alpha", "ringer", "beta/midturn", "s2", "o2")["call_id"] is not None,
       dirmap(sr)["alpha/ringer"]["state"])

    # ---- nothing a client sends may grow without bound.
    # Receipts were capped at 300, now-lines at NOW_MAX, delivery errors at 300 -- but `say` and
    # `leave`, the two paths that actually carry content, took whatever they were given. One caller
    # could write a row of any size into an append-only ledger that is replayed into memory at every
    # start. And `_rows` kept EVERY row forever to serve a 200-row operator snapshot.
    SBb, sb2, cb = fresh(tempfile.mkdtemp(prefix="sb_"))
    sb2.register("alpha", "big", now="n", feed=True)
    sb2.register("alpha", "recv", now="n", feed=True)
    huge = "x" * (SBb.TEXT_MAX + 1)
    # EVERY path that writes text, not the one this check happened to be written against. It tested
    # `leave` alone and stayed green while `operator_say` -- reachable over HTTP with no credential --
    # put a 40,000-character row into the ledger, which is then fanned out to both parties as part
    # frames. A cap enforced on two of three doors is not a cap.
    cb.t += 1
    rb = sb2.call("alpha", "big", "alpha/recv", "s", "o")
    sb2.answer("alpha", "recv")
    doors = {
        "leave": lambda: sb2.leave("alpha", "big", "alpha/recv", huge),
        "say": lambda: sb2.say("alpha", "big", huge),
        "operator_say": lambda: sb2.operator_say(rb["call_id"], huge),
    }
    accepted = []
    for _name, _call in doors.items():
        try:
            _call()
            accepted.append(_name)
        except SBb.SwitchError as e:
            if str(SBb.TEXT_MAX) not in str(e):
                accepted.append("%s (refused, but not for length: %s)" % (_name, str(e)[:40]))
    ck("an over-long message is refused rather than silently truncated", not accepted, accepted)
    ck("...and no ledger row carries more text than the cap allows",
       max([len(str(x.get("text", ""))) for x in sb2.ledger_rows()] or [0]) <= SBb.TEXT_MAX,
       max([len(str(x.get("text", ""))) for x in sb2.ledger_rows()] or [0]))
    sb2.hangup("alpha", "big", "done")
    ok = sb2.leave("alpha", "big", "alpha/recv", "x" * SBb.TEXT_MAX)
    ck("...and a message exactly at the limit is accepted", ok["queued"] is True, ok)
    for i in range(SBb.ROWS_KEPT + 50):
        sb2.set_now("alpha", "big", "line %d" % i)
    ck("the in-memory ledger is bounded, however long the broker runs",
       len(sb2.ledger_rows()) <= SBb.ROWS_KEPT, len(sb2.ledger_rows()))
    ck("...and it keeps the NEWEST rows, which is what the operator page shows",
       "line %d" % (SBb.ROWS_KEPT + 49) in json.dumps(sb2.ledger_rows()[-1]),
       sb2.ledger_rows()[-1])
    # THE PROPERTY THE BOUND COULD HAVE BROKEN. The file stays the source of truth: replay reads
    # every row and applies it, and only what is RETAINED afterwards is capped. An extension
    # registered long before the window must still exist after a restart.
    deep = tempfile.mkdtemp(prefix="sb_")
    SBv, sv3, _ = fresh(deep)
    sv3.register("alpha", "survivor", now="registered at the very start", feed=True)
    for i in range(SBv.ROWS_KEPT + 100):
        sv3.set_now("alpha", "survivor", "line %d" % i)
    SBw, sw3, _ = fresh(deep)                       # same ledger, fresh broker
    ck("replay still rebuilds state from rows older than the memory window",
       "alpha/survivor" in dirmap(sw3), sorted(dirmap(sw3)))

    # ---- 8. wait buffering
    async def wcase():
        SB3, s3, c3 = fresh(tempfile.mkdtemp(prefix="sb_"))
        s3.register("beta", "a", now="n", session_id="s-a")
        s3.register("alpha", "b", now="n", feed=True)
        s3.call("alpha", "b", "beta/a", "s", "o")
        s3.answer("beta", "a")
        for x in ("beta/a", "alpha/b"):
            for e in s3.pending_for(x):
                s3.mark_delivered(e["id"], "x")
        t = asyncio.ensure_future(s3.wait("beta", "a", 5))
        await asyncio.sleep(0.05)
        s3.say("alpha", "b", "hello wait")
        got = await t
        ck("sw_wait returns the line and it is not also pending (no double delivery)",
           got and "hello wait" in got[0]["text"] and not s3.pending_for("beta/a"), got)
    asyncio.run(wcase())

    # ---- 17. a torn last line must not stop the broker starting
    # `_commit` is one buffered append with no flush, so a crash mid-write leaves a partial final
    # line; `_replay` called json.loads on every line unguarded, so that line raised inside __init__
    # and the broker did not start. The recommended deployment restarts automatically, which turns
    # one unclean shutdown into a container loop -- and the ledger is the broker's only memory.
    #
    # The two cases are NOT the same and must not be treated the same. A torn tail is a crash during
    # an append: everything before it is intact. A broken row in the MIDDLE is corruption of a
    # completed row, and starting anyway would continue with a hole in the state and say nothing.
    _t17 = tempfile.mkdtemp(prefix="sb_")
    SBt, st, _ = fresh(_t17)
    for i in range(3):
        st.register("beta", "lane-%d" % i, now="n", session_id="s-%d" % i)
    _lpath = os.path.join(_t17, "sb.jsonl")
    _whole = open(_lpath, encoding="utf-8").read()
    open(_lpath, "w", encoding="utf-8", newline="\n").write(_whole + '{"event": "register", "ts": 17')
    try:
        _re1 = SBt.Switchboard(ledger_path=_lpath, calls_dir=os.path.join(_t17, "calls"))
        ck("a half-written last line does not stop the broker starting",
           len(_re1.directory()) == 3, [e["ext"] for e in _re1.directory()])
        # The intact prefix is kept and the unfinished bytes are gone -- and the file is LONGER than
        # the prefix, because dropping them is itself recorded. Asserting equality with the original
        # was wrong for that reason, not because the truncation failed.
        # The property is that the FILE PARSES, not that some marker is absent: the first version
        # looked for '"ts": 17' as the torn line's fingerprint, and every real row carries it too --
        # epoch timestamps begin with 17. A sentinel that matches everything proves nothing.
        _after = open(_lpath, encoding="utf-8").read()
        _parses = all(json.loads(ln) for ln in _after.splitlines() if ln.strip())
        ck("...and the torn tail is removed, so the next append is not corrupt in turn",
           _after.startswith(_whole) and _parses and _after.endswith("\n"), repr(_after[-60:]))
        ck("...and the ledger records that a partial row was dropped",
           any(x["event"] == "ledger_truncated" for x in _re1.ledger_rows()),
           [x["event"] for x in _re1.ledger_rows()[-3:]])
    except Exception as e:
        ck("a half-written last line does not stop the broker starting", False, repr(e)[:90])

    _t17b = tempfile.mkdtemp(prefix="sb_")
    SBu, su, _ = fresh(_t17b)
    for i in range(3):
        su.register("beta", "lane-%d" % i, now="n", session_id="s-%d" % i)
    _lp2 = os.path.join(_t17b, "sb.jsonl")
    _ls = open(_lp2, encoding="utf-8").read().splitlines()
    _ls[1] = _ls[1][: len(_ls[1]) // 2]
    open(_lp2, "w", encoding="utf-8", newline="\n").write("\n".join(_ls) + "\n")
    try:
        SBu.Switchboard(ledger_path=_lp2, calls_dir=os.path.join(_t17b, "calls"))
        ck("a broken row in the MIDDLE is refused, not silently skipped", False, "it started anyway")
    except Exception as e:
        ck("a broken row in the MIDDLE is refused, not silently skipped",
           isinstance(e, SBu.SwitchError) and "line 2" in str(e), "%s: %s" % (type(e).__name__, str(e)[:70]))

    # ---- 16. a replacement is allowed, and the ledger says whether it was the same session
    # Taking a name nobody holds is deliberate, and the held voicemail goes with it -- PROTOCOL says
    # so. What was missing is any way to tell "the session came back" from "somebody else took the
    # name", which on a board with no authentication is the whole of the answer: it is not gated, it
    # is LOGGED. Gating the mail on identity was tried and rejected: a lane registered by session id
    # alone never gets a gone_since, lives the full idle day, and a session returning inside that day
    # with a new id -- the ordinary case -- would have been refused its own messages.
    SBz, sz, cz = fresh(tempfile.mkdtemp(prefix="sb_"))
    sz.register("beta", "lane", now="working", session_id="sess-REAL", sid="sid-REAL")
    cz.t += 3 * 3600                                   # past STALE: a LIVE lane is refused outright
    sz.register("beta", "lane", now="back", session_id="sess-REAL", feed=True, sid="sid-REAL")
    same = [x for x in sz.ledger_rows() if x["event"] == "retired" and x.get("ext") == "beta/lane"]
    ck("a replacement proving the lane's identity is recorded as the same session",
       same and same[-1].get("same_identity") is True, same[-1:] or "no retired row")
    cz.t += 3 * 3600
    sz.register("beta", "lane", now="mine now", session_id="sess-OTHER", feed=True, sid="sid-OTHER")
    diff = [x for x in sz.ledger_rows() if x["event"] == "retired" and x.get("ext") == "beta/lane"]
    ck("a replacement by a different session is recorded as such, not silently",
       diff[-1].get("same_identity") is False, diff[-1:])

    # ---- 15. the silence rule applies to ONE of the two feed types, and the contract said neither
    # PROTOCOL told every client that 90 seconds of silence marks its lane GONE. That rule is
    # conditional on the lane having a session id, and the feed the README recommends has none -- so
    # for the shipped feed type the stated consequence does not happen at all. Nothing asserted it
    # either way, which is how a document and a code path drift apart: the behaviour is RELIED ON in
    # this very file (case 12 registers without a session id precisely so the rule cannot end its
    # call underneath it) and was never checked.
    #
    # Two ticks, and the second is the one that matters: the first marks a silent feed down and
    # stamps the moment, and the lane only reads GONE once the window has elapsed since that stamp.
    SBq, sq, cq = fresh(tempfile.mkdtemp(prefix="sb_"))
    sq.register("alpha", "sid-only", now="holding", feed=True, sid="s-A")
    sq.register("beta", "with-session", now="holding", feed=True, sid="s-B", session_id="host-B")
    cq.t += 5 * M
    marked = sq.tick().get("feed_silent", [])
    cq.t += 2 * M
    sq.tick()
    ck("only a feed bound to a session id is marked silent (a --sid feed pings nothing)",
       marked == ["beta/with-session"], marked)
    ck("a feed with no session id is never GONE from silence -- its socket closing is the signal",
       sq.entry("alpha", "sid-only")["hygiene"] == "LIVE"
       and sq.entry("beta", "with-session")["hygiene"] == "GONE",
       (sq.entry("alpha", "sid-only")["hygiene"], sq.entry("beta", "with-session")["hygiene"]))
    # ...and the consequence a reader is owed: its last_seen goes stale anyway, and a stale last_seen
    # is what decides whether a second holder may take the lane.
    ck("...but its last_seen goes stale regardless, which is what makes it evictable",
       sq.entry("alpha", "sid-only")["last_seen_age_s"] > 90,
       sq.entry("alpha", "sid-only")["last_seen_age_s"])
    return finish()


def finish():
    print()
    if FAILS:
        print("FAILED %d: %s" % (len(FAILS), FAILS))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
