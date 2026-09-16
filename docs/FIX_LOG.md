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

⚠ **Corrected by R-2.** That new check was GREEN while the guard did not hold. Its stranger never
read `/directory`, where both identities this guard accepts were published in full — so the check
modelled an attacker weaker than the one named in the severity line two paragraphs above, and the
fix below was defeated by the first URL the README hands out. The guard itself is sound; what was
wrong is everything that published the thing it checks. See R-2.

**The fix, in two parts.**

*Identity on re-attach.* A re-attach is allowed when the caller proves the identity the lane was
registered with — or when the lane carries no identity at all, in which case there is nothing to
prove. An anonymous lane cannot be protected; the code says so rather than pretending otherwise.

⚠ **Scope corrected by R-8.** This guards the RE-ATTACH path — a socket coming back to a lane that
still exists. It is not a general protection of the name. `register()` refuses only a **LIVE** lane,
so a name whose holder has gone STALE, GONE or UNREACHABLE can be taken by anyone, with no identity
and no waiting. That is deliberate — a lane which has lost its line cannot be rung to be told so —
but this entry read as though identity now guarded lanes generally, and it does not.

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

⚠ **Corrected by R-18.** "The two are distinguishable because a dropped feed leaves `feed_up` false"
is exactly why this was wrong: once **both** have happened, `feed_up` is false and the silence branch
takes over — and it read `gone_since` without asking why it was set. A lane the host had reported
absent read LIVE again the moment its socket dropped, for the whole window. The sentence above is
true now; it described one ordering and not the other.

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

⚠ **Narrowed by R-2.** "Raw" is no longer exact: `sid` and `session_id` are stripped from those rows
on the way to the observer, because they are what the re-attach guard checks. Everything else — the
message text, the call subjects and openings this section exists to warn about — is still there.

**The fix.** The security section now states the operator surface separately, lists exactly what it
exposes, and says plainly that there is no switch to turn it off.

⚠ **Corrected by R-6.** "Lists exactly what it exposes" was true of the three surfaces it looked at
and false as a claim about the boundary: **six** surfaces need no team name, not three, and two of
them are writes. The list was hand-written, so nothing made it follow the code; it is derived from
the source now.

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

⚠ **"Two" corrected by R-20.** Two were found and fixed; they were not all of them. The delivery
bookkeeping sitting immediately beside the ledger — two dictionaries keyed by message id — was only
ever written to, so a broker that had carried a million messages still held a million keys for
messages settled long ago. `R-20` closes that, and names the two structures that genuinely cannot be
bounded — which this entry should have done rather than counting to two and stopping.

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

⚠ **Corrected by R-7.** "A message longer than `TEXT_MAX` is refused" was true of two of the three
doors into the ledger. `operator_say` — reachable over HTTP with **no credential** — was capped by
nobody, and a 40,000-character row went in and was fanned out to both parties as part frames. The
check written with this entry tested one door. The sentence above is true now; it was not when it
was written.

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

⚠ **A consequence, added by R-8.** UNREACHABLE is not LIVE, so a lane registered this way is
replaceable by anyone for as long as it exists — which, having no feed, is never less than the idle
day. That is the documented path for a session whose harness cannot hold a socket, and it is the
least protected one. The replacement is not refused; it is **recorded**, with whether the caller
proved the lane's identity.

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
tree and, more usefully, the **history** — a push publishes all commits, not the working tree.

**🔴 A CORRECTION TO THIS ENTRY, and of the worst kind.** This paragraph originally declared the
commit history free of the private team names. That was false, and it was false when it was written.
Two commits carried them: the initial commit, in source text, and the second commit, in its own
message. A later cold review found them. Their hashes are deliberately not cited here — see the
resolution below, which removed those commits.

The check that produced the wrong answer was `git log -S`, which is **case-sensitive**, run against
the lower-case and mixed-case spellings. The names are in the history in CAPITALS, so it returned
zero, and the zero was read as absence. `git log --all -i --pickaxe-regex -S` finds every one of
them. Worse, the commit under examination at that moment announces the cause in its own title — a
*case-sensitive rename* having missed the identity — and that title was quoted in the same paragraph
that concluded the opposite.

What survives from the original claim: the second commit was cleaning the grammatical wreckage of the
rename rather than removing a name. What does not: anything about the history being free of them.

**HISTORY STATUS (checked by the suite): CLEAN** — no private name of any kind appears in any commit,
in either case, across every ref. This line is not decoration: a check reads it and compares it with
a live search of the history, and fails if the two disagree in *either* direction. It is here because
prose cannot carry that claim — the paragraphs around it describe the history's past in the past
tense, and a check reading prose cannot tell a description from an assertion about now.

⚠ **"team name" was the right word for one day only.** The first rewrite took the three project names
out of the commits and that is what this line used to claim. It was true and it was narrow: the
private deployment's **lane names**, and two phrases naming a machine and a network, were still in
the history — in the initial commit, and in the very commits that removed them from the working tree,
because deleting a name from a file puts it in that commit's diff. A second rewrite, on the same
instruction, removed those as well. The word now is *any*.

⚑ Since R-12 the names being searched for are **not in this repository**, so that comparison runs
only where the list is supplied — the maintainer's pre-publication gate. On a clone without it the
check prints `NOT RUN` rather than passing, because an unmeasured history reads clean and would have
made the check pass vacuously while guarding the most expensive claim in this log.

**RESOLVED, by rewriting the history on the repository owner's instruction.** Every commit was
rewritten so that the three names are replaced by the public vocabulary this repository already uses
for its teams, in blobs and in commit messages alike. Upper case only, which is all that was there; a
case-insensitive pass would have rewritten ordinary prose that merely matched.

Verified afterwards, not assumed: zero occurrences of any of the three, in either case, across every
ref; `refs/original` dropped and the objects pruned, so nothing unreachable still holds them; the
working tree byte-identical before and after, which it must be, since the current tree never contained
them; the commit count unchanged; both suites green.

**Every commit hash therefore changed.** Hashes cited in this log before the rewrite no longer
resolve, which is why the paragraph above names the commits by position instead. Deliberately NOT
rewritten: a private-range address that identifies nobody and was already a stand-in — the real broker
address, the network name and the machine names were never in the history at all — and the author
identities, which are the owner's to decide rather than a fix log's.

### The second rewrite, and why one was not enough

The pass above was scoped to the three project names. Four more strings were still in the commits: two
lane names of the private deployment, and two phrases naming a machine and a network. They were in the
initial commit, and — this is the part worth stating plainly — **in the commits that removed them from
the working tree.** Deleting a name from a file writes it into that commit's diff. Every fix in this
log that took a private name out of a file put that name into the history in the same motion.

They were also present in a second form. Until `R-12` the de-identification check held each name split
across two string literals so it could scan its own file, so every historical version of that file
carried the halves — and rejoining them is one line of `ast`, which is how `R-12` was reproduced in
the first place. A rewrite matching only the joined spelling would have left them all.

So the second pass replaced both forms: the joined name, and any adjacent pair of string literals
whose concatenation was a private name, substituted by the same split of the replacement so the
historical code still reads as code rather than as something scrubbed.

**Verified by content, not by diff.** `git log -S` finds commits where the *number* of occurrences
changed; a commit that merely carries a string, unchanged from its parent, is invisible to it — a
weaker question than the one that matters. Instead: a case-insensitive content search for all fifteen
names across all 47 commits, and the `R-12` reassembly attack run over **every `.py` blob in every
commit** — 115 distinct blobs — joining adjacent literals to see whether any join lands on a private
name. Both clean. The working tree's hash is byte-identical before and after, the commit count is
unchanged, `refs/original` is dropped and the objects pruned.

**What this costs a reader:** every hash changed again. Nothing in this log cites one.

