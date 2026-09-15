# Fix log

An honest, per-defect record of what was wrong in this repository, how it was found, how it was
fixed, and what attacking each fix afterwards turned up. It exists so that nobody has to re-derive
this work from the code: if you are looking at a behaviour here and wondering whether it is a bug, a
deliberate choice, or something already investigated, look for it below.

It includes the parts that do not flatter the work — fixes that were wrong the first time, tests of
ours that passed while the defect was live, and findings we could not reproduce and dismissed. A
dismissal you disagree with is an invitation to re-open it; the reasoning is recorded so you can.

## Where the defects came from

Before the first public release, the repository was put through an adversarial review: seven
independent reviewers, each given one dimension (correctness, de-identification, documentation
accuracy, security, portability, test quality, first impression) and told that a claim without
evidence is not a finding. They produced 94 findings, deduplicated here to 43 items.

Two things about that process are worth stating plainly, because they shaped what follows. The
review was run at a scale that exhausted the budget it was running on, and its own verification
stage died part-way through; a harness bug then discarded most of its output, which was recovered
afterwards from the run's journal. And the review found several defects in tests that had been
written days earlier specifically to prevent them. Neither fact makes the findings less real.

## Method

One defect at a time. Never a batch — fixes landed together interfere, and then nobody can say which
change caused the next problem.

For each defect: reproduce it independently first, because a finding is a claim until confirmed;
name the test that should have caught it and write it if it does not exist; make it red and check
that the failure names *this* defect rather than a typo; apply the smallest fix that turns it green;
then **walk the changed code with real values** — the statements touched and the ones that consume
their results — because a green suite does not prove anyone read the change; then attack the fix
adversarially and assume it is wrong; anything the attack finds goes back into the same loop. Only
when an attack round finds nothing new is the defect closed, with one commit for that defect alone.

---

## A1 — an ack from any holder settled any message

**Severity:** high. Silent message loss and a false delivery record, reachable by any client that
can open a feed.

**What was wrong.** `on_frame()` receives the frame *and* the extension whose socket it arrived on,
and `awaiting[msg_id]` records the intended recipient at push time. The ack path consulted neither.
Any holder could send `{"ack": "<any message id>", "accepted": true}` and the broker would honour it.

Because a `delivered` row removes the message from the outbox permanently, this did not merely
mislabel a delivery — it **destroyed the real recipient's message** and ledgered it against the
sender's session. The `accepted: false` branch was equally open, letting any holder force a retry
and backoff on another lane's traffic.

**How it was found.** The security reviewer, reading the ack path against the push path.

**The test that should have caught it.** None existed. Both suites exercised acks only from the lane
the message was addressed to, so the happy path was well covered and the question "what if it comes
from somewhere else" was never asked. The new check registers a lane that holds a socket and never
acks, sends it a voicemail, then acks that message from an unrelated lane's socket. Before the fix it
failed with the ledger row that names the problem:

```
{'event': 'delivered', 'msg_id': 'f4fffded…', 'session_id': 'sess-ATTACKER'}
```

and the victim's `pending` count at zero.

**The fix.** One guard in the ack path: the message must be in the outbox *for the extension whose
socket the frame arrived on*. Anything else is dropped and the frame counts only as liveness.

It is validated against the outbox rather than against `awaiting`, and that choice came out of the
walk rather than from preference. `awaiting` is popped when the ack window expires, so a genuine but
late ack would become unvalidatable; `delivery_failed` has no branch in `_apply`, so a message that
failed delivery stays in the outbox and remains checkable. Validating against the outbox therefore
keeps late acks working while still rejecting foreign ones.

**What attacking the fix found.** Two rounds.

The first round found a defect in the fix itself: it called `pending_for()`, which builds a dict copy
of every matching outbox entry, to answer a boolean — on a path that runs on every single delivery.
Replaced with a direct scan over the outbox, matching what `resend_pending()` and the state writer
already do. The second round found nothing new.

Two things the attack cleared rather than changed. The rejected-ack path now counts as liveness
(`touch`), which is what every other unrecognised frame already does, so it grants no capability a
client did not have by sending a ping. And a duplicate ack from the true owner, after delivery, is
now rejected by the guard instead of being absorbed by `mark_delivered`'s own duplicate check —
different route, same outcome.

**Verified by.** Both suites green on their exit codes, not on a grep of their output. The guard's
discriminating clause was perturbed — dropping the lane comparison — and both new checks went red,
so they are tied to the mechanism rather than passing by construction.

**Found along the way, recorded and not fixed here.** A client can still put an arbitrary
`session_id` on the ack for *its own* message, so that field on a `delivered` row is
self-reported. Low harm — it mislabels only the sender's own delivery — but it is not verified, and
it is queued rather than folded into this fix.

**Also found: a defect in the new test.** The first version of the victim holder waited for 2.5
seconds of silence. A lane that never acks is re-pushed every ack-timeout plus backoff, so it never
falls silent and the suite hung. Bounded by an absolute deadline instead. Worth knowing if you write
a client: an acking lane that does not ack is not quiet, it is a loop.

---

## A2 — a dropped socket handed the lane to whoever connected next

**Severity:** high. Impersonation and message interception, reachable by anyone who can read the
directory and open a socket.

**What was wrong.** Two doors, and closing either one alone left the other open.

The re-attach path allowed `e["feed"] and not e["feed_up"]` on its own: once a holder's socket
closed, any client naming the same `team/ext` took the lane, carrying no `sid` and no `session_id`.
Both halves of the name are public in `/directory`, so "knows the name" proved nothing.

