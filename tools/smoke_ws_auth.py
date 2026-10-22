#!/usr/bin/env python3
"""Does Intellex log in to a droid that wants its access token -- and leave every
other droid exactly as it was?

    python tools/smoke_ws_auth.py

No hardware, no network, no netsh, nothing outside this process. A fake board --
a real WebSocket server on 127.0.0.1 speaking the token protocol the firmware
speaks (src/wsauth.py has it) -- stands in for the droid, and the REAL
WebSocketTransport, discover.probe(), Bridge, /_api routes and reconnect loop run
against it. Settings go to a temporary INTELLEX_DATA_DIR.

What it holds the app to:
  A  the [AUTH:...] filter takes whole lines out and passes every other byte
     through untouched, UTF-8 split anywhere included (CLAUDE.md rule 1)
  B  a board with no token: nothing sent that was not sent before, no ?AUTH
  C  the right token: one ?AUTH, logged in before open() returns, and no [AUTH:...]
     line reaches the page
  D  no token saved: AuthNeeded "missing", nothing sent
  E  a wrong token, then the right one: refused once, never resent, and the right
     one waits out the board's 1 s lockout instead of being refused by it
  F  a prompt after open() stopped waiting: the link drops to log in, and the next
     open waits longer for that board
  G  discover.probe(): logs in and identifies a locked board, or reports it as
     locked -- never "not a droid"
  H  the host: a 401 asking for the token, the reconnect loop holding (no
     sessions, no resent token) until one is saved, a reattach logging in again,
     a token changed on the board asked for again, and no token in the log
  I  which board a token belongs to: the ARP parsers, and droid_key's fallbacks
  J  what a token may be (the firmware's rules) and that no error repeats it

The tokens here are test values; even so, no PASS/FAIL line prints one.
"""
from __future__ import annotations

import asyncio
import contextlib
import hmac
import io
import json
import os
import pathlib
import random
import re
import sys
import tempfile
import threading
import time

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

# Before anything reads them: a private settings file, no GitHub, nothing else on
# this PC probed or opened (CLAUDE.md "A test host needs a leash").
_DATA = tempfile.mkdtemp(prefix="intellex-wsauth-")
os.environ["INTELLEX_DATA_DIR"] = _DATA
os.environ["INTELLEX_OFFLINE"] = "1"
os.environ["INTELLEX_SERIAL_ALLOW"] = ""
os.environ["INTELLEX_DISCOVER_HOSTS"] = ""
os.environ.pop("INTELLEX_WS_ALLOW", None)

try:
    from websockets.sync.server import serve
    import websockets.sync.client as ws_client
except ImportError:
    sys.exit("websockets not installed -- run: pip install -r requirements.txt")

import discover                                            # noqa: E402
import settings                                            # noqa: E402
import wsauth                                              # noqa: E402
from transport import TransportError                       # noqa: E402
from ws_transport import WebSocketTransport                # noqa: E402

GOOD = "R2-good-token"
WRONG = "R2-wrong-tok"
NEW = "R2-new-token!"
HOST = "127.0.0.1"
KEY = "host:127.0.0.1"          # droid_key() of a loopback host
SECRETS = (GOOD, WRONG, NEW)

_bad = 0
# The console, kept: case H captures the host's log with redirect_stdout, and the
# results must not vanish into it.
_OUT = sys.stdout


def check(ok, what, detail=""):
    global _bad
    _bad += not ok
    print(f"  {'PASS' if ok else 'FAIL'}  {what}" + ("" if ok or not detail else f"  -- {detail}"), file=_OUT)
    return ok


def scrub(text) -> str:
    """Any detail printed on a failure, with the test tokens blanked."""
    s = str(text)
    for t in SECRETS:
        s = s.replace(t, "<token>")
    return s


