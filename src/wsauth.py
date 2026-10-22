"""Logging in to a droid whose WebSocket asks for an access token.

WHY THERE IS ONE
A board that joins a router puts its command channel on that router's network, in
front of everyone on it. So the firmware can hold a token, and a WebSocket session
runs nothing until it logs in. The same protocol on every WebSocket that takes
commands: the WCB's (Code/WCB/WCB_WS.cpp), NaviCore's (navicore_wsserver.h) and
WCBClient's MgmtRelay example (mgmt_wsserver.h):

  - A server with a token set sends the line [AUTH:REQUIRED] as a session opens.
    Until the session logs in it gets NO console mirror, and every line it sends is
    dropped unexecuted and answered [AUTH:REQUIRED].
  - ?AUTH,<token> is answered [AUTH:OK] (logged in for the life of the session) or
    [AUTH:FAIL], after which the server closes the session -- and for 1 s after ANY
    failure, every ?AUTH on any session fails.
  - A server with no token sends nothing on open and answers ?AUTH,<anything> with
    [AUTH:OK].

The tools never send ?AUTH: Intellex does it underneath them, here, and takes the
[AUTH:...] lines it handled out of what the pages see (AuthLines). Login is per
session, so every attach and every reattach logs in again.

ANSWERED, NEVER VOLUNTEERED. ?AUTH goes out only after the board has said
[AUTH:REQUIRED]. Sending it at every open would put a command nobody asked for on
every board that has no token -- and on every board running firmware from before
tokens, which answers it as an unknown command in a console both tools mirror --
and it would need the board's key looked up (an ARP query, droid_key) on every
attach of every board, for the few that have a token. The cost of waiting is
PROMPT_WAIT_S on an attach to a board that has no token and says nothing at open.

ONE TRY PER TOKEN. A token the board refused is never sent to it again until the
user saves one (token_saved clears the mark, even for the same value): a reconnect
loop or a discovery scan resending it every pass would fail every pass, and each
failure locks the board's ?AUTH for 1 s for every session, the user's other client
included. And the next try waits out that second (LOCKOUT_S): a token typed and
saved straight after a refusal would otherwise be refused by the lockout, and
marked wrong when it was right.

WHO GETS A TOKEN: the board it was saved for (droid_key). Never the address --
every access point here is 192.168.4.1.
"""
from __future__ import annotations

import threading
import time
from typing import Optional

import settings
from transport import TransportError

AUTH_REQUIRED = "[AUTH:REQUIRED]"
AUTH_OK = "[AUTH:OK]"
AUTH_FAIL = "[AUTH:FAIL]"

# How long an open waits for [AUTH:REQUIRED] from a board that says nothing at all.
# A board with a token sends it the moment the session opens, from its loop task, so
# it normally arrives in a few ms; one that has no token sends nothing, and this is
# what an attach to it costs. Ended early by ANY bytes: a board with a token sends
# nothing else before a login, so anything else means it has none.
PROMPT_WAIT_S = 1.0
# The same wait for a host that once prompted after it ran out (AuthLines' late
# prompt, note_slow): without the longer wait such a board would flap forever --
# attach, prompt arrives late, drop to log in, attach again, late again.
PROMPT_WAIT_SLOW_S = 5.0
# [AUTH:OK] or [AUTH:FAIL] after ?AUTH. Generous: the same board was measured
# stalling 3.8 s on a plain ICMP ping (ws_transport.py), and a wait that ends early
# turns a right token into "did not answer".
REPLY_WAIT_S = 5.0
# The board refuses every ?AUTH for 1 s after any failure; a little over it.
LOCKOUT_S = 1.2

_lock = threading.Lock()
_refused: dict = {}        # key -> the token the board refused (not sent again)
_failed_at: dict = {}      # key -> time.monotonic() of the refusal (the lockout)
_slow: set = set()         # hosts that prompted after PROMPT_WAIT_S


class AuthNeeded(TransportError):
    """The board asked for its access token and there is none to send: none saved
    for it (why "missing"), or the one saved was refused (why "rejected").

    A TransportError, so every caller that handles a failed attach still does; the
    host catches it first, holds the reconnect loop and asks the user (host.py
    Bridge.note_auth). Its message names the board and never carries a token.
    """

    def __init__(self, key: str, label: str, host: str, why: str):
        self.key, self.label, self.host, self.why = key, label, host, why
        super().__init__(need_message(label, why))

    def as_dict(self) -> dict:
        return {"key": self.key, "label": self.label, "host": self.host,
                "why": self.why, "message": str(self)}


def need_message(label: str, why: str) -> str:
    if why == "rejected":
        return f"{label} refused the saved access token"
    return f"{label} needs its access token"


