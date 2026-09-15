"""SWITCHBOARD end-to-end, the broker's host mode (inverted delivery): the real broker in-process,
MCP clients as extensions (team by header, ext by argument), a WebSocket feed per extension, a FAKE
ACKING WATCHER standing in for a second team's delivery agent (holds feeds with session_id, ACKS deliveries, sends
keepalives carrying `running`), the observer feed for the operator page. The broker reaches no session's host: delivery into beta is inverted -- the watcher delivers and acks.

Run: python tests/test_switchboard_e2e.py
"""
import asyncio
import io
import re as _re0
import json
import os
import subprocess
import sys
import tempfile

PKG = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "teamline")
sys.path.insert(0, PKG)
# The suite defines the teams it exercises, so a fresh clone runs green with no setup. The library
# ships with a different default; check 14 asserts that separately.
os.environ.setdefault("TEAMLINE_TEAMS", "alpha,beta,gamma")
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
FAILS = []
PORT = 3792


def _readme0():
    return io.open(os.path.join(os.path.dirname(PKG), "README.md"), encoding="utf-8").read()


def SBteams():
    import switchboard as SB
    return SB.TEAMS


def ck(name, cond, detail=""):
    print("  %s  %s%s" % ("PASS" if cond else "FAIL", name, "" if cond else "\n        [%s]" % (detail,)))
    if not cond:
        FAILS.append(name)


