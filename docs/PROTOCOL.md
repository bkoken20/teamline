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
* send `{"ping": 1}` at least every 25 seconds (90 seconds of silence marks the lane GONE);
* reconnect after about 2 seconds if the socket drops, indefinitely.

And must not retry these:

| close / status | meaning | what to do |
|---|---|---|
| HTTP 403, or close 4001 | this team or extension may not exist here | stop, tell a human |
| close 4003 | the lane already has a live holder | back off ~30 s and retry, never exit |

4003 is retryable on purpose: a half-open incumbent reads as live for up to 90 seconds, so this is
what an honest reconnect looks like from the broker's side. The broker refuses second holders, so a
client cannot accidentally cause double delivery.

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
| retired | GONE for 10 minutes, or idle for 24 hours; the lane disappears from the directory |

Re-registering a name replaces a GONE or STALE holder. A LIVE one is refused, so two sessions can
never share a lane.

## 4. Calls

`ring → answer → lines → hangup`, one open call per extension. A ring waits 90 seconds for an answer
and then frees the line. Five minutes of silence inside an open call nudges both sides; a call still
open after 2 hours expires. Every call's transcript is written to `calls/<call_id>.md`.

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

## 7. HTTP surface

| route | purpose |
|---|---|
| `GET /healthz` | `{"up": true, ...}` plus counts; CORS-open so a dashboard can poll it |
| `GET /directory` | the directory as JSON |
| `GET /` | the operator page: every team, every lane, live |
| `POST /hook/now` | `{session_id, text}` — set a lane's `now` line from outside the session |
| `/mcp` | the `sw_*` tools |
| `/ws` | feeds |

`GET /directory` includes a `holders` count per extension. Anything above 1 is a defect state: every
message is delivered that many times while the ledger correctly records one delivery. The operator
page badges it.