# ── Which board this is ─────────────────────────────────────────────────────
def droid_key(host: str) -> str:
    """The name a token for the board at `host` is saved under. Asked when the
    board prompts, so a connection to it is open.

    THE BOARD'S MAC ADDRESS, from the ARP cache (discover.mac_for_host), wherever it
    can be read -- not the network name, and never the address alone:
      - The address is shared by every droid. Every access point here is 192.168.4.1,
        so a token kept by address is sent to whichever board's network the laptop
        joined last.
      - The network name tells the access points apart, on Windows. But macOS cannot
        read it (ssid_for_host answers None there), which would leave every droid on
        a Mac under the one address. And every board that joins the same router
        shares the router's name -- the case the token exists for -- so two boards
        with two tokens would take turns refusing each other's.
      - The MAC is the board's own: different for every board, the same on both
        platforms and on every network, and read from the connection the token is
        about to go down. So moving the laptop to another board's network cannot
        carry one droid's token to the next: the key is worked out again from
        whatever answered, at every login.
    One board can have two keys: an ESP32's access point and its station have
    different MACs, so a board moved from hosting its own network to joining a
    router asks for its token once more.

    Falls back when the cache cannot say (discover.mac_for_host): to the network
    name with the address, then the address alone (a loopback test host, a board
    beyond a router). Those are the weaker keys above.
    """
    import discover
    if host in ("127.0.0.1", "localhost", "::1"):
        return f"host:{host}"
    mac = discover.mac_for_host(host)
    if mac:
        return f"mac:{mac}"
    ssid = discover.ssid_for_host(host)
    return f"ssid:{ssid}@{host}" if ssid else f"host:{host}"


def droid_label(host: str) -> str:
    """How to name the board at `host` to the user: its network and its address
    where the OS can say (Windows), else the address. Asked only when there is
    something to tell the user -- it costs two netsh runs, which a login that
    works has no use for."""
    import discover
    if host in ("127.0.0.1", "localhost", "::1"):
        return host
    ssid = discover.ssid_for_host(host)
    return f'"{ssid}" ({host})' if ssid else host


# ── What may be sent ────────────────────────────────────────────────────────
def usable_token(key: str) -> Optional[str]:
    """The token to send the board `key` names: saved, and not refused since."""
    t = settings.token(key)
    with _lock:
        if t is None or _refused.get(key) == t:
            return None
    return t


def can_try(key: str) -> bool:
    return usable_token(key) is not None


def why_not(key: str) -> str:
    """Why usable_token(key) is None: "rejected" when a saved token was refused."""
    t = settings.token(key)
    with _lock:
        return "rejected" if t is not None and _refused.get(key) == t else "missing"


def token_saved(key: str) -> None:
    """The user saved a token for `key` (POST /_api/token): it may be tried, even
    when it is the one refused before. The lockout still applies."""
    with _lock:
        _refused.pop(key, None)


def note_refused(key: str, t: str) -> None:
    with _lock:
        _refused[key] = t
        _failed_at[key] = time.monotonic()


def note_accepted(key: str) -> None:
    with _lock:
        _refused.pop(key, None)


def wait_out_lockout(key: str) -> None:
    """Sleep until the board's 1 s refusal window after our last failure has passed."""
    with _lock:
        at = _failed_at.get(key)
    if at is not None:
        left = LOCKOUT_S - (time.monotonic() - at)
        if left > 0:
            time.sleep(left)


def note_slow(host: str) -> None:
    with _lock:
        _slow.add(host)


def prompt_wait(host: str) -> float:
    with _lock:
        return PROMPT_WAIT_SLOW_S if host in _slow else PROMPT_WAIT_S


