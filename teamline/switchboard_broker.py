"""SWITCHBOARD wiring for the broker (inverted delivery): sw_* MCP tools, per-extension feeds,
delivery by the extension's own feed holder, liveness from keepalives. The broker calls no session's host.

Feed contract (frozen 2026-09-03 08:26 with team beta):
  broker -> feed : events {id,to,kind,lane,call_id,text[,part:[i,n]]} (long texts as numbered parts)
  feed -> broker : {"ack": "<msg_id>", "session_id": "...", "accepted": true|false}
                   {"ping": 1, "running": true|false}    keepalive every <= 25 s
                   anything else                          counts as touch
  alpha feeds (Claude Monitor) never ack: a frame sent = delivered (the app shows it).
  beta feeds (their watcher) ack; no ack within ack_timeout_s -> delivery_failed, retry with backoff.
"""
import asyncio
import json
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import switchboard as SB          # noqa: E402

from mcp.server.mcpserver import Context   # noqa: E402

HEADER = "x-teamline-party"
CHUNK = 300     # the Claude app truncates a Monitor frame at roughly 330-500 chars (measured 16:31)
HOLDER_BUSY = 4003   # "another holder is live" -- RETRYABLE. 4001 stays for the unanswerable
HOLDER_RETRY_S = 30  # refusals (unknown team, no ext), which a client must never retry.


def frames(ev, chunk=CHUNK):
    """One event -> one frame, or numbered part frames whose texts concatenate to the original."""
    text = ev.get("text") or ""
    if len(text) <= chunk:
        return [ev]
    parts = [text[i:i + chunk] for i in range(0, len(text), chunk)]
    n = len(parts)
    return [dict(ev, text=f"[{i + 1}/{n}] " + p, part=[i + 1, n]) for i, p in enumerate(parts)]


