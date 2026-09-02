"""NaviLink host — serves the config tool locally and bridges it to a droid.

    python src/host.py                       # serve + open nothing
    python src/host.py --serial COM5         # attach a port at startup
    python src/host.py --ws 192.168.4.1      # attach the droid's AP at startup

                    ┌─ GET /            → the bundled config tool
  browser/webview ──┼─ GET /_api/*      → control (list ports, attach, detach)
                    └─ WS  /_link       → the byte pipe
                                              │
                                              ├─ SerialTransport  → COM port
                                              └─ WebSocketTransport → ws://<droid>/ws

WHY THE PAGE AND THE SOCKET SHARE ONE ORIGIN
Everything is served from one aiohttp app on one port, so the page's WebSocket is
same-origin. That is not tidiness: a page served from https://…github.io cannot
open ws:// (mixed content) and cannot fetch http:// either, which is exactly what
killed the "just point the hosted tool at the board" option. Serving locally makes
the whole class of problem disappear.

WHY THE BRIDGE IS A DUMB BYTE PIPE
It does not parse, reframe, or line-split. The config tool already owns UTF-8
stream decoding, line assembly, `;w20,` bridge framing, fragmentation and write
pacing — all of it hard-won. This moves bytes and nothing else, so serial and
droid-WiFi are literally the same code path in the page.

THREADING
Transports call on_data from their own reader thread. aiohttp lives on an event
loop. Every hand-off between them goes through loop.call_soon_threadsafe — see
_on_transport_data. Getting this wrong produces corruption that only shows up
under load, so it is funnelled through exactly one place.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import pathlib
import sys
import threading
from typing import Optional

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import certs                                             # noqa: E402
import flash                                             # noqa: E402

try:
    from aiohttp import web, WSMsgType                   # noqa: E402
except ImportError:                                      # pragma: no cover
    # Almost always "ran with the wrong interpreter" rather than "not installed":
    # the dependencies live in .venv, and `python src/host.py` uses whatever is on
    # PATH. Say so, because the bare ModuleNotFoundError reads like a broken app.
    sys.stderr.write(
        "\nNaviLink: aiohttp is not available to this interpreter.\n"
        f"  running: {sys.executable}\n\n"
        "Use the launcher, which creates the venv and installs deps on first run:\n"
        "  Windows:  scripts\\run-windows.bat --serial COM5\n"
        "  macOS:    ./scripts/run-macos.command --serial /dev/cu.usbmodem1101\n\n"
        "Or run the venv's interpreter directly:\n"
        "  .venv\\Scripts\\python.exe src\\host.py --serial COM5\n\n"
    )
    raise SystemExit(1)
from transport import Transport, TransportError, list_serial_ports   # noqa: E402
from serial_transport import SerialTransport             # noqa: E402
from ws_transport import WebSocketTransport              # noqa: E402
import discover                                          # noqa: E402

WEBUI_DIR = pathlib.Path(__file__).resolve().parent / "webui"
DEFAULT_PORT = 8765
# Loopback only, always. This exposes an unauthenticated command channel to the
# droid; binding it to a routable address would put that on the network.
BIND_HOST = "127.0.0.1"


class Bridge:
    """Owns at most one transport and fans its bytes to the connected page."""

    def __init__(self) -> None:
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._transport: Optional[Transport] = None
        self._ws: Optional[web.WebSocketResponse] = None
        self.target_label = ""     # human-readable label for /_api/status
        self.last_error = ""
        # What to reconnect to, and whether we should be trying. Set by attach()
        # and NOT cleared when the link drops -- that is the whole point.
        self._spec: Optional[dict] = None
        self._want: bool = False
        self.reconnecting: bool = False
        # When we last heard ANYTHING from the droid. Traffic is proof of life, and
        # cheaper and safer than any probe -- see the idle gate in reconnect_loop().
        self.last_rx: float = 0.0
        # attach()/detach()/_drop() run from the event loop, from asyncio.to_thread
        # workers AND from transport reader threads. Without this they interleave:
        # attach() drops the old link, blocks in open() for up to 5 s, then publishes
        # over whatever a second attach installed meanwhile -- leaking that transport
        # open and unreachable for the life of the process, with its reader still
        # feeding bytes from a device nobody thinks is attached.
        #
        # NEVER hold this across a blocking call. _drop() joins a reader thread and
        # open() waits on a handshake; a reader parked in _on_transport_lost waiting
        # for the same lock would stall the joiner. Take it only around the state
        # swap and do the I/O outside.
        self._lock = threading.RLock()
        # Bumped by every attach() and detach(). An attach whose generation is stale
        # by the time open() returns has been superseded, and must close what it
        # opened rather than publish it.
        self._gen: int = 0

    # -- page side ---------------------------------------------------------------
    def bind_page(self, ws: web.WebSocketResponse, loop: asyncio.AbstractEventLoop) -> None:
        self._ws, self._loop = ws, loop

    def unbind_page(self, ws: Optional[web.WebSocketResponse] = None) -> None:
        """Clear the page binding, but only if this ws is still the bound one.

        Every /_link handler unbinds in its finally. With two overlapping page
        sockets (a second tab, a reload racing its predecessor's teardown, or
        tools/smoke_host.py) the OLD handler's finally would otherwise clear the
        NEW socket, and _on_transport_data then drops every byte on the floor with
        nothing left to re-bind it: the page shows Connected and receives nothing.
        """
        if ws is None or self._ws is ws:
            self._ws = None

    # -- transport side ----------------------------------------------------------
    def attach(self, t: Transport, label: str, spec: Optional[dict] = None) -> None:
        with self._lock:
            self._gen += 1
            mine = self._gen
        self._drop()                  # takes the lock itself; no I/O held under it
        t.open()                      # raises TransportError; caller reports it
        with self._lock:
            superseded = mine != self._gen
            if not superseded:
                self._transport, self.target_label = t, label
                self.last_error = ""
        if superseded:
            # Another attach, or a detach, started while we were inside open().
            # They win: close what we opened instead of publishing over them.
            with contextlib.suppress(Exception):
                t.close()
            raise TransportError("superseded by a newer attach")
        # Start the idle clock NOW, not at 0. time.monotonic() is seconds since boot,
        # so a default of 0.0 made a freshly attached link look idle for the machine's
        # entire uptime — logged as "unreachable after 1212322s idle" (14 days) and,
        # worse, it meant the liveness probe fired immediately on every new
        # connection instead of after the intended quiet period. A link that has
        # simply not spoken yet is not a dead link.
        import time as _t
        with self._lock:
            self.last_rx = _t.monotonic()
            if spec is not None:
                self._spec = spec     # remember it so we can rebuild this link later
                self._want = True

    def detach(self) -> None:
        """User asked to stop. Forget the target so nothing reconnects."""
        with self._lock:
            self._gen += 1            # cancel any attach still inside open()
            self._want = False
            self._spec = None
            self.reconnecting = False
        self._drop()

    def _drop(self) -> None:
        """Tear the link down but KEEP the target — a drop is not a decision."""
        with self._lock:          # swap under the lock, close OUTSIDE it: t.close()
            t, self._transport = self._transport, None   # joins a reader thread and
            self.target_label = ""                       # would stall anyone waiting
        if t:
            with contextlib.suppress(Exception):
                t.close()
        # TELL THE PAGE. Reconnecting transparently underneath it seemed kind, but
        # the tool learns the firmware version from the PONG it gets at connect
        # time and never asks again. So after an OTA the board rebooted into the
        # new firmware, the host silently re-attached, and the tool went on
        # displaying the OLD version — which reads as "the update did not take"
        # and invites flashing it a second time.
        #
        # The droid really did go away, so the page should see that. Dropping its
        # socket makes the tool run its own link-lost path: reconnect, re-handshake,
        # re-PING, and pick up the new version. One honest transition beats a
        # comfortable lie.
        self._close_page()

    def _close_page(self) -> None:
        loop, ws = self._loop, self._ws
        if loop is None or ws is None:
            return
        self._ws = None
        async def shut():
            with contextlib.suppress(Exception):
                await ws.close()
        with contextlib.suppress(Exception):
            loop.call_soon_threadsafe(lambda: asyncio.ensure_future(shut()))

    def set_target(self, spec: dict) -> None:
        """Want this link, whether or not it can be opened right now.

        Separate from attach() so a startup target survives the droid being down
        at launch -- the exact case that otherwise leaves the host serving with
        nothing attached and no intention to retry.
        """
        self._spec = spec
        self._want = True

    def rebuild(self) -> Transport:
        """Construct a fresh transport for the remembered target."""
        spec = self._spec or {}
        if spec.get("kind") == "serial":
            return SerialTransport(spec["port"], self._on_transport_data, self._on_transport_lost)
        return WebSocketTransport(spec.get("host", "192.168.4.1"),
                                  self._on_transport_data, self._on_transport_lost)

    @property
    def wants_link(self) -> bool:
        return self._want and self._spec is not None

    @property
    def attached(self) -> bool:
        return self._transport is not None and self._transport.is_open

    def write(self, data: bytes) -> None:
        t = self._transport
        if t is None:
            raise TransportError("nothing attached")
        t.write(data)                 # raises on device loss — contract 2

    # -- the thread boundary -----------------------------------------------------
    def _on_transport_data(self, chunk: bytes) -> None:
        """Called on a READER THREAD. Must not touch aiohttp directly."""
        import time as _t
        self.last_rx = _t.monotonic()     # cheap, and it gates the liveness probe
        loop, ws = self._loop, self._ws
        if loop is None or ws is None:
            return                    # no page attached; drop rather than buffer forever
        # Bytes stay bytes all the way to the page. A str here would decode
        # per-chunk and mangle any multi-byte character split across a read.
        loop.call_soon_threadsafe(lambda: asyncio.ensure_future(self._push(ws, chunk)))

    async def _push(self, ws: web.WebSocketResponse, chunk: bytes) -> None:
        if ws.closed:
            return
        with contextlib.suppress(Exception):
            await ws.send_bytes(chunk)

    def _on_transport_lost(self, why: str) -> None:
        # A drop is NOT a decision to stop. Keep the target so the reconnect loop
        # rebuilds it -- this is what makes OTA survivable: the board reboots into
        # new firmware, the link necessarily dies, and it must come back by itself.
        self.last_error = why
        self._drop()


bridge = Bridge()


# ── control plane ───────────────────────────────────────────────────────────────
async def api_ports(_req: web.Request) -> web.Response:
    # In a thread: enumerating serial devices is a blocking registry/udev walk and
    # takes a noticeable moment on Windows with a busy USB tree. Run on the loop it
    # stalls the very page that asked for it.
    return web.json_response({"ports": await asyncio.to_thread(list_serial_ports)})


async def api_discover(req: web.Request) -> web.Response:
    """Which droids are reachable, and via which adapter.

    Runs in a thread: the probes are blocking socket work and would otherwise
    stall the event loop that is also serving the page.
    """
    extra = req.query.get("hosts", "")
    cands = [h.strip() for h in extra.split(",") if h.strip()] or None
    found = await asyncio.to_thread(discover.scan, cands)
    return web.json_response({"candidates": found})


async def api_wifi_bounce(req: web.Request) -> web.Response:
    """Reconnect the droid's WLAN profile — the scripted version of the manual
    adapter toggle. Explicit action, never automatic."""
    try:
        body = await req.json()
    except Exception:
        body = {}
    ssid = body.get("ssid", "NaviCore")
    ok, msg = await asyncio.to_thread(discover.wifi_bounce, ssid)
    return web.json_response({"ok": ok, "message": msg})


_DTG_RE = __import__("re").compile(r'id="footer-dtg"[^>]*>([^<]+)<')


def _bundled_dtg() -> str:
    f = WEBUI_DIR / "index.html"
    if not f.is_file():
        return ""
    m = _DTG_RE.search(f.read_text(encoding="utf-8", errors="replace"))
    return m.group(1).strip() if m else ""


# Why the last probe came back empty. Offline is the expected reason and needs no
# fuss, but it is not the ONLY reason, and reporting every failure as "offline" is
# how a stock macOS Python turns a one-line fix into an afternoon. See src/certs.py.
_published_error = ""


def _published_dtg() -> str:
    global _published_error
    import urllib.request
    try:
        with urllib.request.urlopen(
                "https://greghulette.github.io/NaviCore/config_tool/index.html",
                timeout=10, context=certs.context()) as r:
            head = r.read(400_000).decode("utf-8", "replace")   # stamp is near the top
        _published_error = ""
        m = _DTG_RE.search(head)
        return m.group(1).strip() if m else ""
    except Exception as e:
        _published_error = (certs.ADVICE if certs.is_cert_error(e)
                            else f"{type(e).__name__}: {e}")
        return ""            # offline is normal and not an error


async def api_webui_version(_req: web.Request) -> web.Response:
    """Is the bundled config tool behind what is published?

    Worth surfacing rather than leaving to be noticed: the bundle is a COPY, so it
    silently ages every time the tool is updated. Drifting three hours behind while
    debugging the tool's own behaviour is a genuinely confusing place to be.
    """
    bundled = _bundled_dtg()
    published = await asyncio.to_thread(_published_dtg)
    return web.json_response({
        "bundled": bundled,
        "published": published,
        "stale": bool(bundled and published and bundled != published),
        "checked": bool(published),
        "reason": "" if published else _published_error,
    })


LAUNCHER_FILE = pathlib.Path(__file__).resolve().parent / "launcher.html"


async def launcher(_req: web.Request) -> web.StreamResponse:
    return web.FileResponse(LAUNCHER_FILE, headers={
        "Content-Type": "text/html",
        "Cache-Control": "no-store, must-revalidate",
    })


async def api_update_webui(_req: web.Request) -> web.Response:
    """Run the fetcher, so refreshing the tool is a button rather than a command.

    Shelled out rather than imported: it is a script with its own argument
    handling and atomic-swap logic, and duplicating that here would be a second
    implementation to keep in step.
    """
    import subprocess
    script = pathlib.Path(__file__).resolve().parent.parent / "tools" / "fetch_webui.py"

    def run():
        return subprocess.run([sys.executable, str(script)],
                              capture_output=True, text=True, timeout=300)

    try:
        r = await asyncio.to_thread(run)
    except Exception as e:
        return web.json_response({"ok": False, "error": f"{type(e).__name__}: {e}"}, status=500)
    if r.returncode != 0:
        return web.json_response(
            {"ok": False, "error": (r.stdout or r.stderr or "fetch failed").strip()[-300:]},
            status=502)
    return web.json_response({"ok": True, "version": _bundled_dtg(),
                              "log": (r.stdout or "").strip()[-400:]})


# ── Native flashing ─────────────────────────────────────────────────────────
# The page CANNOT flash through this app: navilink_shim.js presents the host's byte
# pipe as a Web Serial port, and esptool-js needs real DTR/RTS to walk the board
# into download mode. Over a WebSocket there are no control lines, so it waits
# forever. The host has the actual port and esptool as a Python package, which is
# both the approach ESP-Flasher-Companion takes and strictly better than the
# browser's -- so do it here and let the shim drive the tool's own buttons.
_flash_lock  = threading.Lock()
_flash_state: dict = {"running": False, "ok": None, "error": "", "version": "",
                      "percent": 0, "log": []}


def _flash_log(msg: str) -> None:
    with _flash_lock:
        _flash_state["log"].append(msg)
        del _flash_state["log"][:-400]      # a full esptool run is chatty
    print(f"flash     {msg}")


def _flash_progress(pct: int) -> None:
    with _flash_lock:
        # Monotonic: esptool restarts its percentage for every region, and a bar
        # that jumps backwards four times reads as a stall, not as progress.
        _flash_state["percent"] = max(_flash_state["percent"], min(99, pct))


async def api_flash(req: web.Request) -> web.Response:
    """Start a native flash. Returns immediately; poll /_api/flash-status.

    Not synchronous: a full write is a minute or more, and holding an HTTP request
    open across a board reset is how you get a timeout that looks like a failure
    while the flash is still running and must not be started twice.
    """
    try:
        body = await req.json()
    except Exception:
        body = {}
    erase_nvs = bool(body.get("eraseNvs"))

    with _flash_lock:
        if _flash_state["running"]:
            return web.json_response({"ok": False, "error": "a flash is already running"},
                                     status=409)

    # Refuse over WiFi rather than failing obscurely three steps later. esptool needs
    # the USB line; a remote board is what "Update over WCB (OTA)" is for.
    spec = dict(bridge._spec or {})
    if spec.get("kind") != "serial" or not spec.get("port"):
        return web.json_response({"ok": False, "error": (
            "Flashing needs a direct USB serial connection. This session is "
            + (_label_for(spec) if spec else "not attached")
            + " — attach the board over USB and try again.")}, status=409)
    port = spec["port"]

    with _flash_lock:
        _flash_state.update(running=True, ok=None, error="", version="",
                            percent=0, log=[])

    async def run_flash() -> None:
        try:
            # esptool opens the device itself, so the port has to be ours to give.
            _flash_log(f"Releasing {port} so esptool can open it...")
            bridge.detach()
            await asyncio.sleep(0.4)          # let the OS finish closing it
            version = await asyncio.to_thread(
                flash.flash, port, erase_nvs, _flash_log, _flash_progress)
            with _flash_lock:
                _flash_state.update(ok=True, version=version, percent=100)
            _flash_log(f"Done — board is running {version}.")
        except Exception as e:                # noqa: BLE001 - reported, never raised at a user
            with _flash_lock:
                _flash_state.update(ok=False, error=str(e))
            _flash_log(f"FAILED: {e}")
        finally:
            # ALWAYS hand the port back, success or not. set_target first so the
            # reconnect loop keeps trying on its own: the board reboots as esptool
            # lets go, so the first attach here often lands before it has finished
            # coming up, and that is normal rather than a failure worth reporting.
            try:
                bridge.set_target(spec)
                await asyncio.to_thread(
                    lambda: bridge.attach(bridge.rebuild(), _label_for(spec), spec))
                _flash_log("Reattached.")
            except Exception as e:            # noqa: BLE001
                _flash_log(f"not reattached yet ({e}); the reconnect loop will retry")
            with _flash_lock:
                _flash_state["running"] = False

    asyncio.create_task(run_flash())
    return web.json_response({"ok": True, "started": True,
                              "target": _label_for(spec), "eraseNvs": erase_nvs})


async def api_flash_status(_req: web.Request) -> web.Response:
    with _flash_lock:
        st = dict(_flash_state)
        st["log"] = list(st["log"])
    return web.json_response(st)


async def api_status(_req: web.Request) -> web.Response:
    return web.json_response({
        "attached": bridge.attached,
        "target": bridge.target_label,
        "lastError": bridge.last_error,
        "reconnecting": bridge.wants_link and not bridge.attached,
        "wantsLink": bridge.wants_link,
    })


async def api_attach(req: web.Request) -> web.Response:
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"ok": False, "error": "body must be JSON"}, status=400)

    kind = body.get("kind")
    if kind == "serial":
        port = body.get("port")
        if not port:
            return web.json_response({"ok": False, "error": "port required"}, status=400)
        spec = {"kind": "serial", "port": port}
    elif kind == "ws":
        # role/ssid ride along with the target.
        #   role  "navicore" (default) or "relay" — the relay is a BRIDGE, so the
        #         tool auto-switches itself to Via WCB once it gets no direct PONG.
        #         Recorded so the label and the UI can say which one you picked.
        #   ssid  which network to re-associate if the link drops. It is not
        #         derivable from the address: on the relay's own AP that is
        #         MgmtRelay-<id>, but with the relay JOINED to NaviCore's AP the
        #         very same relay address sits on the NaviCore network. The
        #         chooser knows which case it saw, so it tells us.
        spec = {"kind": "ws", "host": body.get("host", "192.168.4.1")}
        role = body.get("role")
        if role in ("navicore", "relay"):
            spec["role"] = role
        ssid = body.get("ssid")
        if isinstance(ssid, str) and ssid.strip():
            spec["ssid"] = ssid.strip()
    else:
        return web.json_response({"ok": False, "error": "kind must be serial|ws"}, status=400)

    # Remember it FIRST: an attach that fails now should still be retried by the
    # reconnect loop rather than forgotten, which is the difference between a droid
    # that is merely rebooting and one the user has to re-attach by hand.
    bridge.set_target(spec)
    try:
        # In a thread — open() blocks (pyserial open, WebSocket handshake), and doing
        # it inline wedges the event loop that is also serving the page.
        await asyncio.to_thread(lambda: bridge.attach(bridge.rebuild(),
                                                      _label_for(spec), spec))
    except TransportError as e:
        bridge.last_error = str(e)
        return web.json_response({"ok": False, "error": str(e)}, status=502)

    return web.json_response({"ok": True, "target": bridge.target_label})


async def api_identify(req: web.Request) -> web.Response:
    """Ask a serial port whether it is a NaviCore. Briefly opens the port.

    POST rather than GET because it touches hardware, which also puts it behind
    the cross-origin guard.
    """
    try:
        body = await req.json()
    except Exception:
        body = {}
    port = body.get("port")
    if not port or not isinstance(port, str):
        return web.json_response({"ok": False, "error": "port required"}, status=400)

    spec = bridge._spec or {}
    if (bridge.attached and spec.get("kind") == "serial"
            and spec.get("port") == port):
        # We are holding it ourselves. Probing would fail with "Access is denied"
        # and render as "in use by something else", which is misleading when the
        # something else is this app.
        return web.json_response({"ok": True, "attached": True, "version": None})

    res = await asyncio.to_thread(discover.identify_serial, port)
    return web.json_response({"ok": res["version"] is not None,
                              "version": res["version"], "busy": res["busy"]})


async def api_detach(_req: web.Request) -> web.Response:
    bridge.detach()
    return web.json_response({"ok": True})


async def api_signals(req: web.Request) -> web.Response:
    """DTR/RTS from the page's shimmed setSignals().

    Must actually reach the port. The config tool deasserts both right after
    open() precisely to stop the board resetting, so dropping this on the floor
    would reintroduce the reboot this project exists to fix. A WebSocket target
    has no control lines and no-ops by design (Transport.set_signals).
    """
    try:
        body = await req.json()
    except Exception:
        body = {}
    t = bridge._transport
    if t is None:
        return web.json_response({"ok": False, "error": "nothing attached"}, status=409)
    try:
        # Threaded for the same reason as the rest: driving DTR/RTS is a blocking
        # driver call.
        await asyncio.to_thread(
            lambda: t.set_signals(dtr=body.get("dataTerminalReady"),
                                  rts=body.get("requestToSend")))
    except TransportError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=502)
    return web.json_response({"ok": True})


# ── the byte pipe ───────────────────────────────────────────────────────────────
def _origin_ok(req: web.Request) -> bool:
    """Refuse a cross-origin /_link upgrade.

    This socket is unauthenticated and drives the droid: reboot, SET_CONFIG,
    RESET_DEFAULTS. Binding to 127.0.0.1 keeps other machines out but NOT other
    web pages -- a browser will happily open ws://127.0.0.1:8765/_link from any
    site the user happens to be visiting, because the same-origin policy does not
    apply to WebSockets. Origin is the check that does.

    A MISSING Origin is allowed: browsers always send one, so its absence means a
    native client (tools/smoke_host.py, a script), which is not the threat here.
    """
    origin = req.headers.get("Origin")
    if not origin:
        return True
    from urllib.parse import urlparse
    try:
        host = urlparse(origin).hostname
    except Exception:
        return False
    return host in ("127.0.0.1", "localhost", "::1")


async def ws_link(req: web.Request) -> web.WebSocketResponse:
    if not _origin_ok(req):
        raise web.HTTPForbidden(text="cross-origin /_link is refused")
    ws = web.WebSocketResponse(heartbeat=30)
    await ws.prepare(req)
    bridge.bind_page(ws, asyncio.get_running_loop())
    try:
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                payload = msg.data
            elif msg.type == WSMsgType.TEXT:
                payload = msg.data.encode("utf-8")
            else:
                continue
            try:
                bridge.write(payload)
            except TransportError as e:
                # Tell the page rather than dying quietly. A failed write is how it
                # learns the link is gone (contract 2) — swallowing it here would
                # recreate the frozen-but-"Connected" panel one layer up.
                with contextlib.suppress(Exception):
                    await ws.send_str(json.dumps(
                        {"type": "ERROR", "msg": f"link write failed: {e}"}) + "\n")
    finally:
        bridge.unbind_page(ws)
    return ws


# ── static UI ───────────────────────────────────────────────────────────────────
SHIM_FILE = pathlib.Path(__file__).resolve().parent / "navilink_shim.js"
SHIM_TAG = '<script src="/_navilink.js"></script>'


async def shim_js(_req: web.Request) -> web.StreamResponse:
    # no-store: the shim changes far more often than the tool during development,
    # and a cached copy means editing it appears to do nothing. Costs nothing --
    # it is 7 KB from loopback.
    return web.FileResponse(SHIM_FILE, headers={
        "Content-Type": "application/javascript",
        "Cache-Control": "no-store, must-revalidate",
    })


def _inject_shim(html: str) -> str:
    """Put the shim ahead of the tool's own scripts, at SERVE time only.

    The file on disk is never touched. That is the point: src/webui/index.html
    stays byte-identical to the copy published on Pages, so the update button is
    a plain overwrite and there is no fork to keep in step. Injecting a tag is
    the difference between "we ship the same file" and "we ship our version".
    """
    if SHIM_TAG in html:
        return html
    # Before the FIRST script, whatever it is -- navigator.serial has to exist
    # before any tool code runs, not merely before the tool connects.
    lower = html.lower()
    at = lower.find("<script")
    if at == -1:
        at = lower.find("</head>")
    if at == -1:
        return SHIM_TAG + html          # no head, no script: prepend and hope
    return html[:at] + SHIM_TAG + "\n" + html[at:]


async def index(_req: web.Request) -> web.StreamResponse:
    f = WEBUI_DIR / "index.html"
    if f.is_file():
        return web.Response(
            text=_inject_shim(f.read_text(encoding="utf-8", errors="replace")),
            content_type="text/html",
            # no-store, or a bundle refresh does nothing visible: the browser keeps
            # serving the page it already has and the user concludes the update
            # failed. That is exactly what happened after the first fetch_webui run
            # — the file on disk was current while the tab was hours behind. Costs
            # nothing here; it is a local read over loopback.
            headers={"Cache-Control": "no-store, must-revalidate"},
        )
    if not f.is_file():
        # Explain rather than 404. src/webui/ is gitignored and populated at build
        # time from the public NaviCore repo — an empty one is the normal state of a
        # fresh clone, not a fault.
        return web.Response(status=503, content_type="text/plain", text=(
            "No bundled config tool.\n\n"
            f"Expected: {f}\n\n"
            "src/webui/ is deliberately NOT committed — the public NaviCore repo is the\n"
            "single source of truth for the UI and a copy here would be a fork waiting to\n"
            "happen. Fetch it from https://greghulette.github.io/NaviCore/config_tool/\n"
            "(index.html + flasher.js + serial-hub.js + cmdlib/ — about 30 files).\n\n"
            "The control API and the /_link byte pipe work without it."
        ))
    return web.FileResponse(f)


# How long the link may stay down before we suspect the routing problem rather than
# a droid that is simply still booting. A NaviCore is back in ~3 s over USB but an
# AP restart plus re-association is slower, so wait long enough not to bounce an
# adapter that was about to recover on its own.
BOUNCE_AFTER_FAILS = 2       # act before Windows gets round to it on its own
# Only probe after this much silence, and require this many consecutive failures.
#
# PATIENCE IS THE POINT, not speed. Declaring death early does not get the link
# back sooner -- measured, twice -- because the floor is Windows deciding the
# association is gone at ~12 s. What early declaration DOES do is reconnect into a
# state that has not settled: the AP really is back at 2.6 s, so the reconnect
# succeeds, and then the link dies again at ~12 s when the stale lease is finally
# dropped. The page sees connect -> disconnect -> connect instead of one clean
# transition, which reads as instability that is not there.
#
# So wait until we would be right. Roughly 6 s idle plus 3 failures lands at ~9 s,
# just inside Windows' own conclusion, and produces ONE transition.
PROBE_IDLE_S = 6.0
PROBE_FAILS_NEEDED = 3
BOUNCE_COOLDOWN_S  = 10.0    # short: a bounce is now conditional on being misrouted,
                             # so retrying is cheap and the first one after the AP
                             # returns is the one that sticks
auto_bounce = True           # --no-auto-bounce turns it off


async def reconnect_loop(_app: web.Application) -> None:
    """Rebuild the link whenever it is down but still wanted.

    THIS IS WHAT MAKES OTA WORK. Flashing reboots the board, so the link is
    GUARANTEED to die partway through -- that is a normal step, not a failure. The
    tool's reopenAfterFlash() then re-opens its port and waits for a PONG; through
    the shim that re-open reaches the HOST, which succeeds instantly, so without
    this loop the tool sees a healthy socket with a dead droid behind it and gives
    up. Same story for the stale-adapter case: cycle the adapter and it comes back
    on its own instead of needing a manual re-attach.

    Poll fast (1 s). A NaviCore is back in ~3 s over USB; over WiFi the AP has to
    restart and the client re-associate, which can take 15-30 s. Retrying a local
    open() every second for that long is free, and a slow reconnect is the
    difference between "it recovered" and "it hung".
    """
    fails = 0
    last_bounce = -1e9
    probe_fails = 0
    while True:
        try:
            await asyncio.sleep(1.0)

            # ── Route-based liveness ──────────────────────────────────────────
            # Do not wait for the socket to notice. When the droid's AP goes away
            # the TCP connection can look healthy for a long time -- measured at
            # 12.5 s even with 1 s WebSocket keepalive pings, because the failure is
            # below TCP and the pings simply queue.
            #
            # But the OS knows immediately: the moment the lease drops, the source
            # address it would pick for the droid stops being on the droid's subnet.
            # Asking that question costs a UDP connect() with no traffic, so it can
            # be asked every second, and it detects the exact failure in ~1 s rather
            # than waiting out a TCP timeout.
            spec_now = bridge._spec or {}
            if bridge.attached and spec_now.get("kind") == "ws":
                host_now = spec_now.get("host", "192.168.4.1")
                import time as _t
                # Same clock as _on_transport_data — loop.time() is not guaranteed
                # to be monotonic() and mixing them makes idle nonsense.
                idle = _t.monotonic() - (bridge.last_rx or 0)

                # ACTIVE PROBE, gated on IDLE.
                #
                # Watching the socket or the route both wait on Windows deciding the
                # association is gone -- measured at ~12 s either way. A TCP connect
                # fails as soon as ARP does, which is earlier, so probing genuinely
                # beats both.
                #
                # But it must never fire during an OTA. Writing flash on an ESP32
                # disables the instruction cache and can stall BOTH cores, so a probe
                # mid-erase can fail on a perfectly healthy board -- and tearing the
                # link down mid-update is far worse than a slow reconnect.
                #
                # Hence the idle gate: traffic IS proof of life, so while bytes are
                # flowing (an OTA, the live monitor, any command) we never probe at
                # all. Only genuine silence gets probed, which is exactly the case
                # where a dead AP hides.
                if idle > PROBE_IDLE_S:
                    alive = await asyncio.to_thread(discover.tcp_open, host_now, 80, 1.0)
                    if alive:
                        probe_fails = 0
                    else:
                        probe_fails += 1
                        # Several in a row: one failure could be a momentary stall,
                        # and acting early only causes a reconnect flap (see the
                        # note on PROBE_IDLE_S).
                        if probe_fails >= PROBE_FAILS_NEEDED:
                            print(f"{host_now} unreachable after {idle:.0f}s idle — link is dead")
                            bridge.last_error = "probe failed (droid AP down?)"
                            probe_fails = 0
                            bridge._drop()   # keep the target; the loop rebuilds it
                else:
                    probe_fails = 0          # traffic is flowing; nothing to prove

            if bridge.attached or not bridge.wants_link:
                bridge.reconnecting = False
                continue
            try:
                # IN A THREAD. Transport.open() is blocking -- pyserial's open and
                # the WebSocket handshake both are, the latter for up to its connect
                # timeout. Calling it directly from here stalls the event loop, so
                # while a droid was unreachable the whole server stopped answering:
                # /_api/status timed out, and the page went dead exactly when the
                # user most needs it to explain itself.
                spec = bridge._spec
                await asyncio.to_thread(lambda: bridge.attach(bridge.rebuild(),
                                                              _label_for(spec), spec))
                print(f"reconnected  {bridge.target_label}")
                fails = 0
            except TransportError:
                fails += 1
                # ── Self-heal the routing problem ─────────────────────────────
                # A NaviCore reboot takes its SoftAP down. Windows drops the DHCP
                # lease on the adapter joined to it, which removes the on-link
                # route for 192.168.4.0/24 -- so traffic for the droid falls back
                # to the DEFAULT route and leaves via the house network, where it
                # dies. Retrying cannot fix that: every attempt goes out the wrong
                # adapter. Only re-associating restores the route.
                #
                # Safe to do unattended because the bounce is scoped to the single
                # interface associated with the droid's SSID -- the house adapter is
                # never touched. That scoping is what makes automation reasonable
                # here when it would not have been for a blanket `netsh wlan
                # disconnect`.
                spec = bridge._spec or {}
                now = asyncio.get_running_loop().time()
                if (auto_bounce and fails >= BOUNCE_AFTER_FAILS
                        and spec.get("kind") == "ws"
                        and (now - last_bounce) > BOUNCE_COOLDOWN_S):
                    # Bounce only when the ROUTE is actually wrong -- i.e. the source
                    # address the OS would use to reach the droid is not on the
                    # droid's own subnet. That is the specific damage a reboot does
                    # (lease lost, on-link route gone, traffic falls back to the
                    # default route and leaves via the house adapter).
                    #
                    # Checking this rather than just counting seconds matters: if the
                    # route is fine and the droid is merely still booting, bouncing
                    # achieves nothing and burns a cooldown -- which is exactly what
                    # made the first measured recovery take 73 s instead of ~15 s.
                    host = spec.get("host", "192.168.4.1")
                    via = await asyncio.to_thread(discover.local_ip_for, host)
                    same_subnet = bool(via) and via.rsplit(".", 1)[0] == host.rsplit(".", 1)[0]
                    if not same_subnet:
                        last_bounce = now
                        ssid = spec.get("ssid", "NaviCore")
                        ok, msg = await asyncio.to_thread(discover.wifi_bounce, ssid)
                        print(f"routed via {via or 'nothing'} instead of {host} — "
                              f"re-associating {ssid}: {'ok' if ok else 'FAILED'} ({msg})")
                        fails = 0
        except asyncio.CancelledError:
            raise
        except Exception:
            # A bug in here must never take the server down -- the page and the
            # control API stay useful even when nothing is attached.
            pass


def _label_for(spec: Optional[dict]) -> str:
    if not spec:
        return "link"
    if spec.get("kind") == "serial":
        return f"serial {spec.get('port')}"
    host = spec.get("host", "192.168.4.1")
    if spec.get("role") == "relay":
        # Say so. Through a relay everything rides the mesh -- 187-byte payloads,
        # fragmented config, the slower OTA path -- and that is worth seeing in the
        # status line rather than inferring from a bare address.
        return f"relay {host} → mesh"
    return f"ws://{host}/ws"


async def _start_bg(app: web.Application) -> None:
    app["reconnect"] = asyncio.create_task(reconnect_loop(app))


async def _stop_bg(app: web.Application) -> None:
    task = app.get("reconnect")
    if task:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task


@web.middleware
async def _guard_origin(req: web.Request, handler):
    """Refuse cross-origin state changes.

    The Origin check on /_link is not enough on its own. A cross-origin fetch()
    carrying JSON triggers a CORS preflight this server never answers, so those are
    already blocked -- but a plain HTML form POST is a "simple request" and needs no
    preflight, and the handlers that tolerate an unparseable body accept it anyway:
    /_api/detach takes no body at all, and /_api/wifi-bounce falls back to
    ssid="NaviCore" and bounces the adapter. So any page the user is visiting could
    drop their droid link or cycle their Wi-Fi.

    Applied to every POST rather than a list, so a handler added later is covered
    without anyone having to remember. GETs are read-only and stay open.
    """
    if req.method == "POST" and not _origin_ok(req):
        # SAY SO. A silent 403 on every POST is indistinguishable from a frozen
        # app: the page's buttons simply stop working. If a future window backend
        # sends an Origin this does not recognise, this line is the only thing
        # that will point at the guard rather than at the droid.
        print(f"refused cross-origin POST {req.path} from Origin="
              f"{req.headers.get('Origin')!r}")
        return web.json_response(
            {"ok": False, "error": "cross-origin request refused"}, status=403)
    return await handler(req)


def build_app() -> web.Application:
    app = web.Application(middlewares=[_guard_origin])
    app.on_startup.append(_start_bg)
    app.on_cleanup.append(_stop_bg)
    app.add_routes([
        web.get("/", index),
        web.get("/_api/ports", api_ports),
        web.get("/_api/status", api_status),
        web.get("/_api/discover", api_discover),
        web.get("/_api/webui-version", api_webui_version),
        web.post("/_api/update-webui", api_update_webui),
        web.post("/_api/flash", api_flash),
        web.get("/_api/flash-status", api_flash_status),
        web.get("/_launcher", launcher),
        web.post("/_api/wifi-bounce", api_wifi_bounce),
        web.post("/_api/attach", api_attach),
        web.post("/_api/detach", api_detach),
        web.post("/_api/identify", api_identify),
        web.post("/_api/signals", api_signals),
        web.get("/_navilink.js", shim_js),
        web.get("/_link", ws_link),
    ])
    # Create it, then register UNCONDITIONALLY. src/webui/ is gitignored, so on a
    # fresh clone it does not exist, the static route was never added, and aiohttp
    # fixes its routing table at build time. Pressing "Update tool" then populated
    # the directory and "/" began serving index.html (a per-request handler), while
    # every asset that page pulls -- flasher.js, serial-hub.js, the whole
    # cmdlib/*.json library -- kept 404ing until the process was restarted.
    # Silently: the page renders, the flasher fails only when used, the command
    # library is simply empty. index() already explains an empty directory itself.
    WEBUI_DIR.mkdir(parents=True, exist_ok=True)
    app.router.add_static("/", WEBUI_DIR, show_index=False)
    return app


def main() -> int:
    ap = argparse.ArgumentParser(description="NaviLink host")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--serial", help="attach this COM port at startup")
    ap.add_argument("--ws", help="attach this droid host at startup")
    ap.add_argument("--ssid", default="NaviCore",
                    help="SSID to re-associate when the droid AP restarts (default: NaviCore)")
    ap.add_argument("--no-auto-bounce", action="store_true",
                    help="do not re-associate the droid adapter automatically")
    a = ap.parse_args()

    if a.serial and a.ws:
        return ap.error("--serial and --ws are mutually exclusive") or 2

    # Record the target BEFORE trying, so a droid that is down at launch is still
    # waited for rather than silently forgotten.
    spec = ({"kind": "serial", "port": a.serial} if a.serial
            else {"kind": "ws", "host": a.ws, "ssid": a.ssid} if a.ws else None)
    global auto_bounce
    auto_bounce = not a.no_auto_bounce
    if spec:
        bridge.set_target(spec)
        try:
            bridge.attach(bridge.rebuild(), _label_for(spec), spec)
            print(f"attached  {bridge.target_label}")
        except TransportError as e:
            print(f"attach failed: {e}")
            print("           will keep retrying every second")

    url = f"http://{BIND_HOST}:{a.port}/"
    sys.stdout.reconfigure(line_buffering=True)   # so a redirected log is live, not buffered
    print(f"serving   {url}")
    _b = _bundled_dtg()
    _p = _published_dtg()
    if not _b:
        print("ui        NOT bundled (see / for why)")
    elif _p and _p != _b:
        print(f"ui        {_b}  ** OUT OF DATE ** published is {_p}")
        print("          refresh with: python tools/fetch_webui.py")
    else:
        print(f"ui        {_b}")
        if not _p:
            print("          could not compare with Pages:")
            for _line in (_published_error or "offline").splitlines():
                print(f"          {_line}")
    print("ctrl-c to stop")
    try:
        web.run_app(build_app(), host=BIND_HOST, port=a.port, print=None)
    except KeyboardInterrupt:
        pass
    finally:
        bridge.detach()
    return 0


if __name__ == "__main__":
    sys.exit(main())