*(The false phrase is deliberately paraphrased rather than quoted above: a check now asserts that this
log does not contain that assertion, and quoting it verbatim would trip that check. Same reason the
de-identification check's own comment carries no example.)*

**The fix.** The four comments keep their engineering fact and lose the pointer: *one lane held five
holders* rather than a named lane. Numbered `D-L1`, not a queue id, for the reason given in B-L1.

**The check.** No identifier from the private deployment may appear anywhere in the tree. Its needles
were assembled from string fragments so that the check could scan **its own file** — a denylist
written out literally reports itself, and then the only way to keep the suite green is to stop
scanning the files most likely to carry a leak.

⚠ **Superseded by R-12.** Splitting each name across two literals let the check scan its own file and
published the names anyway: a reader rejoins them in one line of `ast`, and thirteen of them were
sitting in this tree — including three that a history rewrite had just removed from every commit. The
list now comes from **outside the repository**; what ships is a check that carries no list at all.

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

## R-12 — the list of names that must not be published was published, in the check that hides them

**Severity:** high in this repository's own terms, whatever a general reader would grade it. The
names were the private deployment's lanes, its machine and its three teams — and the three team names
had been removed from every commit by a history rewrite the day before, while sitting in the working
tree the whole time. A name absent from the history and present in the tree is published.

**What was wrong.** D-L1's check scans every file for a list of private names. A list written out
literally matches itself, so each name was split across two string literals and rejoined at runtime.
The log said so plainly and never called it obfuscation — but it was still the whole list, in the
published tree, and rejoining it is one line of `ast`:

```
    reassembled from adjacent string literals in the published tree:
      tests/test_switchboard_e2e.py :322   <eight lane names>
      tests/test_switchboard_e2e.py :328   <two phrases naming a machine and a network>
      tests/test_switchboard_e2e.py :359   <the three team names>
      tests/perturbations.py        :145   <a lane name, inside a perturbation payload>
    13 strings, with no knowledge of any name beforehand.
```

**The test that should have caught it.** The de-identification check itself, and it was green. It
searches for each name as a contiguous string, and no name is contiguous anywhere — **including in
its own needle list.** It was built blind to exactly the encoding it uses. A check that cannot see
its own leak is not a weak check, it is the wrong check.

**The fix.** Two halves, because no single check can do both jobs.

*A structural check, which needs no list and therefore ships.* No file in this tree may assemble a
string out of literal fragments. It reads the syntax rather than the text, so it is about the
mechanism rather than about any particular name, and it cannot be defeated by choosing different
words. It is the check D-L1's could not be.

*A content scan, whose list comes from outside.* `TEAMLINE_DEID_LIST` names a file of strings that
must not appear. There is deliberately **no default path** — a default would name the machine it
points at. With no list the scan does not run and prints `NOT RUN`; it never reports a pass it did
not earn. The maintainer's pre-publication gate supplies the real list, which is where a list of
private names belongs.

**Attacking the fix found a real hole in it.** The same name was planted six ways. The structural
check caught `+`, `"".join`, `%` and an f-string smuggling a literal; the content scan caught the
name written whole. **Implicit concatenation — `"ghost-" "lane"` — got through both**: the parser
folds it into a single constant before anything runs, so the joined name exists in the program and
appears nowhere in the file's text, and there is no `+` for the structural check to find. It is also
the one idiom that happens by accident, whenever a long string is wrapped across two lines. The
content scan now reads the string constants **as the parser sees them** as well as the raw text —
the text still matters on its own, because comments are not constants, and a name in a comment is
what started all of this. All six idioms are now caught by one check or the other.

**What this does NOT claim.** It is not a defence against someone determined to hide a name: a string
can be built at runtime in ways no parser can enumerate, and anyone who can commit to this repository
can do so. It forbids the idioms that publish a name *by accident* or *by well-meant cleverness*,
which is what happened here, and it names the boundary rather than implying a proof.

**The checks.** *"no file in this tree assembles a string out of literal fragments"* — perturbed by
rewriting a two-element tuple as `("s" + "id", …)`, a change with **no behavioural effect at all**, so
the structural check is the only thing that can go red. And *"no string from the supplied
private-name list survives in the publish tree"*, which D-L1's and V-2's claims now pin: the runner
generates a one-off needle per run and hands it to the suite, because a needle written down in the
claims file would already be in the tree the scan walks — and a claim that fires whether or not the
perturbation was applied proves nothing.

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
that the broker sat on a private network with no other authentication, so the guard bought
nothing there. It also carried a
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

## V-1 — the command this README advertises fails on a fresh clone

**Severity:** high. It is the project's strongest evidence about itself, and it worked only for the
people who wrote it.

**What was wrong.** `python tests/test_perturbations.py` exits 1 on a fresh clone on Windows,
reporting that a claim "no longer describes the repository". The code it looks for is present. Five
of the runner's needles embed a newline; git checks out CRLF wherever `core.autocrlf` is true, which
is the Windows default; the needles cannot match. The runner is telling the truth about the file it
was handed.

**Why nothing caught it.** Every run happened in a working tree git had never re-checked out — files
written by editors as LF stayed LF. Measured: a fresh clone is **823 CRLF / 0 LF** for one module; the
development tree is **0 CRLF / 823 LF**. The defect is invisible from inside that tree by construction
and appears for every reader who clones. It was found by a check that runs the suites against a clone
rather than against the working copy, which is the only place it exists.

**The fix, and the alternative that lost.** `.gitattributes` with `* text=auto eol=lf`, so that every
checkout matches the tree the suite is developed and passes in.

The alternative was to make the perturbation matcher line-ending agnostic — normalise on compare,
preserve on write. It was rejected on a count: it fixes the five needles and leaves the cause, and the
same suite holds twelve further byte-sensitive reads that would meet the same mismatch. Its one real
advantage is independence from the reader's git honouring attributes; git applies them on clone and on
archive alike, so that advantage is small. `eol=lf` rather than `text=auto` alone, because `text=auto`
normalises what is *stored* while the working tree still comes out CRLF — which is the exact
configuration the defect needs.

**What this also closes.** The open-findings section recorded "no `.gitattributes`" as *latent, not
present*. That judgement was wrong: it was actively breaking the advertised command for every reader.

**The check.** A fresh clone is made, its checked-out line endings measured, and the advertised command
run inside it. It passes only if the clone is LF **and** that command exits 0 — neither of which can
be established from the working tree.

---

## R-2 — the guard A2 added was defeated by the directory that published its credential

**Severity:** high. A2 is this log's highest-severity code entry, and the protection it claims did not
hold against the attacker population its own entry names.

**What was wrong.** A2 closed a takeover by requiring a re-attaching client to PROVE the identity the
lane was registered with — its `sid` or its `session_id`. Every directory row carried both, and
`GET /directory` takes no credential; it is the first URL this README hands out after the start
command. The proof the guard demanded was published beside the name it protected.

Four doors, found in this order:

| door | what it hands a caller who presents nothing |
|---|---|
| `GET /directory` | the row, with `sid` and `session_id` in it |
| `ws?party=<team>&ext=<name>` | the same rows — every feed is sent the directory on connect |
| `ws?party=operator` | the last 200 **ledger** rows, and a `register` row carries the `sid` in full |
| the refusal text | `"<ext> is LIVE (session <id>, seen 3s ago)"` — ask for a name that is taken, be told who holds it |

The first three are one mechanism, `_entry`, the single row builder. The fourth is not, and it was
found by walking the fix rather than by the review: it turns a *refusal* into the credential, so a
probe that is turned away leaves with exactly what it needs to come back and succeed.

**How it was found.** A second adversarial review, reading this repository. Reproduced by attack
before anything was changed: the owner registers with a `sid`, its socket drops, the thief reads
`/directory` with no credential, copies the `sid`, connects with it, and the lane answers
`now='not-mine' holders=1`.

**The test that should have caught it.** One existed and it was green — *"a client with no matching
identity cannot take over a lane whose socket dropped"*, shipped with A2 itself. It passed because
its thief never read the directory: it presented no identity because it had gone looking for none.
**A check that models an attacker weaker than the one its own entry names is not evidence about that
attacker.** That thief now reconnoitres first. It reads all four doors, harvests whatever identity
they offer *by the field names `PROTOCOL.md` documents* — so it knows no value in advance — and
attacks with the loot. Against the unfixed code it takes the lane; against the fixed code it finds
nothing to carry and is refused, which is the original check unchanged.

**The fix, and the alternative that lost.** `_entry` publishes neither identity; the observer path
redacts both from ledger rows on the way out; the refusal gives the age and not the session.

The alternative was to publish `bound` — a boolean saying whether a lane holds an identity at all,
keeping the row informative without publishing the secret. It lost on a count: **nothing reads either
field off a row.** Not the operator page, the headless page probe, the CLI, or the feed client; the
only reader was one assertion in the suite, which now takes the binding from the ledger. A field with
no reader is not worth an API — and `bound=false` would have named, in one unauthenticated GET and
with **zero ledger entries**, exactly which lanes are anonymous and therefore seizable. Leaving it
out makes an attacker earn that list by connecting, which the ledger records.

**What this deliberately does NOT fix.** The ledger still records both identities. It is the audit
trail, and a record that omits who held a lane cannot answer the question it exists for. The
redaction is on the way OUT; the file on disk is unchanged, so anyone who can read the broker's
working directory can still read every identity — as they can read every message text, which is what
the trusted-network model in the README already says.

**The check.** *"no unauthenticated door hands out the identity the re-attach guard checks"* reads all
four doors and fails if any of them carries it, under any key or none: it searches the payload rather
than the fields this fix happens to know about, so a fifth door added later is caught by what it
emits and not by whoever remembered to redact it. Perturbing any one of the three changed statements
turns it red — claims `R-2-row`, `R-2-observer` and `R-2-refusal`.

---

## R-9 — the shipped source read as one household's incident diary

**Severity:** medium by a general measure, high by this repository's own: it is what a stranger reads
first, and none of it is explained anywhere here.

**What was wrong.** Comments dated to a day. Constants stamped with the wall-clock minute somebody
chose them. Decisions attributed to "the operator", a person this repository never introduces. And
words used as though the reader already knew them — a module that is not in this package, another
project's handover file, the name of a chat tab. Re-derived against the tree as it stands, because
the review that found it predates two other fixes and its list was partly stale: **58 sites across
ten files, and 5 check names.**

Check names are the sharp case. They are **printed by every run**, so the first evidence a reader
collects about this project announced `reply Q6` and `the DM tab` — a numbered answer in a
conversation nobody else was part of, and a tab in an application that is not this one.

**The test that should have caught it.** None existed, and nothing in the suite was of this class.
Which is why it survived a de-identification pass, a README audit and two cold reviews: every one of
those looked for *names*, and this is not a name. It is provenance — true, verifiable, and useless to
anyone who was not there.

**The fix.** Keep the engineering fact, drop the provenance. `# now-line cap: 120 truncated real
now-lines` says everything the date and the minute were standing in for, and says it to a reader who
cannot see either. Where a comment recorded a real measurement, the measurement stays and only the
day goes.

One of the sites was a different defect wearing the same clothes: the feed contract's docstring
described the two feed types **by team name** — the privilege `B1` removed from the code and left
standing in the prose beside it. It now names the two behaviours, and says the distinction comes from
how a feed registered rather than from what it is called.

**Attacking the check found its blind spot.** The first version scanned `teamline/` and `tests/`,
which is D-L1's mistake exactly — that scan used an extension allowlist and was quietly skipping the
Dockerfile, the LICENSE and a shipped `.js`. It now walks the whole tree, with `docs/` as the single
exclusion, and that exclusion is a real distinction rather than a convenience: a fix log **records**
when something was decided, which is the opposite of a comment that merely happens to be dated.

**What this does NOT claim.** It matches ISO dates and `HH:MM` clock times. "8 Sept", "half past
seven" and "last Tuesday" all pass it. It is a regression guard on the forms that were actually
there, not a proof that no provenance remains.

**The walk.** A change made almost entirely of comments has one hazard in each direction: an edit
meant for a comment that lands on code, and a renamed check that something still refers to by name.
Both were measured rather than assumed — every changed file's syntax tree was compared with its
version at `HEAD` with all string literals blanked, and every one was **identical**, so no statement,
operator or name moved; the one file whose tree differs is the one that gained this check. The check
census: 96 → 96 in the unit suite and 73 → 74 in the end-to-end, with five renamed and none lost, and
all 34 perturbation claims still name a check that exists.

**The checks.** Claim `R-9-source` puts a dated comment back into the package; `R-9-name` puts a clock
back into a check name, breaking the *unit* file while running the *end-to-end* suite, because that is
where the check lives and it reads the other file's names rather than its own output. Neither claim
can spell a date, because the claims file is itself inside the tree this check walks — written there,
it would make the check red for ever and both claims would fire with the perturbation doing nothing.
The runner formats `{DATE}` and `{CLOCK}` from the clock at run time, so no date and no wall-clock
time appears anywhere in this repository outside `docs/`.

---

## V-3 — the verification that guards the push read FAILED on every run, and was read past

**Severity:** high, and not because of what it found. **A gate whose verdict is FAILED every time
protects nothing**: the next real failure arrives indistinguishable from the standing one. This
stage had been failing since it was written, and the failure was being carried as "known".

**What was wrong.** One test fixture sent a Host header carrying an **RFC 1918 address**, to prove
the MCP endpoint serves a client whose Host is not loopback. A private-range address — it identifies nobody, and on that
reading the failure was harmless, which is precisely why it survived. But this repository was forked
out of a private deployment, so an address that *could* be somebody's real machine is a question a
reader has no way to answer. The gate was right to ask it, and the answer should have been given in
writing or the address changed — not neither.

**The test that should have caught it.** The gate caught it, on every single run. Nothing in the
shipped suite did, so a reader cloning this repository could not see the rule at all, and the only
thing standing between a real address and publication was somebody reading a line of output.

**The fix, and the shape that matters more than the fix.** The fixture uses `198.51.100.2`, from RFC
5737's documentation range, which is reserved so that it can never route to a real host. Any
non-loopback Host exercises the guard; a documentation address does it without raising a question.

The rule is now an **allowlist** — loopback, unspecified, or one of the three documentation ranges;
everything else fails. The denylist it replaces named two prefixes and would have passed the most
common private range of all straight through. That is D-L1's stated limitation arriving in a second place: *a denylist cannot catch what
nobody thought to list.* Walked with real values: it refuses every RFC 1918 range, the
carrier-grade NAT range and ordinary public addresses, and admits all three documentation ranges.

**What it does NOT cover, measured rather than guessed.** IPv4 only: an IPv6 address, or an internal
hostname like `broker.something.internal`, would pass. Neither appears anywhere in this tree — that
was checked, not assumed — and writing coverage for a class with zero instances is a structural
choice with no number behind it. A four-part version string would be flagged as an address; there is
none today, and a false positive that stops a publish fails in the right direction.

**The check.** Claim `V-3` puts a private-range address back into a shipped file. The Host-header
check beside it stays green either way, because both addresses are served — so the only thing that
can go red is the address rule, which is what makes the claim worth anything. The claim cannot spell
that address: this file is inside the tree the check walks, so the runner formats it at run time.

---

## R-6 — the security section's boundary was wrong, and it is the one claim a reader can test

**Severity:** medium by severity, high by position. It is the "read this before deploying" section,
and the sentence that was wrong is the single security claim in this repository that anyone can check
with `curl` in ten seconds.

**What was wrong.** The section said a team name is the price of entry: *"anyone who can reach the
port and knows a team name can register an extension, read the directory, call any session and read
call transcripts"* — and then listed three surfaces that need no team name, as the exception.

There are **six**, and two of them write. Measured against a running broker with no header of any
kind:

| surface | what a stranger got |
|---|---|
| `GET /directory` | every lane, its now-line, its state, its call id |
| `GET /healthz` | liveness, and the count of extensions and calls |
| `POST /hook/now` | **rewrote another lane's now-line** — it read `SET BY A STRANGER` afterwards |
| `POST /operator/say` | a line injected into an open call |
| `ws?party=operator` | the ledger, the directory and every open call's transcript |
| `sw_log(call_id)` | a call transcript, by id — the one MCP tool that never consults the team |

`/hook/now` is the one the section had never mentioned at all, and it is a write. It needs at least
twelve trailing characters of the target lane's session id, which `/directory` used to publish and no
longer does (`R-2`) — so the door is narrower than it was, and it is still a door.

**The test that should have caught it.** None. A3 shipped two checks about *particular* surfaces —
that `operator` is not a configured team, and that the snapshot carries ledger rows — and both still
pass. Neither says anything about the list being **complete**, and completeness was the claim. A
hand-written list of what a program exposes starts accurate and decays from the first commit
afterwards, because adding a route is one line and nothing makes the prose follow.

**The fix.** The section states the real boundary: what reaching the port alone is enough for, as a
table, with the two writes marked; then what a team name additionally buys; and that a team name is a
label the caller chooses, not a credential.

The check **derives** the list from the source — every route, and every `sw_` tool, whose
implementation never mentions `team_of` — and requires the section to name each one. An unknown
handler is assumed gated, so the check may accuse but never excuse.

**The walk found the mistake the walk exists to find, in the walk itself.** Comparing the derivation
against a live broker, three gated tools came back as "served" — because they had been called with no
arguments, MCP rejected the arguments before the tool body ran, and *absence of a refusal* was read
as success. That is this log's oldest recurring error in a new place. The walk supplies real
arguments now and reports "inconclusive" when a call never reaches the body, which is the honest
third answer. With that fixed, the derivation and the live server agree on every conclusive case.

**The checks.** Two, because this rots in both directions and only one of them is obvious. `R-6-code`
adds a route whose handler never consults `team_of` — one line, which is the shape this defect
actually takes — and the section cannot know about it. `R-6-doc` removes a surface from the section
while the code still serves it. A single-direction check would have expired the moment the section
was correct, which is exactly how `D-L2`'s predecessor stopped firing.

---

## R-10 — the shipped text pointed at three documents that are not here, and gave one instruction that fails

**Severity:** medium. Four of the five sites cost a reader ten minutes of looking for a file that was
never here. The fifth is worse: it is an instruction, in the first lines of a file a shell user
opens, and following it does not work.

**What was wrong.** Three references named two documents of the deployment this was forked from --
a protocol file and a session quickstart -- neither of which was ever part of this repository. They
are described rather than spelled here, because writing the name of a document that does not exist
is the defect: the check reads this file too, and it is right to. A fourth reference named
`docs/PROTOCOL.md` and quoted a section, *"why there is no standby holder"*, which that file did not
contain. And the CLI's docstring told the reader a session holds its line with a feed URL carrying a
team and **no extension** -- which the broker closes on sight, and which this suite already asserts
is refused.

The account of the lost review queue in this log says those pointers "did not travel". These are the
same pointers, shipped.

**The test that should have caught it.** None — and the class is worth naming. A reference is prose
that makes a **checkable** claim: every one of these could have been settled by opening a file.

**The fix.** The three dangling names are repointed at `docs/PROTOCOL.md`. The CLI docstring names
`teamline_feed.py` and the URL form that works, and says plainly that one with no extension is closed.

The quoted section is fixed the other way round: **the passage was written, not the pointer deleted.**
The suite argues the standby-holder hazard at length from two directions, and `PROTOCOL.md` — the
contract document — never mentioned it. §2 now carries it: a standby holder does not keep a lane's
messages, it takes them, because attaching to an absent lane goes through registration, and
registration releases held voicemail to whoever now holds the socket. The real session comes back to
an empty mailbox and a ledger saying everything arrived.

**That paragraph was walked before it was believed.** Fixing a dangling pointer by writing the passage
it pointed at means adding prose that asserts behaviour — the exact class of defect being fixed here.
So each sentence was put to the state machine with real values: a lane retired after silence;
voicemail to it accepted, held, delivered to nobody; a standby holder attaching and being handed that
message, the hold then empty and a `voicemail_released` row in the ledger; and the real session
returning to **zero** messages. The paragraph says what the code does.

**Attacking the check moved its boundary.** The URL rule first scanned only the package — which would
have left the README free to instruct the broken form. It now scans everything a reader might copy,
with `tests/` the single exclusion, because that is where the refused form is legitimately written out
in order to prove it is refused.

**What it does not cover.** Only `.md` references are resolved: a dangling pointer to a `.py` file
would pass. A quoted passage is matched as a substring, so a reference that paraphrases a heading
rather than quoting it is not checked.

**The checks.** `R-10-file` points a reference at a document that is not here. `R-10-passage` is the
harder half — it leaves the document in place and renames the section that two comments quote, so the
file resolves and the passage does not. `R-10-url` restores the instruction that fails when followed.

---

## R-11 — the ring contract stated a bound the code does not honour

**Severity:** medium, and mislocated by that word. It is the contract document, and the number in it
was wrong by up to **119 minutes** for the caller's own lane.

**What was wrong.** §4 said *"A ring waits 90 seconds for an answer and then frees the line."* Those
90 seconds are the callee's **idle** time: the clock advances only while the callee is not mid-turn,
so a callee whose harness keeps reporting it busy never advances it at all, and the ring is ended by
the same two-hour cap an open call gets — throughout which the caller's lane is occupied, because a
lane holds one call at a time.

Measured against the state machine, with both sides sending the keepalives a live session sends:

```
  after    90 s, the caller's line is  HELD   ring_wait=0.0
  after  3600 s, the caller's line is  HELD   ring_wait=0.0
  after  7150 s, the caller's line is  HELD   ring_wait=0.0
  after  7260 s, the caller's line is  free
  ledger reason: "unanswered for more than 2 h (the callee reported itself mid-turn throughout)"
```

The hold is deliberate and `A7` describes it. What was missing is that the contract file never did: a
reader sizing a client timeout had one number, and it was the wrong one.

**The test that should have caught it.** None. `A7`'s check asserts the *code* ends a held ring at the
cap, and it passes. Nothing ever compared the document with the constants, so the two were free to
disagree — and the disagreement was written down in this log while the contract file kept its old
sentence.

**The fix.** §4 leads with both numbers, says which one to size a timeout from, explains why the clock
stops rather than runs — a mid-turn callee cannot see the ring yet, and timing it out would discard a
call it was never given the chance to answer — and states plainly that **the caller cannot cancel its
own ring**.

**The walk tested the sentences, not the numbers.** The reproduction had settled the two bounds; the
new paragraph asserted three further things, and asserting those unchecked would be this very defect.
Put to the state machine: a second call from the same lane while ringing is refused — *"already holds
call …"*; the caller's own hangup is refused — *"is RINGING, not IN_CALL"*, so the no-cancel sentence
is true, which was the one worth doubting; and the ring ends at 7225 s with the ledger naming the cap.

**The first reproduction was wrong, in a way worth recording.** Without keepalives the callee went
GONE after 90 seconds of feed silence and the ring ended — at almost exactly the documented time, for
an entirely unrelated reason. It read as a refutation of the finding. Both lanes are kept alive now,
which is what two live sessions do.

**What the check does not do.** It requires both numbers to appear in the sentences that mention the
ring. It cannot verify the explanation around them: a document could state both and explain neither.
Requiring both is what forces an author to say why there are two — a check on the wording would only
be a check on this author's phrasing.

**The checks.** `R-11-doc` restores the old single-number sentence. `R-11-code` moves the enforced cap
without touching the document, which is the direction that actually happens: a constant is tuned and
the contract file is never reopened.

---

## R-14 — the keepalive rule did not apply to the feed this README tells you to run

**Severity:** low in consequence, awkward in position: it is a rule in the contract file, addressed to
every client, and it is false for the client this repository ships.

**What was wrong.** §2 said *"send `{"ping": 1}` at least every 25 seconds (90 seconds of silence marks
the lane GONE)."* The silence rule is conditional on the lane carrying a `session_id`. The feed the
README recommends — `teamline_feed.py --sid` — carries none, so for it the stated consequence never
happens. Two lanes, identical but for the identity they registered with, after 420 s of silence:

```
  alpha/sid-only      (--sid, no session_id)   LIVE    last_seen 420 s ago
  beta/with-session   (session_id)             GONE    last_seen 420 s ago
```

And the consequence a reader was actually owed is not in the document at all: `last_seen` goes stale
either way, and a stale `last_seen` is what decides whether a **second holder may take the lane**. So
the advice is right — ping — and every reason given for it was wrong.

**The test that should have caught it.** None, and this one is sharper than usual: the behaviour was
*relied on inside the suite* and never asserted. A case in the unit suite registers without a session
id precisely so the silence rule cannot end its call underneath it, with a comment saying so. The
suite knew; the contract file did not; nothing connected them.

**The fix.** §2 now says which feed each half of the rule applies to, and says to ping regardless,
with the reason that actually applies to a `sid` feed. Three checks in the unit suite pin the
behaviour itself, which nothing had.

**Two things the runner caught that I would not have.**

*A perturbation that looked silent and was not.* The first claim made the silence rule unconditional.
It reported `SILENT` — "the check did not fail" — which reads as *this check cannot fail*, the most
expensive wrong conclusion this runner can draw. In fact the perturbation stopped the suite before
those checks ran at all. **The runner now distinguishes the two**: it asks whether the named check
appears in the output at all, and reports `NORUN — the suite stopped before this check ran, so the
claim is UNTESTED, not silent`. That distinction did not exist while every perturbation happened to
be narrow enough.