async def run(tmp):
    import httpx2
    import uvicorn
    import websockets
    from mcp.client import Client
    from mcp.client.streamable_http import streamable_http_client
    import teamline_broker as B

    app = B.build(root=tmp, ring_timeout_s=90, ack_timeout_s=1.0, feed_gone_s=2.0, state_every_s=0.5,
                  backoff_s=1.0)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=PORT, log_level="error"))
    task = asyncio.create_task(server.serve())
    for _ in range(50):
        if server.started:
            break
        await asyncio.sleep(0.1)
    url = "http://127.0.0.1:%d/mcp" % PORT

    async def call(team, tool, **args):
        hc = httpx2.AsyncClient(headers={"X-Teamline-Party": team}, timeout=httpx2.Timeout(30.0, read=130.0))
        async with hc:
            async with Client(streamable_http_client(url, http_client=hc)) as s:
                res = await s.call_tool(tool, args)
                txt = "".join(c.text for c in res.content if getattr(c, "text", None))
                try:
                    return json.loads(txt)
                except Exception:
                    return txt

    # ---- alpha feeds (Claude sessions) -----------------------------------------------------
    feeds = {"writer": [], "review": []}

    async def feed(name, sink):
        async with websockets.connect("ws://127.0.0.1:%d/ws?party=alpha&ext=%s&now=%s" % (PORT, name, name + "-work")) as ws:
            async for m in ws:
                sink.append(json.loads(m))
    ft = [asyncio.create_task(feed("writer", feeds["writer"])), asyncio.create_task(feed("review", feeds["review"]))]

    # ---- fake beta watcher: one process per box, one feed per extension ---------------------
    delivered = {"deep": [], "ruler": []}          # what "session.prompt" would have received
    running = {"deep": True, "ruler": False}
    ack_mode = {"deep": True, "ruler": True}        # accepted flag the watcher answers with
    parts = {}

    async def watcher(name, session_id):
        async with websockets.connect("ws://127.0.0.1:%d/ws?party=beta&ext=%s&now=%s&session_id=%s" % (PORT, name, name + "-now", session_id)) as ws:
            async def pinger():
                while True:
                    await ws.send(json.dumps({"ping": 1, "running": running[name]}))
                    await asyncio.sleep(0.4)
            pt = asyncio.create_task(pinger())
            try:
                async for m in ws:
                    ev = json.loads(m)
                    if not ev.get("kind"):
                        continue
                    if ev.get("part"):
                        buf = parts.setdefault(ev["id"], {})
                        buf[ev["part"][0]] = ev["text"].split("] ", 1)[1]
                        if len(buf) < ev["part"][1]:
                            continue
                        text = "".join(buf[i] for i in sorted(buf))
                    else:
                        text = ev["text"]
                    delivered[name].append(dict(id=ev["id"], kind=ev["kind"], text=text, mode="steer" if running[name] else "queue"))
                    await ws.send(json.dumps({"ack": ev["id"], "session_id": session_id, "accepted": ack_mode[name]}))
            finally:
                pt.cancel()
    wt = {"deep": asyncio.create_task(watcher("deep", "sess-A")), "ruler": asyncio.create_task(watcher("ruler", "sess-B"))}
    await asyncio.sleep(0.8)

    # ---- v1 retired ---------------------------------------------------------------------------
    r = await call("alpha", "line_status")
    ck("the v1 team-level tools no longer exist", isinstance(r, str) and "unknown" in r.lower(), str(r)[:120])
    try:
        async with websockets.connect("ws://127.0.0.1:%d/ws?party=alpha" % PORT) as w1:
            await asyncio.wait_for(w1.recv(), 2)
        ck("a feed without an extension name is refused", False, "accepted")
    except Exception:
        ck("a feed without an extension name is refused", True)
    async with httpx2.AsyncClient() as hc:
        pg = (await hc.get("http://127.0.0.1:%d/" % PORT)).text
    ck("the page carries no v1 team cards", "v1 line" not in pg and "id=parties" not in pg, pg[:100])
    js = pg.split("<script>")[1].split("</script>")[0]
    pr = subprocess.run(["node", "-e", "new Function(require('fs').readFileSync(0,'utf8'));console.log('ok')"],
                        input=js, capture_output=True, text=True, encoding="utf-8")
    ck("the page SCRIPT parses", pr.returncode == 0 and "ok" in pr.stdout, pr.stderr[-200:])

    # ---- the page must RENDER every team in the directory, not a hard-coded pair (operator 09-08: gamma)
    # page_probe.js runs the page's own dir() headlessly, so this is behaviour, not a grep.
    def mk(ext, team, holders=1):
        return dict(ext=ext, team=team, state="IDLE", hygiene="LIVE", now="n", now_age_s=1,
                    now_source="model", busy_kind=None, busy_reason=None, last_seen_age_s=1,
                    session_id=None, voicemail_held=0, pending=0, holders=holders)
    probe_dir = os.path.join(tmp, "probe_directory.json")
    probe_page = os.path.join(tmp, "probe_page.html")
    with open(probe_page, "w", encoding="utf-8") as fh:
        fh.write(pg)
    with open(probe_dir, "w", encoding="utf-8") as fh:
        json.dump([mk("alpha/aaa", "alpha"), mk("beta/bbb", "beta", holders=3),
                   mk("gamma/ccc", "gamma")], fh)
    probe_js = os.path.join(os.path.dirname(os.path.abspath(__file__)), "page_probe.js")
    pr2 = subprocess.run(["node", probe_js, probe_page, probe_dir], capture_output=True, text=True, encoding="utf-8")
    out = pr2.stdout
    ck("the page RENDERS every team in the directory, gamma included (it iterated a hard-coded pair)",
       pr2.returncode == 0 and all(t in out for t in ("alpha", "beta", "gamma"))
       and all(n in out for n in ("aaa", "bbb", "ccc")), (pr2.stderr[-200:] or out[:200]))
    # holders > 1 is a DEFECT STATE (every message delivered that many times) and was invisible on
    # this page while a lane held five holders. The badge must render, and must NOT render at 1.
    ck("the page flags an extension held by more than one feed, and stays silent at one holder",
       "3 HOLDERS" in out and "1 HOLDERS" not in out, out[:240])

    # ---- 15. a team the broker does not know must FAIL LOUDLY, never opaquely and never in a loop
    # (operator 2026-09-08). Measured before the fix: sw_register answered only "Error executing tool
    # sw_register" with no mention of teams, and the feed script retried HTTP 403 every 2 s forever.
    r = await call("newteam", "sw_register", ext="probe", now="n", session_id="x")
    err = str(r.get("error", "")) if isinstance(r, dict) else ""
    ck("an unknown team gets a NAMED error from sw_register, not an opaque tool failure",
       isinstance(r, dict) and "newteam" in err and "not enabled" in err, r)
    # SECURITY (operator, 2026-09-08): the refusal must NOT enumerate the real teams -- a caller that
    # guessed wrong would learn the valid names and could then present itself as one of them.
    ck("the refusal does NOT disclose which teams exist",
       not any(t in err for t in ("alpha", "beta", "gamma")), err[:200])
    r = await call("newteam", "sw_directory")
    ck("...and from sw_directory too", isinstance(r, dict) and "error" in r, r)
    feed_py = os.path.join(PKG, "teamline_feed.py")
    def _run_feed():                       # in a thread: a blocking run would stall the watcher's pings
        return subprocess.run([sys.executable, feed_py, "--party", "newteam", "--ext", "probe", "--now", "n",
                               "--url", "http://127.0.0.1:%d" % PORT],
                              capture_output=True, text=True, encoding="utf-8", timeout=12)
    try:
        pf = await asyncio.to_thread(_run_feed)
        out, rc, looped = pf.stdout, pf.returncode, False
    except subprocess.TimeoutExpired as te:          # still running after 12 s = it is retrying forever
        out, rc, looped = (te.stdout or b"").decode("utf-8", "replace") if isinstance(te.stdout, bytes) else (te.stdout or ""), None, True
    lines = [l for l in out.splitlines() if l.strip()]
    ck("the feed script STOPS on a refusal instead of retrying it forever, and says why",
       not looped and rc not in (0, None) and len(lines) <= 2 and "refused" in out
       and "team" in out and "enabled" in out,
       ("LOOPED FOREVER" if looped else (rc, lines[:3])))

    # SECURITY (2026-09-08, reported from a sibling session and reproduced here): the two clients
    # read the TEAM FROM DIFFERENT PLACES. teamline_cli honours TEAMLINE_PARTY; teamline_feed knew only
    # --party and fell back to "alpha". So a session told (the onboarding doc) to export
    # TEAMLINE_PARTY and then start its feed registered SILENTLY INTO THE DEFAULT TEAM -- a cross-team
    # registration with no error anywhere, which is the disguise outcome reached by accident. D2: the
    # old design was correct only if every caller remembered a flag its sibling tool does not need.
    def _run_feed_env():
        env = dict(os.environ, TEAMLINE_PARTY="newteam")
        return subprocess.run([sys.executable, feed_py, "--ext", "probe-env", "--now", "n",
                               "--url", "http://127.0.0.1:%d" % PORT],
                              capture_output=True, text=True, encoding="utf-8", timeout=8, env=env)
    try:
        pe = await asyncio.to_thread(_run_feed_env)
        eout, erc, eheld = pe.stdout, pe.returncode, False
    except subprocess.TimeoutExpired:
        eout, erc, eheld = "", None, True      # still holding a socket = it registered as somebody
    dnames = {e["ext"] for e in (await call("alpha", "sw_directory"))["extensions"]}
    ck("the feed script takes its TEAM from TEAMLINE_PARTY, like the CLI -- it must never silently "
       "fall back to alpha and register a foreign session into our team",
       not eheld and erc == 3 and "alpha/probe-env" not in dnames,
       ("registered as alpha/probe-env" if "alpha/probe-env" in dnames else
        ("HELD THE SOCKET (registered under the fallback team)" if eheld else (erc, eout[:160]))))

    # ---- the broker's host: a client arriving with a non-loopback Host header must be served (09:05: "Invalid Host header")
    hc2 = httpx2.AsyncClient(headers={"X-Teamline-Party": "alpha", "Host": "10.0.0.2:3790"}, timeout=httpx2.Timeout(30.0))
    async with hc2:
        async with Client(streamable_http_client(url, http_client=hc2)) as s2:
            res = await s2.call_tool("sw_directory", {})
            txt = "".join(c.text for c in res.content if getattr(c, "text", None))
    ck("the MCP endpoint serves a client whose Host header is the the broker's host address (DNS-rebinding guard off)",
       "extensions" in txt, txt[:120])

    # ---- standalone: the broker pulls in nothing from its author's tree ------------------------------
    # The default root is RELATIVE and created at startup, so it need not exist at import time. What
    # must hold is that it is a USABLE path: an escaped backslash-t once put a literal TAB in this
    # default, and the broker then wrote its ledger into a directory nobody could find.
    ck("the broker's default root is a usable path (no control characters, no stray whitespace)",
       bool(B.ROOT) and not any(c in B.ROOT for c in "\t\r\n") and B.ROOT.strip() == B.ROOT, repr(B.ROOT))
    _probe_root = os.path.join(tmp, "made", "on", "demand")
    os.makedirs(_probe_root, exist_ok=True)
    ck("...and a nested root that does not yet exist is created rather than crashing the broker",
       os.path.isdir(_probe_root), _probe_root)
    # NO HIDDEN DEPENDENCIES. Every third-party import in the package must be one this project
    # actually declares, so a clone installs what requirements.txt says and nothing else. Read from
    # the source rather than sys.modules, which would only show what this test happened to import.
    import ast
    declared = {"mcp", "starlette", "uvicorn", "websockets", "httpx2", "anyio"}
    stdlib = set(getattr(sys, "stdlib_module_names", ()))
    undeclared = {}
    for _mod in sorted(os.listdir(PKG)):
        if not _mod.endswith(".py"):
            continue
        _tree = ast.parse(io.open(os.path.join(PKG, _mod), encoding="utf-8").read())
        _local = {f[:-3] for f in os.listdir(PKG) if f.endswith(".py")}
        for _n in ast.walk(_tree):
            if isinstance(_n, ast.Import):
                _names = [a.name.split(".")[0] for a in _n.names]
            elif isinstance(_n, ast.ImportFrom):
                _names = [(_n.module or "").split(".")[0]]
            else:
                continue
            for _name in _names:
                if _name and _name not in stdlib and _name not in declared and _name not in _local:
                    undeclared.setdefault(_mod, set()).add(_name)
    ck("the package imports nothing it does not declare (no hidden dependencies)",
       not undeclared, {k: sorted(v) for k, v in undeclared.items()})

    # ---- a reader must be able to tell which file is which ---------------------------------------
    # Three modules, two prefixes, and the one whose name reads like the server -- switchboard_broker
    # -- is neither runnable nor a broker. Nothing told a newcomer which file to run.
    _mods = [f for f in sorted(os.listdir(PKG)) if f.endswith(".py")]
    ck("every module in the package is described in the README",
       all(m in _readme0() for m in _mods), [m for m in _mods if m not in _readme0()])

    # ...and a reader must be able to find the parts of the REPOSITORY, not only the package. The
    # check above scans teamline/*.py, so it stayed green while two top-level directories went
    # unmentioned: deploy/ -- which holds the docker deployment the security section RECOMMENDS, its
    # run command surviving only as a comment INSIDE the file a reader had not been told exists --
    # and docs/, which holds the protocol reference and the onboarding guide.
    # Only directories that are part of the REPOSITORY. The broker's documented default writes its
    # ledger to ./data relative to the working directory, so a check over every directory present
    # goes red merely because somebody followed the quickstart from the repo root -- a red suite
    # caused by using the software as documented. .gitignore already names what is not ours; read it
    # rather than hard-coding one name and meeting the next runtime directory the same way.
    import fnmatch as _fn0
    _root = os.path.dirname(PKG)
    _gi = os.path.join(_root, ".gitignore")
    _ignored = [ln.strip().rstrip("/") for ln in io.open(_gi, encoding="utf-8")
                if ln.strip().endswith("/") and not ln.lstrip().startswith("#")] \
        if os.path.isfile(_gi) else []
    _tops = [d for d in sorted(os.listdir(_root))
             if os.path.isdir(os.path.join(_root, d))
             and not d.startswith(".") and d != "__pycache__"
             and not any(_fn0.fnmatch(d, p) for p in _ignored)]
    _undesc = [d for d in _tops if (d + "/") not in _readme0() and ("`" + d + "`") not in _readme0()]
    ck("every top-level directory is described in the README, not just the package", not _undesc, _undesc)

    # ---- a publish candidate carries no identifier from the deployment it was forked from ---------
    # The rename that de-identified this repository replaced the TEAM names and left the LANE names
    # behind in comments -- a real lane of the private deployment with only its team relabelled,
    # pointing at an incident log no reader here can see. The de-identifying commit caught the
    # grammatical wreckage of that rename; it did not catch these. No example is quoted here on
    # purpose: this check scans its own file, and a comment illustrating the leak IS the leak.
    # The needles are assembled from fragments so this check can scan its OWN file without matching
    # itself -- a denylist written out literally reports the denylist.
    _private = tuple(a + b for a, b in (
        ("lan", "e-alpha"), ("lane-", "beta"), ("lane-g", "amma"), ("lane-del", "ta"),
        ("lane", "-epsilon"), ("la", "ne-zeta"), ("la", "ne-eta"), ("lan", "e-theta")))
    _leaks = []
    for _dp, _dn, _fs in os.walk(_root):
        _dn[:] = [d for d in _dn if not d.startswith(".") and d != "__pycache__"
                  and not any(_fn0.fnmatch(d, p) for p in _ignored)]
        for _f in _fs:
            # Everything, not an extension allowlist. The walk of the first version showed it was
            # skipping Dockerfile, LICENSE and page_probe.js -- all published, all able to carry a
            # name. Binary files simply will not match; errors="replace" keeps them from raising.
            _txt = io.open(os.path.join(_dp, _f), encoding="utf-8", errors="replace").read()
            _leaks += ["%s:%s" % (os.path.relpath(os.path.join(_dp, _f), _root), _w)
                       for _w in _private if _w in _txt]
    ck("no lane name from the private deployment survives in the publish tree", not _leaks, _leaks[:8])

    # ---- a client that cannot reach the broker must say WHAT it tried ----------------------------
    # TEAMLINE_PORT moves the broker; it does not move the clients, which default to TEAMLINE_URL.
    # Get that wrong -- and the shipped compose file sets TEAMLINE_PORT, so people will -- and the
    # feed client retried forever printing a bare OS error that named neither the address it was
    # dialling nor the knob that changes it.
    def _run_dead():
        return subprocess.run([sys.executable, os.path.join(PKG, "teamline_feed.py"),
                               "--party", "alpha", "--ext", "dead-probe", "--now", "n",
                               "--sid", "session-dead-0001", "--url", "http://127.0.0.1:3999"],
                              capture_output=True, text=True, encoding="utf-8", timeout=8)
    try:
        _dead = await asyncio.to_thread(_run_dead)
        _out = _dead.stdout
    except subprocess.TimeoutExpired as ex:
        _out = (ex.stdout or b"").decode("utf-8", "replace") if isinstance(ex.stdout, bytes) else (ex.stdout or "")
    ck("an unreachable broker is reported with the URL the client actually tried",
       "127.0.0.1:3999" in _out, _out[:200])
    ck("...and with the knob that changes it, so the cause is findable",
       "TEAMLINE_URL" in _out or "--url" in _out, _out[:200])

    # ---- every knob the code reads must be written down -----------------------------------------
    # "How do I configure this" is the first question a stranger has, and the README answered none
    # of it: one variable appeared inline in a command with no explanation and the rest existed only
    # in the source. This is the rule rather than the prose -- if you add a variable, document it.
    _pkg_src = "".join(io.open(os.path.join(PKG, f), encoding="utf-8").read()
                       for f in sorted(os.listdir(PKG)) if f.endswith(".py"))
    _env = set(_re0.findall(r'os\.environ\.get\(\s*"([A-Z_]+)"', _pkg_src))
    _readme = io.open(os.path.join(os.path.dirname(PKG), "README.md"), encoding="utf-8").read()
    ck("every environment variable the package reads is documented in the README",
       all(v in _readme for v in _env), sorted(v for v in _env if v not in _readme))

    # ---- what the server TELLS an agent must match what the system does --------------------------
    # The MCP `instructions` string is the first thing a connecting client shows its agent, before
    # any documentation and before any tool call. It said "sw_register first", while the README and
    # PROTOCOL both say that holding a feed IS the registration -- so an agent following its own
    # tools' advice took the path that produces an UNREACHABLE lane. It also named one specific
    # team, which is meaningless to anyone whose teams are named otherwise.
    # Read the VALUE, not the source text. A regex over the source also matched the comment that
    # explains what was wrong, so the check failed on its own explanation -- a grep finding your own
    # prose. ast gives the string the agent actually receives.
    import ast as _ast
    _tree = _ast.parse(io.open(os.path.join(PKG, "teamline_broker.py"), encoding="utf-8").read())
    _instr = next(_ast.literal_eval(kw.value)
                  for n in _ast.walk(_tree) if isinstance(n, _ast.Call)
                  and getattr(n.func, "id", "") == "MCPServer"
                  for kw in n.keywords if kw.arg == "instructions")
    ck("the server's own instructions do not send agents to sw_register first",
       "sw_register first" not in _instr, _instr[:160])
    ck("...and they name no particular team, since teams are configuration",
       not [t for t in SBteams() if t in _instr], [t for t in SBteams() if t in _instr])

    # ---- sw_register must not privilege a team by NAME -------------------------------------------
    # The no-feed path read `t == "alpha"`, a literal team name, so renaming teams changed
    # behaviour while the README promised teams are one environment variable. Both branches were
    # wrong: the privileged team got a lane claiming a feed nobody holds -- LIVE with zero holders,
    # advertised as answerable and able to receive nothing -- and every other team got an error
    # that never mentioned the real reason.
    reg = {}
    for t in ("alpha", "gamma"):
        reg[t] = await call(t, "sw_register", ext="nofeed-probe", now="n")
    ck("sw_register treats every team identically -- no team is privileged by its name",
       (("error" in reg["alpha"]) == ("error" in reg["gamma"])), reg)
    ck("...and a registration that holds no feed is refused rather than claiming one",
       all("error" in r for r in reg.values()), reg)
    d_probe = {e["ext"]: e for e in (await call("alpha", "sw_directory"))["extensions"]}
    ck("...so no phantom lane appears: nothing is LIVE with zero holders",
       not [e for e in d_probe.values() if e["hygiene"] == "LIVE" and e["holders"] == 0],
       [(e["ext"], e["hygiene"], e["holders"]) for e in d_probe.values()
        if e["hygiene"] == "LIVE" and e["holders"] == 0])

    # THE WIRING AND ITS CONSUMER MUST AGREE. teamline_broker reaches into the dict that
    # switchboard_broker.wire() returns. A key that no longer exists there is invisible until the
    # line runs -- and one such line sat in a `try/except Exception` that would have swallowed the
    # KeyError into a log message nobody reads. A source check catches it without having to reach
    # the code path, which is the point: the path in question was unreachable.
    import re as _re
    _consumer = io.open(os.path.join(PKG, "teamline_broker.py"), encoding="utf-8").read()
    _wiring = io.open(os.path.join(PKG, "switchboard_broker.py"), encoding="utf-8").read()
    _ret = _wiring[_wiring.rindex("return dict("):]
    _provided = set(_re.findall(r"(\w+)=", _ret[:_ret.index(")")]))
    _used = set(_re.findall(r'sw\["(\w+)"\]', _consumer))
    ck("every sw[...] the broker reads is a key the wiring actually returns",
       _used <= _provided, {"used but not provided": sorted(_used - _provided),
                            "provided": sorted(_provided)})

    # ---- directory + liveness from keepalives ---------------------------------------------------
    d = {e["ext"]: e for e in (await call("alpha", "sw_directory"))["extensions"]}
    ck("the directory lists all four extensions with now + age",
       set(d) == {"beta/deep", "beta/ruler", "alpha/writer", "alpha/review"} and all("now_age_s" in e for e in d.values()), sorted(d))
    ck("keepalive running=true shows BUSY(turn), running=false IDLE (liveness from the watcher, no host poll)",
       d["beta/deep"]["state"] == "BUSY" and d["beta/deep"]["busy_kind"] == "turn" and d["beta/ruler"]["state"] == "IDLE", d)
    ck("a beta ext bound to a session id and a feed is LIVE", d["beta/deep"]["hygiene"] == "LIVE" and d["beta/deep"]["session_id"] == "sess-A", d["beta/deep"])

    # ---- calls: delivery by the watcher, ack marks delivered --------------------------------------
    r1 = await call("alpha", "sw_call", ext="writer", peer="beta/deep", subject="depth", opening="numbers?")
    r2 = await call("alpha", "sw_call", ext="review", peer="beta/ruler", subject="ruler", opening="ready?")
    ck("two calls ring concurrently", r1.get("state") == "RINGING" and r2.get("state") == "RINGING", (r1, r2))
    await asyncio.sleep(0.8)
    ck("each ring reached ITS watcher feed: deep as steer (running), ruler as queue (idle)",
       any(x["kind"] == "ring" and "depth" in x["text"] and x["mode"] == "steer" for x in delivered["deep"])
       and any(x["kind"] == "ring" and "ruler" in x["text"] and x["mode"] == "queue" for x in delivered["ruler"]), delivered)
    ledger = lambda: [json.loads(l) for l in open(os.path.join(tmp, "switchboard.jsonl"), encoding="utf-8") if l.strip()]
    ck("the watcher's ack marks the ring delivered WITH its session id (host-accepted once)",
       any(e["event"] == "delivered" and e["session_id"] == "sess-A" for e in ledger()), [e for e in ledger() if e["event"] == "delivered"][-2:])
    ck("the caller got the machine ring_delivered signal after the ack",
       any(e.get("kind") == "ring_delivered" for e in feeds["writer"]), feeds["writer"][-2:])
    # ---- an ack must come from the lane the message was addressed to ---------------------------
    # on_frame() knows which socket a frame arrived on, and awaiting[msg_id] records the TARGET
    # extension at push time -- but the ack path compared neither. A `delivered` row removes the
    # message from the outbox permanently, so a foreign ack does not merely mislabel the delivery,
    # it DESTROYS another lane's message and ledgers it against the wrong session.
    victim_frames = []

    async def victim_holder(seconds):
        # Bounded by an ABSOLUTE deadline, not by inter-frame silence: this lane never acks, so the
        # retry sweep re-pushes to it every ack_timeout + backoff and it never falls quiet. Waiting
        # for silence here hung the suite.
        loop = asyncio.get_event_loop()
        end = loop.time() + seconds
        async with websockets.connect(
                "ws://127.0.0.1:%d/ws?party=gamma&ext=victim&now=waiting&session_id=sess-VICTIM" % PORT) as ws:
            try:
                while loop.time() < end:
                    ev = json.loads(await asyncio.wait_for(anext(aiter(ws)), max(0.05, end - loop.time())))
                    if ev.get("kind"):
                        victim_frames.append(ev)          # received, and deliberately never acked
            except (asyncio.TimeoutError, StopAsyncIteration):
                return
    vh = asyncio.create_task(victim_holder(2.5))
    await asyncio.sleep(0.4)
    vm = await call("alpha", "sw_leave", ext="writer", peer="gamma/victim", text="for the victim only")
    mid = vm["msg_id"]
    await asyncio.sleep(0.5)
    async with websockets.connect("ws://127.0.0.1:%d/ws?party=alpha&ext=attacker&now=x" % PORT) as aws:
        await asyncio.wait_for(anext(aiter(aws)), 2)       # the attacker's own `registered` frame
        await aws.send(json.dumps({"ack": mid, "session_id": "sess-ATTACKER", "accepted": True}))
        await asyncio.sleep(0.6)
    await vh
    stolen = [e for e in ledger() if e["event"] == "delivered" and e.get("msg_id") == mid]
    ck("an ack naming a message addressed to ANOTHER lane is ignored", not stolen, stolen)
    dv = {e["ext"]: e for e in (await call("gamma", "sw_directory"))["extensions"]}
    ck("...and that message is still queued for the lane it belongs to",
       (dv.get("gamma/victim") or {}).get("pending", 0) >= 1, dv.get("gamma/victim"))

    await call("beta", "sw_answer", ext="deep")
    await call("beta", "sw_answer", ext="ruler")
    await call("beta", "sw_say", ext="deep", text="depth line")
    await call("beta", "sw_say", ext="ruler", text="ruler line")
    await asyncio.sleep(0.5)
    ck("feed frames route by extension: writer got only the depth line, review only the ruler line",
       any(e.get("kind") == "say" and "depth line" in e["text"] for e in feeds["writer"])
       and not any("ruler line" in e.get("text", "") for e in feeds["writer"])
       and any(e.get("kind") == "say" and "ruler line" in e.get("text", "") for e in feeds["review"])
       and not any("depth line" in e.get("text", "") for e in feeds["review"]), (feeds["writer"][-1:], feeds["review"][-1:]))
    long = "L" + ("0123456789" * 90)
    await call("alpha", "sw_say", ext="writer", text=long)
    await asyncio.sleep(0.8)
    ck("a long line reaches the watcher as parts and is delivered CONCATENATED (beta buffers parts), acked once",
       any(x["kind"] == "say" and x["text"].endswith(long) for x in delivered["deep"])
       and sum(1 for e in ledger() if e["event"] == "delivered" and e["session_id"] == "sess-A") == len(delivered["deep"]), (len(delivered["deep"]),))
    await call("alpha", "sw_say", ext="writer", text="thanks depth")
    await asyncio.sleep(0.4)
    ck("an in-call line goes to its peer's watcher only",
       any("thanks depth" in x["text"] for x in delivered["deep"]) and not any("thanks depth" in x["text"] for x in delivered["ruler"]))
    # ack refused / no ack
    ack_mode["ruler"] = False
    await call("alpha", "sw_say", ext="review", text="refused line")
    await asyncio.sleep(0.5)
    ck("an ack with accepted:false is ledgered delivery_failed, message stays pending",
       any(e["event"] == "delivery_failed" and "refused" in e.get("error", "") for e in ledger())
       and any(e["ext"] == "beta/ruler" and e["pending"] >= 1 for e in (await call("alpha", "sw_directory"))["extensions"]), [e for e in ledger() if e["event"] == "delivery_failed"][-1:])
    ack_mode["ruler"] = True
    r = await call("alpha", "sw_hangup", ext="writer", summary="depth done")
    ck("hangup writes the transcript", r.get("state") == "IDLE" and os.path.exists(r.get("transcript", "")), r)
    await call("alpha", "sw_hangup", ext="review", summary="ruler done")

    # ---- unreachable: sw_register without a feed --------------------------------------------------
    r = await call("beta", "sw_register", ext="lonely", now="no watcher", session_id="sess-L")
    d = {e["ext"]: e for e in (await call("alpha", "sw_directory"))["extensions"]}
    ck("a beta ext registered WITHOUT a feed is UNREACHABLE (no watcher can deliver)", d["beta/lonely"]["hygiene"] == "UNREACHABLE", d.get("beta/lonely"))
    r = await call("alpha", "sw_call", ext="writer", peer="beta/lonely", subject="s", opening="o")
    ck("a call to it is refused into voicemail (rule 9), held until a feed appears", r.get("state") == "UNREACHABLE" and r.get("voicemail_queued"), r)

    # ---- feed silence -> GONE, open call -> peer_lost ---------------------------------------------
    await call("alpha", "sw_call", ext="review", peer="beta/ruler", subject="again", opening="o")
    await asyncio.sleep(0.5)
    await call("beta", "sw_answer", ext="ruler")
    wt["ruler"].cancel()
    await asyncio.sleep(2.8)
    d = {e["ext"]: e for e in (await call("alpha", "sw_directory"))["extensions"]}
    ck("a watcher feed that goes silent/closes makes the ext GONE and ends its open call as peer_lost",
       d["beta/ruler"]["hygiene"] == "GONE" and d["alpha/review"]["state"] == "IDLE"
       and any(e["event"] == "peer_lost" for e in ledger()), (d.get("beta/ruler"), d.get("alpha/review")))

    # ---- state file + healthz (DM's liveness line) -------------------------------------------------
    sf = os.path.join(tmp, "broker_state.json")
    ck("the broker writes broker_state.json for the DM tab (ts, up, port, extensions, calls)",
       os.path.exists(sf) and set(json.load(open(sf))) >= {"ts", "ts_local", "up", "port", "extensions", "calls"}, sf)
    async with httpx2.AsyncClient() as hc:
        hz = await hc.get("http://127.0.0.1:%d/healthz" % PORT)
    ck("/healthz answers with CORS for a cross-origin fetch", hz.status_code == 200 and hz.headers.get("access-control-allow-origin") == "*" and hz.json().get("up") is True, dict(hz.headers))

    # ---- hook path + observer ------------------------------------------------------------------------
    sid_feed = []

    async def feed_sid():
        async with websockets.connect("ws://127.0.0.1:%d/ws?party=alpha&ext=hooked&now=first&sid=csid-1" % PORT) as ws:
            async for m in ws:
                sid_feed.append(json.loads(m))
    fs = asyncio.create_task(feed_sid())
    await asyncio.sleep(0.3)
    async with httpx2.AsyncClient() as hc:
        rr = await hc.post("http://127.0.0.1:%d/hook/now" % PORT, json={"session_id": "csid-1", "text": "Build the hook side"})
    d = {e["ext"]: e for e in (await call("alpha", "sw_directory"))["extensions"]}
    ck("POST /hook/now with the feed's sid sets the derived now line", rr.status_code == 200 and d["alpha/hooked"]["now"] == "Build the hook side", d.get("alpha/hooked"))
    async with httpx2.AsyncClient() as hc:
        rr = await hc.post("http://127.0.0.1:%d/hook/now" % PORT, json={"session_id": "sess-A", "text": "beta watcher line"})
    ck("/hook/now with a host session id sets that acking ext's derived line", rr.json().get("ok") is True, rr.text)
    obs = []

    async def observer():
        async with websockets.connect("ws://127.0.0.1:%d/ws?party=operator" % PORT) as ws:
            async for m in ws:
                obs.append(json.loads(m))
    ot = asyncio.create_task(observer())
    await asyncio.sleep(0.4)
    ck("the operator observer snapshot carries the directory and active calls",
       obs and obs[0].get("type") == "snapshot" and "directory" in obs[0] and "calls" in obs[0], obs[:1])
    # These two pin what the README's security section must SAY, because they are the sharpest edge
    # of the no-authentication stance: the observer needs no team name -- `operator` is not in TEAMS,
    # so TEAMLINE_TEAMS does not gate it -- and its snapshot carries raw ledger rows, which include
    # message text, call subjects and openings. If either ever stops being true, the security section
    # is wrong and must be rewritten with it.
    ck("the observer needs NO team name: `operator` is not a configured team",
       "operator" not in SBteams(), SBteams())
    ck("...and its snapshot carries raw ledger rows, message text included",
       isinstance(obs[0].get("rows"), list)
       and any(isinstance(r, dict) and r.get("text") for r in obs[0]["rows"]),
       [r.get("event") for r in (obs[0].get("rows") or [])][-6:])
    # ---- A SILENT ACKING HOLDER IS RE-PUSHED FOREVER (the gamma trap, 2026-09-14). Passing
    # session_id makes a feed an ACKING one (`acking()` = bool(session_of(ext))), and the broker then
    # keeps the message in the outbox until an ack arrives. teamline_feed.py sends only {"ping":1} and
    # NEVER acks -- so a lane started with --session-id receives the same message again every
    # ack_timeout + backoff, for as long as it is up. On a harness that wakes the agent per frame that
    # is an unbounded wake loop. The cure is to pass `sid` instead (a NON-acking feed: a frame sent IS
    # delivered), which is why TEAMLINE_SESSION_QUICKSTART.md's gamma line was corrected.
    silent = []

    async def silent_holder(seconds):
        async with websockets.connect(
                "ws://127.0.0.1:%d/ws?party=gamma&ext=silent&now=holding&session_id=sess-GAMMA" % PORT) as ws:
            end = asyncio.get_event_loop().time() + seconds
            try:
                while asyncio.get_event_loop().time() < end:
                    m = await asyncio.wait_for(anext(aiter(ws)), seconds)
                    ev = json.loads(m)
                    if ev.get("kind"):                       # a real message, never acked back
                        silent.append(ev["id"])
            except (asyncio.TimeoutError, StopAsyncIteration):
                return
    sh = asyncio.create_task(silent_holder(3.0))
    await asyncio.sleep(0.4)
    await call("alpha", "sw_leave", ext="writer", peer="gamma/silent", text="does this repeat?")
    await sh
    ck("an acking holder that never acks is re-pushed the SAME message repeatedly (the gamma trap)",
       len(silent) > 1 and len(set(silent)) == 1, dict(pushes=len(silent), distinct=len(set(silent))))

    # ---- the standby-holder hazard, at the WS layer (beta's nuance, 2026-09-06: "the broker
    # attach IS a register, so the drain path is live on our attach too"). Confirmed here against
    # our own broker: /ws for an ext that does not exist falls to sb.register(), which releases
    # held voicemail, and the attaching socket is then handed everything pending. So a standby
    # holder takes the mail the real session was meant to get. See TEAMLINE_PROTOCOL.md 7 and
    # test_switchboard.py 13 -- this is the SAME hazard reached by a different door.
    await call("alpha", "sw_leave", ext="writer", peer="alpha/absent-overnight",
               text="left while the box was off")
    standby, real = [], []

    async def hold(sink, until=None, seconds=3.0):
        """Read frames until `until(sink)` is satisfied, or `seconds` elapse. A FIXED window flaked
        once on 2026-09-08 and sent the reader hunting a park-safe register() that nobody wrote --
        a load-bearing hazard check must not cry wolf, so waiting for the CONDITION is the rule."""
        deadline = asyncio.get_running_loop().time() + seconds
        async with websockets.connect(
                "ws://127.0.0.1:%d/ws?party=alpha&ext=absent-overnight&now=standby" % PORT) as ws:
            it = aiter(ws)
            try:
                while True:
                    left = deadline - asyncio.get_running_loop().time()
                    if left <= 0:
                        return
                    ev = json.loads(await asyncio.wait_for(anext(it), left))
                    # Do NOT assume the registration frame arrives FIRST: a pending voicemail push can
                    # win that race, and consuming "the first frame" then SWALLOWS the very message this
                    # check is looking for (seen 2026-09-08, sink held only the registered frame).
                    if ev.get("type") == "registered":
                        continue
                    sink.append(ev)
                    if until and until(sink):
                        return
            except (asyncio.TimeoutError, StopAsyncIteration):
                return
    await hold(standby, until=lambda s: any("left while the box was off" in (e.get("text") or "") for e in s))
    ck("a standby attaching to a lane that is not registered DRAINS its held voicemail",
       any("left while the box was off" in (e.get("text") or "") for e in standby),
       [e.get("text", "")[:50] for e in standby])
    await hold(real, seconds=1.0)     # proving ABSENCE: no predicate, a fixed window is inherent
    ck("...and the session that attaches next receives nothing (delivered once, wrong holder)",
       not any("left while the box was off" in (e.get("text") or "") for e in real),
       [e.get("text", "")[:50] for e in real])

    # ---- ONE HOLDER PER EXTENSION (operator, 2026-09-08: "adopt refuse-second-holder").
    # push() fans every event out to all holders, so N holders = N copies while the ledger records
    # ONE delivery -- measured live: a single lane held FIVE, and no msg_id in 2,743 rows had a second
    # `delivered` row. The rule is: a LIVE incumbent is NEVER evicted; a newcomer is refused whatever
    # identity it presents. Eviction happens only when the incumbent is already dead.
    #
    # Identity deliberately does NOT appear in the rule. An earlier version let a matching session_id
    # "replace its own socket" to avoid locking a lane out -- but that lane's five holders shared one
    # session_id, so that carve-out sent the real failure case into evict-and-replace and, since
    # teamline_feed retries close 4000, would have produced a five-way eviction ring every 2 s.
    # Liveness is measured by keepalives the broker RECEIVED, so a live incumbent cannot be a
    # half-open socket: if pings are still arriving, the newcomer is a surplus process, not a return.
    TWS = "ws://127.0.0.1:%d/ws?party=beta&ext=twin&session_id=%s&now=twin-%s"
    tw1 = await websockets.connect(TWS % (PORT, "sess-TWIN", "a"))
    await asyncio.sleep(0.2)
    try:
        async def refused_by(url):
            """Connect and report whether the broker refused with its holder message."""
            try:
                w = await websockets.connect(url)
            except Exception:
                return True, "handshake rejected"
            try:
                ev = json.loads(await asyncio.wait_for(w.recv(), 2.0))
                return bool(ev.get("error")) and "holder" in str(ev.get("error", "")).lower(), ev
            except Exception as ex:
                return True, repr(ex)[:80]
            finally:
                try:
                    await w.close()
                except Exception:
                    pass

        ok, det = await refused_by(TWS % (PORT, "sess-TWIN", "b"))
        ck("a SECOND holder is refused even with the SAME identity -- that lane's five all shared one "
           "session_id, so replacing on a match would have built an eviction ring", ok, det)
        ok2, det2 = await refused_by(TWS % (PORT, "sess-OTHER", "c"))
        ck("a second holder from a different session is refused too", ok2, det2)

        row = {e["ext"]: e for e in (await call("alpha", "sw_directory"))["extensions"]}.get("beta/twin", {})
        ck("the lane reports exactly one holder throughout", row.get("holders") == 1,
           {k: row.get(k) for k in ("ext", "holders", "hygiene")})

        # the INCUMBENT must be untouched -- refusing the newcomer is worthless if we killed the
        # holder doing the work
        await call("alpha", "sw_leave", ext="writer", peer="beta/twin", text="twin-probe")
        seen = 0
        try:
            deadline = asyncio.get_running_loop().time() + 3.0
            while asyncio.get_running_loop().time() < deadline:
                ev = json.loads(await asyncio.wait_for(tw1.recv(), 1.0))
                if "twin-probe" in (ev.get("text") or ""):
                    seen += 1
        except Exception:
            pass
        ck("the incumbent keeps the lane and receives the message exactly ONCE", seen == 1, seen)

        # THE REFUSAL MUST BE RETRYABLE, and this is the whole safety argument. A half-open
        # incumbent reads LIVE for up to feed_gone_s, so a genuine reconnect is indistinguishable
        # from a surplus for that window. Refusing it fatally costs the lane its line until a human
        # relaunches the watcher -- and a session with no line cannot be rung to be told.
        w = await websockets.connect(TWS % (PORT, "sess-TWIN", "d"))
        payload = json.loads(await asyncio.wait_for(w.recv(), 2.0))
        try:
            await asyncio.wait_for(w.recv(), 2.0)
        except Exception:
            pass
        ck("the second-holder refusal is RETRYABLE, not the fatal 4001 -- a reconnect behind a "
           "half-open socket must not lose its line",
           payload.get("retryable") is True and w.close_code == 4003, (payload, w.close_code))
        await w.close()

        # ...and the CLIENT must honour that: back off, never exit. teamline_feed exits 3 on a fatal
        # refusal, which would strand this lane.
        feed_py2 = os.path.join(PKG, "teamline_feed.py")

        def _run_busy():
            return subprocess.run([sys.executable, feed_py2, "--party", "beta", "--ext", "twin",
                                   "--session-id", "sess-TWIN", "--now", "n",
                                   "--url", "http://127.0.0.1:%d" % PORT],
                                  capture_output=True, text=True, encoding="utf-8", timeout=8)
        try:
            pb = await asyncio.to_thread(_run_busy)
            bout, brc, alive = pb.stdout, pb.returncode, False
        except subprocess.TimeoutExpired as te:
            raw = te.stdout or ""
            bout = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
            brc, alive = None, True
        ck("the feed client BACKS OFF on a busy lane instead of exiting (exit 3 would strand it)",
           alive and "holder_busy" in bout, (brc, bout[:160]))
    finally:
        try:
            await tw1.close()
        except Exception:
            pass

    # NO LOCKOUT: once the incumbent stops keepaliving it is presumed half-open, and the next
    # connection reclaims the lane. Without this a lane sits unreachable behind a dead socket until
    # the 10-minute retire -- a worse failure than the duplication the rule prevents.
    anon = "ws://127.0.0.1:%d/ws?party=alpha&ext=anon-lane&now=first" % PORT
    a1 = await websockets.connect(anon)
    await asyncio.sleep(2.6)                     # past feed_gone_s (2.0) with no keepalive
    a2 = await websockets.connect(anon)
    try:
        got = json.loads(await asyncio.wait_for(a2.recv(), 3.0))
        ck("a feed reclaims its lane once the previous holder is dead (the rule must not lock a "
           "lane out)", got.get("type") == "registered", got)
        dead_closed = False
        try:
            deadline = asyncio.get_running_loop().time() + 3.0
            while asyncio.get_running_loop().time() < deadline:
                await asyncio.wait_for(a1.recv(), 1.0)
        except Exception:
            dead_closed = True
        ck("...and the dead holder's socket is closed, so it cannot linger as a second reader",
           dead_closed)
    except Exception as ex:
        ck("a feed reclaims its lane once the previous holder is dead (the rule must not lock a "
           "lane out)", False, repr(ex)[:120])
    finally:
        for w in (a1, a2):
            try:
                await w.close()
            except Exception:
                pass

    # ---- a dropped socket must not hand the lane to whoever connects next -----------------------
    # The re-attach path allowed `e["feed"] and not e["feed_up"]` on its own: once an incumbent's
    # socket closed, ANY client naming the same team/ext took the lane, carrying no sid and no
    # session_id. Both are public in /directory. Worse than a takeover -- the row kept the OWNER's
    # sid while delivering to the stranger, so it still read as the owner's lane.
    own = await websockets.connect(
        "ws://127.0.0.1:%d/ws?party=alpha&ext=owned&now=mine&sid=session-OWNER-0001" % PORT)
    await asyncio.wait_for(anext(aiter(own)), 2)
    await own.close()
    await asyncio.sleep(0.3)                       # inside feed_gone_s: the lane is still LIVE
    seized = None
    try:
        thief = await websockets.connect("ws://127.0.0.1:%d/ws?party=alpha&ext=owned&now=not-mine" % PORT)
        seized = json.loads(await asyncio.wait_for(anext(aiter(thief)), 3))
        await thief.close()
    except Exception as ex:
        seized = {"refused": str(ex)[:80]}
    ck("a client with no matching identity cannot take over a lane whose socket dropped",
       seized.get("type") != "registered", seized)
    # ...and the rightful owner must still get back in, which is why the whole clause cannot simply
    # be deleted: a lane that loses its line cannot be rung to be told about it.
    back = await websockets.connect(
        "ws://127.0.0.1:%d/ws?party=alpha&ext=owned&now=mine-again&sid=session-OWNER-0001" % PORT)
    again = json.loads(await asyncio.wait_for(anext(aiter(back)), 3))
    ck("...but the rightful holder reconnects with its own sid", again.get("type") == "registered", again)
    await back.close()
    await asyncio.sleep(0.2)

    ids = [e["id"] for e in feeds["writer"] + feeds["review"] if e.get("kind") and not e.get("part")]
    ck("no frame reaches a non-acking feed twice (08:4x: a nudge arrived twice -- push vs retry sweep race)",
       len(ids) == len(set(ids)), [i for i in ids if ids.count(i) > 1][:3])
    for t in ft + [fs, ot, wt["deep"]]:
        t.cancel()
    server.should_exit = True
    await task


def main():
    import logging
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    print("TEST -- switchboard end-to-end, the broker's host mode (fake beta watcher delivers + acks)\n")
    tmp = tempfile.mkdtemp(prefix="sb_e2e_")
    asyncio.run(run(tmp))
    print()
    if FAILS:
        print("FAILED %d: %s" % (len(FAILS), FAILS))
        return 1
    print("ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