And a lane became GONE the *instant* its socket closed, with no grace period, while
`register()` hands a GONE lane to whoever asks next. So even with the first door shut, a stranger
simply re-registered over the lane.

The reproduction was worse than the report. After the takeover the directory row still carried the
**owner's** `sid` while the now-line and the socket were the stranger's:

```
directory now   : now='not-mine' sid='session-OWNER-0001' holders=1
```

so the lane still read as the owner's to everyone else, and its traffic arrived on the stranger's
socket.

**How it was found.** The security reviewer. Reproduced here with a standalone script before
anything was changed: owner registers with a `sid`, its socket closes, a stranger connects with no
credentials at all and is handed the lane.

**The test that should have caught it.** None. The suite covered the *duplicate* holder case
thoroughly — a second socket while the first is alive is refused — but never asked what happens to
an identity when the first socket is merely gone. The new check asserts both halves: a stranger is
refused, and the rightful holder still gets back in with its own `sid`. The second half matters as
much as the first, and is why the offending clause could not simply be deleted.

**The fix, in two parts.**

*Identity on re-attach.* A re-attach is allowed when the caller proves the identity the lane was
registered with — or when the lane carries no identity at all, in which case there is nothing to
prove. An anonymous lane cannot be protected; the code says so rather than pretending otherwise.

*A silence window before GONE.* `PROTOCOL.md` already promised that a lane is presumed dead after a
period of silence. The code presumed it instantly. Restoring the window closes the register door and
removes a divergence between the documented contract and the behaviour.

Deleting the dead-socket clause outright was the alternative, and it is worse: a lane whose socket
blipped could not reconnect until it was retired, and a session with no line cannot be rung to be
told about it.

**What attacking the fix found.** The first attempt, identity-on-re-attach alone, did not work: the
test stayed red because the takeover simply moved to `register()`. That is the reason the fix has two
parts rather than one, and it was the test that said so, not review.

Then a sharper problem. The window was first applied to *every* route into GONE, which broke three
unit checks asserting that a session reported missing by its host is GONE at once. That is correct:
a host saying "this session is not running" is **evidence**, while a quiet socket is only
**silence**, and only silence deserves a grace period. The two are distinguishable because a dropped
feed leaves `feed_up` false whereas a host-reported absence does not. The window now applies to
silence alone.

Both halves were then perturbed independently, and each turns the seizure check red on its own — the
identity clause because the stranger re-attaches directly, the window because the stranger
re-registers. Neither is redundant.

**Verified by.** Both suites green on exit codes; the two new checks; the pre-existing
reclaim-a-dead-lane check still passing, which is the property the fix had to preserve.

**A consequence, recorded rather than hidden.** Within the silence window a ring to a lane whose
socket just dropped is now accepted instead of being refused immediately. The ring timer and the
peer-lost path still resolve it, so the worst outcome is a voicemail arriving later than it used to.
That is the price of the window, and it matches what the protocol document already promised.

---

## A3 — the operator surface needs no credential, and the README said otherwise

**Severity:** the behaviour is by design; the sentence describing it was false. Recorded as a
documentation defect, not a code one, and the reasoning is here so you can disagree.

**What was wrong.** The README's security section stated the boundary as "anyone who can reach the
port **and knows a team name**". The operator surface needs no team name at all. `operator` is not a
configured team, so `TEAMLINE_TEAMS` — the only access control a deployment has — does not gate it:

* `GET /` serves the dashboard;
* `ws://…/ws?party=operator` is accepted with no credential, sends a snapshot of the last 200 ledger
  rows (message text, subjects and openings included) and then streams every new row;
* `POST /operator/say` injects a line into any open call, attributed to the operator.

**Why this was NOT fixed in code.** The system is deliberately trusted-network-only and says so in
its first section, and the README already states that teams are administrative rather than a security
boundary. Under that model an open dashboard is the design, not a bug. Adding authentication here
would mean shipping new, untested security code, which is worse than an honest boundary.

An environment switch to disable the operator surface was considered — for someone who wants the
broker reachable but not the dashboard — and deliberately not added, because it buys little in the
documented deployment (bind to loopback, reach it over a VPN, where everyone is trusted anyway) and
adds a code path. If you want it, it is a small change and this paragraph is your notice that nobody
overlooked it.

**No test, this is prose.** The defect was a sentence, and a sentence has no test.

What *could* be pinned was the behaviour the sentence has to describe, so that if anyone later adds
a credential the suite forces the document to be rewritten with it. Two checks now assert that
`operator` is not a configured team, and that the observer's snapshot carries raw ledger rows with
message text. Both were perturbed — adding `operator` to the team list, and emptying the snapshot's
rows — and each fires.

**The fix.** The security section now states the operator surface separately, lists exactly what it
exposes, and says plainly that there is no switch to turn it off.

---

## A4 — a lane marked down by silence never came back

**Severity:** high. A healthy, connected session lost its line permanently and could not be told,
because a session with no line cannot be rung.

**What was wrong.** `tick()` presumes a keepaliving holder dead after `feed_gone_s` of quiet. That
is right. But the holder's socket may be perfectly healthy and merely paused — a long turn, a stall,
a suspended process — and when it resumed keepalives, **nothing restored the lane**:
`set_running_ext()` only refreshed `last_seen`. `feed_up` stayed false and `gone_since` stayed set,
so the lane read GONE while still connected, was retired ten minutes later, and its holder went on
pinging into a lane that no longer existed.