*A perturbation that removed the wrong sentence.* The doc claim first replaced one line of a six-line
bullet. The rest still named both feed types, the check stayed green, and it reported `SILENT` again.
A perturbation has to remove the thing the check looks for, not the sentence its author thinks is the
important one.

**The checks.** `R-14-doc` restores the original bullet whole. The three behavioural checks are pins
rather than claims: there is no fix to undo for them, because the code was always right — it was the
document that was wrong.

---

## R-24 — the configuration section contradicted its own table, and misattributed a flag

**Severity:** low, and it is the section a reader follows while deciding what to set where.

**What was wrong.** Two sentences. *"The broker reads the first four, a client reads the last two"* —
of a table with **seven** rows, five of which the broker reads: `TEAMLINE_PAGE` is the fifth, and the
broker is what serves the page. And `` `--party` overrides it ``, written in the row about the
**client's** team, where the CLI has no such flag: only the feed client takes it.

**The test that should have caught it.** `C-L1` established that this README counts itself and that
nothing checked the counts — and then checked the counts it happened to think of: files, suites, line
counts. This is the same defect one row down, in a sentence that also counts.

**The fix.** Five, not four. And the flag is attributed to the script that has it, with a note that
the other reads the variable only.

**The check derives the split from `os.environ` calls, not from the variable name.** A first version
matched the name anywhere in the file and mis-attributed one immediately: the feed client contains the
string `TEAMLINE_PORT` inside an error message whose whole purpose is to tell you it is the *broker's*
knob. Matching names would have turned a correct sentence into a false failure.

