"""TEAMLINE SWITCHBOARD -- named extensions per session, a directory, concurrent calls, hygiene.

Contract: docs/PROTOCOL.md. Pure state:
every transition is ONE appended ledger row, the ledger is replayed on restart (feed presence
excepted). Delivery adapters live in the broker. Tests: test_switchboard.py. v1 (teamline_state)
keeps running beside this until the cut-over.
"""
import asyncio
import io
import json
import os
import difflib
import re
import sys
import time
import uuid

import datetime as _dt
import tempfile

# The teams this broker serves -- ONE list. The broker, the operator page and the tests all derive
# from it, so enabling a team is a single edit or one environment variable:
#
#     TEAMLINE_TEAMS="alpha,beta,gamma"
#
# A team is an ADMINISTRATIVE boundary, not a security one: the team arrives in a header that the
# client sets for itself. Read "Security model" in the README before exposing a broker to a network
# you do not control.
TEAMS = tuple(t.strip().lower() for t in
              os.environ.get("TEAMLINE_TEAMS", "alpha,beta").split(",") if t.strip())
OPERATOR = "operator"
BUSY_KINDS = ("walk", "review", "commit", "away", "turn", "other")
PREFIX = "TEAMLINE"
STEER, QUEUE = "steer", "queue"
NAME_RE = re.compile(r"^[a-z0-9-]{1,32}$")
STALE_S, RETIRE_IDLE_S, GONE_RETIRE_S, VM_HOLD_S = 2 * 3600, 24 * 3600, 10 * 60, 7 * 86400
NOW_MODEL_WINS_S, RING_TIMEOUT_S = 30 * 60, 90
# Optional concurrency cap, PER TEAM: every ringing or open call whose CALLEE is on that team counts
# against it. It is not per session -- a session already holds one call at a time. The cap exists
# because a team whose sessions are woken by delivery has a wake budget: N simultaneous calls means N
# sessions interrupted at once. A team with no entry here is uncapped.
#
#     Switchboard(..., cap_into={"beta": 6})
#
# Default: uncapped. In production we run one team at 6, reached by measuring how many of their
# sessions could answer at once rather than by picking a number.
CAP_INTO_DEFAULT = {}
NUDGE_S, CALL_CAP_S = 5 * 60, 2 * 3600
NOW_MAX = 200                                     # now-line cap (operator 2026-09-03 07:3x: raised from 120)          # etiquette (operator 2026-09-02): silence nudges, orphan cap


def _ts_local(t):
    """Operator's clock (the box's local zone), seconds, no zone label (operator 2026-09-02 16:4x)."""
    return _dt.datetime.fromtimestamp(t).strftime("%y-%m-%d %H:%M:%S")


def _write_atomic(path, text):
    """Compute-then-replace: never leaves a truncated file (the HANDOFF zero-byte lesson)."""
    d = os.path.dirname(os.path.abspath(path))
    os.makedirs(d, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=d, prefix=".tmp-", suffix=".part")
    with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
        fh.write(text)
    os.replace(tmp, path)


class SwitchError(RuntimeError):
    pass


