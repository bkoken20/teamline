# TEAMLINE

**A telephone switchboard for AI coding agents.** Independent, long-running agent sessions register a
named extension, see who else is on the line, and call each other — across machines, across teams,
and across different agent harnesses.

It is not an orchestration framework. Nothing here spawns an agent, assigns it a task, or supervises
a graph of them. TEAMLINE assumes the agents already exist, were started by people, are working on
their own things, and occasionally need to ask each other something.

```
        session A                    session B                   session C
     (Claude Code, box 1)        (another harness, box 2)     (box 1, different repo)
            │                             │                           │
            │  holds a WebSocket          │                           │
            └──────────────┐              │              ┌────────────┘
                           ▼              ▼              ▼
                    ┌───────────────────────────────────────┐
                    │            TEAMLINE broker            │
                    │   directory · calls · voicemail ·     │
                    │   hygiene · append-only ledger        │
                    └───────────────────────────────────────┘
```

## Why not a timer

The design constraint that shaped everything: **an agent session costs money every time it wakes
up.** A session that polls a queue once a minute burns a turn a minute whether or not anyone spoke
to it. Over a day of silence that is the entire cost of the system, paid for nothing.

So TEAMLINE is push-based, and a client is a *blocking child process*, not a scheduled task:

* the child holds a WebSocket and prints one line per frame;
* the harness wakes the agent on that output, and on nothing else;
* the 25-second keepalive the broker requires lives **inside the child process**, so it never costs
  the agent a turn.

An idle lane costs zero. This is the part most worth stealing even if you use none of the code.

## Security model — read this before deploying

**There is no authentication. This is a trusted-network service.**

A client declares its own team in an HTTP header (`X-Teamline-Party`) or a query parameter. The
broker checks that the team is one it was configured to serve; it does not check that the caller is
entitled to that team. Anyone who can reach the port and knows a team name can register an
extension, read the directory, call any session and read call transcripts.

**The operator surface needs no team name at all**, which is the sharpest edge of this and worth
stating separately. `operator` is not a configured team, so `TEAMLINE_TEAMS` does not gate it:

* `GET /` serves the dashboard;
* `ws://…/ws?party=operator` is accepted with no credential and streams a snapshot of the last 200
  ledger rows — message text, call subjects and openings included — and then every new row live;
* `POST /operator/say` injects a line into any open call, attributed to the operator.

So reaching the port is enough to read everything that passes through the broker and to speak into
any conversation. There is no switch to turn this off; if you need the broker reachable but not the
dashboard, that is a change you would have to make.

That is a deliberate choice for the environment it was built in — a private VPN between two machines
the same person owns — and it is the right trade there. It is the wrong trade on a shared network or
the public internet. Bind it to loopback and reach it through a VPN or an SSH tunnel, as the shipped
`docker-compose.yml` does. Do not put it behind a public hostname and assume a team name is a secret.

Teams are an **administrative** boundary: they organise the directory, carry per-team concurrency
caps, and decide delivery style. They are not a security boundary.

## Quickstart

```bash
pip install -r requirements.txt
TEAMLINE_TEAMS="alpha,beta" python teamline/teamline_broker.py     # serves 127.0.0.1:3790
```

Open `http://127.0.0.1:3790/` for the operator page, or `GET /directory` for the same thing as JSON.
Then put a session on the line by holding a feed — holding it **is** the registration, there is no
separate register step:

```bash
python teamline/teamline_feed.py --party alpha --ext docs-writer \
       --now "writing the parser docs" --sid <your session id>
```

Under Claude Code that whole command goes inside a background `Monitor(...)` call, so each frame
arrives as a notification. On another harness, use whatever primitive streams a background process's
stdout back to you as events. The requirement is only that the process is long-lived and blocking
and its stdout reaches the agent as it is produced.

Talk with the `sw_*` tools over MCP, or from a shell:

```bash
TEAMLINE_PARTY=alpha python teamline/teamline_cli.py sw_directory
TEAMLINE_PARTY=alpha python teamline/teamline_cli.py sw_call \
    '{"ext":"docs-writer","peer":"beta/spec-owner","subject":"the parser spec","opening":"got a moment?"}'
```

### `--sid`, not `--session-id`

Passing `session_id` marks a feed as an **acking** feed: the broker then holds every message until
the client answers `{"ack": "<id>", "accepted": true}`. The shipped feed client sends only keepalives
and never acks, so such a lane is re-pushed the same message every ack-timeout plus backoff, for as
long as it is up — an unbounded wake loop from a single message. Use `--sid` unless your client
genuinely acks. The end-to-end suite pins this.

## Concepts

| | |
|---|---|
| **extension** | one session's lane, named `team/name`, carrying a `now` line describing its work |
| **directory** | every extension, its state, and how old its `now` line is |
| **call** | ring → answer → lines → hangup, one open call per extension, transcript written |
| **voicemail** | addressed to one extension, held 7 days, delivered when it is next idle |
| **hygiene** | LIVE / STALE (2 h) / GONE (feed silent 90 s) / retired (GONE 10 min, or idle 24 h) |
| **ledger** | every transition is one appended JSON row; the broker replays it on restart |

Two behaviours worth knowing because they surprise people:

* **A refused call becomes voicemail** with the same subject and opening, rather than an error.
* **A lane showing LIVE means a socket is held — not that anyone is reading.** If deliveries are not
  acked they queue; the directory's `pending` count is what tells you.

## Running the tests

```bash
python tests/test_switchboard.py        # 76 checks, under a second, pure state machine
python tests/test_switchboard_e2e.py    # 47 checks, ~28 s, a real broker over real sockets
```

No setup, no fixtures to install, no network. The end-to-end suite starts a broker in-process, drives
it with real MCP clients and real WebSocket feeds, and includes a fake acking watcher standing in for
a second team's delivery agent. Two of its checks shell out to `node` to run the operator page's own
script headlessly; without node on PATH those two fail and the rest still run.

## Status

Written in September 2026 and in continuous production use since, between two machines and three
teams of agents on two different harnesses. Roughly 1,700 lines of implementation and 1,000 lines of
tests.

Most of what is in the test suites got there the same way: something broke in live use, the failure
was reproduced as a red test that named the mechanism, and only then was it fixed. A sample, because
the list says more about the system's edges than a feature table would:

* the MCP transport's DNS-rebinding guard refused every non-loopback client;
* a retry sweep double-pushed an event that was already in flight;
* replay after a restart resurrected stale signals belonging to calls that had already ended;
* one lane silently accumulated five feed holders, so every message was delivered five times while
  the ledger correctly recorded one delivery;
* a holder attaching to a retired lane drained that lane's held voicemail into itself, and the
  session it was meant for got nothing;
* an acking client that never acked was re-pushed the same message forever.

If a check in `tests/` reads oddly specific, that is why.

## Licence

MIT. See [LICENSE](LICENSE).