It also went looking for a sentence that was not there. The prose wraps across two lines, and the
first regex assumed single spaces — so it found nothing and reported *(None, None)*, which is "the
prose is missing" rather than "the prose is wrong". Same verdict, different reason, and the
difference is the whole value of the check. It reads whitespace-normalised text now.

**The walk did not re-derive; it measured.** Confirming this fix with the same regex would only prove
the regex agrees with itself. Instead `TEAMLINE_PAGE` was pointed at a file written for the purpose
and `GET /` served it — so the broker reads the fifth row — and each client was asked for its own
`--help`, which is how the flag was attributed rather than by grepping for a string.

**The checks.** `R-24-doc` restores the miscount. `R-24-code` stops the broker reading the fifth
variable, leaving the prose right about a document that no longer describes the code — the direction
that actually happens, where a variable is moved and the configuration section is never reopened.

---

## R-25 — retirement was timed from the wrong event, and a replaceable state was missing

**Severity:** low, and both halves are the kind of thing a reader checks rather than assumes.

**What was wrong.** Two claims.

*The clock.* Both the README and `PROTOCOL` said a lane is retired after **"GONE for 10 minutes"**.
`gone_since` is stamped when the feed **drops**, and the sweep retires at `gone_since +
GONE_RETIRE_S` — so it is ten minutes after the drop, which is eight and a half minutes after the
lane reads GONE. Measured on the clock:

```
  the feed dropped at            t+0 s
  the lane first reads GONE at   t+90 s
  it is retired at               t+600 s      (GONE_RETIRE_S, counted from the DROP)
  the gap between the two        8.5 minutes, not 10
```

The operator page had it right all along, which is the tell worth recording: three descriptions of
one constant, and the two in the documents agreed **with each other** rather than with the code.

*The states.* `PROTOCOL` said re-registering replaces "a GONE or STALE holder". `register()` refuses
only a **LIVE** one, so every other hygiene value is replaceable — including `UNREACHABLE`, which is
what a lane reads when it registered and holds no feed, and which the same document lists in its own
hygiene table two lines above.

**The test that should have caught it.** None. Both are constants-versus-prose, and the suite checked
neither; `C-L1` had established the principle for counts and stopped at the counts it thought of.

**The fix.** Both documents time retirement from the drop and say what the gap from GONE actually is.
The replace rule is stated as the code states it — *anything that is not LIVE* — and names all three,
with a note that `UNREACHABLE` is the easiest to reach by accident.

**The check computes what each phrasing implies, rather than looking for words.** "GONE for N minutes"
implies the silence window plus N; "N minutes after the feed drops" implies N. Either is compared with
the constant, so a document may say it in whatever words it likes and still be wrong only if it is
wrong. Its first version read the window only *backwards* from the number and mis-read a row that
names GONE earlier in the same cell for an unrelated reason — it reads both sides now.

**The walk disproved a correction, and the walk was what was wrong.** Testing the `UNREACHABLE` half
against a state machine built with defaults showed a feedless lane reading `LIVE` and the
re-registration refused — an apparently clean refutation. `UNREACHABLE` exists only when
`require_feed` is set, and the shipped broker sets it. The walk now constructs the state machine the
way the broker does, which is the only construction whose behaviour anyone is entitled to describe.
Third time today that a reproduction, not a finding, was the thing at fault.

**The checks.** `R-25-timing` restores the clock started from the wrong event. `R-25-states` drops
`UNREACHABLE` back out of the replaceable list.

---

## R-26 — the HTTP table was missing a route, and demonstrated a knob nothing can set

**Severity:** low, and the missing route is the one a reader would most want in that table.

**What was wrong.** §7 listed six routes; the broker serves seven. The one absent was
`POST /operator/say` — **a write, reachable with no credential**, which injects a line into any open
call. Everything else in that table is a read.

And §5 showed `Switchboard(..., cap_into={"beta": 6})` the way configuration is shown. Nothing in the
shipped program passes it: not `build()`, not `wire()`, and no environment variable — of the seven
`TEAMLINE_*` variables the package reads, none concerns a cap. So the cap could not be set at all
without editing the source, and the document did not say so.

**The test that should have caught it.** None. `R-6` had just taught the neighbouring lesson — a
hand-written list of what a program exposes decays from the first commit after it is written — and
this is the same list, one document over.

**The fix, and the design that lost.** The route is in the table, marked as a write needing no team
name, as is `/hook/now` beside it.

For the cap there were two ways. Wire it to an environment variable — `TEAMLINE_CAP_INTO=beta:6` —
or say plainly that it is a source edit. **Wiring it loses on a count that is zero:** no deployment
has asked for a cap, so a new variable buys nothing measurable, and it adds a configuration row that
then has to be kept true — which is precisely the class of defect this entry and `R-24` are both
about. The document says it is a source edit, and says why it is not a variable.

**The walk was wrong twice before it was right, both times in the same way.** First it read the HTTP
status: `operator_say` answers **200 with `{"ok": false}`** when it refuses, so the status code says
nothing. Then, reading the body, it showed `{"ok": false, "error": "pick a call and type a line"}` —
because its two lanes had been registered *without feeds*, which under `require_feed` leaves them
UNREACHABLE, turns the call into voicemail, and leaves no call id to aim at. With real feeds held the
route answers `{"ok": true}` and the line is in the transcript.

That is the fourth time today a probe of mine judged a wrapper instead of a payload — an MCP error
returned as content, a tool-argument rejection, a 200 with a false body. **A transport that succeeded
is not an operation that succeeded**, and this repository's own checks now say so in four places.

**The checks.** `R-26-route` removes the route from the table again. `R-26-cap` restores the
implication that the knob can be configured.

---

## R-7 — the message cap had three doors and guarded two

**Severity:** medium. Reachable with no credential, and it undid `A8` on the ledger and on delivery
at the same time.

**What was wrong.** `say` and `leave` call `_check_text`. `operator_say` did not, and it is the third
way text enters the ledger — reached by `POST /operator/say`, which takes no team name. Reproduced
side by side on the same message:

```
  say          : refused -> message is 40000 chars; the limit is 4000
  operator_say : ACCEPTED
  longest say row in the ledger: 40000 chars;  TEXT_MAX = 4000
```

And that row does not simply sit there: it is fanned out to **both** parties as `len/300` part
frames, so an unbounded row is unbounded delivery as well.

**The test that should have caught it.** It existed, it was green, and it is the one `A8` shipped: *"an
over-long message is refused rather than silently truncated"*. It exercised `leave`. One door of
three, and the entry it belonged to stated the rule as though it held everywhere.

**The fix, in two parts, because one of them only fixes today.** `operator_say` calls `_check_text`
first, and the behavioural check now tries **every** door with the same message rather than the one
it was written against. That closes the defect.

The second part closes the *class*: a structural check that reads the source and requires every
`_commit` of a `text=` to bound it — by refusing over the cap, or by truncating with a slice. A
fourth door cannot arrive the way the third did.

**And it accused an innocent method first.** Asked whether the *method* mentioned a cap anywhere, it
flagged `answer`, which bounds its receipt inline as `(receipt or "")[:300]`. Bounded is bounded; a
check that recognises only one spelling of it reports a defect that is not there, and a false
accusation costs exactly the trust a missed one does. It reads the argument now, not the function.