def wire(mcp, root, loop_ref, ring_timeout_s=90, ack_timeout_s=30, feed_gone_s=90, backoff_s=30,
         state_every_s=10, port=3790, accept_parallel=8):
    sb = SB.Switchboard(ledger_path=os.path.join(root, "switchboard.jsonl"), calls_dir=os.path.join(root, "calls"),
                        require_feed=True, feed_gone_s=feed_gone_s)
    SB.RING_TIMEOUT_S = ring_timeout_s
    feeds = {}                                            # ext -> set(ws)
    awaiting = {}                                         # msg_id -> (sent_at, ext)  (acking feeds only)
    retry_at = {}                                         # msg_id -> earliest re-send
    accept_sem = asyncio.Semaphore(accept_parallel)       # reconnect stagger after a broker/the broker's host restart
    state_path = os.path.join(root, "broker_state.json")
    state = dict(stamp_ok=True, last_error=None)

    def team_of(ctx):
        h = ctx.headers or {}
        p = (h.get(HEADER) or h.get(HEADER.title()) or "").strip().lower()
        if p not in SB.TEAMS:
            # SECURITY (operator, 2026-09-08): say WHAT is wrong, never WHICH teams exist. Listing them
            # hands a caller that guessed wrong the valid names, and the next guess is a disguise.
            raise SB.SwitchError(
                f"team {p!r} is not enabled on this broker. The team comes from the {HEADER} header "
                f"(or ?party= on /ws) and is fixed by your harness config, never chosen by the model -- "
                f"retrying, or trying a variant of the name, cannot work. Ask the operator to have the "
                f"teamline maintainer enable it: a broker change plus a restart of the broker.")
        return p

    def guard(fn):
        try:
            return fn()
        except SB.SwitchError as e:
            return dict(error=str(e))

    def acking(ext):
        """A feed that acks (beta watcher) vs one that cannot (Claude Monitor)."""
        return bool(sb.session_of(ext))

    # ---------------------------------------------------------------- feeds
    async def _send(ws, ev):
        try:
            for fr in frames(ev):
                await ws.send_text(json.dumps(fr, ensure_ascii=False))
            if acking(ev["to"]):
                awaiting[ev["id"]] = (time.time(), ev["to"])
            else:
                sb.mark_delivered(ev["id"], "ws")
        except Exception as e:
            sb.mark_failed(ev["id"], f"ws: {e}")

    pushed_at = {}                                        # msg_id -> last push time (any feed kind)

    def directory_h():
        """The directory with HOLDERS: how many sockets are held for each extension.

        push() below fans every event out to all of them, so holders > 1 means the reader receives
        each message that many times -- while the ledger still records ONE delivery, because delivery
        is ledgered per message and not per socket. Measured in production: one lane held 5 holders,
        and no msg_id in 2,743 ledger rows had a second `delivered` row. The state was therefore
        invisible in every record we keep, and ran through three incidents unnoticed.

        Only the session_id re-attach branch in feed() can accumulate: register() refuses an ext that
        is LIVE, so a non-acking feed (--sid, no session_id) cannot. A watcher relaunched on resume
        without killing its predecessor adds one each time.
        """
        return [dict(e, holders=len(feeds.get(e["ext"], ()))) for e in sb.directory()]

    def push(ev):
        loop = loop_ref.get("loop")
        if not feeds.get(ev["to"]):
            return
        pushed_at[ev["id"]] = time.time()
        for ws in list(feeds.get(ev["to"], ())):
            if loop:
                loop.create_task(_send(ws, ev))
    sb.on_event(push)

    def soon():
        loop = loop_ref.get("loop")
        if loop:
            loop.create_task(_resend_soon())

    async def _resend_soon():
        await asyncio.sleep(0.05)
        resend_pending()

    def resend_pending():
        """Re-push pending events whose retry time has come (no ack / refused ack / feed was down)."""
        now = time.time()
        for ev in sb._outbox:
            if ev["id"] in awaiting or retry_at.get(ev["id"], 0) > now:
                continue
            # a push already in flight (scheduled, not yet marked delivered) must not be doubled:
            # only re-push what was never pushed, or was pushed longer than the ack window ago
            if now - pushed_at.get(ev["id"], 0) < ack_timeout_s:
                continue
            if feeds.get(ev["to"]):
                push(ev)

    def on_frame(ext, raw):
        try:
            m = json.loads(raw)
        except Exception:
            sb.touch(ext)
            return
        if not isinstance(m, dict):
            sb.touch(ext)
            return
        if "ack" in m:
            mid = str(m["ack"])
            # An ack may only settle a message addressed to the lane whose socket it arrived on.
            # Validated against the OUTBOX rather than `awaiting`, because `awaiting` is popped when
            # the ack window expires, and a late-but-genuine ack must still be checkable. Anything
            # else is dropped: a `delivered` row is irreversible, so honouring a foreign ack does not
            # merely mislabel a delivery, it destroys the real recipient's message.
            # Scanned over the outbox directly rather than via pending_for(), which copies every
            # matching entry into a new dict: this runs on EVERY delivery, and the question is a
            # boolean. Reading sb._outbox here matches what resend_pending() and write_state()
            # already do.
            if not any(e["id"] == mid and e["to"] == ext for e in sb._outbox):
                sb.touch(ext)
                return
            awaiting.pop(mid, None)
            if m.get("accepted") is True:
                sb.mark_delivered(mid, str(m.get("session_id") or sb.session_of(ext) or "feed"))
            else:
                sb.mark_failed(mid, f"refused by watcher: {m.get('reason') or 'accepted=false'}")
                retry_at[mid] = time.time() + backoff_s
            return
        if "ping" in m or "running" in m:
            sb.set_running_ext(ext, bool(m.get("running", False)))
            return
        sb.touch(ext)

    async def feed(ws, team, name, now, sid=None, session_id=None):
        """WebSocket /ws?party=<team>&ext=<name>&now=<text>[&sid=][&session_id=]: registers the
        extension (or re-attaches / self-attaches); holding the socket is presence."""
        full = f"{team}/{name}"
        evict = ()
        replacing = False
        async with accept_sem:
            await asyncio.sleep(random.uniform(0, 0.05))
            e = sb._ext.get(full)
            held = list(feeds.get(full) or ())
            if held:
                # ONE HOLDER PER EXTENSION (operator, 2026-09-08: "adopt refuse-second-holder").
                # A LIVE incumbent is never evicted -- evicting one lets N clients form a ring, each
                # replacing the last, which is what would have happened to that lane's five (they share one
                # session_id, so any identity carve-out routes the real failure case into replace).
                #
                # The refusal is RETRYABLE, and that is load-bearing. last_seen can be up to
                # feed_gone_s stale and still read LIVE, so a socket that went half-open is
                # INDISTINGUISHABLE from a live one for that whole window -- roughly 45 client
                # reconnect attempts at the 2 s cadence. Refusing fatally there would cost a lane its
                # line until a human relaunched it, and a session with no line cannot be rung to be
                # told. So a surplus retries harmlessly forever and a genuine reconnect gets in as
                # soon as the incumbent is reaped. Culling surplus watchers is a human job; losing a
                # lane is not recoverable from inside the system, and that asymmetry decides it.
                alive = bool(e) and (time.time() - (e.get("last_seen") or 0)) <= feed_gone_s
                if alive:
                    await ws.accept()
                    await ws.send_text(json.dumps(dict(error=(
                        f"{full} already has a live feed holder, so this one is refused: every message "
                        f"would be delivered once per socket while the ledger recorded a single "
                        f"delivery. This is RETRYABLE -- if you are that lane reconnecting, the "
                        f"incumbent is reaped after {feed_gone_s:.0f}s of silence and your next "
                        f"attempt gets in. If you left a previous watcher running, KILL IT."),
                        retry_s=HOLDER_RETRY_S, retryable=True)))
                    await ws.close(code=HOLDER_BUSY)
                    return
                evict = held
                replacing = True        # take the RE-ATTACH path below, never a fresh register():
                # register() refuses an ext whose hygiene is LIVE, so replacing a dead holder would
                # be rejected with "pick another name" -- the very lockout this escape exists to stop.
            try:
                # WHO MAY TAKE OVER A LANE. Both halves of an extension's name are public in
                # /directory, so "knows the name" proves nothing. A re-attach is allowed only when
                # the caller PROVES the identity the lane was registered with -- or when the lane
                # carries no identity at all, in which case there is nothing to prove and an
                # anonymous lane simply cannot be protected (say so rather than pretending).
                #
                # Dropping the dead-socket clause outright was the alternative and it is worse: a
                # lane whose socket blipped could not reconnect until it was retired, and a session
                # with no line cannot be rung to be told about it.
                bound = (e.get("sid") or e.get("session_id")) if e else None
                mine = bool(e) and ((session_id and e.get("session_id") == session_id)
                                    or (sid and e.get("sid") == sid))
                if e and (mine or (not bound and ((e["feed"] and not e["feed_up"]) or replacing))):
                    e["feed"] = True
                    sb.feed(team, name, True)
                    if now:
                        sb.set_now(team, name, now)
                    if sid:
                        e["sid"] = sid
                else:
                    sb.register(team, name, now=now or "", feed=True, sid=sid or None, session_id=session_id or None)
            except SB.SwitchError as ex:
                await ws.accept()
                await ws.send_text(json.dumps(dict(error=str(ex))))
                await ws.close(code=4001)
                return
            await ws.accept()
        feeds.setdefault(full, set()).add(ws)
        for old_ws in evict:                    # a reconnect replaces its own socket
            feeds[full].discard(old_ws)         # drop first so holders is correct immediately
            try:
                await old_ws.close(code=4000)
            except Exception:
                pass
        await ws.send_text(json.dumps(dict(type="registered", ext=full, directory=directory_h(),
                                           timeouts=dict(ack_s=ack_timeout_s, gone_s=feed_gone_s)), ensure_ascii=False))
        for ev in sb.pending_for(full):
            await _send(ws, ev)
        try:
            while True:
                raw = await ws.receive_text()
                on_frame(full, raw)
        except Exception:
            pass
        finally:
            feeds[full].discard(ws)
            if not feeds[full]:
                try:
                    sb.feed(team, name, False)
                except SB.SwitchError:
                    pass

    # ---------------------------------------------------------------- MCP tools
    @mcp.tool()
    def sw_register(ext: str, now: str, session_id: str = "", ctx: Context = None) -> dict:
        """Register THIS session as an extension WITHOUT holding a feed: ext = short name (the team prefix is added), now = what you are doing right now, session_id = the id your harness knows this session by, and it is REQUIRED here because nothing else can say where to deliver. The lane is UNREACHABLE until something holds a feed for it, so callers get voicemail rather than a ring. If your session can hold a socket, do that instead -- holding it IS the registration. Returns the directory."""
        def _do():
            t = team_of(ctx)                       # inside the guard: an unknown team must be a NAMED error,
            #                                        not an opaque "Error executing tool sw_register"
            #
            # NO TEAM IS PRIVILEGED BY ITS NAME. This once read `feed=(t == "<a literal team name>"
            # and not session_id)`, which meant renaming your teams changed what this tool did. And
            # the branch it enabled was wrong anyway: claiming a feed the caller does not hold
            # produced a lane advertised as LIVE with zero holders -- answerable-looking, and unable
            # to receive anything. Holding a socket is the registration; this tool never asserts one
            # on your behalf.
            return sb.register(t, ext, now=now, session_id=session_id or None, feed=False)
        return guard(_do)

    @mcp.tool()
    def sw_unregister(ext: str, ctx: Context) -> dict:
        """Leave the directory."""
        return guard(lambda: sb.unregister(team_of(ctx), ext))

    @mcp.tool()
    def sw_now(ext: str, text: str, ctx: Context) -> dict:
        """Refresh your 'now' line (what this session is doing right now, one short sentence, <= 200 chars)."""
        return guard(lambda: sb.set_now(team_of(ctx), ext, text))

    @mcp.tool()
    def sw_directory(ctx: Context) -> dict:
        """Every extension on both sides: state, now + age, busy reason, hygiene, voicemail counts. Copy names from here, never type one from memory."""
        return guard(lambda: (team_of(ctx),
                             dict(extensions=directory_h(), calls=sb.active_calls()))[1])

    @mcp.tool()
    def sw_call(ext: str, peer: str, subject: str, opening: str, ctx: Context) -> dict:
        """Ring a specific extension (peer = 'team/name') from yours (ext = your short name). BUSY/GONE/UNREACHABLE/on-another-call/CAPPED -> no ring; your subject + opening become an addressed voicemail (rule 9)."""
        r = guard(lambda: sb.call(team_of(ctx), ext, peer, subject, opening))
        soon()
        return r

    @mcp.tool()
    def sw_answer(ext: str, receipt: str = "received, working on it", ctx: Context = None) -> dict:
        """Answer the ring on your extension IMMEDIATELY; the broker sends the caller your receipt line with it (rule 7). Then work, short sw_say lines while you do, then the real answer, then sw_hangup."""
        r = guard(lambda: sb.answer(team_of(ctx), ext, receipt)); soon(); return r

    @mcp.tool()
    def sw_decline(ext: str, reason: str, ctx: Context) -> dict:
        """Decline the ring with a reason."""
        r = guard(lambda: sb.decline(team_of(ctx), ext, reason)); soon(); return r

    @mcp.tool()
    def sw_say(ext: str, text: str, ctx: Context) -> dict:
        """One line into your active call. Short 'still working' lines are welcome: 5 min of silence nudges both sides."""
        r = guard(lambda: sb.say(team_of(ctx), ext, text)); soon(); return r

    @mcp.tool()
    async def sw_wait(ext: str, timeout_s: int, ctx: Context) -> dict:
        """Hold the line on your extension up to timeout_s (max 110): returns what arrived."""
        try:
            t = team_of(ctx)
            got = await sb.wait(t, ext, max(1, min(int(timeout_s), 110)))
        except SB.SwitchError as e:
            return dict(error=str(e))
        return dict(received=got, timeout=not got, state=sb.entry(t, ext)["state"])

    @mcp.tool()
    def sw_hangup(ext: str, summary: str, ctx: Context) -> dict:
        """End your call the moment your answer is delivered; the summary heads the transcript (rule 5)."""
        r = guard(lambda: sb.hangup(team_of(ctx), ext, summary)); soon(); return r

    @mcp.tool()
    def sw_busy(ext: str, on: bool, kind: str = "other", reason: str = "", ctx: Context = None) -> dict:
        """Do-not-disturb on your extension: kind in walk|review|commit|away|other + free-text reason."""
        return guard(lambda: sb.busy(team_of(ctx), ext, on, kind, reason))

    @mcp.tool()
    def sw_leave(ext: str, peer: str, text: str, ctx: Context) -> dict:
        """Voicemail for a specific extension; delivered once, when it is next IDLE."""
        r = guard(lambda: sb.leave(team_of(ctx), ext, peer, text)); soon(); return r

    @mcp.tool()
    def sw_log(call_id: str, ctx: Context) -> str:
        """Transcript of a call."""
        try:
            return sb.log(call_id)
        except SB.SwitchError as e:
            return f"error: {e}"

    # ---------------------------------------------------------------- loops
    def write_state():
        """broker_state.json for the DM tab's liveness line. If it cannot be written the broker keeps
        serving and stops stamping (DM reads age > 30 s as UNREACHABLE) -- agreed with DM 08:3x."""
        t = time.time()
        doc = dict(ts=t, ts_local=SB._ts_local(t), up=True, port=port, extensions=len(sb._ext),
                   calls=len(sb.active_calls()), pending=len(sb._outbox), awaiting_ack=len(awaiting))
        try:
            SB._write_atomic(state_path, json.dumps(doc, ensure_ascii=False))
            state["stamp_ok"], state["last_error"] = True, None
        except Exception as e:
            state["stamp_ok"], state["last_error"] = False, str(e)
            sys.stderr.write(f"[switchboard] state file: {e}\n")

    def ack_timeouts():
        now = time.time()
        for mid, (sent, ext) in list(awaiting.items()):
            if now - sent > ack_timeout_s:
                awaiting.pop(mid, None)
                sb.mark_failed(mid, f"no ack from {ext}'s watcher in {ack_timeout_s}s")
                retry_at[mid] = now + backoff_s

    async def ticker():
        last_state = 0.0
        while True:
            try:
                sb.tick()
                ack_timeouts()
                resend_pending()
                if time.time() - last_state >= state_every_s:
                    write_state()
                    last_state = time.time()
            except Exception as e:
                sys.stderr.write(f"[switchboard] tick: {e}\n")
            await asyncio.sleep(min(1.0, state_every_s))

    async def hook_now(req):
        """POST /hook/now {session_id, text}: the harness prompt hook's derived now line."""
        from starlette.responses import JSONResponse
        try:
            body = await req.json()
            r = sb.set_now_derived_by_sid(str(body.get("session_id", "")), str(body.get("text", ""))[:SB.NOW_MAX])
            return JSONResponse(dict(ok=True, ext=r["ext"], now=r["now"]))
        except SB.SwitchError as e:
            return JSONResponse(dict(ok=False, error=str(e)))
        except Exception as e:
            return JSONResponse(dict(ok=False, error=f"bad request: {e}"))

    async def directory_json(_req):
        """GET /directory: the switchboard directory as JSON (hooks, scripts, the operator)."""
        from starlette.responses import JSONResponse
        return JSONResponse(dict(extensions=directory_h(), calls=sb.active_calls()))

    async def healthz(_req):
        """GET /healthz: tiny, CORS-open (a cross-origin page may fetch it); nothing else is CORS-open."""
        from starlette.responses import JSONResponse
        return JSONResponse(dict(up=True, ts=time.time(), extensions=len(sb._ext), calls=len(sb.active_calls()),
                                 state_file=state["stamp_ok"]),
                            headers={"Access-Control-Allow-Origin": "*", "Cache-Control": "no-store"})

    def snapshot_extra():
        return dict(directory=directory_h(), calls=sb.active_calls(),
                    transcripts={c["call_id"]: sb.log(c["call_id"]) for c in sb.active_calls()})

    return dict(sb=sb, feed=feed, ticker=ticker, snapshot_extra=snapshot_extra, hook_now=hook_now,
                directory_json=directory_json, healthz=healthz, write_state=write_state)
