"""TEAMLINE broker — one loopback process, the SWITCHBOARD, phone semantics between named sessions.

    python teamline/teamline_broker.py            # 127.0.0.1:3790

Surfaces:
  /mcp            MCP streamable-http. The TEAM IS THE CONNECTION: header `X-Teamline-Party:
                  alpha|beta` set in each harness's MCP config, never chosen by the model.
                  Tools: sw_* (switchboard_broker.py). Every message names ONE extension.
  /ws?party=<team>&ext=<name>&now=<text>[&sid=<harness session id>][&session_id=<the host id>]
                  a session's feed; holding it is presence; registers the extension. A feed
                  WITHOUT ext is refused (the team-level line was retired 2026-09-02 18:3x,
                  operator: every message must be addressed to one session).
  /ws?party=operator   observer feed for the page (snapshot, then every ledger row).
  /directory      JSON.   /hook/now  POST {session_id, text} (the prompt hook's derived now-line).
  /               operator page (teamline_page.html, read per request).   /operator/say  POST.
Contract: docs/PROTOCOL.md (v2 + etiquette). Tests: test_switchboard.py, test_switchboard_e2e.py.
"""
import asyncio
import contextlib
import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import switchboard_broker as SWB   # noqa: E402

from mcp.server.mcpserver import MCPServer                    # noqa: E402
from starlette.applications import Starlette                  # noqa: E402
from starlette.responses import HTMLResponse, JSONResponse    # noqa: E402
from starlette.routing import Mount, Route, WebSocketRoute    # noqa: E402

ROOT = os.environ.get("TEAMLINE_ROOT", "./data")   # a container deployment sets its own
PORT = int(os.environ.get("TEAMLINE_PORT", "3790"))
BIND = os.environ.get("TEAMLINE_BIND", "127.0.0.1")        # 0.0.0.0 inside a container
PAGE_FILE = os.environ.get("TEAMLINE_PAGE", os.path.join(os.path.dirname(os.path.abspath(__file__)), "teamline_page.html"))   # in a container: mount this file read-only and page tweaks need no rebuild
TEAMS = SWB.SB.TEAMS        # never a second copy: adding a team must be ONE edit (switchboard.TEAMS)
OPERATOR = "operator"


def build(root=ROOT, ring_timeout_s=90, ack_timeout_s=30, feed_gone_s=90, state_every_s=10, backoff_s=30):
    loop_ref = {}
    observers = set()

    mcp = MCPServer("teamline", instructions=(
        # This is the FIRST thing a connecting client shows its agent, before any documentation and
        # before any tool call, so it must agree with the rest of the system. It used to open with
        # "sw_register first", which is the opposite of how registration works -- an agent following
        # its own tools' advice ended up with a lane nothing could deliver to. It also named one
        # particular team, which means nothing to anyone whose teams are named otherwise.
        "TEAMLINE switchboard: one named extension per session, addressed as team/name. "
        "YOU REGISTER BY HOLDING A FEED -- a long-lived WebSocket on /ws -- and holding it IS the "
        "registration; sw_register is only for a session that cannot hold one, and its lane stays "
        "unreachable until something holds a feed for it. "
        "sw_directory lists every extension with its 'now' line -- copy peer names from it, never type "
        "one from memory; sw_call(ext=yours, peer='team/name', subject, opening). "
        "Etiquette: answer IMMEDIATELY, short still-working lines, the answer, sw_busy before long "
        "work, HANG UP with a summary at once."))
    sw = SWB.wire(mcp, root, loop_ref, ring_timeout_s=ring_timeout_s, ack_timeout_s=ack_timeout_s,
                  feed_gone_s=feed_gone_s, state_every_s=state_every_s, backoff_s=backoff_s, port=PORT)
    sb = sw["sb"]

    # ---------------------------------------------------------------- observers (operator page)
    def snapshot():
        return dict(type="snapshot", rows=sb.ledger_rows()[-200:], **sw["snapshot_extra"]())

    async def _send_raw(ws, msg):
        try:
            await ws.send_text(msg)
        except Exception:
            observers.discard(ws)

    def broadcast_row(row):
        loop = loop_ref.get("loop")
        if not loop or not observers:
            return
        msg = json.dumps(dict(type="row", row=row, **sw["snapshot_extra"]()), ensure_ascii=False)
        for ws in list(observers):
            loop.create_task(_send_raw(ws, msg))
    sb.on_row(broadcast_row)

    # ---------------------------------------------------------------- WS
    async def ws_feed(ws):
        party = ws.query_params.get("party", "")
        ext = ws.query_params.get("ext")
        if party == OPERATOR:
            await ws.accept()
            await _send_raw(ws, json.dumps(snapshot(), ensure_ascii=False))
            observers.add(ws)
            try:
                while True:
                    await ws.receive_text()
            except Exception:
                pass
            finally:
                observers.discard(ws)
            return
        if party in TEAMS and ext:
            await sw["feed"](ws, party, ext, ws.query_params.get("now", ""), ws.query_params.get("sid"),
                             ws.query_params.get("session_id"))
            return
        await ws.close(code=4000)          # no team-level address: a feed names its extension

    async def page(_req):
        with open(PAGE_FILE, encoding="utf-8") as fh:
            return HTMLResponse(fh.read())

    async def operator_say(req):
        form = await req.form()
        text = (form.get("text") or "").strip()
        call_id = (form.get("call_id") or "").strip()
        if not text or not call_id:
            return JSONResponse(dict(ok=False, error="pick a call and type a line"))
        try:
            r = sb.operator_say(call_id, text)
            return JSONResponse(dict(ok=True, **r))
        except SWB.SB.SwitchError as e:
            return JSONResponse(dict(ok=False, error=str(e)))

    # The transport's DNS-rebinding guard accepts only Host: 127.0.0.1, so on any deployment that is
    # not pure loopback every client is refused with "Invalid Host header" -- including the shipped
    # compose file, which binds 0.0.0.0 inside its container. It is DISABLED here, and that is a
    # security trade rather than a detail: see the README's security section, which says what it
    # costs and when you should turn it back on.
    from mcp.server.transport_security import TransportSecuritySettings
    mcp_app = mcp.streamable_http_app(streamable_http_path="/mcp", json_response=True, stateless_http=True,
                                      transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False))

    @contextlib.asynccontextmanager
    async def lifespan(app):
        loop_ref["loop"] = asyncio.get_running_loop()
        sw["write_state"]()
        t = asyncio.create_task(sw["ticker"]())
        async with mcp_app.router.lifespan_context(mcp_app):
            try:
                yield
            finally:
                t.cancel()

    app = Starlette(routes=[WebSocketRoute("/ws", ws_feed), Route("/", page),
                            Route("/operator/say", operator_say, methods=["POST"]),
                            Route("/hook/now", sw["hook_now"], methods=["POST"]),
                            Route("/directory", sw["directory_json"]), Route("/healthz", sw["healthz"]),
                            Mount("/", app=mcp_app)],
                    lifespan=lifespan)
    app.state.sb = sb
    return app


def main():
    import uvicorn
    os.makedirs(ROOT, exist_ok=True)
    uvicorn.run(build(), host=BIND, port=PORT, log_level="warning")


if __name__ == "__main__":
    main()
