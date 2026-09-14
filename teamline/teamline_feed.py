"""TEAMLINE feed client for a Claude session -- run under Monitor(command=...).

    python teamline/teamline_feed.py --ext <name> --now "<what you are doing>" --sid <session id>

Holds the extension's feed on the broker (default TEAMLINE_URL, the the broker's host), prints every frame as one
JSON line (each line becomes a notification in the session), reconnects forever (2 s cadence), and
sends a keepalive every 25 s. Exists because the app's Monitor(ws=...) refuses non-loopback private
addresses (09:02 2026-09-03: "address is in a private, link-local, or cloud-metadata range"), so
the socket is held by this process instead. Never raises: a dead feed prints a line and retries.
"""
import argparse
import asyncio
import json
import os
import sys
import time
import urllib.parse

DEFAULT_URL = os.environ.get("TEAMLINE_URL", "http://127.0.0.1:3790")
# The TEAM comes from the same place as the CLI's (teamline_cli.PARTY). Until 2026-09-08 this script
# knew only --party and fell back to "alpha", so a session that exported TEAMLINE_PARTY and started
# its feed registered SILENTLY INTO ALPHA -- no error on either side. --party still wins when given.
DEFAULT_PARTY = os.environ.get("TEAMLINE_PARTY", "alpha").strip().lower()


def ws_url(base, party, ext, now, sid=None, session_id=None):
    q = dict(party=party, ext=ext, now=now or "")
    if sid:
        q["sid"] = sid
    if session_id:
        q["session_id"] = session_id
    base = base.replace("http://", "ws://").replace("https://", "wss://").rstrip("/")
    return base + "/ws?" + urllib.parse.urlencode(q)


REFUSED_EXIT = 3
BUSY_RETRY_S = 30


def _refusal(e):
    """A handshake REFUSAL (HTTP 403 / close 4001) never resolves by retrying: the broker is saying
    this feed may not exist -- unknown team, or no ext. Measured 2026-09-08: the old code retried it
    every 2 s forever and the session simply never registered."""
    status = getattr(getattr(e, "response", None), "status_code", None) or getattr(e, "status_code", None)
    if status == 403 or "HTTP 403" in str(e):
        return "HTTP 403 at the handshake"
    if getattr(e, "code", None) == 4001:
        return "the broker closed with 4001"
    return None


HOLDER_BUSY = 4003


def _busy(e):
    """4003: the lane already has a LIVE holder. RETRYABLE, deliberately -- a half-open incumbent
    reads live for up to feed_gone_s, so this is how a genuine reconnect looks during that window.
    Exiting here would cost the session its line until a human relaunched the feed. Back off instead
    of hammering: if it never clears, a watcher of yours is still running and should be killed."""
    return getattr(e, "code", None) == HOLDER_BUSY


async def hold(url, ping_s=25.0):
    import websockets
    first = True
    while True:
        try:
            async with websockets.connect(url, ping_interval=None, max_size=None) as ws:
                async def pinger():
                    while True:
                        await asyncio.sleep(ping_s)
                        await ws.send(json.dumps({"ping": 1}))
                pt = asyncio.create_task(pinger())
                try:
                    async for m in ws:
                        try:
                            ev = json.loads(m)
                        except Exception:
                            continue
                        if ev.get("type") == "registered":
                            if first:
                                print(json.dumps(dict(type="registered", ext=ev.get("ext"), broker=url.split("/ws?")[0]),
                                                 ensure_ascii=False), flush=True)
                            first = False
                            continue
                        print(json.dumps(ev, ensure_ascii=False), flush=True)
                finally:
                    pt.cancel()
        except Exception as e:
            why = _refusal(e)
            if why:
                print(json.dumps(dict(
                    type="refused", error=why,
                    detail=("the broker REFUSED this feed and retrying cannot help: either your TEAM is not "
                            "enabled on the broker (switchboard.TEAMS), or no --ext was given. STOPPING rather "
                            "than looping. Ask the operator to have the TEAMLINE maintainer enable the team -- it "
                            "is a broker change plus a the broker's host container rebuild, and cannot be done from here."),
                    url=url.split("/ws?")[0], exit=REFUSED_EXIT), ensure_ascii=False), flush=True)
                return REFUSED_EXIT
            elif _busy(e):
                print(json.dumps(dict(type="holder_busy", retry_s=BUSY_RETRY_S, error=(
                    "this lane already has a live feed holder; backing off rather than hammering. "
                    "If this does not clear, a previous watcher of yours is still running -- kill it."
                )), ensure_ascii=False), flush=True)
                await asyncio.sleep(BUSY_RETRY_S)
                continue
            else:
                print(json.dumps(dict(type="feed_down", error=str(e)[:120], retry_s=2)), flush=True)
        await asyncio.sleep(2)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ext", required=True)
    ap.add_argument("--now", default="")
    ap.add_argument("--sid", default="")
    ap.add_argument("--party", default=DEFAULT_PARTY)
    ap.add_argument("--session-id", default="")
    ap.add_argument("--url", default=DEFAULT_URL)
    a = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    rc = asyncio.run(hold(ws_url(a.url, a.party, a.ext, a.now, a.sid or None, a.session_id or None)))
    sys.exit(rc or 0)


if __name__ == "__main__":
    main()