# ── The fake board ──────────────────────────────────────────────────────────
class Board:
    """One WebSocket server speaking the firmware's token protocol.

    token None: the board has none -- silent at open, ?AUTH answered OK. Otherwise
    [AUTH:REQUIRED] at open (after prompt_delay), every line before a login dropped
    and answered with it, a wrong ?AUTH answered [AUTH:FAIL] and the session closed,
    and every ?AUTH refused for 1 s after any failure. kind "navicore" answers a
    PING with a direct PONG; kind "wcb" answers ?WDP,DUMP with a SELF row.
    """

    def __init__(self, token=None, kind="navicore", prompt_delay=0.0, hello=None):
        self.token, self.kind, self.prompt_delay, self.hello = token, kind, prompt_delay, hello
        self.after_login: list = []       # frames sent right after [AUTH:OK]
        self.lock = threading.Lock()
        self.sessions = 0
        self.auths: list = []             # (time.monotonic(), accepted)
        self.lines: list = []             # every line received, ?AUTH lines masked
        self.locked_until = 0.0
        self.conns: set = set()
        self.server = serve(self._handler, HOST, 0)
        self.port = self.server.socket.getsockname()[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def stop(self):
        self.drop_all()
        self.server.shutdown()

    def drop_all(self):
        for c in list(self.conns):
            with contextlib.suppress(Exception):
                c.close()

    def _handler(self, conn):
        with self.lock:
            self.sessions += 1
            self.conns.add(conn)
        authed = self.token is None
        try:
            if not authed:
                if self.prompt_delay:
                    time.sleep(self.prompt_delay)
                conn.send("[AUTH:REQUIRED]\r\n")
            elif self.hello:
                conn.send(self.hello)
            buf = ""
            for msg in conn:
                buf += msg if isinstance(msg, str) else msg.decode("utf-8", "replace")
                parts = re.split(r"\r\n|\r|\n", buf)
                buf = parts.pop()
                for line in parts:
                    if not line:
                        continue
                    self.lines.append("?AUTH,<masked>" if line.startswith("?AUTH,") else line)
                    if not authed:
                        if line.startswith("?AUTH,"):
                            now = time.monotonic()
                            ok = (now >= self.locked_until and self.token is not None
                                  and hmac.compare_digest(line[6:], self.token))
                            self.auths.append((now, ok))
                            if not ok:
                                self.locked_until = now + 1.0
                                conn.send("[AUTH:FAIL]\r\n")
                                conn.close()
                                return
                            authed = True
                            conn.send("[AUTH:OK]\r\n")
                            for f in self.after_login:
                                conn.send(f)
                        else:
                            conn.send("[AUTH:REQUIRED]\r\n")
                        continue
                    self._answer(conn, line)
        except Exception:
            pass
        finally:
            with self.lock:
                self.conns.discard(conn)

    def _answer(self, conn, line):
        if line.startswith("?AUTH,"):
            conn.send("[AUTH:OK]\r\n")          # a board with no token takes any
        elif _is_ping(line):
            if self.kind == "navicore":
                conn.send('{"type":"PONG","version":"vTEST"}\r\n')
        elif line == "?WDP,DUMP":
            if self.kind == "wcb":
                conn.send("[WDP:N=1,CLIENT=0,ALIAS=Body,HW=1,HWREV=,FW=7.0.1,CAP=00D0,CTRL=20,CAPTAGS=,"
                          "MAESTRO=1,AGE=0,SEEN=1,PEER=3]\r\n[WDP:END,count=0]\r\n")
        elif line == "?RELAY,WIFI":
            pass
        else:
            conn.send(f"echo {line}\r\n")

    def auth_count(self):
        return len(self.auths)


def _is_ping(line):
    try:
        return json.loads(line).get("type") == "PING"
    except (ValueError, AttributeError):
        return False


class Sink:
    def __init__(self):
        self.data = bytearray()
        self.lost: list = []

    def on_data(self, b):
        self.data += b

    def on_lost(self, why):
        self.lost.append(why)

    def wait_for(self, needle: bytes, timeout=3.0) -> bool:
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if needle in self.data:
                return True
            time.sleep(0.02)
        return needle in self.data


def fresh():
    """Forget every token and every note between cases."""
    with contextlib.suppress(OSError):
        (pathlib.Path(_DATA) / "settings.json").unlink()
    with wsauth._lock:
        wsauth._refused.clear()
        wsauth._failed_at.clear()
        wsauth._slow.clear()


def save(tok):
    err = settings.set_token(KEY, tok, "test board")
    wsauth.token_saved(KEY)
    return err


def transport(board, sink):
    return WebSocketTransport(HOST, sink.on_data, sink.on_lost, port=board.port)


# probe() asks for ws://<host>/ws, port 80: send it to the board under test instead.
_real_connect = ws_client.connect
_probe_port = {"port": None}


def _connect_to_board(url, *a, **k):
    if _probe_port["port"] and url == f"ws://{HOST}/ws":
        url = f"ws://{HOST}:{_probe_port['port']}/ws"
    return _real_connect(url, *a, **k)


ws_client.connect = _connect_to_board
# No netsh: a loopback host has no network name, and asking would only be slow.
discover.ssid_for_host = lambda _h: None


# ── A ───────────────────────────────────────────────────────────────────────
def case_a():
    print("-- A  the [AUTH:...] filter")
    f = wsauth.AuthLines()
    out, found = f.feed(b"[AUTH:REQUIRED]\r\n")
    check(out == b"" and found == ["[AUTH:REQUIRED]"], "a prompt line is taken out whole", (out, found))

    f = wsauth.AuthLines()
    o1, f1 = f.feed(b"[AU")
    o2, f2 = f.feed(b"TH:OK]\r\nhello\n")
    check(o1 == b"" and o2 == b"hello\n" and f1 == [] and f2 == ["[AUTH:OK]"],
          "a line split across frames is held, then taken out, and what follows passes", (o1, o2, f1, f2))

    f = wsauth.AuthLines()
    out, found = f.feed(b"say [AUTH:OK]\n[AUTH:OKAY]\n")
    check(out == b"say [AUTH:OK]\n[AUTH:OKAY]\n" and found == [],
          "mid-line, or not exactly one of the three, it is ordinary text", (out, found))

    f = wsauth.AuthLines()
    out, found = f.feed(b"[AUTH:REQUIRED]")
    check(out == b"" and found == ["[AUTH:REQUIRED]"], "a bare prompt frame with no newline still counts",
          (out, found))

    f = wsauth.AuthLines()
    o1, _ = f.feed(b"[A")
    o2, _ = f.feed(b"LIAS=Body]\n")
    check(o1 == b"" and o2 == b"[ALIAS=Body]\n", "a held '[A' goes out the moment it is not ours", (o1, o2))

    f = wsauth.AuthLines()
    long = b"[AUTH:" + b"x" * 100
    out, _ = f.feed(long)
    check(out == long, "a would-be AUTH line over 64 bytes is not held", len(out))

    # Every split of a stream with multi-byte characters and AUTH lines in it: what
    # comes out is the input with exactly the AUTH lines removed, byte for byte.
    text = ("Ωmega ✓ ready\r\n[AUTH:REQUIRED]\r\n{\"a\":\"é\"}\n[AUTH:OK]\n"
            "naïve → 🙂\n[AUTH:FAIL]\r\ntail ünï")
    raw = text.encode("utf-8")
    want = re.sub(rb"\[AUTH:(REQUIRED|OK|FAIL)\]\r?\n", b"", raw)
    rnd = random.Random(7)
    bad = 0
    for _ in range(400):
        f = wsauth.AuthLines()
        cuts = sorted(rnd.sample(range(1, len(raw)), rnd.randint(1, 12)))
        got, seen = bytearray(), []
        for a, b in zip([0] + cuts, cuts + [len(raw)]):
            o, fnd = f.feed(raw[a:b])
            got += o
            seen += fnd
        bad += bytes(got) != want or seen != ["[AUTH:REQUIRED]", "[AUTH:OK]", "[AUTH:FAIL]"]
    check(bad == 0, "400 random splits, UTF-8 cut mid-character: every byte but the AUTH lines, in order",
          f"{bad} splits differed")


# ── B ───────────────────────────────────────────────────────────────────────
def case_b():
    print("-- B  a board with no token")
    fresh()
    save(GOOD)                  # a saved token must still not be volunteered
    board = Board(token=None)
    try:
        s = Sink()
        t = transport(board, s)
        t0 = time.monotonic()
        t.open()
        took = time.monotonic() - t0
        check(0.8 <= took <= 2.0, f"a silent board costs the prompt wait, once ({took:.2f} s)")
        t.write(b'{"type":"PING"}\n')
        check(s.wait_for(b'"PONG"'), "PING -> PONG as before")
        check(board.auth_count() == 0 and not any(x.startswith("?AUTH") for x in board.lines),
              "no ?AUTH sent, even with a token saved for that key", board.lines)
        check(b"[AUTH" not in s.data and not s.lost, "nothing of the protocol reached the page", s.lost)
        t.close()

        board.hello = "WCB ready\r\n"
        s = Sink()
        t = transport(board, s)
        t0 = time.monotonic()
        t.open()
        took = time.monotonic() - t0
        check(took < 0.8, f"a board that speaks at open ends the wait at once ({took:.2f} s)")
        check(s.wait_for(b"WCB ready\r\n"), "and what it said is passed on, first")
        t.close()
    finally:
        board.stop()


# ── C, D ────────────────────────────────────────────────────────────────────
def case_c_d():
    print("-- C  the right token")
    fresh()
    save(GOOD)
    board = Board(token=GOOD)
    # The mirror starting after the login, a character cut across two frames.
    mirror = "Ω mirror ✓\r\n".encode("utf-8")
    board.after_login = [mirror[:1], mirror[1:5], mirror[5:]]
    try:
        s = Sink()
        t = transport(board, s)
        t.open()
        check(board.auth_count() == 1 and board.auths[0][1], "one ?AUTH, accepted, before open() returned",
              board.auths)
        check(t._logged_in, "the transport says it logged in")
        t.write(b'{"type":"PING"}\n')
        check(s.wait_for(b'"PONG"'), "PING -> PONG once logged in")
        check(mirror in bytes(s.data), "the mirror's multi-byte line arrives byte-exact across frames")
        check(b"[AUTH" not in s.data, "no [AUTH:...] line reached the page", bytes(s.data)[:80])
        check(not any(x == '{"type":"PING"}' for x in board.lines[:1]),
              "the first line the board got was the login, not the page's", board.lines[:2])
        t.close()

        print("-- D  no token saved")
        fresh()
        s = Sink()
        t = transport(board, s)
        n = board.auth_count()
        try:
            t.open()
            check(False, "open() refuses a board it has no token for")
        except wsauth.AuthNeeded as e:
            check(e.why == "missing" and e.key == KEY, f"AuthNeeded missing for {e.key}", e.as_dict())
            check("needs its access token" in str(e), "and it says so", str(e))
        check(board.auth_count() == n, "nothing sent")
        check(not t.is_open, "nothing left open")
    finally:
        board.stop()


# ── E ───────────────────────────────────────────────────────────────────────
def case_e():
    print("-- E  a wrong token, then the right one")
    fresh()
    save(WRONG)
    board = Board(token=GOOD)
    try:
        t = transport(board, Sink())
        try:
            t.open()
            check(False, "a wrong token is refused")
        except wsauth.AuthNeeded as e:
            check(e.why == "rejected" and board.auth_count() == 1 and not board.auths[0][1],
                  "refused once: AuthNeeded rejected", scrub(e.as_dict()))
        try:
            transport(board, Sink()).open()
            check(False, "a refused token is not tried again")
        except wsauth.AuthNeeded as e:
            check(e.why == "rejected" and board.auth_count() == 1,
                  "the next open does not resend it (still one ?AUTH)", board.auth_count())
        check(not wsauth.can_try(KEY), "can_try is false until a token is saved")

        failed_at = board.auths[0][0]
        save(GOOD)              # straight away: inside the board's 1 s lockout
        s = Sink()
        t = transport(board, s)
        t.open()
        gap = board.auths[-1][0] - failed_at
        check(board.auths[-1][1] and gap >= 1.0,
              f"the right one, saved at once, waits out the lockout and is accepted ({gap:.2f} s after)")
        t.close()

        # Saving the SAME value again is a decision to try it again.
        fresh()
        save(WRONG)
        with contextlib.suppress(wsauth.AuthNeeded):
            transport(board, Sink()).open()
        n = board.auth_count()
        save(WRONG)
        with contextlib.suppress(wsauth.AuthNeeded):
            transport(board, Sink()).open()
        check(board.auth_count() == n + 1, "re-saving a refused token tries it once more")
    finally:
        board.stop()


# ── F ───────────────────────────────────────────────────────────────────────
def case_f():
    print("-- F  a prompt that comes late")
    fresh()
    save(GOOD)
    board = Board(token=GOOD, prompt_delay=wsauth.PROMPT_WAIT_S + 0.6)
    try:
        s = Sink()
        t = transport(board, s)
        t.open()
        check(not t._logged_in, "open() gave up waiting for a prompt")
        end = time.monotonic() + 3
        while not s.lost and time.monotonic() < end:
            time.sleep(0.05)
        check(bool(s.lost) and "after the link was up" in s.lost[0], "the late prompt drops the link to log in",
              s.lost)
        check(not t.is_open and board.auth_count() == 0, "without answering it on that link")
        check(wsauth.prompt_wait(HOST) == wsauth.PROMPT_WAIT_SLOW_S, "the next open waits longer for this board")
        s2 = Sink()
        t2 = transport(board, s2)
        t2.open()
        check(t2._logged_in and board.auth_count() == 1, "and logs in")
        t2.close()
    finally:
        board.stop()


# ── G ───────────────────────────────────────────────────────────────────────
def case_g():
    print("-- G  discover.probe()")
    fresh()
    board = Board(token=GOOD, kind="wcb")
    _probe_port["port"] = board.port
    try:
        info = discover.probe(HOST)
        auth = info.get("auth") or {}
        check(info["kind"] == "locked" and auth.get("why") == "missing" and auth.get("key") == KEY,
              "a locked board with no token saved: kind locked, not 'not a droid'", scrub(info))
        check(board.auth_count() == 0, "nothing sent to it")
        row = {"kind": "locked", "auth": auth}
        check("needs its access token" in discover._hint(HOST, True, row), "the chooser's hint says why")

        save(GOOD)
        info = discover.probe(HOST)
        check(info["kind"] == "wcb" and info["alias"] == "Body" and info["relayId"] == "1",
              "with its token saved it logs in and names the board", scrub(info))
        check(board.auth_count() == 1, "one ?AUTH")

        fresh()
        save(WRONG)
        info = discover.probe(HOST)
        check(info["kind"] == "locked" and (info.get("auth") or {}).get("why") == "rejected",
              "a wrong token: locked, refused", scrub(info))
        n = board.auth_count()
        discover.probe(HOST)
        check(board.auth_count() == n, "and the next scan does not resend it")

        fresh()
        nc = Board(token=None, kind="navicore")
        _probe_port["port"] = nc.port
        info = discover.probe(HOST)
        check(info["kind"] == "navicore" and nc.auth_count() == 0, "a board with no token: as before",
              scrub(info))
        nc.stop()
    finally:
        _probe_port["port"] = None
        board.stop()


# ── H ───────────────────────────────────────────────────────────────────────
async def case_h():
    print("-- H  the host: attach, hold, save, reattach")
    import aiohttp
    from aiohttp import web
    import host as hostmod

    fresh()
    board = Board(token=GOOD, kind="wcb")
    port_holder = {"p": board.port}
    hostmod.WebSocketTransport = (lambda host, on_data, on_lost:
                                  WebSocketTransport(host, on_data, on_lost, port=port_holder["p"]))
    hostmod.auto_bounce = False
    # The move check is smoke_reconnect_identity's; here it would probe the board too.
    hostmod._reidentify_if_moved = lambda _spec: None
    # The idle liveness probe asks TCP port 80, where nothing listens here: it would
    # call every quiet stretch a dead link.
    real_tcp_open = discover.tcp_open
    discover.tcp_open = lambda *_a, **_k: True
    log = io.StringIO()

    app = hostmod.build_app()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, hostmod.BIND_HOST, 0)
    await site.start()
    base = f"http://{hostmod.BIND_HOST}:{site._server.sockets[0].getsockname()[1]}"

    async def until(cond, timeout):
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if await cond():
                return True
            await asyncio.sleep(0.2)
        return await cond()

    try:
        with contextlib.redirect_stdout(log):
            async with aiohttp.ClientSession() as s:
                async def status():
                    async with s.get(f"{base}/_api/status") as r:
                        return await r.json()

                async def post(path, body):
                    async with s.post(f"{base}{path}", json=body) as r:
                        return r.status, await r.json()

                code, body = await post("/_api/attach", {"kind": "ws", "host": HOST, "role": "wcb", "relayId": 1})
                need = body.get("authNeeded") or {}
                check(code == 401 and need.get("key") == KEY and need.get("why") == "missing"
                      and isinstance(need.get("seq"), int),
                      "attach answers 401 with what to ask for", (code, scrub(body)))
                st = await status()
                check(st["authNeeded"] and st["authNeeded"]["seq"] == need.get("seq") and not st["attached"]
                      and st["wantsLink"] and "needs its access token" in st["lastError"],
                      "/_api/status reports it, same seq", scrub(st))

                sessions = board.sessions
                await asyncio.sleep(3.2)
                check(board.sessions == sessions and board.auth_count() == 0,
                      "the reconnect loop holds: no session opened, nothing sent in 3 s",
                      (sessions, board.sessions))

                for bad in ("short", "has,comma1", "has space1", "x" * 33):
                    code, body = await post("/_api/token", {"key": KEY, "token": bad})
                    check(code == 400 and bad not in json.dumps(body), f"a token like {bad[:6]}... is refused (400)",
                          body)
                code, body = await post("/_api/token", {"key": "nonsense", "token": GOOD})
                check(code == 400, "an unknown key is refused", body)

                code, body = await post("/_api/token", {"key": KEY, "token": GOOD, "label": "test board"})
                check(code == 200 and body == {"ok": True}, "the right token is saved", body)

                async def attached():
                    return (await status())["attached"]
                check(await until(attached, 6), "the loop logs in by itself and attaches")
                st = await status()
                check(st["authNeeded"] is None and st["lastError"] == "", "nothing needed any more", scrub(st))
                check(board.auth_count() == 1, "one ?AUTH", board.auth_count())

                page = await s.ws_connect(f"{base}/_link")
                await page.send_str("?WDP,DUMP\n")
                got = b""
                end = time.monotonic() + 4
                while b"[WDP:END" not in got and time.monotonic() < end:
                    with contextlib.suppress(asyncio.TimeoutError):
                        m = await asyncio.wait_for(page.receive(), 1)
                        got += m.data if isinstance(m.data, bytes) else str(m.data).encode()
                check(b"ALIAS=Body" in got and b"[AUTH" not in got, "the page's command runs; no [AUTH:...] for it",
                      got[:120])
                await page.close()

                # A reboot: the board drops every session; the reattach logs in again.
                n = board.auth_count()
                board.drop_all()
                await asyncio.sleep(0.5)
                check(await until(attached, 8) and board.auth_count() == n + 1,
                      "a reattach logs in again", (board.auth_count(), n))

                # The token is changed on the board, and the board restarts.
                board.token = NEW
                n = board.auth_count()
                board.drop_all()

                async def refused():
                    st = await status()
                    return bool(st["authNeeded"]) and st["authNeeded"]["why"] == "rejected"
                check(await until(refused, 8), "the board refuses the old token: asked for again (rejected)")
                n2 = board.auth_count()
                check(n2 == n + 1, "after exactly one try", (n, n2))
                await asyncio.sleep(3.2)
                check(board.auth_count() == n2, "and it is not resent while the loop holds")

                code, _ = await post("/_api/token", {"key": KEY, "token": NEW})
                check(code == 200 and await until(attached, 8), "the new token is saved and the loop attaches")

                code, _ = await post("/_api/detach", {})
                st = await status()
                check(st["authNeeded"] is None and not st["wantsLink"], "a detach forgets the need", scrub(st))
    finally:
        await runner.cleanup()
        hostmod.bridge.detach()
        board.stop()
        discover.tcp_open = real_tcp_open
    out = log.getvalue()
    check(not any(t in out for t in SECRETS), "no token in the host's log",
          [ln for ln in out.splitlines() if any(t in ln for t in SECRETS)][:3])
    check("access token saved for" in out and "reconnect held:" in out,
          "the log says what happened, without the value")
    stored = json.loads((pathlib.Path(_DATA) / "settings.json").read_text(encoding="utf-8"))
    check(stored.get("tokens", {}).get(KEY, {}).get("token") == NEW, "kept in settings.json under its key")


