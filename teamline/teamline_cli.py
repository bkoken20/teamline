"""TEAMLINE CLI -- the phone for a session WITHOUT the MCP tools (any existing session, either team).
    python teamline_cli.py <tool> [json-args]      party = env TEAMLINE_PARTY (default alpha)
Beta: copy this file into your own tree and set TEAMLINE_PARTY=beta (or edit the default).
Inbound reaches a session through whatever its harness uses to deliver, once it holds a feed;
a session holds one by running teamline_feed.py (a feed names its extension:
/ws?party=<team>&ext=<name>; a URL with no ext is closed on sight). docs/ONBOARDING.md
"""
import asyncio, json, logging, os, sys, httpx2
from mcp.client import Client
from mcp.client.streamable_http import streamable_http_client
logging.getLogger("httpx2").setLevel(logging.WARNING)
PARTY = os.environ.get("TEAMLINE_PARTY", "alpha").strip().lower()
URL = os.environ.get("TEAMLINE_URL", "http://127.0.0.1:3790").rstrip("/") + "/mcp"   # the broker, wherever it runs
async def call(tool, **args):
    hc = httpx2.AsyncClient(headers={"X-Teamline-Party": PARTY}, timeout=httpx2.Timeout(30, read=130))
    async with hc:
        async with Client(streamable_http_client(URL, http_client=hc)) as s:
            r = await s.call_tool(tool, args)
            txt = "".join(c.text for c in r.content if getattr(c, "text", None))
            try: return json.loads(txt)
            except Exception: return txt
USAGE = """teamline_cli.py -- call a switchboard tool from a shell.

  python teamline_cli.py <tool> [json-args]

  python teamline_cli.py sw_directory
  python teamline_cli.py sw_register '{"ext": "docs-writer", "now": "writing the parser docs"}'
  python teamline_cli.py sw_call '{"ext": "docs-writer", "peer": "beta/deep", "subject": "s", "opening": "o"}'

Run sw_directory first and copy peer names from it. The team comes from TEAMLINE_PARTY (currently
%s) and the broker from TEAMLINE_URL (currently %s). The full tool list is whatever the broker
serves: docs/PROTOCOL.md section 7."""

if __name__ == "__main__":
    # A traceback is this program failing, not this program telling you how to use it. `sys.argv[1]`
    # on its own raised IndexError for the commonest possible mistake: typing the name and pressing
    # return. Exit 2 is the convention for a usage error, and the message goes to stderr so that a
    # pipeline reading stdout gets nothing rather than an error it might parse as a result.
    if len(sys.argv) < 2 or sys.argv[1] in ("-h", "--help", "help"):
        print(USAGE % (PARTY, URL), file=sys.stderr)
        raise SystemExit(0 if len(sys.argv) > 1 else 2)
    tool = sys.argv[1]; args = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    try:
        print(json.dumps(asyncio.run(call(tool, **args)), ensure_ascii=False))
    except BaseException as ex:
        # CLASSIFY, never swallow. A stopped broker answered with 127 lines of ExceptionGroup
        # traceback naming neither the address dialled nor the variable that moves it -- and this is
        # the first command the README tells a reader to type. But a catch-all that reported every
        # failure as "broker unreachable" would be worse than the traceback: a real bug would wear a
        # diagnosis pointing at the network. So the tree is searched for a transport-level failure,
        # and anything else is re-raised untouched, traceback and all.
        #
        # The tree, not the exception: the MCP client raises through an ExceptionGroup, so the
        # ConnectError is a leaf several levels down. __cause__ and __context__ are followed too,
        # with an id set because those links can form a cycle.
        def _transport(e):
            seen, stack = set(), [e]
            while stack:
                cur = stack.pop()
                if cur is None or id(cur) in seen:
                    continue
                seen.add(id(cur))
                if isinstance(cur, (httpx2.TransportError, ConnectionError, OSError)):
                    return cur
                stack.extend(getattr(cur, "exceptions", None) or ())
                stack.append(cur.__cause__)
                stack.append(cur.__context__)
            return None

        hit = _transport(ex)
        if hit is None:
            raise
        # Same two facts the feed client reports, in the same order: what was dialled, what moves it.
        print("cannot reach the TEAMLINE broker at %s -- %s: %s"
              % (URL, type(hit).__name__, str(hit)[:120]), file=sys.stderr)
        print("Nothing is answering there. Check the broker is up and that this client is dialling "
              "the right place: TEAMLINE_URL sets it (currently %s). TEAMLINE_PORT moves the BROKER, "
              "not the client." % (os.environ.get("TEAMLINE_URL") or "unset, so the default"),
              file=sys.stderr)
        raise SystemExit(3)
