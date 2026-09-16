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
         find='rows=[public(r) for r in sb.ledger_rows()[-200:]]', repl='rows=[]',
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
         # `{NEEDLE}` is filled in by the runner with a string generated for this run, and the same
         # string is the list handed to the suite. It used to be a real lane name assembled from two
         # literals, because the scan reads this file's text -- which is R-12: the needle was in the
         # published tree either way, and joining two literals is one line of `ast`.
         repl="Measured in one deployment: beta/{NEEDLE} held 5,",
         must_fail="no string from the supplied private-name list survives in the publish tree",
         why="puts a name from the list back into a shipped comment, which is the state the "
             "de-identifying rename left four comments in -- the team relabelled, the lane untouched"),

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

    # This one perturbs the RUNNER's own source. That is safe and deliberate: the runner has already
    # been imported by the time it edits anything, so its behaviour this run is unaffected, and the
    # check it is proving reads that file as DATA rather than importing it.
    dict(id="E-L1", suite="e2e", file="tests/test_perturbations.py",
         find='print("all %d claims in the fix log hold: each names a check that goes RED when its fix is undone."',
         repl='print("all %d claims hold: every fix is load-bearing and every check can fail."',
         must_fail="the perturbation runner does not claim to verify every check when it pins a subset",
         why="puts back the verdict the repository shipped with -- it pinned 24 of 178 checks and "
             "said every check can fail, which devalues the 24 that are real"),

    dict(id="D-L2", suite="e2e", file="docs/FIX_LOG.md",
         find="HISTORY STATUS (checked by the suite): CLEAN",
         repl="HISTORY STATUS (checked by the suite): CARRIES PRIVATE NAMES",
         must_fail="the fix log's stated history status matches the commits, in both directions",
         why="makes the log state the OPPOSITE of what the commits say. This perturbs the direction "
             "the first version of the check could not reach: it only forbade claiming clean while "
             "dirty, so once the history was rewritten its condition became unreachable, the log "
             "could say anything, and this very claim stopped firing -- caught by this runner"),

    dict(id="D-L2b", suite="e2e", file="docs/FIX_LOG.md",
         find="**HISTORY STATUS (checked by the suite): CLEAN**",
         repl="**HISTORY STATUS (checked by the suite): CLEAN**\n\n*(illustrative, from an older "
              "entry: HISTORY STATUS (checked by the suite): CARRIES PRIVATE NAMES)*",
         must_fail="the fix log's stated history status matches the commits, in both directions",
         why="plants a SECOND, contradicting status marker, phrased as an innocent quotation -- which "
             "is how it would really arrive. Demonstrated before the fix: re.search took the first "
             "match and the check PASSED with two markers disagreeing. Silently choosing between "
             "contradictory claims is worse than either claim"),

    dict(id="V-2", suite="e2e", file="teamline/switchboard_broker.py",
         find="# reconnect stagger after a broker or host restart",
         # A PHRASE with spaces, not an identifier -- that distinction is the point of this claim, and
         # the runner's needle is substituted into the middle of one. Same change as D-L1 above: the
         # phrase used to be spelled here in fragments, which published it (R-12).
         repl="# reconnect stagger after a broker or the {NEEDLE} host restart",
         must_fail="no string from the supplied private-name list survives in the publish tree",
         why="puts back a phrase naming a specific machine. It survived the lane-name pass because it "
             "is a PHRASE, not an identifier -- and it read, to a stranger, as a reference to "
             "infrastructure they were assumed to know and which is defined nowhere in this repository"),

    dict(id="R-24-doc", suite="e2e", file="README.md",
         find="The broker reads the first five, a",
         repl="The broker reads the first four, a",
         must_fail="the configuration prose counts the variables each side reads, correctly",
         why="restores the miscount: seven rows, five of them read by the broker, described as four"),

    dict(id="R-24-code", suite="e2e", file="teamline/teamline_broker.py",
         find='PAGE_FILE = os.environ.get("TEAMLINE_PAGE"',
         repl='PAGE_FILE = os.environ.get("TEAMLINE_PAGE_RENAMED"',
         must_fail="the configuration prose counts the variables each side reads, correctly",
         why="stops the broker reading the fifth variable, so the prose's count is right about a "
             "document that no longer describes the code. The direction that actually happens: a "
             "variable is retired or moved and the configuration section is not reopened"),

    dict(id="R-14-doc", suite="e2e", file="docs/PROTOCOL.md",
         # The WHOLE bullet, all six lines. Replacing only its first line left the rest of the
         # paragraph still naming both feed types, so the check stayed green and the runner reported
         # the claim SILENT -- a perturbation has to remove the thing the check looks for, not the
         # sentence the author happens to think is the important one.
         find="* send `{\"ping\": 1}` at least every 25 seconds. **Which feed you are decides what silence costs you.**\n"
              "  A feed registered with `session_id` is marked GONE after 90 seconds of silence, and any call it is\n"
              "  in ends. A feed registered with `sid` alone is *never* marked GONE by silence — for that one the\n"
              "  socket closing is the only signal, which is why the shipped client can be a process that only\n"
              "  receives. **Ping regardless:** a lane's `last_seen` goes stale either way, and a stale `last_seen`\n"
              "  is what lets a second holder take the lane out from under you;",
         repl="* send `{\"ping\": 1}` at least every 25 seconds (90 seconds of silence marks the lane GONE);",
         must_fail="the keepalive rule names which of the two feed types it applies to",
         why="restores the sentence that told every client a consequence which does not happen to the "
             "feed the README tells them to run. Replacing only part of the paragraph did NOT fire -- "
             "the rest still named both types -- which the runner reported before this was recorded"),

    dict(id="R-11-doc", suite="e2e", file="docs/PROTOCOL.md",
         find="**A ring waits 90 seconds of the callee's IDLE time, and up to 2 hours of yours.**",
         repl="**A ring waits 90 seconds for an answer and then frees the line.**",
         must_fail="the ring contract states both bounds the code enforces, not just the one it aims at",
         why="restores the sentence a client author sizes a timeout from, which was wrong by up to 119 "
             "minutes for the caller's own lane"),

    dict(id="R-11-code", suite="e2e", file="teamline/switchboard.py",
         find="NUDGE_S, CALL_CAP_S = 5 * 60, 2 * 3600",
         repl="NUDGE_S, CALL_CAP_S = 5 * 60, 4 * 3600",
         must_fail="the ring contract states both bounds the code enforces, not just the one it aims at",
         why="moves the enforced cap without touching the document. The other direction, and the one "
             "that happens in practice: a constant is tuned and the contract file is not reopened"),

    dict(id="R-10-file", suite="e2e", file="tests/test_switchboard.py",
         find="revisit the decision in docs/PROTOCOL.md 2",
         # `{MISSING}` is filled by the runner with a document name that does not exist. Spelled out
         # here it would be a dangling reference in its own right, and this file is scanned.
         repl="revisit the decision in {MISSING}",
         must_fail="every document this tree names is in this tree, with the passage it quotes",
         why="points a reader at a document that is not in this repository -- the shape the private "
             "review queue's pointers had, shipped"),

    dict(id="R-10-passage", suite="e2e", file="docs/PROTOCOL.md",
         find="### Why there is no standby holder",
         repl="### A note on standby holders",
         must_fail="every document this tree names is in this tree, with the passage it quotes",
         why="leaves the file in place and renames the section two comments QUOTE. The harder half: "
             "the document exists, the reference resolves, and the passage is not there"),

    dict(id="R-10-url", suite="e2e", file="teamline/teamline_cli.py",
         find="/ws?party=<team>&ext=<name>",
         repl="/ws?party=<team>",
         must_fail="no feed URL a reader might copy names a team without an extension",
         why="restores an instruction that FAILS WHEN FOLLOWED -- the broker closes a feed URL that "
             "names no extension, and the suite asserts it does"),

    dict(id="R-6-code", suite="e2e", file="teamline/teamline_broker.py",
         find='Route("/directory", sw["directory_json"]), Route("/healthz", sw["healthz"]),',
         repl='Route("/directory", sw["directory_json"]), Route("/healthz", sw["healthz"]),\n'
              '                            Route("/added-later", page),',
         must_fail="the security section names every surface that needs no team name",
         why="adds a route whose handler never consults team_of -- one line, the shape this defect "
             "really takes. The security section cannot know about it, so the check must"),

    dict(id="R-6-doc", suite="e2e", file="README.md",
         find="| `POST /hook/now` |",
         repl="| `POST /hook-now-renamed` |",
         must_fail="the security section names every surface that needs no team name",
         why="takes one surface out of the security section while the code still serves it. The other "
             "direction, and the one that rots on its own: A3's list was true when it was written"),

    dict(id="V-3", suite="e2e", file="tests/test_switchboard_e2e.py",
         find='"Host": "198.51.100.2:3790"',
         # `{ADDR}` is substituted by the runner. Written out, this file would carry the address the
         # check forbids -- it walks the whole tree, this file included.
         repl='"Host": "{ADDR}:3790"',
         must_fail="every address in this tree is loopback or a documentation range, never a real host",
         why="puts a real-world private-range address back into a shipped file. The Host-header check "
             "beside it stays green either way -- both addresses are served -- so the only thing that "
             "can go red is the address check, which is the point"),

    dict(id="R-9-source", suite="e2e", file="teamline/switchboard.py",
         find="# now-line cap: 120 truncated real now-lines",
         # `{DATE}` and `{CLOCK}` are substituted by the runner. Written out here they would sit in
         # the tree the check walks, and it would be red for ever with this claim firing regardless.
         repl="# now-line cap (operator {DATE} {CLOCK}: raised from 120)",
         must_fail="no shipped source line or check name is dated, clocked, or names something undefined here",
         why="puts back a comment stamped with the day and the minute somebody chose a constant. The "
             "engineering fact -- 120 truncated real now-lines -- is what a reader needs; the date is "
             "what made the package read as one household's incident diary"),

    dict(id="R-9-name", suite="e2e", file="tests/test_switchboard.py",
         find="directory shows now WITH its age (load-bearing: a now-line with no age cannot be judged)",
         repl="directory shows now WITH its age (load-bearing, seen {CLOCK})",
         must_fail="no shipped source line or check name is dated, clocked, or names something undefined here",
         why="puts a wall-clock time back into a check NAME, which is the sharp case: names are "
             "PRINTED by every run, so they are the first thing a reader of the output sees. The file "
             "broken is the unit suite and the suite RUN is the e2e, because that is where the check "
             "lives and it reads the other file's names rather than its own output"),

    dict(id="R-12", suite="e2e", file="teamline/teamline_broker.py",
         find='SECRET_FIELDS = ("sid", "session_id")',
         repl='SECRET_FIELDS = ("s" + "id", "session_id")',
         must_fail="no file in this tree assembles a string out of literal fragments",
         why="reintroduces the idiom that published the private names. The perturbation changes no "
             "behaviour at all -- the tuple is identical -- so the only thing that can go red is the "
             "structural check, which is the point: the mechanism is the defect, not the value"),

    dict(id="R-2-row", suite="e2e", file="teamline/switchboard.py",
         find='call_id=(self._call_of(e["ext"]) or {}).get("call_id"),',
         repl='session_id=e["session_id"], sid=e.get("sid"),\n                    '
              'call_id=(self._call_of(e["ext"]) or {}).get("call_id"),',
         must_fail="no unauthenticated door hands out the identity the re-attach guard checks",
         why="puts both identities back into every published directory row, which is where a reader "
             "of the first URL the README hands out found the proof the re-attach guard demands"),

    dict(id="R-2-observer", suite="e2e", file="teamline/teamline_broker.py",
         find='rows=[public(r) for r in sb.ledger_rows()[-200:]]',
         repl='rows=sb.ledger_rows()[-200:]',
         must_fail="no unauthenticated door hands out the identity the re-attach guard checks",
         why="serves raw ledger rows to the operator socket again -- it is accepted with no "
             "credential, and a `register` row carries the sid in full"),

    dict(id="R-2-refusal", suite="e2e", file="teamline/switchboard.py",
         find="is LIVE (seen {int(self.now() - old['last_seen'])}s ago)",
         repl="is LIVE (session {old['session_id']}, seen {int(self.now() - old['last_seen'])}s ago)",
         must_fail="no unauthenticated door hands out the identity the re-attach guard checks",
         why="makes the refusal name the session holding the lane. Found by walking the fix, not by "
             "the review: probe a taken name, be turned away, read the credential out of the refusal"),
]