**The walk went over the wire, not through the object.** The added line raises, and the HTTP route
catches `SwitchError` and answers `{"ok": false}` — so a raise in the wrong place would have become a
500 rather than a refusal. Over a real socket with no credential: the over-long POST answers *"message
is 4001 chars; the limit is 4000"*, the longest text anywhere in the ledger afterwards is **27
characters**, and a legal line still lands. A cap that breaks the route would not be a fix.

**What this entry does NOT close, stated rather than folded in.** The same review item notes a second
mechanism: any feed holder can append a `touch` row per unrecognised frame, at line rate. `A8` bounded
row **size**; nothing bounds row **rate**. That is a different defect with a different fix and it is
in the open list below, not quietly inside this one.

**The checks.** `R-7-cap` removes the guard and the behavioural check goes red. `R-7-structural` is
the same removal pinning the other check, because a `_commit` of unbounded text is the shape a new
writing path takes when nobody remembers the rule.

---

## R-8 — a name nobody holds can be taken by anyone, and the record could not tell you who

**Severity:** medium, and the interesting part is which half turned out to be the defect.

**What was found.** `A2` protected the re-attach path by requiring proof of the lane's identity.
`register()` refuses only a **LIVE** lane. An extension registered through `sw_register` holds no
feed, so it reads UNREACHABLE — which `B1` made the documented path for a session whose harness
cannot hold a socket — and UNREACHABLE is not LIVE. Reproduced: a stranger with no identity of the
lane's took the name at once, and the **held voicemail went with it**, released to the stranger and
marked delivered in the ledger.

**Two fixes were tried and rejected, and that is most of this entry.**

*Requiring identity to take the name.* Rejected: a lane that has lost its line cannot be rung to be
told so, and a session coming back would be locked out of its own name.

*Withholding the voicemail unless the caller proves the identity.* This was written, and the suite
stayed green, and it was still wrong — which the walk caught and the suite did not. A lane registered
by session id alone never gets a `gone_since`, so it lives for the full idle day; a session returning
inside that day **with a new session id** — the ordinary case here, since every session has a new one
— would have been refused its own messages. `PROTOCOL` §4 already promises the opposite in as many
words: *"whoever registers that lane receives them."* The suite passed only because its own scenario
waited long enough for the lane to be retired, which is exactly the case the rule does not bite.

**What shipped instead.** The answer this system gives everywhere else: it is not authenticated, it
is **logged**. The replacement row now records whether the caller proved the identity the lane was
registered with, so *"the session came back"* and *"somebody else took the name"* are different rows
rather than the same row. Nothing is refused, nothing is withheld, and the operator page shows the
difference.

**A third symptom did not reproduce, and the reason is worth having.** The finding notes that
`register()` is the one retire path that does not end the lane's calls — `unregister`,
`operator_retire` and the silence sweep all do. The asymmetry is real; the state it would matter in
cannot be reached. **Taking part in a call touches the lane**, so a lane in an open call is LIVE, and
a LIVE lane is refused. Going non-LIVE takes the two hours of silence by which the call has hit its
own two-hour cap. A change was written, and then removed along with its check: the perturbation
runner reported the check `SILENT`, which is what a check that cannot fail looks like. The argument
is in the code where the call would have gone.

**What this corrects elsewhere.** `A2`'s entry read as though identity now guarded lanes generally;
its scope is the re-attach path and it says so. `B1`'s entry did not mention that the path it
documents is the least protected one; it does now.

**The check.** `R-8-record` removes the identity marker from the replacement row. The check goes red,
because on a board with no authentication an unrecorded takeover is indistinguishable from a session
coming home.

---

## R-18 — a second failure turned the host's evidence back into silence

**Severity:** low in reach, and worth the entry for what it says about the rule it broke.

**What was wrong.** `A4` drew the distinction this rests on: a host reporting a session absent is
**evidence** and is GONE at once; a quiet socket is only **silence** and gets a grace period. Its
entry then explained why the two could be told apart — *"a dropped feed leaves `feed_up` false whereas
a host-reported absence does not"* — which is precisely the sentence that was wrong. Once **both**
have happened, `feed_up` is false, the silence branch takes over, and it read `gone_since` without
asking why it had been set:

```
  registered, holding a feed          LIVE
  the host reports it absent          GONE     gone_reason='host'
  10 s later its socket drops         LIVE     gone_reason='host' -- still the host's
  at drop + 30 s                      LIVE
  at drop + 89 s                      GONE
```

Not cosmetic: a lane reading LIVE is a lane whose name cannot be reclaimed, and one whose holder the
host has already said is not there.

**The test that should have caught it.** It exists, it passes, and it tests the other ordering: *"a
ping cannot withdraw the HOST's evidence, whichever failure came first"* — which walks a frame
**arriving**. The other way a lane's state is recomputed is its socket **going down**, and nothing
tested that. "Whichever failure came first" was true of the failure the check had in mind.

**The fix.** Two lines: in the silence branch, say GONE at once when the lane was marked gone by the
host. Evidence does not become silence because a second thing failed afterwards.

**The walk ran three orderings, because a branch added to a state function is exactly the change that
fixes one and breaks another.** Host-then-drop is GONE throughout. Host-then-ping is still GONE, which
is `A4`'s case. And a **plain** drop still reads LIVE at 1 s, 30 s and 89 s and GONE at 95 s — that
one was the risk worth naming: if the new branch were reachable by ordinary silence it would collapse
the grace period `A2` added, and a lane that goes GONE at once is a lane anyone may re-attach to,
which is the takeover `A2` exists to prevent. A fix for a flicker would have opened the hole the
neighbouring entry closed.

**The check.** `R-18` removes the two lines and the new check goes red, while `A4`'s own two claims go
on firing — the ordering it guards was never the problem.

---

## R-20 — "two separate leaks" was a count, not a survey

**Severity:** low, and the entry is mostly about the word *two*.

**What was wrong.** `A8` found two unbounded structures, fixed them, and opened with *"two separate
leaks"*. Two were found; they were not all of them. Sitting immediately beside the ledger are two
dictionaries keyed by message id — the delivery bookkeeping: when each message was last pushed, and
when it may be retried. Both were written in three places and **removed in none**. A broker that had
carried a million messages still held a million keys for messages settled long ago, and the sweep that
runs every second walked all of them.

**The test that should have caught it.** `A8-rows` asserts the in-memory ledger is bounded, and it
passes — it is about `_rows`. Nothing looked at the two dictionaries next to it. A check written to a
finding covers the finding, and a count of leaks is not a survey of them.

**The fix, and where it is placed.** The prune happens in the re-send sweep, against the **outbox**,
rather than at each point where a message settles. The outbox is the authority on what is still owed,
so it cannot drift out of step with a settlement path somebody adds later — a settlement-point fix
would be correct today and quietly wrong the first time a new one appears.

**What genuinely cannot be bounded, named rather than counted.** Two structures grow with the file and
must:

* `_delivered` — the set of message ids already handed over. Forgetting one means delivering it twice,
  which is the defect `A1`, `A5` and `A6` all exist to prevent.
* `_calls` — ended calls stay in memory because `sw_log(call_id)` reads them from there. Dropping them
  would bound the memory and break transcript retrieval for every call that has finished.

Both are the working set growing with the ledger, which `A8` conceded in general terms. They are
listed here so the concession names something.

**It is measurable from outside now.** `/healthz` reports `tracked` beside `pending`, so "it does not
grow without bound" is a number an operator can read rather than an assertion about a closure nobody
can see — and it is what the check reads.

**The walk covered the hazard, not the win.** A prune keyed by message id has one obvious benefit and
one obvious way to be wrong: dropping an entry for a message still **owed** discards its backoff, and
the next sweep re-pushes at once. That converts a retry into hammering and would look like an
improvement on any memory graph. Measured on a running broker: 40 messages sent and settled leave
`tracked` at **0** where it would have been 40; one message left owed and never acked keeps its two
entries with `pending` at 1; and that unacked message was re-pushed **once** in two and a half
seconds, not dozens of times.

**The check.** `R-20` removes the prune, and the bookkeeping count stops falling back to what is
actually owed.

---

## C-L1c — a claim can go stale, and only the slowest thing here noticed

**Severity:** low for the software and high for the cost of working on it.

**What was wrong.** A perturbation claim names the exact text it edits. `C-L1b` pointed at the
README's *"roughly N lines"* sentence — and that sentence gets **corrected whenever the suites grow**,
which happened twice in one day as checks were added. Each time, the claim's target text stopped
existing.