# ── I ───────────────────────────────────────────────────────────────────────
def case_i():
    print("-- I  which board a token belongs to")
    win = ("\nInterface: 192.168.1.20 --- 0x5\n"
           "  Internet Address      Physical Address      Type\n"
           "  192.168.4.1           aa-bb-cc-00-00-01     dynamic\n\n"
           "Interface: 192.168.4.2 --- 0x11\n"
           "  Internet Address      Physical Address      Type\n"
           "  192.168.4.1           24-0a-c4-12-34-56     dynamic\n")
    check(discover._mac_from_arp_windows(win, "192.168.4.1", "192.168.4.2") == "24:0a:c4:12:34:56",
          "Windows, two adapters on two boards' networks: the one that routes there")
    check(discover._mac_from_arp_windows(win, "192.168.4.1", None) is None,
          "...and none when it cannot tell which")
    de = win.replace("Interface:", "Schnittstelle:").replace("Internet Address", "Internetadresse")
    check(discover._mac_from_arp_windows(de, "192.168.4.1", "192.168.4.2") == "24:0a:c4:12:34:56",
          "a translated report reads the same")
    check(discover._mac_from_arp_windows("No ARP Entries Found.\n", "192.168.4.1", "192.168.4.2") is None,
          "no entry: none")
    mac = "? (192.168.4.1) at 24:a:c4:12:34:56 on en6 ifscope [ethernet]\n"
    check(discover._mac_from_arp_bsd(mac, "192.168.4.1") == "24:0a:c4:12:34:56",
          "macOS, leading zeros dropped: normalised")
    check(discover._mac_from_arp_bsd("? (192.168.4.1) at (incomplete) on en6 [ethernet]\n", "192.168.4.1") is None,
          "macOS incomplete: none")
    lin = ("IP address       HW type     Flags       HW address            Mask     Device\n"
           "192.168.4.1      0x1         0x2         24:0a:c4:12:34:56     *        wlan0\n"
           "192.168.4.9      0x1         0x0         00:00:00:00:00:00     *        wlan0\n")
    check(discover._mac_from_proc_arp(lin, "192.168.4.1") == "24:0a:c4:12:34:56"
          and discover._mac_from_proc_arp(lin, "192.168.4.9") is None, "Linux /proc/net/arp")

    check(wsauth.droid_key("127.0.0.1") == "host:127.0.0.1" and wsauth.droid_label("127.0.0.1") == "127.0.0.1",
          "a loopback host: by address")
    real_mac, real_ssid = discover.mac_for_host, discover.ssid_for_host
    try:
        discover.mac_for_host = lambda h: "24:0a:c4:12:34:56"
        discover.ssid_for_host = lambda h: "WCB1"
        check(wsauth.droid_key("192.168.4.1") == "mac:24:0a:c4:12:34:56"
              and wsauth.droid_label("192.168.4.1") == '"WCB1" (192.168.4.1)',
              "the MAC when it can be read (the network only labels it)")
        discover.mac_for_host = lambda h: None
        check(wsauth.droid_key("192.168.4.1") == "ssid:WCB1@192.168.4.1", "else the network and the address")
        discover.ssid_for_host = lambda h: None
        check(wsauth.droid_key("192.168.4.1") == "host:192.168.4.1", "else the address alone")
        check(all(settings.valid_token_key(k) for k in ("mac:24:0a:c4:12:34:56", "ssid:My Net@192.168.1.5",
                                                         "host:192.168.4.1")),
              "every key droid_key makes is one /_api/token accepts")
    finally:
        discover.mac_for_host, discover.ssid_for_host = real_mac, real_ssid

    # The parser against this machine's own ARP report (Windows), read-only.
    if sys.platform == "win32":
        import proc
        out = proc.run(["arp", "-a"], capture_output=True, text=True, timeout=10, check=False).stdout or ""
        m = re.search(r"(\d+\.\d+\.\d+\.\d+)\s+-{3}\s+0x[\s\S]*?\n\s+(\d+\.\d+\.\d+\.\d+)\s+"
                      r"([0-9a-f]{2}(?:-[0-9a-f]{2}){5})\s+dynamic", out)
        if m:
            got = discover._mac_from_arp_windows(out, m.group(2), m.group(1))
            check(got == m.group(3).replace("-", ":"), "this PC's real 'arp -a' parses")
        else:
            print("  SKIP  this PC's ARP cache has no dynamic entry to read")


