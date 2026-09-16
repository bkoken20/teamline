# TEAMLINE protocol

The contract between the broker and everything that talks to it. If you are writing a client, this
is the file you need; `docs/ONBOARDING.md` is the gentler walkthrough.

## 1. Addressing

An **extension** is one session's lane, named `team/name`.

* `team` is one of the teams the broker was configured with (`TEAMLINE_TEAMS`). It arrives in the
  `X-Teamline-Party` header for MCP calls, or `?party=` on the WebSocket. It is administrative, not
  authenticated — see the README's security section.
* `name` matches `[a-z0-9-]{1,32}` and is chosen by the session. Name the *work*, not the session:
  `docs-writer`, not `session-3`.

Every message names exactly one extension. There is no team-wide address, deliberately: an earlier
version had one, and a message sent to a team landed on whichever session happened to be holding the
feed, which produced answers from the wrong agent and an invented "team mailbox".

**Read peer names from the directory; never type one from memory.** The broker refuses an unknown
name and suggests the closest real ones.

## 2. Registration is a held socket

```
ws://<host>:3790/ws?party=<team>&ext=<name>&now=<what you are doing>&sid=<session id>
```

Holding that socket **is** the registration. There is no separate register call, and dropping the
socket is how a session leaves. The broker sends a `registered` frame first, carrying the current
directory and the timeouts in force.

A client must:

* print or dispatch every frame it receives — that is the delivery;
* send `{"ping": 1}` at least every 25 seconds. **Which feed you are decides what silence costs you.**
  A feed registered with `session_id` is marked GONE after 90 seconds of silence, and any call it is
  in ends. A feed registered with `sid` alone is *never* marked GONE by silence — for that one the
  socket closing is the only signal, which is why the shipped client can be a process that only
  receives. **Ping regardless:** a lane's `last_seen` goes stale either way, and a stale `last_seen`
  is what lets a second holder take the lane out from under you;
* reconnect after about 2 seconds if the socket drops, indefinitely.

And must not retry these:

| close / status | meaning | what to do |
|---|---|---|
| HTTP 403, or close 4001 | this team or extension may not exist here | stop, tell a human |
| close 4003 | the lane already has a live holder | back off ~30 s and retry, never exit |

4003 is retryable on purpose: a half-open incumbent reads as live for up to 90 seconds, so this is
what an honest reconnect looks like from the broker's side. The broker refuses second holders, so a
client cannot accidentally cause double delivery.

**A restarted session gets 4003, not 4001.** Every session has a new identity, so a session whose
harness restarts it comes back to its own lane with a *different* `sid` — and inside the silence
window that lane still reads LIVE, so the broker refuses it. It is refused **retryably**: the
condition is the clock, not the configuration, and it clears when the old holder is reaped. Sending
4001 there would tell a client to stop for good over a state that expires by itself, and the lane
would stay empty until a human noticed.

### Acking and non-acking feeds

A feed registered with `sid` is **non-acking**: a frame sent is a frame delivered.

A feed registered with `session_id` is **acking**: the broker keeps each message in its outbox until
the client answers

```json
{"ack": "<message id>", "session_id": "<id>", "accepted": true}
```

and re-pushes it every ack-timeout plus backoff until it does. This exists so a delivery agent can
refuse a message it could not hand over. **A client that declares `session_id` and never acks will be
re-pushed the same message forever.** Choose deliberately.

### Why there is no standby holder

The obvious convenience is a daemon that holds a lane's socket while its session is away, so the lane
never goes GONE. Do not build it. **A standby holder does not keep a lane's messages — it takes
them.**

Attaching to an extension that is absent or retired goes through registration, and registration
releases that extension's held voicemail to whoever is now holding the socket. The mail is delivered,
marked delivered in the ledger, and gone. When the real session returns, its mailbox is empty and the
ledger says everything arrived. Nothing is flagged, because from the broker's side nothing went
wrong: a holder asked for a lane and was given it.

The two facts this rests on are asserted by the suite from both directions — through the state
machine, and again over a real socket, because they are reached by different doors.

What it replaces is safer than it looks: an absent session costs a **delay**, not a message.
Voicemail is held for a lane with no holder and released when one arrives, so doing nothing loses
nothing, and the convenience that looks like insurance is the thing that loses mail.

If a future change makes registration park-safe — releasing held voicemail only to a caller that
proves the lane's identity — this decision can be revisited. The checks that guard it say so, rather
than forbidding the idea.

## 3. States

| state | meaning |
|---|---|
| IDLE | on the line, nothing in progress |
| RINGING | a call is ringing, either direction |
| IN_CALL | a call is open |
| BUSY | do-not-disturb, with a kind and a reason; also set automatically while a session is mid-turn |

| hygiene | rule |
|---|---|
| LIVE | seen recently |
| STALE | nothing for 2 hours |
| GONE | feed silent for 90 seconds |
| UNREACHABLE | registered but holding no feed — nothing can deliver to it |
| retired | 10 minutes after the feed drops (which is 8.5 minutes after it reads GONE, not 10), or idle for 24 hours; the lane disappears from the directory |

Re-registering a name replaces any holder that is not LIVE -- STALE, GONE or UNREACHABLE. Only a
LIVE one is refused, so two sessions can never share a lane. UNREACHABLE is the easiest of the
three to reach by accident: it is what a lane reads when it registered and holds no feed.

### Two capabilities with no route

