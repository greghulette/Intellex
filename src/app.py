"""NaviLink — the desktop app.

    python src/app.py                  # window, pick a droid or a port
    python src/app.py --serial COM5    # skip the chooser, attach on launch
    python src/app.py --ws 192.168.4.1
    python src/app.py --browser        # no window; open the default browser

Runs the host on a background thread and puts a window in front of it. The window
is a webview pointed at http://127.0.0.1 -- so the UI is the SAME config tool the
browser gets, from the same server, with the same shim underneath.

WHY THE HOST RUNS IN-PROCESS
It could be a separate process, but then closing the window would leave a serial
port or a socket held by an orphan, and the next launch would fail to attach for
reasons nobody could see. One process means the link dies with the window.

WHY THE CHOOSER IS HTML AND NOT NATIVE
It needs to show discovered droids, serial ports, and the tool's version -- all
of which already exist as /_api endpoints. A native chooser would be a second UI
toolkit in the project for one screen, and would still have to call those same
endpoints. Serving /_launcher costs nothing and looks like the rest of the app.
"""

from __future__ import annotations

import argparse
import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import host as hostmod                                    # noqa: E402
from transport import TransportError                      # noqa: E402


def _serve(port: int) -> None:
    """Run the aiohttp app forever on this thread."""
    from aiohttp import web
    import asyncio
    asyncio.set_event_loop(asyncio.new_event_loop())
    web.run_app(hostmod.build_app(), host=hostmod.BIND_HOST, port=port,
                print=None, handle_signals=False)   # signals belong to the main thread


def _wait_until_up(port: int, timeout: float = 10.0) -> bool:
    """Do not open a window onto a server that is not listening yet."""
    import socket
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            with socket.create_connection((hostmod.BIND_HOST, port), timeout=0.5):
                return True
        except OSError:
            time.sleep(0.1)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="NaviLink desktop app")
    ap.add_argument("--port", type=int, default=hostmod.DEFAULT_PORT)
    ap.add_argument("--serial", help="attach this port at launch and skip the chooser")
    ap.add_argument("--ws", help="attach this droid at launch and skip the chooser")
    ap.add_argument("--ssid", default="NaviCore")
    ap.add_argument("--no-auto-bounce", action="store_true")
    ap.add_argument("--browser", action="store_true",
                    help="open the default browser instead of an app window")
    a = ap.parse_args()

    hostmod.auto_bounce = not a.no_auto_bounce

    # A target given on the command line skips the chooser and lands on the tool.
    spec = ({"kind": "serial", "port": a.serial} if a.serial
            else {"kind": "ws", "host": a.ws, "ssid": a.ssid} if a.ws else None)
    if spec:
        hostmod.bridge.set_target(spec)
        try:
            hostmod.bridge.attach(hostmod.bridge.rebuild(), hostmod._label_for(spec), spec)
        except TransportError as e:
            # Not fatal: the reconnect loop keeps trying, and the window can show
            # the chooser meanwhile rather than refusing to start.
            print(f"attach failed ({e}) — will keep retrying")

    threading.Thread(target=_serve, args=(a.port,), daemon=True, name="navilink-host").start()
    if not _wait_until_up(a.port):
        print(f"host did not start on {hostmod.BIND_HOST}:{a.port}", file=sys.stderr)
        return 1

    # Straight to the tool when a target was given; otherwise pick one first.
    landing = "/" if spec else "/_launcher"
    url = f"http://{hostmod.BIND_HOST}:{a.port}{landing}"

    if a.browser:
        import webbrowser
        print(f"serving {url}")
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return 0

    try:
        import webview
    except ImportError:
        # Do not fail: the whole app works in a browser, the window is a nicety.
        print("pywebview not installed — falling back to the browser.")
        print("  install it with:  pip install -r requirements.txt")
        import webbrowser
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return 0

    webview.create_window("NaviLink", url, width=1280, height=880,
                          min_size=(900, 640), text_select=True)
    # Blocks until the window closes; the daemon host thread goes with it, which is
    # the point of running it in-process.
    webview.start()
    hostmod.bridge.detach()      # release the port/socket deliberately, not by exit
    return 0


if __name__ == "__main__":
    sys.exit(main())
