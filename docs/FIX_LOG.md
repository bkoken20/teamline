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