def _ts_both(t):
    """Both clocks on every ledger row: exchange UTC = box local (zone offset stated)."""
    u = _dt.datetime.fromtimestamp(t, _dt.timezone.utc)
    l = _dt.datetime.fromtimestamp(t).astimezone()
    off = l.utcoffset() or _dt.timedelta(0)
    sign = "+" if off >= _dt.timedelta(0) else "-"
    hh = int(abs(off).total_seconds() // 3600)
    return f"{u:%Y-%m-%d %H:%M} UTC  =  {l:%Y-%m-%d %H:%M} UTC{sign}{hh}"


class Switchboard:
    def __init__(self, ledger_path, calls_dir, now=time.time, cap_into=None, require_feed=False, feed_gone_s=90):
        """require_feed: an extension registered without a held feed is UNREACHABLE -- nothing can
        deliver to it, so callers get voicemail instead of a ring. feed_gone_s: a feed silent for that
        long is GONE (holders send a keepalive every <= 25 s). cap_into: {team: max concurrent calls
        INTO that team}; absent means uncapped."""
        self.ledger_path, self.calls_dir, self.now = ledger_path, calls_dir, now
        self.cap_into = dict(cap_into or CAP_INTO_DEFAULT)
        self.require_feed, self.feed_gone_s = require_feed, feed_gone_s
        self._rows, self._ext, self._calls = [], {}, {}
        self._outbox, self._delivered, self._held = [], set(), []      # held voicemail
        self._waiters, self._listeners, self._row_listeners = {}, [], []
        self._replaying = False
        os.makedirs(os.path.dirname(os.path.abspath(ledger_path)) or ".", exist_ok=True)
        os.makedirs(calls_dir, exist_ok=True)
        self._replay()

    # ------------------------------------------------------------------ ledger
    def _replay(self):
        if not os.path.exists(self.ledger_path):
            return
        self._replaying = True
        with io.open(self.ledger_path, encoding="utf-8") as fh:
            for ln in fh:
                if ln.strip():
                    row = json.loads(ln)
                    self._rows.append(row)
                    self._apply(row)
        self._replaying = False
        # Transient signals about a call that has since ended are stale, not pending. _apply now
        # drops these as each call ends, so a ledger written by this version arrives here clean --
        # but one written before that fix can still carry them, so the sweep stays. Same constant as
        # the live rule, so the two cannot drift apart.
        ended = {c["call_id"] for c in self._calls.values() if c["state"] == "ENDED"}
        self._outbox = [e for e in self._outbox
                        if not (e["kind"] in self.TRANSIENT and e["call_id"] in ended)]
        t = self.now()
        for e in self._ext.values():          # a socket that is gone is gone
            if e["feed"]:
                e["feed_up"] = False
                e["gone_since"] = e["gone_since"] or t
                e["gone_reason"] = e.get("gone_reason") or "feed"

    def _commit(self, event, **f):
        t = self.now()
        row = dict(event=event, ts=t, ts_both=_ts_both(t), **f)
        with io.open(self.ledger_path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
        self._rows.append(row)
        self._apply(row)
        for cb in self._row_listeners:
            try:
                cb(row)
            except Exception:
                pass
        return row

    def ledger_rows(self):
        return list(self._rows)

    def on_event(self, cb):
        self._listeners.append(cb)

    def on_row(self, cb):
        self._row_listeners.append(cb)

    # The transient signals of a call that has ENDED are stale, not pending: a ring_delivered or a
    # nudge about a finished conversation would wake a session for nothing. Replay has always
    # dropped them; the LIVE path did not, so the two disagreed. One rule now, called wherever a
    # call ends, so they cannot drift apart again. Lines and hangup summaries are NOT transient --
    # a party that has not yet read the last thing said to it must still receive it.
    TRANSIENT = ("ring_delivered", "nudge")

    def _drop_transient(self, call_id):
        self._outbox = [e for e in self._outbox
                        if not (e["kind"] in self.TRANSIENT and e.get("call_id") == call_id)]

    # ------------------------------------------------------------------ apply
    def _apply(self, r):
        ev, t = r["event"], r["ts"]
        if ev == "register":
            self._ext[r["ext"]] = dict(ext=r["ext"], team=r["team"], name=r["name"], session_id=r.get("session_id"),
                                       feed=bool(r.get("feed")), feed_up=bool(r.get("feed")), now=r["now"], now_ts=t,
                                       derived=None, derived_ts=None, busy=None, running=False, last_seen=t,
                                       gone_since=None, gone_reason=None, registered=t, sid=r.get("sid"))
        elif ev in ("retired", "unregister"):
            self._ext.pop(r["ext"], None)
        elif ev == "now":
            e = self._ext.get(r["ext"])
            if e:
                if r["source"] == "model":
                    e["now"], e["now_ts"] = r["text"], t
                else:
                    e["derived"], e["derived_ts"] = r["text"], t
                e["last_seen"] = t
        elif ev == "busy":
            e = self._ext.get(r["ext"])
            if e:
                e["busy"] = (r["kind"], r["reason"]) if r["on"] else None
                e["last_seen"] = t
        elif ev == "liveness":                     # measured: team -> {session_id: running}
            live = r["sessions"]
            for e in self._ext.values():
                if e["team"] != r["team"] or not e["session_id"]:
                    continue
                if e["session_id"] in live:
                    e["running"] = bool(live[e["session_id"]])
                    e["gone_since"], e["gone_reason"] = None, None
                else:
                    e["running"] = False
                    # FIRST CAUSE WINS -- but the host's word is the stronger evidence, and only the
                    # host can withdraw it.
                    e["gone_since"] = e["gone_since"] or t
                    e["gone_reason"] = "host"
        elif ev == "feed":
            e = self._ext.get(r["ext"])
            if e:
                e["feed_up"] = bool(r["up"])
                if r["up"]:
                    # A frame only disproves SILENCE. It cannot withdraw the host's report that the
                    # session behind this watcher is gone: a watcher is a separate process, and its
                    # socket being alive says nothing about the session it delivers to.
                    if e.get("gone_reason") in (None, "feed"):
                        e["gone_since"], e["gone_reason"] = None, None
                else:
                    e["gone_since"] = e["gone_since"] or t
                    e["gone_reason"] = e.get("gone_reason") or "feed"
                if r["up"]:
                    e["last_seen"] = t
        elif ev == "touch":
            e = self._ext.get(r["ext"])
            if e:
                e["last_seen"] = t
        elif ev == "running":
            e = self._ext.get(r["ext"])
            if e:
                e["running"] = bool(r["running"])
                e["last_seen"] = t
        elif ev == "call":
            self._calls[r["call_id"]] = dict(call_id=r["call_id"], caller=r["ext"], callee=r["peer"], subject=r["subject"],
                                             state="RINGING", started=t, lines=[], summary=None, ring_msg_id=r["msg_id"],
                                             ring_wait=0.0, last_tick=t, last_line=t, nudges=0)
            self._touch(r["ext"], t)
            self._emit(r["peer"], "ring", STEER, r["msg_id"], r["call_id"],
                       f"{PREFIX} ring from {r['ext']} -- subject: {r['subject']}\n{r['opening']}\n(sw_answer or sw_decline)")
        elif ev == "answer":
            c = self._calls[r["call_id"]]
            c["state"], c["last_line"], c["nudges"] = "IN_CALL", t, 0
            self._touch(r["ext"], t)
            self._emit(c["caller"], "answer", STEER, r["msg_id"], r["call_id"],
                       f"{PREFIX} {r['ext']} answered call {r['call_id']} -- line open (sw_say / sw_wait / sw_hangup)")
        elif ev in ("decline", "ring_timeout", "peer_lost"):
            c = self._calls[r["call_id"]]
            c["state"], c["summary"] = "ENDED", f"{ev}: {r.get('reason', '')}"
            self._drop_transient(c["call_id"])
            other = c["caller"] if r["ext"] == c["callee"] else c["callee"]
            if r["ext"] in self._ext:
                self._touch(r["ext"], t)
            self._emit(other, ev, STEER, r["msg_id"], r["call_id"],
                       f"{PREFIX} call {r['call_id']} {ev} ({r.get('reason', '')}) -- sw_leave for voicemail")
        elif ev == "call_expired":
            c = self._calls[r["call_id"]]
            c["state"], c["summary"] = "ENDED", f"expired: open for more than {CALL_CAP_S // 3600} h"
            self._drop_transient(c["call_id"])
            for q, suf in ((c["caller"], "-a"), (c["callee"], "-b")):
                self._emit(q, "call_expired", STEER, r["msg_id"] + suf, r["call_id"],
                           f"{PREFIX} call {r['call_id']} expired (open > {CALL_CAP_S // 3600} h, the orphan cap)")
        elif ev == "nudge":
            c = self._calls[r["call_id"]]
            c["nudges"] = r["n"]
            mins = int(r["silence_s"] // 60)
            # Rows written before `since` existed default to 0 and keep their old ids.
            stretch = int(r.get("since", 0))
            self._emit(c["callee"], "nudge", STEER, f"{r['call_id']}-nudge-{stretch}-{r['n']}-callee", r["call_id"],
                       f"{PREFIX} call {r['call_id']}: still there? the caller has heard nothing for {mins} min -- "
                       f"sw_say a short 'still working' line, or sw_hangup")
            self._emit(c["caller"], "nudge", STEER, f"{r['call_id']}-nudge-{stretch}-{r['n']}-caller", r["call_id"],
                       f"{PREFIX} call {r['call_id']}: peer silent {mins} min (call still open; sw_hangup if you give up)")
        elif ev == "say":
            c = self._calls[r["call_id"]]
            c["lines"].append((t, r["ext"], r["text"]))
            c["last_line"], c["nudges"] = t, 0
            if r["ext"] == OPERATOR:
                for q, mid in zip((c["caller"], c["callee"]), r["msg_ids"]):
                    self._emit(q, "say", STEER, mid, r["call_id"], f"{PREFIX} {OPERATOR}: {r['text']}")
            else:
                self._touch(r["ext"], t)
                other = c["callee"] if r["ext"] == c["caller"] else c["caller"]
                self._emit(other, "say", STEER, r["msg_id"], r["call_id"], f"{PREFIX} {r['ext']}: {r['text']}")
        elif ev == "hangup":
            c = self._calls[r["call_id"]]
            c["state"], c["summary"] = "ENDED", r["summary"]
            self._drop_transient(c["call_id"])
            self._touch(r["ext"], t)
            other = c["callee"] if r["ext"] == c["caller"] else c["caller"]
            self._emit(other, "hangup", STEER, r["msg_id"], r["call_id"],
                       f"{PREFIX} {r['ext']} hung up call {r['call_id']} -- summary: {r['summary']}")
        elif ev == "voicemail":
            self._held.append(dict(id=r["msg_id"], to=r["peer"], frm=r["ext"], text=r["text"], ts=t))
        elif ev == "voicemail_released":
            vm = [v for v in self._held if v["id"] == r["msg_id"]]
            self._held = [v for v in self._held if v["id"] != r["msg_id"]]
            if vm:
                self._emit(vm[0]["to"], "voicemail", QUEUE, vm[0]["id"], None,
                           f"{PREFIX} voicemail from {vm[0]['frm']}: {vm[0]['text']}")
        elif ev == "voicemail_expired":
            self._held = [v for v in self._held if v["id"] != r["msg_id"]]
        elif ev == "delivered":
            self._delivered.add(r["msg_id"])
            self._outbox = [e for e in self._outbox if e["id"] != r["msg_id"]]
            if r.get("session_id") not in ("ws", "wait", "x"):
                for c in self._calls.values():           # a ring landed on the peer's host: tell the caller
                    if c["state"] == "RINGING" and c.get("ring_msg_id") == r["msg_id"]:
                        busy = self._ext.get(c["callee"], {}).get("running")
                        self._emit(c["caller"], "ring_delivered", STEER, r["msg_id"] + "-d", c["call_id"],
                                   f"{PREFIX} ring {c['call_id']} delivered to {c['callee']}'s session"
                                   + (" -- peer is mid-turn, it can answer at its next step; the ring timer holds meanwhile"
                                      if busy else " -- peer idle, woken"))

    def _touch(self, ext, t):
        e = self._ext.get(ext)
        if e:
            e["last_seen"] = t

    def _emit(self, ext, kind, lane, msg_id, call_id, text):
        if msg_id in self._delivered:
            return
        e = dict(id=msg_id, to=ext, kind=kind, lane=lane, call_id=call_id, text=text)
        w = self._waiters.get(ext)
        if w is not None:
            fut, buf = w
            buf.append(e)
            self._delivered.add(msg_id)
            if not fut.done():
                fut.set_result(True)
            return
        self._outbox.append(e)
        if not self._replaying:
            for cb in self._listeners:
                try:
                    cb(e)
                except Exception:
                    pass

    # ------------------------------------------------------------------ helpers
    def _team(self, team):
        if team not in TEAMS:
            raise SwitchError(f"unknown team {team!r}; teams are {TEAMS}")
        return team

    def _full(self, team, name):
        return f"{team}/{name}"

    def _unknown(self, ext):
        """The refusal names the closest REAL extensions: names come from sw_directory, never from memory."""
        near = difflib.get_close_matches(ext, list(self._ext), n=3, cutoff=0.4)
        hint = f"; did you mean {', '.join(near)}?" if near else ""
        return SwitchError(f"no extension {ext!r}{hint} -- read sw_directory and copy the name; never type one from memory")

    def _get(self, ext):
        e = self._ext.get(ext)
        if not e:
            raise self._unknown(ext)
        return e

    def _mine(self, team, name):
        e = self._get(self._full(self._team(team), name))
        return e

    def _hygiene(self, e, t=None):
        t = self.now() if t is None else t
        # A socket that has JUST dropped is not yet GONE. The contract promises a silence window
        # before a lane is presumed dead, and `register()` hands a GONE lane to whoever asks next --
        # so without the window a momentary blip was an instant takeover: any client naming the
        # team/ext (both public in the directory) inherited the lane and its identity was cleared.
        # Inside the window the lane still belongs to its holder and only a matching identity
        # re-attaches; past it, the lane is genuinely presumed dead and anyone may reclaim it, which
        # is how a session recovers its name after a restart gives it a new session id.
        if e["feed"] and not e["feed_up"]:
            # SILENCE, not evidence. Wait the window out before presuming death.
            since = e["gone_since"] if e["gone_since"] is not None else t
            if t - since >= self.feed_gone_s:
                return "GONE"
        elif e["gone_since"] is not None:
            # EVIDENCE: the host reported this session absent. Nothing to wait for.
            return "GONE"
        if self.require_feed and not e["feed"]:
            return "UNREACHABLE"
        if t - e["last_seen"] > STALE_S:
            return "STALE"
        return "LIVE"

    def _call_of(self, ext):
        for c in self._calls.values():
            if c["state"] in ("RINGING", "IN_CALL") and ext in (c["caller"], c["callee"]):
                return c
        return None

    def _state(self, e):
        h = self._hygiene(e)
        if h in ("GONE", "UNREACHABLE"):
            return h
        c = self._call_of(e["ext"])
        if c:
            return c["state"]
        if e["busy"] or e["running"]:
            return "BUSY"
        return "IDLE"

    def _busy_of(self, e):
        if e["busy"]:
            return e["busy"]
        if e["running"]:
            return ("turn", "mid-turn on its host")
        return (None, None)

    def _now_of(self, e):
        """Model line wins while younger than 30 min  -- unless a DERIVED line is newer than
        it: a new task from the operator is newer information (operator, 2026-09-02 15:3x)."""
        t = self.now()
        derived_newer = e["derived"] is not None and e["derived_ts"] > e["now_ts"]
        if e["now"] and not derived_newer and (e["derived"] is None or t - e["now_ts"] <= NOW_MODEL_WINS_S):
            return e["now"], t - e["now_ts"], "model"
        if e["derived"]:
            return e["derived"], t - e["derived_ts"], "derived"
        return e["now"], t - e["now_ts"], "model"

    def _entry(self, e):
        now, age, src = self._now_of(e)
        bk, br = self._busy_of(e)
        t = self.now()
        return dict(ext=e["ext"], team=e["team"], state=self._state(e), hygiene=self._hygiene(e, t), now=now,
                    now_age_s=age, now_source=src, busy_kind=bk, busy_reason=br, last_seen_age_s=t - e["last_seen"],
                    session_id=e["session_id"], sid=e.get("sid"), call_id=(self._call_of(e["ext"]) or {}).get("call_id"),
                    voicemail_held=sum(1 for v in self._held if v["to"] == e["ext"]),
                    pending=sum(1 for x in self._outbox if x["to"] == e["ext"]))

    def _release_vm(self, ext):
        e = self._ext.get(ext)
        if not e or self._state(e) != "IDLE":
            return
        for v in [v for v in self._held if v["to"] == ext]:
            self._commit("voicemail_released", ext=ext, msg_id=v["id"])

    # ------------------------------------------------------------------ registration + directory
    def register(self, team, name, now, session_id=None, feed=False, sid=None):
        team = self._team(team)
        if not NAME_RE.match(name or ""):
            raise SwitchError(f"ext name {name!r} must match [a-z0-9-]{{1,32}} (team prefix is added by the broker)")
        if not feed and not session_id:
            raise SwitchError("an extension that holds no feed must register its host session id, "
                              "so that something can be told where to deliver")
        # feed + session_id together: the socket mirrors events (a watcher), the host stays the delivery target
        full = self._full(team, name)
        old = self._ext.get(full)
        if old:
            h = self._hygiene(old)
            if h == "LIVE":
                raise SwitchError(f"{full} is LIVE (session {old['session_id']}, seen {int(self.now() - old['last_seen'])}s ago); "
                                  f"pick another name or wait for it to go STALE")
            self._commit("retired", ext=full, reason="replaced", by=full, was=h)
        self._commit("register", ext=full, team=team, name=name, session_id=session_id, feed=bool(feed),
                     now=(now or "")[:NOW_MAX], sid=sid)
        self._release_vm(full)
        return dict(ext=full, state=self._state(self._ext[full]), directory=self.directory())

    def unregister(self, team, name):
        e = self._mine(team, name)
        self._end_calls_of(e["ext"], "unregistered")
        self._commit("unregister", ext=e["ext"])
        return dict(ok=True)

    def operator_retire(self, ext):
        e = self._get(ext)
        self._end_calls_of(ext, "retired by operator")
        self._commit("retired", ext=ext, reason="operator", by=OPERATOR)
        return dict(ok=True, ext=ext)

    def operator_wipe_voicemail(self, ext):
        n = 0
        for v in [v for v in self._held if v["to"] == ext]:
            self._commit("voicemail_expired", ext=ext, msg_id=v["id"], by=OPERATOR)
            n += 1
        return dict(ok=True, wiped=n)

    def set_now(self, team, name, text):
        e = self._mine(team, name)
        self._commit("now", ext=e["ext"], source="model", text=(text or "")[:NOW_MAX])
        return self._entry(e)

    def set_now_derived(self, team, name, text):
        e = self._mine(team, name)
        self._commit("now", ext=e["ext"], source="derived", text=(text or "")[:NOW_MAX])
        return self._entry(e)

    def ext_by_sid(self, sid):
        """Exact match first; else suffix match either way (the Desktop app names a session
        `local_<uuid>` while the hook's stdin carries the bare uuid, or vice versa)."""
        sid = (sid or "").strip()
        if not sid:
            return None
        # a non-acking ext is bound by `sid` (feed); an acking one by `session_id` (sw_register) --
        # beta 16:40: the hook matched only sid, so their watcher could never find their ext
        keys = lambda e: [k for k in (e.get("sid"), e.get("session_id")) if k]
        for e in self._ext.values():
            if sid in keys(e):
                return e["ext"]
        for e in self._ext.values():
            for b in keys(e):
                if len(sid) >= 12 and len(b) >= 12 and (b.endswith(sid) or sid.endswith(b)):
                    return e["ext"]
        return None

    def set_now_derived_by_sid(self, sid, text):
        ext = self.ext_by_sid(sid)
        if not ext:
            raise SwitchError(f"no extension bound to session id {sid!r}")
        e = self._ext[ext]
        self._commit("now", ext=ext, source="derived", text=(text or "")[:NOW_MAX])
        return self._entry(e)

    def set_running(self, team, sessions):
        """Measured liveness for a team: {session_id: running}. Absent sessions are GONE."""
        team = self._team(team)
        before = {x: self._hygiene(e) for x, e in self._ext.items() if e["team"] == team}
        self._commit("liveness", team=team, sessions={k: bool(v) for k, v in sessions.items()})
        for x, h in before.items():
            e = self._ext.get(x)
            if e and self._hygiene(e) == "GONE" and h != "GONE":
                self._end_calls_of(x, "peer session gone", lost=True)
        for x in list(self._ext):
            self._release_vm(x)

    def _feed_alive(self, ext):
        """A frame arrived on this lane's socket, so the silence that marked it down is over.

        tick() presumes a keepaliving holder dead after feed_gone_s of quiet, which is right -- but
        the socket may be perfectly healthy and merely paused. Without this the lane stayed feed-down
        while still connected, went GONE, and was retired, and its holder could not be told because a
        session with no line cannot be rung. Ledgered, so a replay reaches the same state.

        It cannot fire spuriously: a frame requires an open socket, and for a non-acking feed
        `feed_up` false means the socket itself is gone, so nothing can arrive on it.
        """
        e = self._ext.get(ext)
        if e and e["feed"] and not e["feed_up"]:
            self._commit("feed", ext=ext, up=True, reason="the holder spoke again")

    def set_running_ext(self, ext, running):
        """Liveness from the watcher's keepalive ({"ping":1,"running":bool}); ledgered only on change."""
        e = self._ext.get(ext)
        if not e:
            return
        self._feed_alive(ext)
        e["last_seen"] = self.now()
        if bool(running) != e["running"]:
            self._commit("running", ext=ext, running=bool(running))

    def feed(self, team, name, up):
        e = self._mine(team, name)
        self._commit("feed", ext=e["ext"], up=bool(up))
        if up:
            self._release_vm(e["ext"])
        else:
            self._end_calls_of(e["ext"], "feed closed", lost=True)

    def touch(self, ext):
        if ext in self._ext:
            self._feed_alive(ext)          # any frame disproves the silence, not only a keepalive
            self._commit("touch", ext=ext)

    def directory(self):
        return [self._entry(e) for e in sorted(self._ext.values(), key=lambda e: e["ext"])]

    def entry(self, team, name):
        return self._entry(self._mine(team, name))

    def active_calls(self):
        return [dict(call_id=c["call_id"], caller=c["caller"], callee=c["callee"], subject=c["subject"], state=c["state"])
                for c in self._calls.values() if c["state"] in ("RINGING", "IN_CALL")]

    # ------------------------------------------------------------------ calls
    def busy(self, team, name, on, kind="other", reason=""):
        e = self._mine(team, name)
        if on and kind not in BUSY_KINDS:
            raise SwitchError(f"busy kind {kind!r} not in {BUSY_KINDS}")
        self._commit("busy", ext=e["ext"], on=bool(on), kind=kind if on else None, reason=reason if on else None)
        self._release_vm(e["ext"])
        return self._entry(e)

    def call(self, team, name, peer, subject, opening):
        me = self._mine(team, name)
        if peer not in self._ext:
            raise self._unknown(peer)
        if peer == me["ext"]:
            raise SwitchError("an extension cannot call itself")
        if self._call_of(me["ext"]):
            raise SwitchError(f"{me['ext']} already holds call {self._call_of(me['ext'])['call_id']}")
        p = self._ext[peer]
        h = self._hygiene(p)
        bk, br = self._busy_of(p)
        def refused(state, reason, **extra):
            # Rule 9 (operator 18:5x): a refused call becomes an ADDRESSED voicemail with the same subject
            # and opening -- no improvisation by the caller
            self._commit("call_refused", ext=me["ext"], peer=peer, reason=reason, **{k: v for k, v in extra.items() if k == "cap"})
            vm = self.leave(team, name, peer, f"(refused call, peer {reason}) {subject} -- {opening}")
            return dict(call_id=None, state=state, voicemail_offered=True, voicemail_queued=vm["msg_id"],
                        hint=f"peer {reason}: your subject + opening were left as voicemail, delivered when it is next IDLE", **extra)
        if h in ("GONE", "UNREACHABLE"):
            return refused(h, h)
        if self._call_of(peer):
            return refused(self._call_of(peer)["state"], "IN_CALL")
        if p["busy"]:
            return refused("BUSY", "BUSY", busy_kind=bk, busy_reason=br)
        cap = self.cap_into.get(p["team"])
        if cap is not None:
            inflight = sum(1 for c in self._calls.values()
                           if c["state"] in ("RINGING", "IN_CALL") and self._ext.get(c["callee"], {}).get("team") == p["team"])
            if inflight >= cap:
                return refused("CAPPED", "CAPPED", cap=cap)
        cid = uuid.uuid4().hex[:12]
        self._commit("call", ext=me["ext"], peer=peer, call_id=cid, subject=subject, opening=opening, msg_id=uuid.uuid4().hex)
        out = dict(call_id=cid, state="RINGING", peer_busy_kind=bk, peer_busy_reason=br, peer_hygiene=h)
        if h == "STALE":
            out["warning"] = f"{peer} is STALE (last seen {int(self.now() - p['last_seen'])}s ago); it may not answer"
        return out

    def _active(self, team, name, want):
        e = self._mine(team, name)
        c = self._call_of(e["ext"])
        if not c:
            raise SwitchError(f"{e['ext']} has no active call")
        if c["state"] != want:
            raise SwitchError(f"call {c['call_id']} is {c['state']}, not {want}")
        return e, c

    def answer(self, team, name, receipt="received, working on it"):
        """Rule 7 (ring half, operator 18:5x): an answer CARRIES a receipt line to the caller, mechanically."""
        e, c = self._active(team, name, "RINGING")
        if c["callee"] != e["ext"]:
            raise SwitchError("only the callee can answer")
        self._commit("answer", ext=e["ext"], call_id=c["call_id"], msg_id=uuid.uuid4().hex)
        if receipt:
            self._commit("say", ext=e["ext"], call_id=c["call_id"], text=(receipt or "")[:300], msg_id=uuid.uuid4().hex)
        return dict(call_id=c["call_id"], state="IN_CALL", receipt=receipt)

    def decline(self, team, name, reason=""):
        e, c = self._active(team, name, "RINGING")
        if c["callee"] != e["ext"]:
            raise SwitchError("only the callee can decline")
        self._commit("decline", ext=e["ext"], call_id=c["call_id"], reason=reason, msg_id=uuid.uuid4().hex)
        self._release_vm(c["caller"]); self._release_vm(c["callee"])
        return dict(call_id=c["call_id"], state=self._state(e))

    def say(self, team, name, text):
        e, c = self._active(team, name, "IN_CALL")
        self._commit("say", ext=e["ext"], call_id=c["call_id"], text=text, msg_id=uuid.uuid4().hex)
        return dict(call_id=c["call_id"], state="IN_CALL", lines=len(c["lines"]))

    def operator_say(self, call_id, text):
        c = self._calls.get(call_id)
        if not c or c["state"] != "IN_CALL":
            raise SwitchError(f"call {call_id!r} is not open")
        self._commit("say", ext=OPERATOR, call_id=call_id, text=text, msg_ids=[uuid.uuid4().hex, uuid.uuid4().hex])
        return dict(call_id=call_id, state="IN_CALL")

    def hangup(self, team, name, summary=""):
        e, c = self._active(team, name, "IN_CALL")
        self._commit("hangup", ext=e["ext"], call_id=c["call_id"], summary=summary, msg_id=uuid.uuid4().hex)
        self._write_transcript(c)
        self._release_vm(c["caller"]); self._release_vm(c["callee"])
        return dict(call_id=c["call_id"], state=self._state(e), transcript=self._tpath(c["call_id"]))

    def leave(self, team, name, peer, text):
        e = self._mine(team, name)
        mid = uuid.uuid4().hex
        self._commit("voicemail", ext=e["ext"], peer=peer, text=text, msg_id=mid)
        self._release_vm(peer)
        return dict(queued=True, msg_id=mid, peer_state=self._state(self._ext[peer]) if peer in self._ext else "RETIRED")

    def _end_calls_of(self, ext, reason, lost=False):
        c = self._call_of(ext)
        if not c:
            return
        ev = "peer_lost" if lost else ("ring_timeout" if c["state"] == "RINGING" else "hangup")
        if ev == "hangup":
            self._commit("hangup", ext=ext, call_id=c["call_id"], summary=f"ended: {reason}", msg_id=uuid.uuid4().hex)
        else:
            self._commit(ev, ext=ext, call_id=c["call_id"], reason=reason, msg_id=uuid.uuid4().hex)
        self._write_transcript(c)

    def tick(self):
        """Ring timeouts, retirements, voicemail expiry. Returns a summary dict."""
        t, out = self.now(), dict(ring_timeout=[], retired=[], vm_expired=0, nudged=[], expired=[])
        for c in list(self._calls.values()):
            if c["state"] == "RINGING":
                # the first-response bound is 90 s of the CALLEE'S IDLE time: while it is mid-turn it
                # cannot see the ring, so the timer holds (operator etiquette, 2026-09-02)
                callee = self._ext.get(c["callee"])
                if not (callee and callee["running"]):
                    c["ring_wait"] += t - c["last_tick"]
                c["last_tick"] = t
                # ...but the hold needs an OUTER bound. A callee whose watcher keeps reporting
                # "still running" never advances the idle timer, and CALL_CAP_S used to apply only
                # once a call was open -- so a ring pinned the CALLER's lane indefinitely, and a
                # lane holds one call at a time. A ring now gets the same cap an open call gets.
                capped = t - c["started"] > CALL_CAP_S
                if c["ring_wait"] > RING_TIMEOUT_S or capped:
                    self._commit("ring_timeout", ext=c["callee"], call_id=c["call_id"],
                                 reason=(f"unanswered for more than {CALL_CAP_S // 3600} h (the callee "
                                         f"reported itself mid-turn throughout)" if capped
                                         else f"no answer in {RING_TIMEOUT_S}s of idle time"),
                                 msg_id=uuid.uuid4().hex)
                    self._write_transcript(c)
                    out["ring_timeout"].append(c["call_id"])
            elif c["state"] == "IN_CALL":
                if t - c["started"] > CALL_CAP_S:
                    self._commit("call_expired", call_id=c["call_id"], msg_id=uuid.uuid4().hex)
                    self._write_transcript(c)
                    out["expired"].append(c["call_id"])
                    continue
                silence = t - c["last_line"]
                n_due = int(silence // NUDGE_S)
                if n_due > c["nudges"]:
                    # `since` marks WHICH silent stretch this is. Without it the id was
                    # "{call}-nudge-{n}-{who}" and a `say` resets n to 0, so the next quiet
                    # stretch regenerated an id already delivered and _emit dropped it in
                    # silence -- the second nudge of any call simply never arrived.
                    self._commit("nudge", call_id=c["call_id"], n=n_due, silence_s=silence,
                                 since=c["last_line"])
                    out["nudged"].append(c["call_id"])
        for x, e in list(self._ext.items()):
            # silence rule for feeds that PING (a watcher, bound to a session id); a Claude Monitor feed
            # only receives, so for it the socket closing is the signal
            if e["feed"] and e["feed_up"] and e["session_id"] and t - e["last_seen"] > self.feed_gone_s:
                self._commit("feed", ext=x, up=False, reason=f"silent {int(t - e['last_seen'])}s")
                self._end_calls_of(x, "feed silent", lost=True)
                out.setdefault("feed_silent", []).append(x)
        for x, e in list(self._ext.items()):
            h = self._hygiene(e, t)
            if (h == "GONE" and t - (e["gone_since"] or t) > GONE_RETIRE_S) or (t - e["last_seen"] > RETIRE_IDLE_S):
                self._end_calls_of(x, "extension retired", lost=True)
                self._commit("retired", ext=x, reason="gone" if h == "GONE" else "idle", by="hygiene")
                out["retired"].append(x)
        for v in list(self._held):
            if t - v["ts"] > VM_HOLD_S:
                self._commit("voicemail_expired", ext=v["to"], msg_id=v["id"], by="hygiene")
                out["vm_expired"] += 1
        for x in list(self._ext):
            self._release_vm(x)
        return out

    # ------------------------------------------------------------------ delivery side
    def pending_for(self, ext):
        return [dict(e) for e in self._outbox if e["to"] == ext]

    def pending_for_team(self, team):
        return [dict(e) for e in self._outbox if e["to"].split("/", 1)[0] == team]

    def session_of(self, ext):
        e = self._ext.get(ext)
        return e["session_id"] if e else None

    def running_of(self, ext):
        e = self._ext.get(ext)
        return bool(e and e["running"])

    def mark_delivered(self, msg_id, session_id):
        if msg_id in self._delivered:
            return False
        self._commit("delivered", msg_id=msg_id, session_id=str(session_id))
        return True

    def mark_failed(self, msg_id, error):
        self._commit("delivery_failed", msg_id=msg_id, error=str(error)[:300])

    async def wait(self, team, name, timeout_s=60):
        e = self._mine(team, name)
        ext = e["ext"]
        if self._waiters.get(ext) is not None:
            raise SwitchError(f"{ext} already has an open wait")
        loop = asyncio.get_running_loop()
        fut, buf = loop.create_future(), []
        for ev in self.pending_for(ext):
            self._delivered.add(ev["id"])
            self._outbox = [x for x in self._outbox if x["id"] != ev["id"]]
            buf.append(ev)
        self._waiters[ext] = (fut, buf)
        self._commit("wait_open", ext=ext, timeout_s=timeout_s)
        try:
            if not buf:
                try:
                    await asyncio.wait_for(fut, timeout_s)
                except asyncio.TimeoutError:
                    pass
        finally:
            self._waiters[ext] = None
            for ev in buf:
                self._commit("delivered", msg_id=ev["id"], session_id="wait")
            self._commit("wait_close", ext=ext, received=len(buf))
        return buf

    # ------------------------------------------------------------------ transcripts
    def _tpath(self, cid):
        return os.path.join(self.calls_dir, cid + ".md")

    def log(self, call_id):
        c = self._calls.get(call_id)
        if not c:
            raise SwitchError(f"no call {call_id}")
        return self._render(c)

    def _render(self, c):
        out = [f"# TEAMLINE call {c['call_id']} -- {c['subject']}",
               f"caller: {c['caller']} · callee: {c['callee']} · state: {c['state']}",
               f"started: {_ts_local(c['started'])}", f"summary: {c['summary'] or '(open)'}", ""]
        for t, who, text in c["lines"]:
            out.append(f"- {_ts_local(t)} · **{who}**: {text}")
        return "\n".join(out) + "\n"

    def _write_transcript(self, c):
        _write_atomic(self._tpath(c["call_id"]), self._render(c))