# ── J ───────────────────────────────────────────────────────────────────────
def case_j():
    print("-- J  what a token may be")
    for t, ok in (("12345678", True), ("x" * 32, True), ("1234567", False), ("x" * 33, False),
                  ("pass word", False), ("a,bcdefgh", False), ("a^bcdefgh", False), ("a;bcdefgh", False),
                  ("a?bcdefgh", False), ('a"bcdefgh', False), ("a\\bcdefgh", False), ("abcdefgh\n", False),
                  ("abcdéfgh", False), (None, False)):
        check(settings.valid_token(t) is ok, f"{t!r:.14} is {'a token' if ok else 'not a token'}")
    fresh()
    err = settings.set_token(KEY, "no,comma-here", "x")
    check(err and "no,comma-here" not in err, "the refusal does not repeat the value", err)
    check(settings.set_token("mac:24:0a:c4:12:34:56", GOOD, "lab\x00el") is None
          and settings.token("mac:24:0a:c4:12:34:56") == GOOD, "saved and read back")
    stored = json.loads((pathlib.Path(_DATA) / "settings.json").read_text(encoding="utf-8"))
    stored["tokens"]["mac:24:0a:c4:12:34:56"]["token"] = "bad,value!"
    (pathlib.Path(_DATA) / "settings.json").write_text(json.dumps(stored), encoding="utf-8")
    check(settings.token("mac:24:0a:c4:12:34:56") is None, "a hand-edited value that is not a token reads as none")
    check(settings.branch("wcb") == "main", "the branch setting beside it is untouched")


def main() -> int:
    import shutil
    try:
        case_a()
        case_b()
        case_c_d()
        case_e()
        case_f()
        case_g()
        asyncio.run(case_h())
        case_i()
        case_j()
    finally:
        shutil.rmtree(_DATA, ignore_errors=True)
    print("\nws-auth: " + ("OK" if not _bad else f"{_bad} FAILURE(S)"))
    return 1 if _bad else 0


if __name__ == "__main__":
    sys.exit(main())