# ── The [AUTH:...] lines, taken out of the byte stream ──────────────────────
class AuthLines:
    """Takes whole [AUTH:REQUIRED] / [AUTH:OK] / [AUTH:FAIL] lines out of a byte
    stream and passes every other byte through UNTOUCHED.

    The pages get raw bytes, never re-split or re-encoded lines (CLAUDE.md rule 1),
    so this only ever removes whole lines and holds nothing back but the start of a
    line that could still become one of these -- at most "[AUTH:" and the rest of
    its line, never more than _MAX_HELD bytes. Cutting only at a newline cannot split
    a UTF-8 character: no byte of a multi-byte character is 0x0A. Held bytes go out,
    in order, the moment the line turns out to be anything else.

    A WebSocket message is a unit as well, so an [AUTH:...] word that ends a message
    counts as a whole line even without its newline: a server that sends the prompt
    as a bare frame would otherwise have it held until a newline that never comes.
    If its line ending does follow, in the next message, it goes too (_eat).

    feed() returns (bytes to pass on, [the AUTH lines found, in order]).
    """
    _WORDS = (AUTH_REQUIRED.encode(), AUTH_OK.encode(), AUTH_FAIL.encode())
    _PREFIX = b"[AUTH:"
    _MAX_HELD = 64

    def __init__(self) -> None:
        self._held = b""
        self._bol = True            # the next byte starts a line
        self._eat = False           # a message ended on an AUTH word: its \r\n may follow

    def feed(self, chunk: bytes) -> tuple[bytes, list]:
        data = bytes(chunk)
        if self._eat:
            if data[:1] == b"\r":
                data = data[1:]
            if data[:1] == b"\n":
                data, self._eat = data[1:], False
            elif data:
                self._eat = False
        data = self._held + data
        self._held = b""
        out = bytearray()
        found = []
        i, n = 0, len(data)
        while i < n:
            nl = data.find(b"\n", i)
            seg = data[i:] if nl < 0 else data[i:nl + 1]
            whole = nl >= 0
            if self._bol:
                if whole and seg.rstrip(b"\r\n") in self._WORDS:
                    found.append(seg.rstrip(b"\r\n").decode("ascii"))
                    i += len(seg)
                    continue
                if (not whole and len(seg) <= self._MAX_HELD
                        and (self._PREFIX.startswith(seg) or seg.startswith(self._PREFIX))):
                    self._held = seg            # could still be one of ours
                    break
            out += seg
            self._bol = whole
            i += len(seg)
        if self._held and self._held.rstrip(b"\r") in self._WORDS:
            found.append(self._held.rstrip(b"\r").decode("ascii"))
            self._held, self._eat = b"", True
        return bytes(out), found


def _as_bytes(msg) -> bytes:
    return msg.encode("utf-8") if isinstance(msg, str) else bytes(msg)


# ── The two halves of a login, over a websockets.sync connection ────────────
def exchange(ws, host: str, lines: AuthLines) -> bytes:
    """The board at `host` has said [AUTH:REQUIRED]: send its token and wait for
    the verdict. Returns the bytes after [AUTH:OK] (the console mirror starting).

    Raises AuthNeeded with nothing sent when there is no token to send, AuthNeeded
    "rejected" on [AUTH:FAIL] (the board closes the session), and TransportError
    when the board does not answer or goes away. Used by both the transport's open()
    and discover.probe(), so the two cannot log in differently.
    """
    key = droid_key(host)
    tok = usable_token(key)
    if tok is None:
        raise AuthNeeded(key, droid_label(host), host, why_not(key))
    wait_out_lockout(key)
    try:
        # Newline-terminated, one line in one message, like every host-originated
        # line here (discover.PROBE_LINES): all three servers frame on \n or \r.
        ws.send("?AUTH," + tok + "\n")
    except Exception as e:                  # the payload is never in the message
        raise TransportError(f"{host}: could not send its access token ({type(e).__name__})") from None
    end = time.monotonic() + REPLY_WAIT_S
    out = bytearray()
    while True:
        left = end - time.monotonic()
        if left <= 0:
            raise TransportError(f"{host} did not answer its access token within {REPLY_WAIT_S:.0f} s")
        try:
            msg = ws.recv(timeout=left)
        except TimeoutError:
            continue
        except Exception as e:
            raise TransportError(f"{host} closed the link before answering its access token "
                                 f"({type(e).__name__})") from None
        passed, found = lines.feed(_as_bytes(msg))
        out += passed
        # Repeated prompts are ignored: they answer lines sent before the login.
        if AUTH_FAIL in found:
            note_refused(key, tok)
            raise AuthNeeded(key, droid_label(host), host, "rejected")
        if AUTH_OK in found:
            note_accepted(key)
            return bytes(out)


def login(ws, host: str, lines: AuthLines) -> tuple[bytes, bool]:
    """Right after a WebSocket to `host` opens: does the board ask for a token? If
    it does, answer (exchange). Returns (bytes to pass on, logged in).

    Waits up to prompt_wait(host) for the first bytes. [AUTH:REQUIRED] -> log in.
    Anything else -> the board has no token (one with a token sends nothing else
    before a login), so it is passed on and the link is open. Silence -> no token as
    far as anyone can tell; a prompt that comes after all is AuthLines' late case,
    handled by the transport's reader.
    """
    end = time.monotonic() + prompt_wait(host)
    out = bytearray()
    while True:
        left = end - time.monotonic()
        if left <= 0:
            return bytes(out), False
        try:
            msg = ws.recv(timeout=left)
        except TimeoutError:
            return bytes(out), False
        except Exception as e:
            raise TransportError(f"{host} closed the link as it opened ({type(e).__name__}: {e})") from None
        passed, found = lines.feed(_as_bytes(msg))
        out += passed
        if AUTH_REQUIRED in found:
            out += exchange(ws, host, lines)
            return bytes(out), True
        if out:
            return bytes(out), False
