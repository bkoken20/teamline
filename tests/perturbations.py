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
         # The numbers move whenever the suites grow, so this claim is pinned to the SENTENCE and not
         # to a figure: `{LINES}` is substituted by the runner with a count deliberately far enough
         # out to fail the 20% band. Written as literal figures it went stale twice in one day, and
         # the only thing that noticed was a thirty-minute gate run.
         find="Roughly 1,610 lines of implementation and 2,690 lines of",
         repl="Roughly {LINES} lines of implementation and {LINES} lines of",
         must_fail="the README's 'roughly N lines' claims are within 20% of the real counts",
         why="restates the counts as figures the code does not support. The original defect was a "
             "tests claim out by 70%, understating the suite it was describing"),

    dict(id="R-16", suite="e2e", file="requirements.txt",
         find="httpx2==2.12.0",
         repl="# httpx2 arrives with mcp anyway",
         must_fail="the package imports nothing it does not declare (no hidden dependencies)",
         why="undeclares a package the CLI imports directly. The check used to keep its OWN copy of "
             "the declared set, and that copy allowed httpx2 -- so the one thing it exists to catch "
             "was sitting inside its own allowance, along with anyio, which nothing imports at all"),

    dict(id="R-15", suite="e2e", file="teamline/teamline_broker.py",
         find="        await ws.close(code=4000)          # no team-level address: a feed names its extension",
         repl="        await ws.accept()                  # accepted, then silent -- the case that used to pass\n"
              "        await asyncio.sleep(30)",
         must_fail="a feed without an extension name is refused at the handshake, with the documented status",
         why="accepts the socket and then says nothing. The old check caught any exception at all, so "
             "the 2-second read timing out READ AS A REFUSAL -- a broker that silently swallowed "
             "these connections passed it. Asserting the documented 403 is what tells them apart"),

    dict(id="R-13-tail", suite="unit", file="teamline/switchboard.py",
         find='                if _i == len(_lines) - 1 and not ln.endswith("\\n"):\n'
              '                    _torn = ln\n'
              '                    break\n',
         repl="",
         must_fail="a half-written last line does not stop the broker starting",
         why="makes an interrupted append fatal again. A crash mid-write leaves a partial final line, "
             "and the recommended deployment restarts automatically -- so one unclean shutdown became "
             "a container loop with the ledger, the broker's only memory, intact but for a few bytes"),

    dict(id="R-13-middle", suite="unit", file="teamline/switchboard.py",
         find='                raise SwitchError(\n'
              '                    f"the ledger is damaged at line {_i + 1} of {self.ledger_path}: {_ex}. This is a "',
         repl='                continue\n'
              '                raise SwitchError(\n'
              '                    f"the ledger is damaged at line {_i + 1} of {self.ledger_path}: {_ex}. This is a "',
         must_fail="a broken row in the MIDDLE is refused, not silently skipped",
         why="skips a damaged completed row instead of refusing. The board would rebuild with a hole "
             "in it and nothing would say so -- and every later row describes a world that includes "
             "the one that was skipped"),

    dict(id="R-21", suite="e2e", file="docs/PROTOCOL.md",
         find="set_running(team, {session_id: running})",
         repl="the host liveness sweep",
         must_fail="every public method of the state machine is reached by shipped code, or named in the contract",
         why="stops the contract naming the one state-machine capability no route reaches. The method "
             "is then dead code again by the only definition that matters to a reader: nothing calls "
             "it and nothing says what it is for"),

    dict(id="R-19", suite="e2e", file="teamline/switchboard.py",
         find='            if r.get("host", r.get("session_id") not in ("ws", "wait")):',
         repl='            if r.get("session_id") not in ("ws", "wait", "x"):',
         must_fail="a watcher cannot silence the caller's ring_delivered by acking with an internal marker string",
         why="decides the caller's signal from a string the CLIENT sent in its ack instead of from the "
             "row's own host field. Any watcher can then silence the caller by acking with one of the "
             "three marker strings -- and one of the three, 'x', was never written by anything but the "
             "tests, so shipped code was honouring a test fixture"),

    dict(id="R-17", suite="unit", file="teamline/switchboard.py",
         find='                              f"variant of the name, cannot work. Ask the operator to enable it.")',
         repl='                              f"variant of the name; teams are {TEAMS}.")',
         must_fail="...and the state machine's refusal does NOT enumerate the real teams, same as the broker's",
         why="puts the enumerating message back. It hands a caller that guessed a team name wrong the "
             "whole valid list, and the next guess is a disguise. The broker refuses the same condition "
             "without the list -- it only gets to because it checks the header first, so the policy has "
             "to hold here too rather than in the ordering of the two checks"),

    dict(id="R-5", suite="e2e", file="teamline/switchboard_broker.py",
         find='                if _lane is not None and sb._hygiene(_lane) == "LIVE":',
         repl='                if False:',
         must_fail="a restarted session refused inside the window is told to RETRY, not that it is unwelcome",
         why="sends the fatal code again for a refusal that expires by itself. Every session has a new "
             "identity, so a restarted one comes back to its own lane with a different sid -- and the "
             "shipped client stops for good on 4001, printing that the team is not enabled"),

    dict(id="R-4", suite="e2e", file="teamline/switchboard.py",
         find="        os.makedirs(calls_dir, exist_ok=True)\n",
         repl="",
         must_fail="...and a nested root that does not yet exist is created rather than crashing the broker",
         why="stops the broker creating the transcript directory under a root that does not exist "
             "yet. The old check made the directory ITSELF and then asserted it existed, so this "
             "perturbation would have changed nothing at all -- which is the defect"),

    dict(id="R-3", suite="unit", file="tests/test_switchboard.py",
         # It retires the fixture rather than deleting the lines that build it. Deleting them makes
         # the call and the leave below raise, the suite stops before the check runs, and the runner
         # reports NORUN -- untested, which is not the same as red. Ageing the board past the idle
         # retirement empties it exactly as it was originally empty, and every later line still runs.
         find="    live_exts = {e[\"ext\"] for e in sb.directory()}\n",
         repl="    clk.t += 25 * H\n    sb.tick()\n    live_exts = {e[\"ext\"] for e in sb.directory()}\n",
         must_fail="the replay fixture is NOT empty -- these checks compared set() with set() before",
         why="empties the state the replay section reads, which is how it was: everything registered "
             "earlier has been retired by that point, so the suite's HEADLINE property -- restart the "
             "broker and the ledger rebuilds it -- was asserted by set() == set() and all([])"),

    dict(id="C-L1c", suite="e2e", file="tests/perturbations.py",
         # It targets B3's line, not the claim above it: quoting that one made this claim's own `find`
         # match twice -- itself and its target -- and the runner refused it as AMBIG. A perturbation
         # that appears inside its own payload is not exact.
         find='find="| `TEAMLINE_ROOT` |", repl="| `UNDOCUMENTED_NOW` |",',
         repl='find="| `NO_SUCH_VARIABLE_ROW` |", repl="| `UNDOCUMENTED_NOW` |",',
         # BOTH occurrences, because the second one is this claim's own payload. A claim that edits
         # the claims file necessarily contains the text it edits, so "exactly once" can never hold
         # for it -- pointing it at a different line does not help, which was the second attempt.
         all_occurrences=True,
         must_fail="every perturbation claim's target text is still in the file it names",
         why="makes another claim STALE -- its target text no longer in the file it names. The runner "
             "already reports that, but only after re-running a suite per claim; this is the same "
             "question in a second, and the reason it exists is that a stale claim cost half an hour "
             "twice in one day before anything noticed"),

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

    dict(id="R-20", suite="e2e", file="teamline/switchboard_broker.py",
         find="        live_ids = {e[\"id\"] for e in sb._outbox}\n"
              "        for _book in (pushed_at, retry_at):\n"
              "            for _settled in [k for k in _book if k not in live_ids]:\n"
              "                del _book[_settled]\n",
         repl="",
         must_fail="delivery bookkeeping is released once a message is settled, not held for the process's life",
         why="restores two dictionaries that were only ever written to. A8 bounded the ledger and the "
             "rows retained and did not look at the delivery bookkeeping beside them, so a broker "
             "that had carried a million messages held a million keys for messages long settled"),

    dict(id="R-18", suite="unit", file="teamline/switchboard.py",
         find='            if e.get("gone_reason") == "host":\n                return "GONE"\n',
         repl='',
         must_fail="a socket drop cannot withdraw the HOST's evidence either",
         why="restores the branch that waited out the silence window on a lane the HOST had already "
             "reported absent -- so a second failure, the socket dropping, sent it back to LIVE for "
             "the whole window. A4 drew this distinction and guarded only the ping that arrives"),

    dict(id="R-8-record", suite="unit", file="teamline/switchboard.py",
         find='was=h, same_identity=_same)',
         repl='was=h)',
         must_fail="a replacement by a different session is recorded as such, not silently",
         why="takes the identity marker back out of the replacement row. On a board with no "
             "authentication, 'the session came back' and 'somebody else took the name' being the "
             "same row is the whole defect -- the record is the only thing that can tell them apart"),

    dict(id="R-7-cap", suite="unit", file="teamline/switchboard.py",
         find="        self._check_text(text)\n        c = self._calls.get(call_id)",
         repl="        c = self._calls.get(call_id)",
         must_fail="an over-long message is refused rather than silently truncated",
         why="reopens the third door into the ledger: operator_say is reachable over HTTP with no "
             "credential, and its row is fanned out to both parties as part frames. A8 capped the two "
             "doors it was looking at and this check tested only one of those"),

    dict(id="R-7-structural", suite="e2e", file="teamline/switchboard.py",
         # The same removal as R-7-cap, pinning the OTHER check. One is behavioural -- an over-long
         # message is refused -- and one is structural: no method writes unbounded text at all. A
         # fourth door would arrive past the first and be caught by the second.
         find="        self._check_text(text)\n        c = self._calls.get(call_id)",
         repl="        c = self._calls.get(call_id)",
         must_fail="every method that writes text into the ledger bounds it -- by refusing or by truncating",
         why="leaves a _commit of `text` with no cap and no slice, which is exactly the shape a new "
             "writing path takes when nobody remembers the rule"),

    dict(id="R-26-route", suite="e2e", file="docs/PROTOCOL.md",
         find="| `POST /operator/say` | `{text, call_id}` — inject a line into any open call, attributed to the operator. **A write, and it needs no team name** |\n",
         repl="",
         must_fail="the HTTP surface table lists every route the broker serves",
         why="takes the route back out of the surface table. It is the one a reader would most want "
             "there: a write, reachable with no credential, and it was the one missing"),

    dict(id="R-26-cap", suite="e2e", file="docs/PROTOCOL.md",
         find="so setting a cap is a **source edit**",
         repl="so setting a cap is done by configuration",
         must_fail="a constructor keyword the contract demonstrates is reachable, or the contract says it is not",
         why="puts back the implication that a documented knob can be set from outside the program. "
             "Nothing passes cap_into: no environment variable, no build() argument"),

    dict(id="R-25-timing", suite="e2e", file="docs/PROTOCOL.md",
         find="| retired | 10 minutes after the feed drops (which is 8.5 minutes after it reads GONE, not 10), ",
         repl="| retired | GONE for 10 minutes, ",
         must_fail="both documents time retirement from the same event the code does",
         why="restores a retirement clock started from the wrong event -- 90 s out, in a constant a "
             "reader checks. The operator page had it right the whole time, which is how two "
             "documents came to agree with each other rather than with the code"),

    dict(id="R-25-states", suite="e2e", file="docs/PROTOCOL.md",
         find="Re-registering a name replaces any holder that is not LIVE -- STALE, GONE or UNREACHABLE.",
         repl="Re-registering a name replaces a GONE or STALE holder.",
         must_fail="the contract names every hygiene state a re-registration may replace",
         why="drops UNREACHABLE from the replaceable states. register() refuses only LIVE, so every "
             "other hygiene value is replaceable -- and UNREACHABLE is the one a lane sits in when it "
             "registered and holds no feed"),

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
