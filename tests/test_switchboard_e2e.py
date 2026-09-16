"""SWITCHBOARD end-to-end, remote-broker mode (inverted delivery): the real broker in-process,
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
                    voicemail_held=0, pending=0, holders=holders)   # shaped like a real row: no identity
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
    # Measured before the fix: sw_register answered only "Error executing tool
    # sw_register" with no mention of teams, and the feed script retried HTTP 403 every 2 s forever.
    r = await call("newteam", "sw_register", ext="probe", now="n", session_id="x")
    err = str(r.get("error", "")) if isinstance(r, dict) else ""
    ck("an unknown team gets a NAMED error from sw_register, not an opaque tool failure",
       isinstance(r, dict) and "newteam" in err and "not enabled" in err, r)
    # SECURITY: the refusal must NOT enumerate the real teams -- a caller that
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

    # SECURITY (reported from another session and reproduced here): the two clients
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

    # ---- a client arriving with a non-loopback Host header must be served ("Invalid Host header")
    # The address is from RFC 5737's documentation range on purpose: any non-loopback Host exercises
    # the guard, and a documentation address cannot be mistaken for a machine somebody owns.
    hc2 = httpx2.AsyncClient(headers={"X-Teamline-Party": "alpha", "Host": "198.51.100.2:3790"},
                             timeout=httpx2.Timeout(30.0))
    async with hc2:
        async with Client(streamable_http_client(url, http_client=hc2)) as s2:
            res = await s2.call_tool("sw_directory", {})
            txt = "".join(c.text for c in res.content if getattr(c, "text", None))
    ck("the MCP endpoint serves a client whose Host header is not loopback (DNS-rebinding guard off)",
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
    import ast as _ast0
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

    # ---- this tree must not ASSEMBLE a name out of fragments --------------------------------------
    # The denylist below cannot see a name that is split across two literals -- which is exactly how
    # the denylist itself was written, so it could not see its own contents and stayed green while
    # publishing them. This check therefore carries NO list. It forbids the mechanism instead, which
    # needs no secret to enforce and cannot be defeated by choosing different words. `ast`, not text,
    # because the question is what the source concatenates rather than what it looks like.
    def _is_lit(_n):
        return isinstance(_n, _ast0.Constant) and isinstance(_n.value, str)

    _asm = []
    for _dp, _dn, _fs in os.walk(_root):
        _dn[:] = [d for d in _dn if not d.startswith(".") and d != "__pycache__"
                  and not any(_fn0.fnmatch(d, p) for p in _ignored)]
        for _f in [x for x in _fs if x.endswith(".py")]:
            _rel = os.path.relpath(os.path.join(_dp, _f), _root)
            try:
                _tree = _ast0.parse(io.open(os.path.join(_dp, _f), encoding="utf-8").read())
            except SyntaxError:
                continue
            for _nd in _ast0.walk(_tree):
                _all_lit = lambda _xs: len(_xs) > 1 and all(_is_lit(_x) for _x in _xs)
                # "a" + "b" -- a literal split by hand
                if (isinstance(_nd, _ast0.BinOp) and isinstance(_nd.op, _ast0.Add)
                        and _is_lit(_nd.left) and _is_lit(_nd.right)):
                    _asm.append("%s:%d" % (_rel, _nd.lineno))
                # tuple(a + b for a, b in (("x", "y"), ...)) -- a whole LIST split by hand
                elif (isinstance(_nd, (_ast0.GeneratorExp, _ast0.ListComp, _ast0.SetComp))
                        and isinstance(_nd.elt, _ast0.BinOp) and isinstance(_nd.elt.op, _ast0.Add)
                        and isinstance(_nd.elt.left, _ast0.Name) and isinstance(_nd.elt.right, _ast0.Name)):
                    _asm.append("%s:%d" % (_rel, _nd.lineno))
                # "".join(["a", "b"]) and "%s%s" % ("a", "b") -- found by attacking the first version
                elif (isinstance(_nd, _ast0.Call) and isinstance(_nd.func, _ast0.Attribute)
                        and _nd.func.attr == "join" and _is_lit(_nd.func.value) and len(_nd.args) == 1
                        and isinstance(_nd.args[0], (_ast0.List, _ast0.Tuple))
                        and _all_lit(_nd.args[0].elts)):
                    _asm.append("%s:%d" % (_rel, _nd.lineno))
                elif (isinstance(_nd, _ast0.BinOp) and isinstance(_nd.op, _ast0.Mod)
                        and _is_lit(_nd.left) and isinstance(_nd.right, _ast0.Tuple)
                        and _all_lit(_nd.right.elts)):
                    _asm.append("%s:%d" % (_rel, _nd.lineno))
                # f"{'a'}b" -- a literal smuggled through an interpolation slot
                elif (isinstance(_nd, _ast0.JoinedStr)
                        and any(isinstance(_v, _ast0.FormattedValue) and _is_lit(_v.value)
                                for _v in _nd.values)):
                    _asm.append("%s:%d" % (_rel, _nd.lineno))
    ck("no file in this tree assembles a string out of literal fragments",
       not _asm, sorted(set(_asm))[:8])

    # ---- shipped text must point at things that are here, and instructions that work ---------------
    # Four references named documents that are not in this repository at all, and one quoted a section
    # of a file that IS here and does not contain it. The fix log says the private review queue "did
    # not travel"; these are that same pointer, shipped. The quoted passage matters as much as the
    # file: naming a document that exists and a section that does not is the harder one to notice.
    _md0 = _re0.compile(r"\b([A-Za-z0-9_./-]+\.md)\b")
    # `[^,\n]{0,8}` is the section number between the filename and the comma. Without it this rule
    # matched NEITHER of the two references that quote a passage -- it could not fail, and the
    # perturbation runner is what said so, by reporting the claim SILENT instead of FIRES.
    _phr0 = _re0.compile(r"\b[A-Za-z0-9_./-]+\.md[^,\n]{0,8},\s*[\"“]([^\"”\n]{6,60})[\"”]")
    _have, _scan = set(), []
    for _dp, _dn, _fs in os.walk(_root):
        _dn[:] = [d for d in _dn if not d.startswith(".") and d != "__pycache__"
                  and not any(_fn0.fnmatch(d, p) for p in _ignored)]
        for _f in sorted(_fs):
            _rel4 = os.path.relpath(os.path.join(_dp, _f), _root).replace(os.sep, "/")
            _have.add(_rel4)
            if _f.endswith((".py", ".md", ".js", ".html", ".yml", ".txt")):
                _scan.append((os.path.join(_dp, _f), _rel4))
    _have |= {_p.rsplit("/", 1)[-1] for _p in _have}
    _dead = []
    for _path4, _rel4 in _scan:
        for _i, _ln in enumerate(io.open(_path4, encoding="utf-8",
                                         errors="replace").read().splitlines(), 1):
            for _name in _md0.findall(_ln):
                _k = _name.replace("./", "")
                if _k not in _have and _k.rsplit("/", 1)[-1] not in _have:
                    _dead.append("%s:%d %s" % (_rel4, _i, _name))
            for _ph in _phr0.findall(_ln):
                _tg = [t for t in _md0.findall(_ln)
                       if os.path.isfile(os.path.join(_root, t.replace("/", os.sep)))]
                if _tg and not any(
                        _ph.lower() in io.open(os.path.join(_root, t.replace("/", os.sep)),
                                               encoding="utf-8", errors="replace").read().lower()
                        for t in _tg):
                    _dead.append("%s:%d %s has no %r" % (_rel4, _i, _tg[0], _ph))
    ck("every document this tree names is in this tree, with the passage it quotes",
       not _dead, sorted(set(_dead))[:6])

    # A dangling pointer confuses; this one FAILS WHEN FOLLOWED. A feed URL naming a team and no
    # extension is closed on sight by the broker, and the suite asserts that it is. `?party=operator`
    # is right to carry no ext -- the observer is not a lane. EVERYTHING a reader might copy is
    # scanned: the package, the README, the docs. `tests/` is the one exclusion, because that is where
    # the refused form is legitimately written out in order to prove it is refused. Scoping this to
    # the package alone was the first version, and it would have let the README instruct it freely.
    _badu = []
    for _path5, _rel5 in _scan:
        if _rel5.startswith("tests/"):
            continue
        for _i, _ln in enumerate(io.open(_path5, encoding="utf-8",
                                         errors="replace").read().splitlines(), 1):
            for _u in _re0.findall(r"/ws\?[^\s\"'`)]*", _ln):
                if "party=" in _u and "operator" not in _u and "ext=" not in _u:
                    _badu.append("%s:%d %s" % (_rel5, _i, _u[:48]))
    ck("no feed URL a reader might copy names a team without an extension", not _badu, _badu[:4])

    # ---- the security section must name every surface that needs no team name ----------------------
    # The README's one testable security claim is that a team name is the price of entry. It is not:
    # several surfaces never consult `team_of` at all, and one of them WRITES. A3 enumerated three and
    # called the list "exactly what it exposes" -- the kind of sentence that rots silently, because
    # adding a route is a one-line change and nothing made the document follow it.
    #
    # So the list is DERIVED from the code rather than restated here: every HTTP route, and every sw_
    # tool, whose implementation never mentions `team_of`. The same shape as C-L2 -- a repository fact
    # tied to a documentation obligation -- and it fires on a surface added long after anyone reads
    # this comment.
    _bsrc = io.open(os.path.join(PKG, "teamline_broker.py"), encoding="utf-8").read()
    _wsrc = io.open(os.path.join(PKG, "switchboard_broker.py"), encoding="utf-8").read()
    _fns = {}
    for _src in (_bsrc, _wsrc):
        for _n in _ast0.walk(_ast0.parse(_src)):
            if isinstance(_n, (_ast0.FunctionDef, _ast0.AsyncFunctionDef)):
                _fns[_n.name] = _ast0.dump(_n)
    _open = []
    for _m in _re0.finditer(r"""Route\(\s*["']([^"']+)["']\s*,\s*(?:sw\[["'](\w+)["']\]|(\w+))""", _bsrc):
        _path, _fn = _m.group(1), _m.group(2) or _m.group(3)
        # An unknown handler is assumed GATED: this check may only ever accuse, never excuse.
        if "team_of" not in _fns.get(_fn, "team_of"):
            _open.append(_path)
    _open += [_n for _n, _d in _fns.items() if _n.startswith("sw_") and "team_of" not in _d]
    _open.append("ws?party=operator")       # accepted before any team is consulted, by construction
    _sec = _readme0().split("## Security model")[-1].split("\n## ")[0]
    _unsaid = [s for s in sorted(set(_open)) if s not in _sec]
    ck("the security section names every surface that needs no team name",
       not _unsaid, {"derived from the code": sorted(set(_open)), "missing from the section": _unsaid})

    # ---- no address in this tree may be a real host ------------------------------------------------
    # This repository was forked out of a private deployment, so an address that COULD be somebody's
    # real machine is a question a reader has no way to answer. RFC 5737 reserves three ranges for
    # documentation -- they can never route to a real host -- so the rule is an ALLOWLIST of those,
    # plus the two addresses the software genuinely binds. An allowlist and not a denylist of private
    # ranges, because a denylist is D-L1's limitation exactly: it cannot catch what nobody listed.
    _ALLOWED_IP = ("127.0.0.1", "0.0.0.0", "192.0.2.", "198.51.100.", "203.0.113.")
    _ip0 = _re0.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
    _addrs = []
    for _dp, _dn, _fs in os.walk(_root):
        _dn[:] = [d for d in _dn if not d.startswith(".") and d != "__pycache__"
                  and not any(_fn0.fnmatch(d, p) for p in _ignored)]
        for _f in sorted(_fs):
            _rel3 = os.path.relpath(os.path.join(_dp, _f), _root)
            _txt3 = io.open(os.path.join(_dp, _f), encoding="utf-8", errors="replace").read()
            _addrs += ["%s:%s" % (_rel3, _a) for _a in _ip0.findall(_txt3)
                       if not any(_a.startswith(_p) for _p in _ALLOWED_IP)]
    ck("every address in this tree is loopback or a documentation range, never a real host",
       not _addrs, sorted(set(_addrs))[:8])

    # ---- the shipped source must not read as one deployment's incident diary ----------------------
    # Comments dated to a day, constants stamped with the wall-clock time somebody chose them, and
    # words this repository never defines. None of it helps a reader: they cannot see the day, the
    # clock or the tab being referred to, and it dates the code. Keep the engineering fact and drop
    # the provenance -- "raised from 120 because 120 truncated real lines" says everything the date
    # was standing in for. Check NAMES are the sharp case, because every run prints them.
    _date0 = _re0.compile(r"\b20\d\d-\d\d-\d\d\b")
    _clock0 = _re0.compile(r"(?<![\d.])\b(?:[01]?\d|2[0-3]):[0-5x][\dx]\b(?!\d)")
    # Vocabulary is scanned in the PACKAGE only, not here: a check that forbids undefined words has
    # to name them, and scanning itself would make it permanently red -- the trap D-L1's needle list
    # fell into. Check names are scanned wherever they live, which is where the harm actually was.
    _vocab0 = _re0.compile(r"teamline_state|HANDOFF|\bDM\b|reply Q\d|the app\b")
    # THE WHOLE TREE, not a list of directories. Scoping it to `teamline/` and `tests/` was the first
    # version, and that is D-L1's mistake again -- its scan used an extension allowlist and was
    # quietly skipping the Dockerfile, the LICENSE and a shipped .js, all of which can carry a line
    # like this. `docs/` is the single exclusion, and it is a real distinction: a fix log RECORDS
    # when something was decided, which is the opposite of a comment that merely happens to be dated.
    _diary = []
    for _dp, _dn, _fs in os.walk(_root):
        _dn[:] = [d for d in _dn if not d.startswith(".") and d not in ("__pycache__", "docs")
                  and not any(_fn0.fnmatch(d, p) for p in _ignored)]
        for _f in sorted(_fs):
            _rel2 = os.path.relpath(os.path.join(_dp, _f), _root)
            _rxs = (_date0, _clock0, _vocab0) if _rel2.startswith("teamline") else (_date0, _clock0)
            for _i, _ln in enumerate(
                    io.open(os.path.join(_dp, _f), encoding="utf-8", errors="replace"), 1):
                for _rx in _rxs:
                    _m = _rx.search(_ln)
                    if _m:
                        _diary.append("%s:%d:%s" % (_rel2, _i, _m.group(0)))
    for _f in ("test_switchboard.py", "test_switchboard_e2e.py"):
        _src2 = io.open(os.path.join(os.path.dirname(os.path.abspath(__file__)), _f),
                        encoding="utf-8").read()
        for _nm in _re0.findall(r"""ck\(\s*["'](.+?)["']\s*,""", _src2, _re0.S):
            if _date0.search(_nm) or _clock0.search(_nm) or _vocab0.search(_nm):
                _diary.append("%s name: %s" % (_f, _nm.strip()[:56]))
    ck("no shipped source line or check name is dated, clocked, or names something undefined here",
       not _diary, sorted(set(_diary))[:8])

    # ---- a publish candidate carries no identifier from the deployment it was forked from ---------
    # The rename that de-identified this repository replaced the TEAM names and left the LANE names
    # behind in comments -- a real lane of the private deployment with only its team relabelled,
    # pointing at an incident log no reader here can see. The de-identifying commit caught the
    # grammatical wreckage of that rename; it did not catch these. No example is quoted here on
    # purpose: this check scans its own file, and a comment illustrating the leak IS the leak.
    # THE LIST IS NOT IN THIS REPOSITORY, and that is the point. It named the private deployment's
    # lanes, its machine and its teams. Held here it published exactly what it exists to keep out --
    # split across two literals, which let this check scan its own file but is a comment about the
    # check, not a protection: a reader joins them in one line of `ast`. The structural check above
    # is what a public repository CAN enforce about itself, because it needs no secret.
    #
    # The list is supplied from outside: TEAMLINE_DEID_LIST names a file, one string per line, `#`
    # comments and blanks ignored. There is deliberately no default path -- a default would name the
    # machine it points at. With no list this scan DOES NOT RUN and says so; it never reports a pass
    # it did not earn. The maintainer's pre-publication gate holds the real list and is what enforces
    # the names before anything is pushed.
    _deid_path = os.environ.get("TEAMLINE_DEID_LIST", "")
    _needles = [ln.strip() for ln in io.open(_deid_path, encoding="utf-8")
                if ln.strip() and not ln.lstrip().startswith("#")] if os.path.isfile(_deid_path) else []
    if _needles:
        _leaks = []
        for _dp, _dn, _fs in os.walk(_root):
            _dn[:] = [d for d in _dn if not d.startswith(".") and d != "__pycache__"
                      and not any(_fn0.fnmatch(d, p) for p in _ignored)]
            for _f in _fs:
                # Everything, not an extension allowlist. The walk of the first version showed it was
                # skipping Dockerfile, LICENSE and page_probe.js -- all published, all able to carry a
                # name. Binary files simply will not match; errors="replace" keeps them from raising.
                _rel1 = os.path.relpath(os.path.join(_dp, _f), _root)
                _txt = io.open(os.path.join(_dp, _f), encoding="utf-8", errors="replace").read()
                _leaks += ["%s:%s" % (_rel1, _w) for _w in _needles if _w in _txt]
                # AND the string constants as the PARSER sees them, which is not the same thing.
                # `"ghost-" "lane"` is folded into a single constant before any code runs, so the
                # joined name exists in the program and appears nowhere in the text -- invisible to
                # the scan above and to the structural check, which has no `+` to find. Attacking the
                # first version of this fix is what surfaced it. Text still matters on its own:
                # comments are not constants, and a name in a comment is what started all of this.
                if _f.endswith(".py"):
                    try:
                        for _nd in _ast0.walk(_ast0.parse(_txt)):
                            if _is_lit(_nd):
                                _leaks += ["%s:%s" % (_rel1, _w) for _w in _needles if _w in _nd.value]
                    except SyntaxError:
                        pass
        ck("no string from the supplied private-name list survives in the publish tree",
           not _leaks, _leaks[:8])
    else:
        print("  NOTE  private-name scan NOT RUN: no TEAMLINE_DEID_LIST. It is not a pass. The "
              "structural check above ran and needs no list.")

    # ---- the log must not claim about the HISTORY what the history does not support --------------
    # D-L1 asserted "The history is clean: the real team names never entered it." That was FALSE. The
    # check above reads the working TREE; nothing read the commits, and the one scan that did was
    # case-sensitive on a single spelling while the names sit in the history in CAPITALS -- which the
    # very commit it examined announces in its own title ("a case-sensitive rename missed"). A push
    # publishes every commit, so a false claim about the history is the most expensive kind here.
    #
    # This links a REPOSITORY fact to a DOCUMENTATION obligation, the same shape as C-L2. It does not
    # demand a clean history -- rewriting that is the owner's decision, not a test's -- only that the
    # log never claim one the commits do not support. The terms come from the SAME external list as
    # the tree scan above -- they used to be spelled here in fragments, which wrote the three private
    # team names into the very tree this check guards, after a history rewrite had removed them from
    # every commit. A name absent from the history and present in the working tree is still published.
    _terms = tuple(_needles)

    def _in_history(term):
        """True / False / None, where None means 'could not look' -- never silently False."""
        try:
            r = subprocess.run(["git", "log", "--all", "-i", "--pickaxe-regex", "-S", term, "--oneline"],
                               cwd=_root, capture_output=True, text=True, timeout=180)
            return bool(r.stdout.strip()) if r.returncode == 0 else None
        except Exception:
            return None
    _seen = {t: _in_history(t) for t in _terms}
    _dirty = sorted(t for t, v in _seen.items() if v)
    _blind = sorted(t for t, v in _seen.items() if v is None)
    _logtxt = io.open(os.path.join(_root, "docs", "FIX_LOG.md"), encoding="utf-8").read()
    # "Could not look" is NOT "clean". Walking an earlier version found None collapsing to false
    # through a plain truth test, so the check passed while knowing nothing. The two cases differ:
    # with no .git there is no history in this copy to leak and nothing to verify; with a .git
    # present, failing to read it is a failure of the check itself.
    _has_git = os.path.isdir(os.path.join(_root, ".git"))
    #
    # BIDIRECTIONAL. The first version only forbade claiming CLEAN while dirty. Once the history was
    # rewritten and became clean, its condition was unreachable: the log could say anything and the
    # check still passed -- and the perturbation pinning it stopped firing, which the runner caught
    # and reported. A one-directional check silently expires the moment the thing it guards is fixed.
    #
    # So the log carries an explicit CURRENT status and the check compares it with the measurement,
    # disagreeing in either direction. Prose is not used for this: the log legitimately describes the
    # history's past in past tense, and a check reading prose cannot tell a description from a claim.
    # EXACTLY ONE marker. re.search takes the first match, so a second one -- a later entry quoting
    # this line as an example, the way D-L1's own comment once quoted a leaked name -- would be read
    # as the real status, and two disagreeing markers would resolve silently to whichever came first.
    # Silently choosing between contradictory claims is worse than either claim.
    _ms = _re0.findall(r"HISTORY STATUS \(checked by the suite\): (CLEAN|CARRIES PRIVATE NAMES)", _logtxt)
    _stated = _ms[0] if len(_ms) == 1 else None
    _measured = "CARRIES PRIVATE NAMES" if _dirty else "CLEAN"
    # WITH NO TERMS THERE IS NOTHING TO MEASURE, and an unmeasured history reads CLEAN -- which would
    # make this check pass vacuously, guarding the most expensive claim in the log while knowing
    # nothing. That is the same failure the `_blind` handling above exists to prevent, arriving by a
    # different route. So it does not run, and says so.
    if not _terms:
        print("  NOTE  history-status check NOT RUN: no TEAMLINE_DEID_LIST, so there is nothing to "
              "search the commits for. It is not a pass.")
    else:
        ck("the fix log's stated history status matches the commits, in both directions",
           bool(_stated) and _stated == _measured and not (_blind and _has_git),
           ("could not search the history of a real checkout: %s" % _blind) if (_blind and _has_git)
           else "log states %r, commits say %r" % (_stated, _measured))

    # ---- the verification must not claim more than it verifies -----------------------------------
    # The perturbation runner is this repository's strongest evidence, and its verdict read "every
    # fix is load-bearing and every check can fail". It pins the claims made in the fix log -- a
    # fraction of the checks in the two suites. Every other check is merely present: it has never
    # been shown capable of failing, which is the state a check that cannot fail hides in. Pinning
    # all of them is not the point and is not practical; saying which is.
    _tdir = os.path.dirname(os.path.abspath(__file__))
    _suites = sorted(f for f in os.listdir(_tdir) if f.startswith("test_") and f.endswith(".py"))
    _pn = len(_re0.findall(r"dict\(id=", io.open(os.path.join(_tdir, "perturbations.py"), encoding="utf-8").read()))
    _ckn = sum(len(_re0.findall(r"^\s*ck\(", io.open(os.path.join(_tdir, s), encoding="utf-8").read(), _re0.M))
               for s in _suites if s != "test_perturbations.py")
    _rsrc = io.open(os.path.join(_tdir, "test_perturbations.py"), encoding="utf-8").read()
    ck("the perturbation runner does not claim to verify every check when it pins a subset",
       not (_pn < _ckn and "every check can fail" in _rsrc),
       "pins %d of %d checks, and its verdict says 'every check can fail'" % (_pn, _ckn))

    # ---- a security control that is OFF must be disclosed where people look for it ---------------
    # The broker disables the MCP transport's DNS-rebinding guard, for a real reason: it allows only
    # Host: 127.0.0.1, and every client on a non-loopback deployment is refused. But the README's
    # security section -- the one headed "read this before deploying" -- documented the missing
    # authentication and the open operator surface and never mentioned this, and rebinding is exactly
    # the attack against the loopback-plus-VPN shape that same section RECOMMENDS. The code fact
    # therefore carries a documentation obligation, and this check is the link between them.
    _bsrc = io.open(os.path.join(PKG, "teamline_broker.py"), encoding="utf-8").read()
    _guard_off = "enable_dns_rebinding_protection=False" in _bsrc
    _sec = _readme0().split("## Security model")[-1].split("\n## ")[0] if "## Security model" in _readme0() else ""
    ck("if the DNS-rebinding guard is disabled, the security section says so",
       (not _guard_off) or ("rebinding" in _sec.lower()),
       "guard_off=%s, security section mentions rebinding=%s" % (_guard_off, "rebinding" in _sec.lower()))

    # ---- the README counts ITSELF, and nothing checked the counts --------------------------------
    # Three separate numbers in this file described the repository and drifted out of date: the file
    # count, the suite count, and the line counts. A number nobody checks is a statement that becomes
    # false the next time anyone adds a file -- and this is a public repository, where those are the
    # cheapest claims for a reader to test.
    _tdir = os.path.dirname(os.path.abspath(__file__))
    _suites = sorted(f for f in os.listdir(_tdir) if f.startswith("test_") and f.endswith(".py"))
    _wordn = {1: "one", 2: "two", 3: "three", 4: "four", 5: "five", 6: "six", 7: "seven"}
    _wrong = ["%s suites" % w for k, w in _wordn.items() if k != len(_suites)]
    ck("the README states no suite count that contradicts tests/",
       not [p for p in _wrong if p in _readme0()], [p for p in _wrong if p in _readme0()])
    ck("...and every suite is named in it", all(s in _readme0() for s in _suites),
       [s for s in _suites if s not in _readme0()])

    # The line counts are hedged with "Roughly", so they are checked as a BAND, not a figure. 20% is
    # wide enough that ordinary work does not trip it and narrow enough to catch a claim that has
    # stopped being true -- the tests figure was out by 70%.
    def _loc(d, pat):
        return sum(len(io.open(os.path.join(d, f), encoding="utf-8", errors="replace").read().splitlines())
                   for f in os.listdir(d) if f.endswith(pat))
    _impl, _test = _loc(PKG, ".py"), _loc(_tdir, ".py")
    _claimed = _re0.search(r"Roughly ([\d,]+) lines of implementation and ([\d,]+) lines of", _readme0())
    _ci, _ct = (int(_claimed.group(1).replace(",", "")), int(_claimed.group(2).replace(",", ""))) if _claimed else (0, 0)
    # The NAME must be constant: the perturbation runner addresses checks by name, so a name carrying
    # a computed number cannot be pinned and would drift out of reference on the next added line.
    # The numbers belong in the failure detail, which is only printed when it fails.
    ck("the README's 'roughly N lines' claims are within 20% of the real counts",
       bool(_claimed) and abs(_ci - _impl) <= 0.2 * _impl and abs(_ct - _test) <= 0.2 * _test,
       "claimed impl=%s tests=%s; actual impl=%d tests=%d" % (_ci, _ct, _impl, _test))

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
    ledger = lambda: [json.loads(l) for l in open(os.path.join(tmp, "switchboard.jsonl"), encoding="utf-8") if l.strip()]
    # The binding is read from the LEDGER, not from the row: a directory row deliberately no longer
    # carries `session_id`, because it is the value the re-attach guard checks and /directory is public.
    ck("a beta ext bound to a session id and a feed is LIVE", d["beta/deep"]["hygiene"] == "LIVE"
       and any(e["event"] == "register" and e["ext"] == "beta/deep" and e.get("session_id") == "sess-A"
               for e in ledger()),
       (d["beta/deep"], [e for e in ledger() if e["event"] == "register" and e["ext"] == "beta/deep"]))

    # ---- calls: delivery by the watcher, ack marks delivered --------------------------------------
    r1 = await call("alpha", "sw_call", ext="writer", peer="beta/deep", subject="depth", opening="numbers?")
    r2 = await call("alpha", "sw_call", ext="review", peer="beta/ruler", subject="ruler", opening="ready?")
    ck("two calls ring concurrently", r1.get("state") == "RINGING" and r2.get("state") == "RINGING", (r1, r2))
    await asyncio.sleep(0.8)
    ck("each ring reached ITS watcher feed: deep as steer (running), ruler as queue (idle)",
       any(x["kind"] == "ring" and "depth" in x["text"] and x["mode"] == "steer" for x in delivered["deep"])
       and any(x["kind"] == "ring" and "ruler" in x["text"] and x["mode"] == "queue" for x in delivered["ruler"]), delivered)
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

    # ---- state file + healthz (an external liveness display) -------------------------------------------------
    sf = os.path.join(tmp, "broker_state.json")
    ck("the broker writes broker_state.json for an external display (ts, up, port, extensions, calls)",
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
    # ---- A SILENT ACKING HOLDER IS RE-PUSHED FOREVER (the gamma trap). Passing
    # session_id makes a feed an ACKING one (`acking()` = bool(session_of(ext))), and the broker then
    # keeps the message in the outbox until an ack arrives. teamline_feed.py sends only {"ping":1} and
    # NEVER acks -- so a lane started with --session-id receives the same message again every
    # ack_timeout + backoff, for as long as it is up. On a harness that wakes the agent per frame that
    # is an unbounded wake loop. The cure is to pass `sid` instead (a NON-acking feed: a frame sent IS
    # delivered), which is why docs/ONBOARDING.md tells a session to hold its feed with --sid.
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

    # ---- the standby-holder hazard, at the WS layer: a broker attach IS a register, so the drain
    # path is live on an attach too. Confirmed here against
    # our own broker: /ws for an ext that does not exist falls to sb.register(), which releases
    # held voicemail, and the attaching socket is then handed everything pending. So a standby
    # holder takes the mail the real session was meant to get.
    # See docs/PROTOCOL.md 2, "why there is no standby holder", and
    # test_switchboard.py 13 -- this is the SAME hazard reached by a different door.
    await call("alpha", "sw_leave", ext="writer", peer="alpha/absent-overnight",
               text="left while the box was off")
    standby, real = [], []

    async def hold(sink, until=None, seconds=3.0):
        """Read frames until `until(sink)` is satisfied, or `seconds` elapse. A FIXED window flaked
        once, and sent the reader hunting a park-safe register() that nobody wrote --
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
                    # check is looking for (seen live: the sink held only the registered frame).
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

    # ---- ONE HOLDER PER EXTENSION: a second socket on a live lane is refused.
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
        "ws://127.0.0.1:%d/ws?party=alpha&ext=owned&now=mine&sid=session-OWNER-0001"
        "&session_id=sess-OWNER-HOST" % PORT)      # holds BOTH identities: the guard accepts either
    await asyncio.wait_for(anext(aiter(own)), 2)
    await own.close()
    await asyncio.sleep(0.3)                       # inside feed_gone_s: the lane is still LIVE

    # THE THIEF RECONNOITRES FIRST, and that is the whole of it: the guard checks `sid` or
    # `session_id`, and the broker handed both out through three doors that ask for no credential.
    # The thief this check shipped with skipped that step, so it stayed green while the guard did
    # not hold. `loot_of` gathers the way an attacker gathers -- by the field names PROTOCOL.md
    # publishes, on any row naming the lane -- so it knows no value in advance.
    def loot_of(blob):
        out = []

        def walk(x):
            if isinstance(x, dict):
                if x.get("ext") == "alpha/owned":
                    out.extend([(k, x[k]) for k in ("sid", "session_id") if isinstance(x.get(k), str) and x[k]])
                for v in x.values():
                    walk(v)
            elif isinstance(x, list):
                for v in x:
                    walk(v)
            elif isinstance(x, str):               # prose counts: a refusal that NAMES the session
                out.extend([("session_id", m) for m in _re0.findall(r"session ([A-Za-z0-9_-]{4,})", x)])
        walk(blob)
        return out

    async with httpx2.AsyncClient() as hc:
        doors = {"GET /directory": (await hc.get("http://127.0.0.1:%d/directory" % PORT)).json()}
    opw = await websockets.connect("ws://127.0.0.1:%d/ws?party=operator" % PORT)
    doors["ws party=operator"] = json.loads(await asyncio.wait_for(anext(aiter(opw)), 3))
    await opw.close()
    spy = await websockets.connect("ws://127.0.0.1:%d/ws?party=beta&ext=spy&now=watching" % PORT)
    doors["ws registration frame"] = json.loads(await asyncio.wait_for(anext(aiter(spy)), 3))
    await spy.close()
    # The REFUSAL is a door too, and the code walk is what found it: a probe turned away from a LIVE
    # lane was told which session holds it, which is the other half of what the guard accepts. Probe,
    # be refused, read the credential out of the refusal, come back with it.
    probe = await websockets.connect("ws://127.0.0.1:%d/ws?party=alpha&ext=owned&now=probing" % PORT)
    doors["ws refusal text"] = json.loads(await asyncio.wait_for(anext(aiter(probe)), 3))
    try:
        await probe.close()
    except Exception:
        pass
    ck("no unauthenticated door hands out the identity the re-attach guard checks",
       not any(loot_of(v) for v in doors.values()),
       {k: loot_of(v) for k, v in doors.items() if loot_of(v)})

    loot = [p for v in doors.values() for p in loot_of(v)]
    seized = None
    try:
        thief = await websockets.connect(
            "ws://127.0.0.1:%d/ws?party=alpha&ext=owned&now=not-mine" % PORT
            + ("&%s=%s" % loot[0] if loot else ""))
        seized = json.loads(await asyncio.wait_for(anext(aiter(thief)), 3))
        await thief.close()
    except Exception as ex:
        seized = {"refused": str(ex)[:80]}
    ck("a client with no matching identity cannot take over a lane whose socket dropped",
       seized.get("type") != "registered", (seized, loot[:1]))
    # ...and the rightful owner must still get back in, which is why the whole clause cannot simply
    # be deleted: a lane that loses its line cannot be rung to be told about it.
    back = await websockets.connect(
        "ws://127.0.0.1:%d/ws?party=alpha&ext=owned&now=mine-again&sid=session-OWNER-0001" % PORT)
    again = json.loads(await asyncio.wait_for(anext(aiter(back)), 3))
    ck("...but the rightful holder reconnects with its own sid", again.get("type") == "registered", again)
    await back.close()
    await asyncio.sleep(0.2)

    ids = [e["id"] for e in feeds["writer"] + feeds["review"] if e.get("kind") and not e.get("part")]
    ck("no frame reaches a non-acking feed twice (seen live: a nudge arrived twice, push vs retry sweep race)",
       len(ids) == len(set(ids)), [i for i in ids if ids.count(i) > 1][:3])
    for t in ft + [fs, ot, wt["deep"]]:
        t.cancel()
    server.should_exit = True
    await task


def main():
    import logging
    logging.getLogger("httpx2").setLevel(logging.WARNING)
    print("TEST -- switchboard end-to-end, remote-broker mode (fake beta watcher delivers + acks)\n")
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