**The test that should have caught it.** None. The suite covered a feed going silent, and it covered
a feed reconnecting on a *new* socket, but never a holder that goes quiet and then speaks again on
the *same* one.

**The fix.** A frame arriving on a lane's socket disproves the silence, so it restores the feed —
ledgered, so a replay reaches the same state. Applied to the keepalive path and to any other inbound
frame, since the argument is about frames rather than about pings. It cannot fire spuriously: a
frame requires an open socket, and for a non-acking feed `feed_up` false means the socket itself is
gone, so nothing can arrive on it. Every caller was checked — all five are inside `on_frame`.

**What attacking the fix found — a real hole in it.** `_apply` cleared "gone" without knowing *why*
it had been set, and `gone_since` has two very different causes. A quiet socket is **silence**. A
host reporting that a session is not running is **evidence** — and a watcher is a separate process
from the session it delivers to, so its socket being alive says nothing about whether that session
still exists.

The first version of the fix therefore resurrected a session its host had declared dead, in one
specific ordering:

```
host says absent  ->  socket also stalls  ->  watcher pings  =>  LIVE     (evidence lost)
```

The reverse ordering looked fine and passed, which is exactly why it was worth attacking: in that
order `feed_up` was still true, so the restore never fired and the hole stayed hidden.

The lane now carries the *reason* it is gone. A frame clears only a lane that went quiet; a lane the
host declared absent stays gone until the host says otherwise. First cause wins, and the host's word
is the stronger evidence.

**Verified by.** Both suites green on exit codes. Four checks: the recovery, the pure host-absent
case, and the ordering that broke the first attempt. Both halves of the fix perturbed independently
— removing the restore, and restoring without consulting the reason — and each turns its own check
red.

**A consequence, recorded.** Recovering a lane does not resurrect a call that was ended as
`peer_lost` while it was down. The caller was already told; the lane comes back for future traffic
only. That is intended, and stated here so nobody reads it as a second bug.

---

## A5 — a finished call could still wake you

**Severity:** medium. Spurious wake-ups about a conversation that had already ended — which in a
system whose whole design is "only wake for something real" is a defect of the premise.

**What was wrong.** Replay had always dropped the transient signals of ended calls — a
`ring_delivered` or a `nudge` belonging to a call that is over is stale, not pending. The **live**
path had no equivalent. So a signal queued while a call was open stayed deliverable after it ended,
and the recipient was woken for a finished conversation. Two code paths, one rule written once, and
only one of them had it.

**How it was found.** The correctness reviewer, noticing that the cleanup existed only in `_replay`.

**The test that should have caught it.** None. Replay's version was tested; the live path's absence
was not, because no check ever ended a call that had a signal already queued.

The reproduction turned out to be simpler than the one being written. A single `tick()` after six
minutes does both jobs at once: five minutes of silence nudges the parties, and the callee's watcher
having gone quiet past `feed_gone_s` ends the call as `peer_lost`. So the nudge is queued and the
call it belongs to is over, in that order — exactly the state replay cleans up. The first draft of
the test tried to hang up manually and failed with "has no active call", because `tick()` had
already ended it. The mechanism wrote a better test than the intention did.

**The fix.** One rule, applied wherever a call becomes ENDED — all three sites — so the live path and
replay cannot disagree again. Replay keeps its sweep, because a ledger written before this fix can
still contain such rows, but it now shares the same constant rather than a second copy of the tuple.

**What attacking the fix found.** The risk in a purge is purging too much. `say` lines are **not**
transient: a party that has not yet read the last thing said to it must still receive it after the
call ends, or ending a call would swallow its tail. Nothing pinned that, which my own fix made
newly dangerous, so a check for it was added before attacking rather than after.

Both directions were then perturbed: removing the three live purge calls turns the first check red,
and adding `say` to the transient list turns the second red. Neither is passing by construction.

**Verified by.** Both suites green on exit codes, four checks between them.

---

## A6 — the second nudge of any call never arrived

**Severity:** medium, and squarely against the feature's purpose. A nudge exists to break a stall
that neither side has noticed; one that silently does not fire is worse than none, because both
parties believe the mechanism is watching.

**What was wrong.** A nudge's message id was `{call_id}-nudge-{n}-{who}`, and `n` counts rounds
within the current silent stretch — which a `say` resets to zero. So the sequence

```
5 min of silence   -> nudge n=1, ids …-nudge-1-callee / -caller, delivered
someone speaks     -> last_line and the counter both reset
5 min of silence   -> nudge n=1 again, SAME ids
```

regenerated ids that were already in the delivered set, and `_emit` dropped them without a word.
The parties then sat through a second five minutes with nothing; only the ten-minute mark produced
an `n=2` id that was new enough to get through.

**The test that should have caught it.** None. The suite proved a silent call nudges. It never
asked what happens when a call goes quiet, recovers, and goes quiet *again* — which is the ordinary
shape of a long conversation.

**The fix.** The id now carries *which* silent stretch it belongs to, taken from the stretch's start
(`last_line`) and written into the ledger row as `since`, so replay does not have to reconstruct it.
Two different stretches produce different ids; two nudges within one stretch still differ by `n`, so
the deduplication that was wanted still works. Rows written before `since` existed default to zero
and keep exactly the ids they were delivered under.