The runner handles that correctly: it reports `STALE`, meaning *the fix was changed or removed and
this claim no longer describes the repository*. But it finds out by re-running a whole suite per
claim, which at 55 claims is **about half an hour**. So a one-character problem was detected twice, on
each occasion by the most expensive instrument available, after the work that caused it was long
finished.

**The test that should have caught it.** Half of it existed. A cross-check already asserts that every
claim names a **check** that exists — added after a rename orphaned one. Nothing asserted the other
half: that the **text it edits** is still there. Both are the same question — *does this claim still
apply?* — and only one of them was being asked.

**The fix, in two parts.** The check now asks both, and it runs inside the end-to-end suite: every
claim's `find` must occur in the file it names, exactly once unless it declares otherwise. That turns
half an hour into a second, and any run of the suite answers it.

And `C-L1b` itself no longer carries the figures. It is pinned to the sentence, with the runner
substituting a count far enough out to fail the band — because a claim about self-counting prose
cannot hold the numbers without going stale every time the thing it counts changes size.

**The claim that pins it took three attempts, and the reason is worth keeping.** A claim that edits
the claims file necessarily *contains* the text it edits, so it matches twice and the runner refuses
it as `AMBIG` — correctly, since a perturbation must be exact. Pointing it at a different claim's line
did not help: the same thing happened again. It declares `all_occurrences` now, which replaces its own
payload along with its target, and the file still parses.

**The checks.** `C-L1c` makes another claim's target text vanish; the new check goes red. `C-L1b` fires
on the substituted counts.

---

## R-3 — the suite's headline property was asserted against an empty board

**Severity:** high. This is the claim the README makes for the whole design — *the ledger is the
source of truth; restart the broker and it rebuilds* — and it was being proved by comparing nothing
with nothing.

**What was wrong.** By the time the replay section runs, everything registered earlier has been
retired: a 24-hour tick, an operator retire, and an 11-minute GONE sweep between them empty the
directory. So

```
  {e["ext"] for e in sb2.directory()} == {e["ext"] for e in sb.directory()}    ->  set() == set()
  all(e["hygiene"] == "GONE" for e in sb2.directory() if e["team"] == "alpha") ->  all([])
```

Both passed. Neither could have failed.

**The test that should have caught it.** It is the test. There is no outer check that a fixture is
non-empty, and a vacuous assertion looks exactly like a passing one in the output.

**The fix.** The section builds its own state — lanes on two teams, an open call, an undelivered
message — and asserts **that the fixture is not empty** before using it. Then the replay comparison
means something, and two further properties are checked that nothing checked before: the rebuilt call
is the same call in the same state with its subject, and a message still owed is still owed after the
restart.

**Two things the newly-meaningful checks then taught.**

*A replayed lane reads LIVE at the instant of restart.* Asserting "a socket that is gone is gone"
immediately after replay fails — not because presence was rebuilt, but because the silence window
starts again with the new process: nothing has been silent yet from its point of view. It reads GONE
100 seconds later. Measured before the check was changed, because the alternative was to weaken an
assertion to fit a result.

*Voicemail to a reachable lane is delivered, not held.* The first version asserted held voicemail
survives a restart; the recipient was reachable, so there was none. What survives — and is now
checked — is the message still owed in the outbox.

**A check should FAIL when its fixture is missing, not crash.** The first perturbation deleted the
lines that build the state, and the call below them raised: the suite stopped before the check ran
and the runner reported `NORUN` — untested, which is not red. The perturbation now ages the board past
idle retirement, which empties it exactly as it was originally empty while every later line still
runs. The checks read the board through calls that return nothing for a missing lane rather than
raising, so they go red rather than taking the suite down.

**The check.** `R-3` empties the replay fixture; the guard goes red.

---

## R-4 — a check that named the broker's behaviour proved that `os.makedirs` works

**Severity:** high within the tests group, for the same reason as `R-3`: it could not fail, and it
claimed something it never tested.

**What was wrong.** Four lines:

```
  _probe_root = os.path.join(tmp, "made", "on", "demand")
  os.makedirs(_probe_root, exist_ok=True)
  ck("...and a nested root that does not yet exist is created rather than crashing the broker",
     os.path.isdir(_probe_root), _probe_root)
```

The test creates the directory and then asserts the directory exists. `B.build()` is never called.
The name promises the broker handles a root that is not there; the code proves the standard library
does — and it would have passed with the broker deleted.

**The test that should have caught it.** None, and the shape is the one this log keeps returning to:
a check whose name and whose body are about different things reads as coverage in every summary.

**The fix.** It builds a broker at a nested path that does not exist, and asserts the path **and** the
transcript directory beneath it appear afterwards — plus that neither existed beforehand, so the
assertion cannot be satisfied by a leftover from an earlier run.

**The check.** `R-4` stops the broker creating the transcript directory. The check goes red — where
against the old version it would have changed nothing whatsoever, which is the tidiest possible
demonstration of what was wrong with it.

---

## R-5 — the ordinary restart was refused as permanent, with a diagnosis naming the wrong cause

**Severity:** high, and the only one of the three high findings a user would ever meet. It strands a
lane with no retry and tells the operator something untrue about why.

**What was wrong.** Every session here has a new identity. So the ordinary case — a socket drops, the
harness restarts the session, it reconnects a few seconds later — comes back to its **own** lane with
a *different* `sid`. Inside the silence window that lane still reads LIVE, so `register()` refuses it.

The refusal is correct: `A2` requires proof to take a lane that is not yet presumed dead. **How it was
delivered was not.** Close code **4001**, which this contract documents as permanent and which the
shipped client treats as fatal:

```
  the broker REFUSED this feed and retrying cannot help: either your TEAM is not enabled on the
  broker, or no --ext was given. STOPPING rather than looping.
```

Neither half is true. The condition is the clock and clears in at most `feed_gone_s`; the cause named
is configuration, which has nothing to do with it. The lane becomes reclaimable a minute later — and
the process that wanted it has exited. The README tells a session to run that client under a
persistent monitor and rely on it.

**The test that should have caught it.** `A2`'s checks cover the two halves it was written for: a
stranger is refused, and the rightful holder reconnects **with its own sid**. Nobody reconnects with
its own sid here, because nobody has the same sid twice. The case the system is actually built around
was the case not tested.

**The fix.** The broker asks the state machine whether the refusal is permanent or the clock: if the
lane exists and reads LIVE, the refusal expires by itself, so it is sent as **4003** — the retryable
code that already existed for the second-holder case — with `retryable: true`, a retry interval, and
a message that says what is actually happening. 4001 keeps its meaning for the refusals that really
are permanent.

Deliberately *not* fixed by loosening `register()`: a lane inside its window is still protected, and
weakening that is the takeover `A2` closed.

**The walk ran the shipped client, not the broker's reply.** A close code is not the property anyone
cares about — recovery is. So `teamline_feed.py` was started as a subprocess exactly as the README
says to run it, against a lane whose socket had just dropped: it was refused, it printed
`holder_busy` and waited, and it **registered 30 seconds later with no human involved**. Under the
old behaviour it would have exited 3 immediately.

**And the walk's first version disproved the fix by breaking the broker.** Reading the client's output
with a blocking `readline()` starved the event loop the server runs on, so nothing answered and the
client reported `feed_down`. Read asynchronously, the recovery is there. That is the fifth time today
a verification of mine, rather than the thing verified, was at fault.

**The check.** `R-5` restores the fatal code for a refusal that expires by itself.

---

## R-13 — one unclean shutdown and the broker never started again

**Severity:** medium by likelihood and total in effect: the broker does not start, and the ledger is
its only memory.

**What was wrong.** `_commit` appends a row with one buffered write and no `fsync`, so a crash partway
through leaves a half-written final line — a power cut, a killed container, an unclean stop. `_replay`
read every line with an unguarded `json.loads`, so that line raised inside `__init__`, so `build()`
raised, so the broker did not come up. The deployment this README recommends sets `restart:
unless-stopped`, which turns one bad shutdown into a container loop and a traceback naming a line
number in a JSONL file that is, apart from a few bytes, completely intact.

**The test that should have caught it.** None. `A8` discussed the ledger's *size* and nothing
discussed its *integrity*, so the file's happy path was the only path anyone had walked.

**The fix, and the distinction it rests on.** A torn tail is not corruption, and the difference is
exact rather than a judgement: **a completed row always ends in a newline, because that is how it was
written.** So an unreadable line that is both the last one *and* unterminated is an append that did
not finish — the broker starts, drops the unfinished bytes, and commits a `ledger_truncated` row so
the gap is in the history rather than only in somebody's memory.

