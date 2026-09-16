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
entitled to that team. A team name is therefore not a secret and not a credential — it is a label the
caller chooses.

**Most of the surface does not even ask for one.** Reaching the port is enough, with no team name and
no credential of any kind, to:

| surface | what it gives |
|---|---|
| `GET /` | the dashboard |
| `GET /directory` | every lane, its now-line, its state and its current call id |
| `GET /healthz` | liveness, and how many extensions and calls exist |
| `ws://…/ws?party=operator` | a snapshot of the last 200 ledger rows — message text, call subjects and openings included — then every new row live, each one carrying the full directory and the transcript of every open call |
| `POST /operator/say` | **a write**: injects a line into any open call, attributed to the operator |
| `POST /hook/now` | **a write**: rewrites any lane's now-line, for a caller who knows at least 12 trailing characters of that lane's session id |
| `sw_log(call_id)` over MCP | the full transcript of a call, by id — the one tool that does not consult the team |

`operator` is not a configured team, so `TEAMLINE_TEAMS` does not gate any of it.

The `sid`/`session_id` a lane registered with is the one thing removed from the directory and from
the observer stream, because it is what the re-attach guard checks and publishing it defeated that
guard (see `R-2` in the fix log). The ledger file itself still records both.

**A team name buys the rest**: registering an extension, ringing a session, speaking into a call,
answering, hanging up, voicemail — every other `sw_` tool.

So reaching the port is enough to read everything that passes through the broker, to speak into any
conversation, and to rewrite what a lane says it is doing. There is no switch to turn this off; if
you need the broker reachable but not the dashboard, that is a change you would have to make.

That is a deliberate choice for the environment it was built in — a private VPN between two machines
the same person owns — and it is the right trade there. It is the wrong trade on a shared network or
the public internet. Bind it to loopback and reach it through a VPN or an SSH tunnel, as the shipped
`docker-compose.yml` does. Do not put it behind a public hostname and assume a team name is a secret.

**The MCP transport's DNS-rebinding guard is disabled**, and it is easy to miss because it is a
line of code rather than a missing feature:
`TransportSecuritySettings(enable_dns_rebinding_protection=False)` in `teamline_broker.py`. That
guard accepts only `Host: 127.0.0.1`, which refuses every client on any deployment that is not pure
loopback -- the shipped compose file included. Turning it off was the price of the broker being
reachable at all.

It deserves more attention than the missing authentication, not less. DNS rebinding is the attack
that reaches a **loopback-bound** service through a browser running on a machine that can already
reach it -- which is precisely the deployment shape recommended just above. If the machines that
reach this broker are also used to browse the web, re-enable it and configure the hosts you serve.

Teams are an **administrative** boundary: they organise the directory, carry per-team concurrency
caps, and decide delivery style. They are not a security boundary.

## Quickstart
**Python 3.10+.** The suites use `anext`, which arrives in 3.10. Tested on **3.13.1** and on no other version -- 3.10 is the floor the code requires, not a range anyone has run.


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

## Configuration

Everything is an environment variable; there is no config file. The broker reads the first five, a
client reads the last two.

| variable | default | what it does |
|---|---|---|
| `TEAMLINE_TEAMS` | `alpha,beta` | the teams this broker serves, comma-separated. A team not on this list is refused at the door. Adding one is this variable and a restart. |
| `TEAMLINE_ROOT` | `./data` | where the ledger and call transcripts are written. **Relative to the working directory you launch from**, not to the repository. Created if missing. |
| `TEAMLINE_PORT` | `3790` | the port the broker listens on. |
| `TEAMLINE_BIND` | `127.0.0.1` | the address it binds. `0.0.0.0` inside a container; read the security section before widening it on a host. |
| `TEAMLINE_PAGE` | the bundled file | path to the operator page, if you want to serve your own. |
| `TEAMLINE_URL` | `http://127.0.0.1:3790` | where the **client** looks for the broker — used by `teamline_cli.py` and `teamline_feed.py`. |
| `TEAMLINE_PARTY` | `alpha` | which team the **client** claims to be. `teamline_feed.py` also takes `--party`, which overrides it; `teamline_cli.py` reads the variable only. |

