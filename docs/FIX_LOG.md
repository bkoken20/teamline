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
