"""TEAMLINE CLI -- the phone for a session WITHOUT the MCP tools (any existing session, either team).
    python teamline_cli.py <tool> [json-args]      party = env TEAMLINE_PARTY (default alpha)
Beta: copy this file into your own tree and set TEAMLINE_PARTY=beta (or edit the default).
Inbound reaches a session through whatever its harness uses to deliver, once it holds a feed;
a Claude session holds Monitor(ws://127.0.0.1:3790/ws?party=<party>). docs/ONBOARDING.md
"""
import asyncio, json, logging, os, sys, httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
logging.getLogger("httpx2").setLevel(logging.WARNING)
PARTY = os.environ.get("TEAMLINE_PARTY", "alpha").strip().lower()
URL = os.environ.get("TEAMLINE_URL", "http://127.0.0.1:3790").rstrip("/") + "/mcp"   # the the broker's host broker since 2026-09-03 09:0x
async def call(tool, **args):
    hc = httpx2.AsyncClient(headers={"X-Teamline-Party": PARTY}, timeout=httpx2.Timeout(30, read=130))
    async with hc:
        async with Client(streamable_http_client(URL, http_client=hc)) as s:
            r = await s.call_tool(tool, args)
            txt = "".join(c.text for c in r.content if getattr(c, "text", None))
            try: return json.loads(txt)
            except Exception: return txt
if __name__ == "__main__":
    tool = sys.argv[1]; args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    print(json.dumps(asyncio.run(call(tool, **args)), ensure_ascii=False))