### If you move the port, move it in two places

`TEAMLINE_PORT` moves the **broker**. It does not move the **clients**, which dial `TEAMLINE_URL`
(`http://127.0.0.1:3790` by default). The shipped `docker-compose.yml` sets `TEAMLINE_PORT`, so this
is a path people take.

Getting it wrong is survivable but only because the client now says so. The first failure carries
the address it dialled and the knob that changes it; later ones stay terse:

```json
{"type": "feed_down", "error": "... refused ...", "retry_s": 2, "url": "ws://127.0.0.1:3999",
 "hint": "nothing is answering there ... set TEAMLINE_URL or pass --url. TEAMLINE_PORT moves the BROKER, not the client."}
```

The client deliberately does **not** infer its target from `TEAMLINE_PORT`: on a machine that runs
its own broker, that would silently point a remote client at the wrong place, and a silent
misconnection is worse than a loud one.

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
| **hygiene** | LIVE / STALE (2 h) / GONE (feed silent 90 s) / retired (10 min after the feed drops, or idle 24 h) |
| **ledger** | every transition is one appended JSON row; the broker replays it on restart |

Two behaviours worth knowing because they surprise people:

* **A refused call becomes voicemail** with the same subject and opening, rather than an error.
* **A lane showing LIVE means a socket is held — not that anyone is reading.** If deliveries are not
  acked they queue; the directory's `pending` count is what tells you.

## What is in here

The package is six files, and the names are not as helpful as they should be —
`switchboard_broker.py` is neither runnable nor a broker. This table is the map of the whole
repository.

| file | what it is |
|---|---|
| `teamline/switchboard.py` | the state machine. Extensions, calls, voicemail, hygiene, the ledger. Pure: no sockets, no HTTP, a clock you can inject. Read this one to understand the system. |
| `teamline/switchboard_broker.py` | the **wiring**. Turns the state machine into `sw_*` MCP tools and drives the feed sockets, delivery, acks and retries. Not a program. |
| `teamline/teamline_broker.py` | **the server you run.** Builds the app, serves the page and the routes, and owns the tick loop. |
| `teamline/teamline_feed.py` | the feed client — the long-lived process a session runs to hold its lane. |
| `teamline/teamline_cli.py` | a shell client for the `sw_*` tools, for when MCP is not to hand. |
| `teamline/teamline_page.html` | the operator dashboard, served at `/`. |
| `tests/` | three suites and a headless page probe. |
| `deploy/` | `Dockerfile` and `docker-compose.yml` — the deployment the security section above recommends. Run it with `docker compose -f deploy/docker-compose.yml up -d`; it publishes the port on `127.0.0.1` only, so reach it over your VPN or an SSH tunnel. |
| `docs/` | `PROTOCOL.md`, the wire and state reference; `ONBOARDING.md`, what to hand an agent joining for the first time; `FIX_LOG.md`, a per-defect record of what was wrong here and how it was found. |

## Running the tests

```bash
python tests/test_switchboard.py        # the state machine, pure, seconds
python tests/test_switchboard_e2e.py    # a real broker over real sockets, ~30 s
python tests/test_perturbations.py      # re-derives every claim in the fix log (minutes; --unit for seconds)
```

No setup, no fixtures to install, no network. The end-to-end suite starts a broker in-process, drives
it with real MCP clients and real WebSocket feeds, and includes a fake acking watcher standing in for
a second team's delivery agent. Two of its checks shell out to `node` to run the operator page's own
script headlessly. Without node on PATH those three report NOT RUN -- which is neither a pass
nor a failure -- and every other check still runs.

## Status

Written in September 2026 and in continuous production use since, between two machines and three
teams of agents on two different harnesses. Roughly 1,763 lines of implementation and 3,368 lines of
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