**What attacking the fix found.** Nothing that needed changing. The one candidate was a collision:
two stretches whose start falls in the same whole second would share an id. It cannot happen — a new
stretch begins at least `NUDGE_S` after the previous one by construction, so their starts are
minutes apart. Perturbing the id back to its old form turns the check red.

**Note for anyone writing a client.** Message ids here are meaningful, not opaque: the broker
deduplicates on them. If you generate ids yourself, two different events must never produce the same
one — this defect is what that costs.

---

## A7 — a ring could pin a caller's lane forever

**Severity:** medium-high. A caller could be permanently unable to place any call, with no way out
and nothing to tell it why.

**What was wrong.** The ninety-second answer timer counts only the **callee's idle time**, and that
is deliberate: a session mid-turn cannot see a ring, so holding the timer while it is busy is right.
What was missing is an outer bound. `CALL_CAP_S` applied only once a call was already open, so a
callee whose watcher kept reporting "still running" never advanced the idle timer and the ring never
ended. An extension holds one call at a time, so the **caller's** lane stayed pinned — and `decline`
belongs to the callee while `hangup` requires an open call, so the caller had no escape either.

**How it was found.** The correctness reviewer. Reproducing it took two attempts, and the first was
wrong in an instructive way: letting the callee's feed simply go quiet ended the call after an hour
via the feed-silence rule, which looks like the system behaving correctly. The defect only appears
when the callee's watcher stays healthy and keeps pinging — a busy session, not a dead one. A
reproduction that confirms the wrong mechanism would have dismissed a real finding.

Measured: twenty-four hours of a mid-turn callee, `ring_wait` still at zero, the caller blocked with
`alpha/ringer already holds call …`.

**The test that should have caught it.** None. The suite covered a ring that times out and a ring to
a busy session that is correctly held; it never let the hold run long enough to ask whether it ever
ends.

**The fix.** A ring now gets the same cap an open call gets — `CALL_CAP_S`, measured from when it
started ringing — reusing the existing `ring_timeout` event with a reason that says which bound
fired, so the ledger format and replay are unchanged. Within the cap the etiquette rule is exactly
as before.

**What attacking the fix found.** Nothing that changed the code, but one thing worth stating: this
**bounds the damage, it does not remove it**. A caller is still stuck for up to two hours and still
cannot withdraw its own ring. Giving the caller a cancel is a genuine gap, and it is queued as its
own item rather than folded in here — adding an API is a feature, and this defect was "stuck
forever", which the cap fixes.

Perturbing the outer bound away turns the check red.

**Verified by.** Both suites green on exit codes; two checks, the second asserting the caller's lane
is usable again afterwards, since freeing the lane is the point rather than ending the call.

---

## A8 — unbounded growth a single client could drive

**Severity:** medium. Not an exploit so much as an absent limit, which in a long-running service is
the same thing eventually.

**What was wrong.** Two separate leaks.

*Message text was uncapped on exactly the paths that carry content.* Receipts were capped at 300
characters, now-lines at 200, delivery errors at 300 — but `say` and `leave` took whatever they were
given. One caller could write a row of any size into an append-only ledger that is replayed into
memory at every start.

*Every ledger row was retained in memory forever.* `_rows` grew without bound for the life of the
broker, and its only reader was the operator snapshot, which takes the last 200.

**The fix.** A message longer than `TEXT_MAX` is **refused**, not truncated. Silently cutting a
message in a messaging system loses meaning without telling anyone, while a client that is told the
limit can split. That is a deliberate departure from the truncation used elsewhere in this file,
where the values are status lines rather than content. And `_rows` is now a bounded deque, so what is
*retained* is capped while the file stays the source of truth.

**What attacking the fix found.** The obvious way to get this wrong is to bound the wrong thing and
break replay. Checked directly: a ledger of 5,201 rows, with the extension registered at row 1, and
a fresh broker over the same file — the extension survives, because replay applies every row and only
retention is capped. That property is now a check of its own, since it is the one the fix could have
destroyed.

Both bounds perturbed independently, each turns its own check red.

**What this does NOT fix, stated plainly.** The ledger **file** is still unbounded. It is the source
of truth and is replayed in full at startup, so a broker that runs for years will start slowly and
hold a large working set. Compaction — folding old rows into a checkpoint — is not implemented. If
you run this at volume, that is the next thing to build, and nobody has overlooked it.

---

## A9 — dead code reaching for a key that no longer exists

**Severity:** low on its own. Recorded because of *why* it survived, which is the interesting part.

**What was wrong.** `teamline_broker.py` carried `_flush_soon()` and `_safe_flush()`, and
`_safe_flush` read `sw["deliverer"]`. Nothing called either function, and the wiring returns no
`deliverer` key — it provides `sb, feed, ticker, snapshot_extra, hook_now, directory_json, healthz,
write_state`. Left over from the design before delivery was inverted, when the broker pushed into
hosts itself.

Two things hid it. The reference sat inside a `try/except Exception` that would have turned the
`KeyError` into a line on stderr rather than a failure. And the path was unreachable, so no test
could ever have executed it.

**The fix.** Deleted. There is nothing to preserve in code that cannot run.

**The check that now exists.** Deleting dead code teaches nobody anything, so the useful part is the
guard: every `sw[...]` the broker reads must be a key the wiring actually returns. It compares the
two files at the source level *deliberately* — a runtime check cannot reach an unreachable line,
which is exactly the class of defect this was.

