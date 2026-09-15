"""Every "this check can fail" claim in docs/FIX_LOG.md, as a runnable list.

A test that cannot fail is worthless, and the usual proof -- break the code by hand, watch the check
go red, put it back -- is only as good as whoever read the output. That is not a small risk: while
writing this suite, one such claim was recorded in the fix log that was simply false. The
perturbation had been applied to the wrong place, the check passed, and the claim was written without
reading the result.

So the claims live here instead, as data, and `test_perturbations.py` re-derives every one of them.
If a fix stops being load-bearing, or a check quietly becomes a tautology, this run says so -- with
nobody having to be watching.

Each entry: the defect it belongs to, the file to break, an exact substring to replace, and the name
of the check that MUST go red when it is. `suite` picks which suite to run -- "unit" is a second or
two, "e2e" is about half a minute, so keep a perturbation in the unit suite when the behaviour lives
there.
"""

PERTURBATIONS = [
    dict(id="A1", suite="e2e", file="teamline/switchboard_broker.py",
         find='if not any(e["id"] == mid and e["to"] == ext for e in sb._outbox):',
         repl='if not any(e["id"] == mid for e in sb._outbox):',
         must_fail="an ack naming a message addressed to ANOTHER lane is ignored",
         why="drops the lane comparison, so any holder can settle any message again"),

    dict(id="A2-identity", suite="e2e", file="teamline/switchboard_broker.py",
         find='if e and (mine or (not bound and ((e["feed"] and not e["feed_up"]) or replacing))):',
         repl='if e and (e["feed"] and not e["feed_up"] or mine or replacing):',
         must_fail="a client with no matching identity cannot take over a lane whose socket dropped",
         why="restores the no-identity re-attach clause"),

    dict(id="A2-window", suite="e2e", file="teamline/switchboard.py",
         find='            if t - since >= self.feed_gone_s:\n                return "GONE"',
         repl='            return "GONE"',
         must_fail="a client with no matching identity cannot take over a lane whose socket dropped",
         why="removes the silence window, so a dropped socket is GONE at once and register() replaces it"),

    dict(id="A3-teams", suite="e2e", file="teamline/switchboard.py",
         # The parentheses matter: the replaced text is followed by `.split(",")`, so appending
         # without them yields `str + ",operator".split(",")` -- a str plus a list, which fails at
         # import. The first version of this spec did exactly that, the suite never ran, and the
         # runner reported it as "the check did not fail", which would have read as a tautology.
         find='os.environ.get("TEAMLINE_TEAMS", "alpha,beta")',
         repl='(os.environ.get("TEAMLINE_TEAMS", "alpha,beta") + ",operator")',
         must_fail="the observer needs NO team name",
         why="makes `operator` a configured team, which would change what the security section must say"),

    dict(id="A3-rows", suite="e2e", file="teamline/teamline_broker.py",
         find='rows=sb.ledger_rows()[-200:]', repl='rows=[]',
         must_fail="its snapshot carries raw ledger rows",
         why="empties the snapshot, so the exposure the security section describes would be gone"),

    dict(id="A4-restore", suite="unit", file="teamline/switchboard.py",
         find='        self._feed_alive(ext)\n        e["last_seen"] = self.now()',
         repl='        e["last_seen"] = self.now()',
         must_fail="a keepalive on the same socket brings the lane back",
         why="stops a frame from restoring a lane marked down by silence"),

    dict(id="A4-reason", suite="unit", file="teamline/switchboard.py",
         find='                    if e.get("gone_reason") in (None, "feed"):\n                        e["gone_since"], e["gone_reason"] = None, None',
         repl='                    e["gone_since"], e["gone_reason"] = None, None',
         must_fail="a ping cannot withdraw the HOST's evidence",
         why="clears 'gone' without asking why it was set, resurrecting a host-declared-dead session"),

    dict(id="A5-purge", suite="unit", file="teamline/switchboard.py",
         find='\n            self._drop_transient(c["call_id"])', repl='', all_occurrences=True,
         must_fail="none of that call's transient signals are still deliverable",
         why="removes the live purge, leaving replay as the only cleanup"),

    dict(id="A5-toomuch", suite="unit", file="teamline/switchboard.py",
         find='    TRANSIENT = ("ring_delivered", "nudge")',
         repl='    TRANSIENT = ("ring_delivered", "nudge", "say")',
         must_fail="a line already said survives the call ending",
         why="purges too much, swallowing the tail of a conversation"),

    dict(id="A6", suite="unit", file="teamline/switchboard.py",
         find="-nudge-{stretch}-{r['n']}-", repl="-nudge-{r['n']}-", all_occurrences=True,
         must_fail="nudges AGAIN when it goes quiet a second time",
         why="drops the silent-stretch marker, so the second round reuses a delivered id"),

    dict(id="A7", suite="unit", file="teamline/switchboard.py",
         find='if c["ring_wait"] > RING_TIMEOUT_S or capped:',
         repl='if c["ring_wait"] > RING_TIMEOUT_S:',
         must_fail="a ring held by a permanently mid-turn callee still ends at the call cap",
         why="removes the outer bound, so a mid-turn callee pins the caller's lane again"),

    dict(id="A8-rows", suite="unit", file="teamline/switchboard.py",
         find="collections.deque(maxlen=ROWS_KEPT), {}, {}", repl="[], {}, {}",
         must_fail="the in-memory ledger is bounded",
         why="retains every row again"),

    dict(id="A8-text", suite="unit", file="teamline/switchboard.py",
         find="        self._check_text(text)\n", repl="", all_occurrences=True,
         must_fail="an over-long message is refused rather than silently truncated",
         why="accepts a message of any size"),

    dict(id="A9", suite="e2e", file="teamline/teamline_broker.py",
         find="    async def page(_req):",
         repl='    def _never_called():\n        return sw["nope"]\n\n    async def page(_req):',
         must_fail="every sw[...] the broker reads is a key the wiring actually returns",
         why="an UNREACHABLE reference to a key the wiring never returns -- the original defect's shape"),

    dict(id="B1", suite="e2e", file="teamline/switchboard_broker.py",
         find="return sb.register(t, ext, now=now, session_id=session_id or None, feed=False)",
         repl='return sb.register(t, ext, now=now, session_id=session_id or None,\n                               feed=(t == "alpha" and not session_id))',
         must_fail="sw_register treats every team identically",
         why="restores the privilege of one literal team name"),

    dict(id="B2", suite="e2e", file="teamline/teamline_broker.py",
         find='"TEAMLINE switchboard: one named extension per session, addressed as team/name. "',
         repl='"TEAMLINE switchboard: sw_register first. "',
         must_fail="the server's own instructions do not send agents to sw_register first",
         why="puts back the advice that contradicts the documentation"),

    dict(id="B3", suite="e2e", file="README.md",
         find="| `TEAMLINE_ROOT` |", repl="| `UNDOCUMENTED_NOW` |",
         must_fail="every environment variable the package reads is documented in the README",
         why="removes one variable's documentation"),

    dict(id="B4", suite="e2e", file="teamline/teamline_feed.py",
         find='row = dict(type="feed_down", error=str(e)[:120], retry_s=2, url=url.split("/ws?")[0])',
         repl='row = dict(type="feed_down", error=str(e)[:120], retry_s=2)',
         must_fail="an unreachable broker is reported with the URL the client actually tried",
         why="takes the address back out of the failure, leaving a bare OS error"),

    dict(id="B5", suite="e2e", file="README.md",
         find="switchboard_broker.py", repl="the wiring module", all_occurrences=True,
         must_fail="every module in the package is described in the README",
         why="removes a module from the map. NOTE: this one must replace EVERY occurrence -- the "
             "first attempt changed only the table row, the filename survived in the prose above it, "
             "the check passed, and a false claim went into the fix log because the output was not read"),

    dict(id="B-L1", suite="e2e", file="README.md",
         find="deploy/", repl="the deployment directory ", all_occurrences=True,
         must_fail="every top-level directory is described in the README, not just the package",
         why="takes deploy/ back out of the map, which is the state the repository shipped in -- the "
             "security section recommended the compose file and nothing said where it lived. ALL "
             "occurrences, for B5's reason: the run command on the same table row carries the string "
             "a second time, so replacing the cell alone would leave the check green"),

    dict(id="D-L1", suite="e2e", file="teamline/switchboard_broker.py",
         find="Measured in production: one lane held 5 holders,",
         # assembled from fragments for the same reason the check itself is: the scan reads this
         # file's TEXT, so a literal needle written here would make the check red for ever.
         repl="Measured 2026-09-08: beta/one-" + "analysis held 5,",
         must_fail="no lane name from the private deployment survives in the publish tree",
         why="puts back a lane name of the private deployment, which is the state the de-identifying "
             "rename left four comments in -- the team relabelled, the lane name untouched"),

    dict(id="C-L1a", suite="e2e", file="README.md",
         find="| `tests/` | three suites and a headless page probe. |",
         repl="| `tests/` | two suites and a headless page probe. |",
         must_fail="the README states no suite count that contradicts tests/",
         why="puts back the stale suite count the repository shipped with -- there were three"),

    dict(id="C-L1b", suite="e2e", file="README.md",
         find="Roughly 1,550 lines of implementation and 1,700 lines of",
         repl="Roughly 1,700 lines of implementation and 1,000 lines of",
         must_fail="the README's 'roughly N lines' claims are within 20% of the real counts",
         why="puts back the shipped figures: the tests claim was out by 70%, understating the suite "
             "it was describing"),

    dict(id="C-L2", suite="e2e", file="README.md",
         find="rebinding", repl="host-header", all_occurrences=True,
         must_fail="if the DNS-rebinding guard is disabled, the security section says so",
         why="removes the disclosure that a security control is off, which is the state the "
             "repository shipped in. ALL occurrences, for B5's reason: the disclosure explains the "
             "attack as well as naming it, so blanking the heading alone leaves the word in the "
             "paragraph below and the check stays green"),
]