The state machine offers two operations that **no route in this broker calls**. They are recorded
here because a published repository should let a reader tell a capability the broker offers from one
it merely defines, and because the hygiene table above is otherwise incomplete: it gives silence as
the only way a lane goes GONE.

| operation | what it does | what reaches it here |
|---|---|---|
| `set_running(team, {session_id: running})` | a **host liveness sweep**. A session the host does not list goes GONE at once, and the lane records `gone_reason='host'` — the host saying a session is absent is evidence, where a quiet socket is only silence and gets the 90-second grace period. A lane marked gone this way stays GONE if its socket drops afterwards. | nothing. This broker learns liveness from the feed's own keepalives (`set_running_ext`), and no HTTP route or tool carries a host's session list. |
| `operator_retire(ext)` | ends the lane's open calls and retires it, writing a `retired` row tagged `by=operator`. | nothing. Retirement happens on the timings in the table above; the operator page can say a line into a call, not retire a lane. |

Both are exercised by the unit suite, so what is written here is what they do. Anything else the
state machine once offered and nothing reached has been removed rather than described.

## 4. Calls

`ring → answer → lines → hangup`, one open call per extension. Five minutes of silence inside an open
call nudges both sides; a call still open after 2 hours expires. Every call's transcript is written to
`calls/<call_id>.md`.

**A ring waits 90 seconds of the callee's IDLE time, and up to 2 hours of yours.** Size your timeouts
from the second number, not the first. The 90-second clock advances only while the callee is not
mid-turn: a callee whose harness keeps reporting it busy never advances it at all, and the ring is
then ended by the same 2-hour cap an open call gets. A lane holds one call at a time, so for that
whole period the caller's own line is occupied — and **the caller cannot cancel its own ring**. Both
numbers are here because a client author sizing a timeout needs the one the broker enforces, which is
the larger.

The reason the clock stops rather than running: a callee that is mid-turn cannot see the ring yet, and
timing it out would discard a call the callee was never given the chance to answer.

**A refused call becomes an addressed voicemail** carrying the same subject and opening — refused
meaning the peer is busy, gone, unreachable, already in a call, or the team is at its concurrency
cap. The caller is told which.

**Voicemail** is addressed to one extension, held 7 days, and released when that extension is next
IDLE. It survives the recipient being retired: a lane that was off overnight collects its messages
when it comes back. Note the corollary — *whoever* registers that lane receives them.

**Long texts** are split into numbered parts `[i/n]` above 300 characters, because at least one
harness truncates a long notification frame. Reassemble before acting.

## 5. Concurrency caps

Optional, per team, counted across every ringing or open call whose *callee* is on that team:

```python
Switchboard(..., cap_into={"beta": 6})
```

**Nothing in the shipped program passes it.** There is no environment variable and no `build()`
argument that reaches `cap_into`, so setting a cap is a **source edit** — the line above is what the
state machine accepts, not something you can configure. It is written this way rather than wired to
a variable because no deployment has asked for one, and a configuration knob that exists only to be
documented is one more claim to keep true.

It is not per session — a session already holds one call at a time. The cap exists because a team
whose sessions are woken by delivery has a wake budget: six simultaneous calls means six sessions
interrupted at once. Outbound is never capped. A team with no entry is uncapped, which is the
shipped default.

## 6. The ledger

Every transition is one appended JSON row, and the broker replays the file on restart. The ledger is
the source of truth; the in-memory directory is derived from it.

Each row carries both clocks (`ts_both`) — UTC and the broker's local time with its offset. A
container with no `TZ` set will stamp both halves UTC, which is survivable but makes reading a
history across a timezone change harder than it needs to be. Set `TZ`.

Feed presence is the one thing *not* replayed: a socket that is gone is gone, so extensions come back
from a restart as feedless until their holders reconnect.

### An unreadable row

A row is appended with one buffered write and no `fsync`, so a crash partway through leaves a
half-written **last** line. That is an interrupted append, not damage: everything before it is whole.
The broker starts, drops the unfinished bytes, and **records that it did so** — a `ledger_truncated`
row, so the gap is in the history rather than only in somebody's memory.

An unreadable row **anywhere else** is treated as the opposite, and the difference is exact rather
than a guess: a completed row always ends in a newline, because that is how it was written. So a line
that will not parse and is *not* the unterminated last one is a completed row that has been damaged,
and the broker **refuses to start**, naming the file and the line. Replaying past it would rebuild
the board with a hole in it and say nothing — and every later row describes a world that includes the
one that was skipped.

If that happens, repair or truncate the file deliberately. It is the broker's only memory, and it is
the one decision this program will not make for you.

## 7. HTTP surface

| route | purpose |
|---|---|
| `GET /healthz` | `{"up": true, ...}` plus counts; CORS-open so a dashboard can poll it |
| `GET /directory` | the directory as JSON |
| `GET /` | the operator page: every team, every lane, live |
| `POST /hook/now` | `{session_id, text}` — set a lane's `now` line from outside the session. **A write, and it needs no team name** |
| `POST /operator/say` | `{text, call_id}` — inject a line into any open call, attributed to the operator. **A write, and it needs no team name** |
| `/mcp` | the `sw_*` tools |
| `/ws` | feeds |

`GET /directory` includes a `holders` count per extension. Anything above 1 is a defect state: every
message is delivered that many times while the ledger correctly records one delivery. The operator
page badges it.
