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
import contextlib
import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import host as hostmod                                    # noqa: E402
from transport import TransportError                      # noqa: E402


_serve_error: list = []


def _bind(port: int):
    """Claim the port on the MAIN thread, before anything else happens.

    _wait_until_up cannot tell our server from somebody else's: an older NaviLink
    answers /_api/status exactly as ours would. And it could answer before our
    background thread had even got as far as failing to bind -- measured returning
    True against a two-day-old instance while our own server thread was already
    dead, so every launch opened a window onto the OLD process. The app looked
    like it restarted; the host never did, which meant no code change could ever
    take effect and the port stayed occupied indefinitely.

    Binding here makes ownership unambiguous and settles it before a window opens
    or a serial port is claimed. Deliberately NO SO_REUSEADDR -- failing when
    someone else holds the port is the entire point.
    """
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        s.bind((hostmod.BIND_HOST, port))
        s.listen(128)
        return s, None
    except OSError as e:
        with contextlib.suppress(Exception):
            s.close()
        return None, e


def _serve(sock) -> None:
    """Run the aiohttp app forever on this thread, on an already-bound socket."""
    from aiohttp import web
    import asyncio
    try:
        asyncio.set_event_loop(asyncio.new_event_loop())
        web.run_app(hostmod.build_app(), sock=sock,
                    print=None, handle_signals=False)  # signals belong to the main thread
    except BaseException as e:                          # noqa: BLE001
        # Record it. This thread is a daemon and nothing joins it, so an exception
        # here (overwhelmingly "port already in use") vanished silently and the
        # launcher went on to open a window against somebody else's server.
        _serve_error.append(e)


def _wait_until_up(port: int, timeout: float = 10.0) -> bool:
    """Wait for OUR server, not merely for something listening.

    A bare TCP connect answers yes when a PREVIOUS NaviLink (or anything else) already
    holds the port. Our own bind then failed with WinError 10048, this process opened a
    window onto the other instance, and -- because a command-line target attaches BEFORE
    the server thread starts -- it also sat holding COM5, which the live instance then
    retried forever with "Access is denied". So check that the listener answers our own
    control endpoint, and give up early if the serve thread has already died.
    """
    import json as _json
    import urllib.request
    deadline = time.monotonic() + timeout
    url = f"http://{hostmod.BIND_HOST}:{port}/_api/status"
    while time.monotonic() < deadline:
        if _serve_error:
            return False
        try:
            with urllib.request.urlopen(url, timeout=0.5) as r:
                _json.loads(r.read().decode("utf-8", "replace"))
                return True
        except Exception:
            time.sleep(0.1)
    return False


def main() -> int:
    ap = argparse.ArgumentParser(description="NaviLink desktop app")
    ap.add_argument("--port", type=int, default=hostmod.DEFAULT_PORT)
    ap.add_argument("--serial", help="attach this port at launch and skip the chooser")
    ap.add_argument("--ws", help="attach this droid at launch and skip the chooser")
    ap.add_argument("--ssid", default="NaviCore")
    ap.add_argument("--no-auto-bounce", action="store_true")
    ap.add_argument("--dev", action="store_true",
                    help="enable devtools and the right-click menu in the window")
    ap.add_argument("--browser", action="store_true",
                    help="open the default browser instead of an app window")
    a = ap.parse_args()

    hostmod.auto_bounce = not a.no_auto_bounce

    # BEFORE anything else. A failed bind here means another NaviLink owns the
    # port; carrying on would open a window onto that one and -- because the
    # command-line attach below runs first -- would also grab the serial port the
    # live instance is trying to use, which it then retries forever with
    # "Access is denied".
    sock, bind_err = _bind(a.port)
    if sock is None:
        print(f"NaviLink is already running on {hostmod.BIND_HOST}:{a.port}.",
              file=sys.stderr)
        print("  Close the existing NaviLink window and try again.", file=sys.stderr)
        print(f"  ({bind_err})", file=sys.stderr)
        return 1

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

    threading.Thread(target=_serve, args=(sock,), daemon=True, name="navilink-host").start()
    if not _wait_until_up(a.port):
        why = f": {_serve_error[0]}" if _serve_error else ""
        print(f"host did not start on {hostmod.BIND_HOST}:{a.port}{why}", file=sys.stderr)
        # Release anything the early attach opened, or it stays held by this dying
        # process and no other instance can have it.
        with contextlib.suppress(Exception):
            hostmod.bridge.detach()
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

    # Backend failures do NOT raise ImportError -- pywebview picks its GUI toolkit
    # inside start(), and with neither Cocoa nor Qt usable it raises
    # WebViewException. Guarding only the import above therefore killed the app on
    # exactly the platform this fallback exists for, contradicting the comment on
    # it. Anything that goes wrong here falls through to the browser.
    try:
        webview.create_window("NaviLink", url, width=1280, height=880,
                              min_size=(900, 640), text_select=True)
    except Exception as e:
        print(f"could not create the window ({type(e).__name__}: {e}) "
              "-- falling back to the browser.")
        import webbrowser
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return 0
    # Blocks until the window closes; the daemon host thread goes with it, which is
    # the point of running it in-process.
    # debug=True gives devtools and a context menu — worth having when a reload
    # is not enough and you need to see what the page is actually doing.
    try:
        webview.start(debug=a.dev)
    except Exception as e:
        print(f"the window backend failed ({type(e).__name__}: {e}) "
              "-- falling back to the browser.")
        import webbrowser
        webbrowser.open(url)
        try:
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            pass
        return 0
    hostmod.bridge.detach()      # release the port/socket deliberately, not by exit
    return 0


if __name__ == "__main__":
    sys.exit(main())