**What attacking the fix found.** The first perturbation was wrong and said so loudly. Pointing a
*live* line at a missing key crashed the application at startup, so the suite never reached the
check — proving only that the key is used there. The perturbation had to take the shape of the real
defect, an **unreachable** reference, and with that the check fires.

Also checked for collateral: `sys` is still used by the module's path setup, so removing its only
stderr write left no dangling import.

**A limit of the check, stated.** It matches `sw["literal"]` only. A dynamic lookup — `sw[name]` —
would slip past it. Nothing in the module does that today, and if you add one, this guard will not
protect you.

---

## B1 — one team was privileged by its name

**Severity:** high against the project's central promise. The README says teams are one environment
variable; the code granted one literal name a different behaviour.

**What was wrong.** `sw_register`'s no-feed path read `feed=(t == "<a literal team name>" and not
session_id)`. Both branches were wrong, in opposite directions.

The privileged team got a lane that **claimed a feed nobody held**. Measured: `IDLE`, `LIVE`,
`holders: 0` — advertised as answerable, able to receive nothing, and callers ringing into silence.
Every other team got a refusal whose text never mentioned the real reason, so a reader following the
README and naming their teams anything else saw "must register its host session id" and had no way to
learn that the name was the problem.

**How it came about, honestly.** This was not in the original system. It was introduced here, during
the de-identification pass, by a blind search-and-replace that turned a semantic — *the team whose
harness cannot acknowledge deliveries* — into a literal name. A mechanical rename changed behaviour,
which is exactly the failure mode such a pass is supposed not to have.

**The decision.** Three options were weighed: delete the special case; make it configurable with a
second environment variable; or document that the first team listed is the privileged one. The last
two keep the phantom lane and only change who receives it — the team-name coupling is just how the
defect is *reached*, not what it is. Deleted.

**The fix.** `sw_register` never asserts a feed on the caller's behalf. Holding a socket is the
registration; registering without one requires a session id, for every team alike, because nothing
else can say where to deliver. Such a lane reads UNREACHABLE until something holds a feed for it, so
callers get voicemail rather than a ring into nowhere.

**What attacking the fix found.** The fix falsified the tool's own docstring, which still implied
that only one team needed a session id — and that docstring is what an MCP client shows the agent
before it calls anything. Leaving it would have replaced a wrong behaviour with a wrong instruction.
Rewritten to say what the tool now does.

Perturbing the privilege back turns two of the three checks red.

**Verified by.** Three checks: that every team is treated identically, that a registration holding no
feed is refused rather than claiming one, and that no lane anywhere reads LIVE with zero holders —
the last being the shape of the phantom, stated as an invariant rather than a special case.

---

## B2 — the server told agents to do the opposite of what the documentation says

**Severity:** high for adoption. This string is read by every agent that connects, before any
documentation and before any tool call, so it outranks the README in practice.

**What was wrong.** The MCP server's `instructions` opened with **"sw_register first"**. The README
and `PROTOCOL.md` both say that holding a feed *is* the registration. An agent following the advice
its own tools gave it therefore took the path that produces a lane nothing can deliver to — and had
no reason to doubt it, because that advice arrived first and from the system itself.

The same string also named one particular team, which means nothing to anyone whose teams are named
otherwise.

**The test that should have caught it.** None. Nothing in either suite had ever looked at the
instructions string, even though it is the system's most-read sentence.

**The fix.** Rewritten to say what the system does: you register by holding a feed; `sw_register`
exists only for a session that cannot hold one, and its lane stays unreachable until something does.
No team named.

**What attacking the fix found — the check failed on its own explanation.** The first version of the
check matched the source text with a regex, and the comment explaining the fix contains the phrase
`"sw_register first"`. So the check went red because of the sentence describing what had been
repaired. It now reads the *value* through the syntax tree rather than the text around it, which is
what an agent actually receives.

That is worth generalising: a check that greps source will eventually match the prose about the
defect rather than the defect. Compare values, not text, whenever you can.

Perturbing the instructions back turns the check red.

---

## B3 — the README documented no configuration at all

**Severity:** high for a first-time reader. "How do I set this up" is the first question anyone has,
and the repository answered almost none of it.

**What was wrong.** Five of the seven environment variables the package reads — the data directory,
the port, the bind address, the page path, and the client's broker URL — appeared **nowhere** in the
README. `TEAMLINE_TEAMS` appeared once, inline in a command, with no explanation of what it did or
that it was the only access control a deployment has. Anyone wanting to change where the ledger is
written, or which teams exist, had to read the source.

**The test that should have caught it.** None, and prose has no test — but the *rule* does. The new
check extracts every `os.environ.get("…")` name the package reads and requires each to appear in the
README. It reported exactly the five that were missing, and it fails for any variable added later
without documentation.

This is the same lesson as the previous entry, in a different costume: do not test the prose, test
the property the prose has to satisfy.

**The fix.** A configuration section listing every variable, its default, and what it actually does
— including that `TEAMLINE_ROOT` is relative to the working directory you launch from rather than to
the repository, which is the kind of detail that is obvious in the source and invisible from outside.

**A note on method.** The first draft of this fix also documented the port-versus-client-URL trap.
That is a separate queue item with its own behaviour to reproduce, so it was withdrawn from this
change and left for its own turn. Fixing two findings in one edit is how a fix becomes unattributable
when something later breaks.

Perturbing a row out of the table turns the check red, naming the variable that lost its
documentation.

---

## B4 — moving the port broke the clients, silently

**Severity:** medium, and disproportionately painful. Nothing was broken except the ability to find
out what was broken.

**What was wrong.** `TEAMLINE_PORT` moves the broker. It does not move the clients, which dial
`TEAMLINE_URL` and default to port 3790 regardless. The shipped compose file sets `TEAMLINE_PORT`,
so this is a path people take. What they saw, forever, every two seconds:

```
{"type": "feed_down", "error": "[WinError 1225] The remote computer refused the network connection", "retry_s": 2}
```

An OS error, no address, no mention of any setting. The client did not even print where it was
dialling.

**Design, twice.** The tempting fix is to make the client's default follow `TEAMLINE_PORT`. It was
rejected: on a machine that runs its own broker, that variable is about the local broker, and
honouring it would silently point a *remote* client somewhere wrong. That trades a loud confusion
for a silent misconnection — and the whole of this review has been about silent wrong behaviour.
Documenting it alone was also rejected: it leaves the person staring at the bare error, and nobody
reads the configuration section until after they are stuck.

**The fix.** The failure now says what it is. The first failure of an outage carries the address it
dialled and the knob that changes it; later ones stay terse so a long outage does not fill the log.

**What attacking the fix found.** `down_seen` was never reset, so a client that reconnected and then
lost the broker again would have diagnosed only the *first* outage in its lifetime and stayed quiet
about every one after. A reconnect now ends the outage, so each distinct one is diagnosed once.

Perturbing the address out of the row turns the first check red while the second still passes,
which is the right shape: the two halves — *what* it tried and *how* to change it — are checked
separately because either could be lost on its own.

**Recorded, not fixed:** `teamline_cli.py` has the same default and prints a plainer failure. It is
a one-shot command rather than a long-lived holder, so the silence is far less costly there, but the
asymmetry is real and it is queued rather than folded into this change.

---

## B5 — nothing told a reader which file to run

**Severity:** medium, and the first thing a newcomer hits after the quickstart.

**What was wrong.** Seven files, three of them with close names, and the README described none of
them. Worse, the names actively mislead: `switchboard_broker.py` is neither runnable nor a broker —
it is the wiring between the state machine and the transport — while the file you actually run is
`teamline_broker.py`. A reader opening the repository had no way to know that except by reading all
three.

**Why the names were not simply fixed.** Renaming three modules touches every import and every test,
for readability that a table buys just as well. The names are a wart; a map is cheaper than surgery,
and the map says out loud that the names are unhelpful rather than pretending otherwise.

**The fix.** A table naming every file and what it is, with `switchboard.py` marked as the one to
read first and `teamline_broker.py` as the one to run.

**The check.** Every `.py` in the package must be mentioned in the README. It reported exactly the
one that was missing, and it fails for any module added later without a line in the table.

**A correction to this entry.** It first claimed the perturbation turned the check red. It did not.
Replacing only the table row left the filename in the sentence above the table, so the check still
found it and passed — and the claim was written without reading the output properly. Perturbed again
with *every* occurrence removed, the check does fire and names the missing module.

Two things worth taking from that. A perturbation that fails to fire is evidence about the
*perturbation* until you have checked which, and an unread green is how a test that cannot fail gets
believed in the first place.

---

## B-L1 — the README recommends a deployment it never says exists

**Severity:** medium. It is the security section's own advice that a reader cannot follow.

**On the numbering.** The review queue — 43 items, of which these entries close 15 — did not travel
between machines; only its per-group counts did. The three group-B items still open are therefore
known by count and not by content, and this is **not** one of them recovered. It was found by
re-deriving what group B measures: setting the repository up from the README, cold, on a machine that
had never run it. It is numbered `B-L1` rather than `B6` so that nobody later reads it as the
original finding.

**What was wrong.** The security section tells you how to deploy this safely — *"Bind it to loopback
and reach it through a VPN or an SSH tunnel, as the shipped `docker-compose.yml` does"* — and the
port section refers to the same file again. Neither says where it is. The file map covered the
package and `tests/`, so `deploy/` and `docs/` appeared nowhere in the README at all: the reader is
pointed at the recommended deployment, cannot find it, and is given no command to run it. The one
invocation that exists lives in a comment *inside* the file they have not been told about. `docs/`
went the same way, which left `PROTOCOL.md` and `ONBOARDING.md` invisible to anyone who had read
only the README.

The compose file itself is fine — it publishes on `127.0.0.1` exactly as the README claims. This is
not a false statement; it is a true statement about an unreachable file.

**The test that should have caught it, and did not.** One existed: *every module in the package is
described in the README* (pinned as B5). It scans `teamline/*.py`, so it stayed green while two
top-level directories went undescribed — it checks the package, and the defect was in the
repository. Widened to every top-level directory it went red, naming `['deploy', 'docs']`.

**The fix.** Two rows in the map: `deploy/`, with the run command promoted out of the comment, and
`docs/` with what each file in it is for. The lead sentence said "Seven files" for six; it now counts
the package correctly and says the table maps the repository.

**What attacking the fix turned up, and it was a real defect in the fix.** The broker's documented
default writes its ledger to `./data` relative to the working directory. Run the quickstart from the
repo root — which is what the README tells you to do — and a `data/` directory appears, whereupon the
new check went **red demanding that the ledger directory be documented**. A suite turned red by using
the software as documented. Reproduced by creating the directory: exit 1, `[['data']]`.

Fixed by considering only directories that are part of the repository, read from `.gitignore` rather
than hard-coding the name — `data/`, `calls/`, `venv/`, `*.egg-info/` and the rest are declared there
already, so the next runtime directory is handled without another edit. Re-derived afterwards that
the exclusion had not made the check unfalsifiable: the perturbation still fires.

**The check.** Every top-level directory that is not git-ignored must be named in the README.
Perturbing it — removing `deploy/` from the map, *all* occurrences for B5's reason, since the run
command on the same row carries the string a second time — turns it red.

**A limit, stated rather than fixed.** `dist/` is not in `.gitignore`, so a build artefact by that
name would still demand a README entry. That is arguably a gap in `.gitignore` rather than in this
check, and fixing it here would batch a second defect into this one's commit.

**Not verified.** The `docker compose -f deploy/docker-compose.yml up -d` line is the compose file's
own documented invocation, moved to where it can be found. It has **not** been executed — running
Docker on this machine belongs to another session — so this entry claims the command is now
findable, not that it was proven to work here.

---

## D-L1 — the rename took the team names out and left the lane names in

**Severity:** high for a repository about to be published, and irreversible once it is.

**What was wrong.** De-identifying this repository replaced the private deployment's team names with
`alpha`/`beta`/`gamma`. Four comments kept the *lane* names underneath — a real session's lane with
only its team relabelled, next to a bare clock time pointing at an incident log no reader here can
see. One of them sat in shipped source, not just tests.

**How it was found.** Not from the review queue: that queue did not survive the move between
machines, so the group-D items are known by count and not by content. This was found by scanning the
tree and, more usefully, the **history** — a push publishes all commits, not the working tree. The
history is clean: the real team names never entered it, and the commit that "removed an identity"
turns out to have been cleaning the grammatical wreckage the rename left, not a name.

**The fix.** The four comments keep their engineering fact and lose the pointer: *one lane held five
holders* rather than a named lane. Numbered `D-L1`, not a queue id, for the reason given in B-L1.

**The check.** No identifier from the private deployment may appear anywhere in the tree. Its needles
are assembled from string fragments so that the check can scan **its own file** — a denylist written
out literally reports itself, and then the only way to keep the suite green is to stop scanning the
files most likely to carry a leak.

It caught the author immediately: the explanatory comment above it quoted one of the leaked names as
an example, and the check flagged it. The comment now says why no example is quoted.

**What the walk turned up, and the check had to be widened.** Walking it printed the files actually
read, and the list was short: an extension allowlist (`.py`, `.md`, `.html`, `.yml`, `.txt`) was
silently skipping `deploy/Dockerfile`, `LICENSE` and `tests/page_probe.js` — all published, all able
to carry a name, and `page_probe.js` is source. They were clean, which is luck rather than design.
The scan now reads every file outside the excluded directories.

**A limit.** The needle list is a denylist of names known to have leaked. It cannot catch a private
identifier nobody has thought of. It is a regression guard, not a proof of de-identification.

---

## C-L1 — the README counted itself, and nothing checked the counts

**Severity:** medium, and it is the cheapest kind of claim for a reader to test — which matters for a
repository people arrive at from an article.

**What was wrong.** Three numbers in the README described the repository and had drifted:

| claim | actual |
|---|---|
| "Seven files" | six in the package (fixed in B-L1) |
| `tests/` — "two suites and a headless page probe" | three suites |
| "Roughly 1,700 lines of implementation and 1,000 lines of tests" | 1,557 and 1,741 |

The tests figure was out by 70%, and it *understated* the suite it was describing — the repository
was selling itself short on the one number a sceptical reader checks first.

**The cause is the same for all three**, which is why they are one item and not three: a number
written into prose that nothing verifies becomes false the moment anyone adds a file. Correcting the
figures without pinning them buys nothing except the next drift.

**What was checked and was NOT wrong**, since a fix log that records only faults misleads: every
environment-variable default (`TEAMLINE_TEAMS`, `ROOT`, `PORT`, `BIND`, `PAGE`, `URL`, `PARTY`), the
hygiene table (STALE 2 h, GONE 90 s, retired at 10 min, idle 24 h), voicemail held 7 days, and the
25-second keepalive all match the code exactly.

**The fix.** The figures corrected, and two checks so they cannot drift again: no suite count in the
README may contradict `tests/`, every suite must be named there, and the "roughly N lines" claims
must be within 20% of a live count. A band rather than a figure, because the claim is hedged with
"Roughly" and ordinary work should not trip it — 20% is wide enough to leave alone and narrow enough
that a claim which has stopped being true fails.

**What attacking the fix turned up.** The line-count check was first written with the measured
numbers *in its own name*. The perturbation runner addresses checks **by name**, so a name carrying a
computed number cannot be pinned at all, and would drift out of reference the next time anyone added
a line. The numbers moved to the failure detail, where they are printed only when it fails.

The walk then asked the question that decides whether this is a guard or a decoration: if the regex
that finds the claim ever misses, does the check fail or pass? Simulated by rewording the sentence —
it **fails**. A silent miss would have made it a check that cannot fail.

---

## C-L2 — a security control is switched off, and the security section did not say so

**Severity:** the highest in this log. Everything else here is a defect in the software; this one is a
defect in what the software *tells you about itself*, in the section headed "read this before
deploying".

**What was wrong.** `teamline_broker.py` disables the MCP transport's DNS-rebinding guard —
`TransportSecuritySettings(enable_dns_rebinding_protection=False)`. The reason is real: the guard
accepts only `Host: 127.0.0.1`, so every client on any deployment that is not pure loopback is
refused, the shipped compose file included. Turning it off was the price of the broker being
reachable at all.

The security section documented the missing authentication and the wide-open operator surface, and
never mentioned this. The only trace was a line in the Status list — *"the MCP transport's
DNS-rebinding guard refused every non-loopback client"* — which reads as a bug that was **fixed**,
not as a control that was switched off and left off.

**Why this is worse than the missing authentication, which is documented at length.** The missing
auth is a property of the network you deploy on, and the README tells you to put the service behind a
VPN. DNS rebinding is the attack that reaches a **loopback-bound** service through a browser running
on a machine that can already reach it — so it defeats exactly the mitigation the section recommends.
The undocumented hole was in the same paragraph as the advice that it undermines.

**The comment justifying it was true only of the deployment it came from.** It read that the broker
*"lives on the private the private network with no other auth, so the guard buys nothing here"*, and carried a
private address and an internal incident time. A reader deploying this elsewhere would have found a
disabled security control and a note telling them it did not matter — which, for them, is false.

**The fix, and why it is documentation rather than code.** The standing decision for this repository
is trusted-network-only, documented prominently rather than fixed, because adding authentication
would mean shipping untested security code. The same reasoning applies here: re-enabling the guard
would break the deployment the README recommends, and configuring allowed hosts properly is a feature
nobody has tested. So the trade is now stated where people look for it, the code comment says it is a
security trade rather than a detail and points at that section, and the private justification is gone.

**The check.** If the guard is disabled in the source, the README's security section must mention
rebinding. It is a link between a code fact and a documentation obligation, so the two cannot drift
apart silently: re-enable the guard and the check stops demanding the disclosure on its own.

---

## E-L1 — the verification claimed to prove more than it proved

**Severity:** high, and of a particular kind: everything else in this log is a defect in the
software, while this is a defect in the **evidence** the log itself rests on.

**What was wrong.** The perturbation runner ended a successful run with

> all 24 claims hold: every fix is load-bearing and **every check can fail**.

The second half is false. The runner pins the claims made in this log. Measured at the time: **24 of
the 178 checks** in the two suites. The other 154 are present and pass, and nobody has ever shown
that any of them is capable of failing — which is the exact state a check that cannot fail hides in,
and this repository has already shipped one of those (see B5).

The README repeated the claim in shorter form: *"proves the other two can FAIL"*.

**Why it matters more than its size suggests.** This sentence is the strongest evidence the project
offers about its own quality, and it is the one a reader will quote. An overclaim there devalues the
24 claims that *are* real.

**The fix.** The verdict now states its scope, and computes the fraction rather than carrying a
written-down number that would drift:

> all 24 claims in the fix log hold: each names a check that goes RED when its fix is undone.
> Scope: 24 of the 178 checks in the two suites are pinned this way. The rest are present but
> UNPROVEN — no one has shown they can fail.

Pinning all 178 is not the goal and is not practical. Saying which are pinned is.

**Three mistakes made while fixing it**, recorded because a log that keeps only the clean version is
worth less. The first version of the change used `io` and `re` without importing them, which would
have raised `NameError` in the runner's *success* path — the worst place for it — and was caught
before running. A suite run that failed on a port collision was briefly mistaken for the new check
going red; it was not, and the predicate was evaluated directly instead.

And the third is the one worth generalising. That direct evaluation — computing the check's condition
in a standalone script — **passed, while the check itself crashed where it sits**: it was placed above
the lines defining two of the variables it uses, and raised `UnboundLocalError` on its first real run.
Evaluating a predicate in isolation recomputes its inputs, so it proves the arithmetic and nothing
about whether the check executes. A check is only verified by running it where it lives.

---

## The perturbation runner — how these claims are checked

Every entry above ends with a line like "perturbing X turns the check red". That claim is only worth
as much as whoever read the output, and at least one such claim in this log was **false** when first
written: the perturbation was applied to the wrong place, the check passed, and the sentence was
written without reading the result (see B5).

So the claims are no longer prose. They live in `tests/perturbations.py` as data, and
`tests/test_perturbations.py` re-derives all of them:

```bash
python tests/test_perturbations.py            # every claim  (minutes)
python tests/test_perturbations.py --unit     # the fast ones (seconds)
python tests/test_perturbations.py A4 B2      # by id
```

For each: break the code exactly as described, run the suite, and require that the **named** check
goes red. Then restore, and verify the restore byte-for-byte before moving on — a run that left a
deliberately broken source behind would be worse than the thing it was proving.

**What it found on its first run, about this log.** Three claims did not hold, and none of them were
code defects — all three were errors in the claims themselves:

* two perturbations legitimately touch several call sites, and the runner refused them as ambiguous
  rather than guessing which one was meant;
* the nudge perturbation broke only the *callee's* message id while the check reads the **caller's**
  queue, so nothing fired. Done by hand, both had been replaced. The written claim had silently
  narrowed to something that proves less than it says.

**And one thing it found about itself.** A fourth claim was reported as "the check did not fail",
which reads as "this check is a tautology". It was not: the perturbation had produced `str + list`
through operator precedence, the module failed to import, and the suite never ran at all. The runner
now distinguishes *the check did not fire* from *the suite never ran*, because conflating them turns
a broken perturbation into a false accusation against a good test.

**What it does not do.** It proves a check can fail; it cannot prove the check tests the right thing.
Nothing here saves you from a well-perturbed test that asserts something irrelevant.