An unreadable row **anywhere else** is a completed row that has been damaged, and the broker refuses
to start, naming the file and the line. Skipping it would rebuild the board with a hole and say
nothing, while every later row describes a world that includes the one that was skipped. That is the
decision this program will not make for you, and `PROTOCOL` §6 now says so.

**The truncation is not optional.** Leaving the unfinished bytes in place would put the next append
*behind* them — and on the following restart that partial line is no longer last, so it is corruption
by our own hand, and the broker refuses. Tolerating a torn tail without removing it would have
converted a recoverable state into a permanent one.

**Two checks of mine were wrong before the code was.** The first asserted the file was byte-identical
to the original afterwards — but dropping the bytes is itself recorded, so it is deliberately longer.
The second looked for `"ts": 17` as the torn line's fingerprint, and **every real row carries it**,
because epoch timestamps begin with 17. A sentinel that matches everything proves nothing. The check
asks the real question now: does every line in the file parse.

**The checks.** `R-13-tail` makes an interrupted append fatal again. `R-13-middle` skips a damaged
completed row instead of refusing.

---

## R-15 — "refused" was asserted by catching anything at all

**Severity:** low, and it is the same disease as `R-3` and `R-4` in a smaller place: a check that
passes for reasons other than the one it names.

**What was wrong.**

```
  try:
      async with websockets.connect(<a feed URL naming a team and no extension>) as w1:
          await asyncio.wait_for(w1.recv(), 2)
      ck("a feed without an extension name is refused", False, "accepted")
  except Exception:
      ck("a feed without an extension name is refused", True)
```

(The URL is described rather than written: `R-10` forbids a copyable feed URL that names a team
without an extension, and this file is one of the ones it reads. That check caught this entry.)

`except Exception` covers the outcome that matters. A broker that **accepted** the socket and then
said nothing would time out after two seconds, the timeout would raise, and the check would report
the connection as refused — the exact failure it exists to catch, recorded as a pass.

**The fix.** The refusal has a documented form — an HTTP **403** at the handshake, in `PROTOCOL` §2 —
and that is what is asserted now: refused *before* acceptance, carrying the status the contract names.
A timeout no longer looks like anything but a timeout, because it carries neither a status nor a close
code and the check says so in its own failure detail.

**The check.** `R-15` makes the broker accept the socket and go quiet. The old check passed on that.
The new one fails, which is the whole difference between the two.

---

## R-16 — the check that enforces `requirements.txt` kept its own copy of `requirements.txt`

**Severity:** low, and it had drifted in **both** directions, which is the argument against copies.

**What was wrong.** The "no hidden dependencies" check compared the package's imports against a set
written in the test:

```
  declared = {"mcp", "starlette", "uvicorn", "websockets", "httpx2", "anyio"}
```

`requirements.txt` listed four of those. `anyio` was allowed and is imported by nothing. `httpx2` was
allowed and **is imported directly by `teamline_cli.py` while the file did not list it** — so the one
thing this check exists to catch was sitting inside its own allowance, and had been since it was
written. A check that names a file and then keeps its own copy is a check about the copy.

**The fix, in two parts.** The set is parsed from `requirements.txt`, so there is nothing left to
drift. And the missing dependency is **declared rather than excused**: `httpx2` arrives with `mcp`
anyway, which is exactly why nobody noticed — everything worked. Relying on a package you did not ask
for is relying on somebody else's dependency list, and the day `mcp` stops needing it the CLI stops
importing while the pins still look complete.

**A check that raises is not a check that failed.** The first version read the repository root from a
name bound further down the same function, so it raised `UnboundLocalError` *inside* the check — no
PASS, no FAIL, just a traceback where a verdict should be. It reads the package's own parent
directory now.

**The check.** `R-16` undeclares `httpx2` again; the check goes red naming the module that imports it.

---

## R-17 — the two suites pinned opposite policies on naming the real teams in a refusal

**Severity:** low as a leak today — no client reaches the message — and high as a signal, because
both checks were **green**. Two suites asserting opposite things about the same event means one of
them is wrong, and a reader comparing them cannot tell which.

**What was wrong.** For the same condition — a team the broker does not know — the two layers said
opposite things, and each had a check pinning its own answer:

```
  switchboard.py   raise SwitchError(f"unknown team {team!r}; teams are {TEAMS}")
  test_switchboard.py   "an unknown team is still refused, and the error names the real ones"

  switchboard_broker.py   "team 'x' is not enabled on this broker. ..."   (no list)
  test_switchboard_e2e.py   "the refusal does NOT disclose which teams exist"
```

Measured, with `TEAMLINE_TEAMS=alpha,beta,gamma`: the state machine's message discloses
`['alpha', 'beta', 'gamma']`; the broker's discloses none. A caller that guessed a team name wrong
would have been handed the valid ones, and the next guess is a disguise.

**The policy held by accident.** `team_of()` checks the header before any `Switchboard` method runs,
so in the shipped arrangement the broker's message is the one a client sees. That is an *ordering*,
not a property: `_team()` is called from three places (`_mine`, `register`, `set_running`), and every
other route into the state machine produced the list. A security rule that depends on which of two
checks happens to run first is not being enforced anywhere.

**The test that should have caught it is the one that asserted the opposite.** It was green the whole
time the defect was live, which by the method makes it the defect. It now pins the same policy the
end-to-end suite pins: the refusal names **what the caller sent**, and nothing else.

**A distinction the fix rests on, measured rather than assumed.** The walk found that a *real* team
with an unknown extension raises `_unknown()`, which names the closest real **extensions** on
purpose. That is not the same policy contradicting itself. A team name is the gate — you need one to
reach any tool at all. An extension name sits behind that gate, and `sw_directory` hands the whole
list to anyone already through it. The observer surfaces likewise carry team names inside the
directory they display; that is the documented trusted-network posture in the README's security
table, not this defect.

**A check that goes red for the fixture is not a check.** The first version scanned the whole message
for a team name — so an input like `alphax`, which merely *contains* one, would have failed it while
disclosing nothing. It removes the caller's own token before scanning, and was measured against all
three cases: the enumerating message red, the fixed message green, the echo-only message green.

**The check.** `R-17` puts the enumeration back on the last line of the message; the check goes red
naming the disclosure.

---

## Open findings — known, and NOT fixed

Everything above is closed. This section exists because the log had no place to put a finding that
was *not*, which is how a list of them gets lost: the closed ones are written down as they close,
and the open ones live in somebody's memory until they do not.

| finding | state |
|---|---|
| ~~No `.gitattributes`~~ | **CLOSED by V-1.** It was not latent: it was breaking the advertised command on every clone. |
| 170 of the 213 checks in the two suites are not pinned by a perturbation. They pass; none has been shown able to fail. | open, by design — see E-L1 |
| **Row RATE is unbounded.** Any feed holder can append a `touch` row per unrecognised frame, as fast as it can send them. `A8` bounded row *size*; nothing bounds how many. Found alongside R-7 and deliberately not folded into it: a different mechanism, and it needs a different fix. | open |
| Two checks need a list of private names that is deliberately not in this repository, and print `NOT RUN` without it. | open by design — see R-12 |
| `dist/` is not in `.gitignore`, so a build artefact by that name would trip the top-level-directory check in B-L1. | open |
| No `pyproject.toml`: this is run-from-source, not an installable package. The README does not claim otherwise. | open, may be intended |

**On the review this log describes, and what became of it.** The adversarial review produced 94
findings, deduplicated to 43 items in six groups. Fourteen of those are closed above — `A1`–`A9` and
`B1`–`B5`. The remaining 29 were recorded **outside the repository**, in the working session that ran
the review, and did not survive a move between machines. Their content is currently unknown.

The five entries numbered with an `-L` suffix are therefore a different thing from the rest, and are
marked so deliberately: they were re-derived by inspecting the repository rather than taken from that
list, and whether any of them corresponds to one of the 29 is unknown. They are not that list
recovered.

A **second** adversarial review was then run against this repository — not against the first review's
list, which was gone — and produced 32 findings of its own, each with a file and a line. Entries
numbered `R-n` come from it, and this time the list is in version control rather than in a session.
Eight of its findings dispute entries this log already calls closed; `R-2` above is the first of
those to be worked, and the entry it disputes now says so where a reader meets it.

This is stated plainly because the alternative is a log that implies a completeness it does not have —
and because the lesson is the one the section above exists for. The fourteen survived a machine move
for exactly one reason: they had been written down here as they closed. The twenty-nine did not.

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
