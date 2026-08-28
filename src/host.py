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
from typing import Optional

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

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

    # -- page side ---------------------------------------------------------------
    def bind_page(self, ws: web.WebSocketResponse, loop: asyncio.AbstractEventLoop) -> None:
        self._ws, self._loop = ws, loop

    def unbind_page(self) -> None:
        self._ws = None

    # -- transport side ----------------------------------------------------------
    def attach(self, t: Transport, label: str) -> None:
        self.detach()
        t.open()                      # raises TransportError; caller reports it
        self._transport, self.target_label = t, label
        self.last_error = ""

    def detach(self) -> None:
        t, self._transport = self._transport, None
        self.target_label = ""
        if t:
            with contextlib.suppress(Exception):
                t.close()

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
        self.last_error = why
        self.detach()


bridge = Bridge()


# ── control plane ───────────────────────────────────────────────────────────────
async def api_ports(_req: web.Request) -> web.Response:
    return web.json_response({"ports": list_serial_ports()})


async def api_status(_req: web.Request) -> web.Response:
    return web.json_response({
        "attached": bridge.attached,
        "target": bridge.target_label,
        "lastError": bridge.last_error,
    })


async def api_attach(req: web.Request) -> web.Response:
    try:
        body = await req.json()
    except Exception:
        return web.json_response({"ok": False, "error": "body must be JSON"}, status=400)

    kind = body.get("kind")
    try:
        if kind == "serial":
            port = body.get("port")
            if not port:
                return web.json_response({"ok": False, "error": "port required"}, status=400)
            t: Transport = SerialTransport(port, bridge._on_transport_data,
                                           bridge._on_transport_lost)
            bridge.attach(t, f"serial {port}")
        elif kind == "ws":
            host = body.get("host", "192.168.4.1")
            t = WebSocketTransport(host, bridge._on_transport_data, bridge._on_transport_lost)
            bridge.attach(t, f"ws://{host}/ws")
        else:
            return web.json_response({"ok": False, "error": "kind must be serial|ws"}, status=400)
    except TransportError as e:
        bridge.last_error = str(e)
        return web.json_response({"ok": False, "error": str(e)}, status=502)

    return web.json_response({"ok": True, "target": bridge.target_label})


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
        t.set_signals(dtr=body.get("dataTerminalReady"), rts=body.get("requestToSend"))
    except TransportError as e:
        return web.json_response({"ok": False, "error": str(e)}, status=502)
    return web.json_response({"ok": True})


# ── the byte pipe ───────────────────────────────────────────────────────────────
async def ws_link(req: web.Request) -> web.WebSocketResponse:
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
        bridge.unbind_page()
    return ws


# ── static UI ───────────────────────────────────────────────────────────────────
SHIM_FILE = pathlib.Path(__file__).resolve().parent / "navilink_shim.js"
SHIM_TAG = '<script src="/_navilink.js"></script>'


async def shim_js(_req: web.Request) -> web.StreamResponse:
    return web.FileResponse(SHIM_FILE, headers={"Content-Type": "application/javascript"})


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


def build_app() -> web.Application:
    app = web.Application()
    app.add_routes([
        web.get("/", index),
        web.get("/_api/ports", api_ports),
        web.get("/_api/status", api_status),
        web.post("/_api/attach", api_attach),
        web.post("/_api/detach", api_detach),
        web.post("/_api/signals", api_signals),
        web.get("/_navilink.js", shim_js),
        web.get("/_link", ws_link),
    ])
    if WEBUI_DIR.is_dir():
        app.router.add_static("/", WEBUI_DIR, show_index=False)
    return app


def main() -> int:
    ap = argparse.ArgumentParser(description="NaviLink host")
    ap.add_argument("--port", type=int, default=DEFAULT_PORT)
    ap.add_argument("--serial", help="attach this COM port at startup")
    ap.add_argument("--ws", help="attach this droid host at startup")
    a = ap.parse_args()

    if a.serial and a.ws:
        return ap.error("--serial and --ws are mutually exclusive") or 2

    if a.serial:
        try:
            bridge.attach(SerialTransport(a.serial, bridge._on_transport_data,
                                          bridge._on_transport_lost), f"serial {a.serial}")
            print(f"attached  serial {a.serial}")
        except TransportError as e:
            print(f"attach failed: {e}")
    elif a.ws:
        try:
            bridge.attach(WebSocketTransport(a.ws, bridge._on_transport_data,
                                             bridge._on_transport_lost), f"ws://{a.ws}/ws")
            print(f"attached  ws://{a.ws}/ws")
        except TransportError as e:
            print(f"attach failed: {e}")

    url = f"http://{BIND_HOST}:{a.port}/"
    print(f"serving   {url}")
    print(f"ui        {'bundled' if (WEBUI_DIR / 'index.html').is_file() else 'NOT bundled (see / for why)'}")
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
