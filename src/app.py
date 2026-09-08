"""Intellex — the desktop app.

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
import os
import pathlib
import sys
import threading
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))

import applog                                             # noqa: E402
import appicon                                            # noqa: E402
import host as hostmod                                    # noqa: E402
import winsize                                            # noqa: E402
from transport import TransportError                      # noqa: E402


_serve_error: list = []


def _bind(port: int):
    """Claim the port on the MAIN thread, before anything else happens.

    _wait_until_up cannot tell our server from somebody else's: an older Intellex
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


def _tell_user(msg: str) -> None:
    """Say something the user can actually see.

    Intellex.bat launches with pythonw, which has NO CONSOLE -- so a message on
    stderr goes precisely nowhere. Refusing to start was therefore completely
    silent: double-click, nothing appears, no explanation. Worse, the reason it
    refuses is usually "an older copy is still running", so the user goes on using
    a stale instance believing they restarted it. That is exactly how a two-day-old
    host survived every restart, and how a launch flag can appear to have been
    ignored.

    A message box is the only channel that exists in a windowed process. Best
    effort: if even this fails there is nothing further to try, and the stderr
    print above still serves anyone running from a terminal.
    """
    if sys.platform != "win32":
        return
    try:
        import ctypes
        MB_OK, MB_ICONWARNING, MB_TOPMOST = 0x0, 0x30, 0x40000
        ctypes.windll.user32.MessageBoxW(
            None, msg, "Intellex", MB_OK | MB_ICONWARNING | MB_TOPMOST)
    except Exception:
        pass


def _wait_until_up(port: int, timeout: float = 10.0) -> bool:
    """Wait for OUR server, not merely for something listening.

    A bare TCP connect answers yes when a PREVIOUS Intellex (or anything else) already
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
    # BEFORE argparse, and before anything else starts. A frozen build cannot spawn
    # `python -m esptool`: there is no python on the machine and the interpreter IS
    # this app, so flash.py re-invokes THIS executable with --run-esptool and we
    # hand the rest of argv straight to esptool. Exactly ESP-Flasher-Companion's
    # trick, and the reason CLAUDE.md calls it out. Harmless unfrozen, where
    # flash.py uses `-m esptool` instead and this never fires.
    if len(sys.argv) > 1 and sys.argv[1] == "--run-esptool":
        import esptool
        sys.argv = ["esptool"] + sys.argv[2:]
        return esptool.main(sys.argv[1:]) or 0

    # Same trick, same reason, for the config-tool fetcher: a frozen build has no
    # python to run tools/fetch_webui.py with, and no tools/ directory either. The
    # module is bundled (see Intellex.spec) so importing it here is the whole job.
    if len(sys.argv) > 1 and sys.argv[1] == "--run-fetch-webui":
        import fetch_webui
        sys.argv = ["fetch_webui"] + sys.argv[2:]
        return fetch_webui.main()

    # And the firmware cache, for the same reason.
    if len(sys.argv) > 1 and sys.argv[1] == "--run-fetch-firmware":
        import fetch_firmware
        sys.argv = ["fetch_firmware"] + sys.argv[2:]
        return fetch_firmware.main()

    if len(sys.argv) > 1 and sys.argv[1] == "--run-fetch-wiki":
        import fetch_wiki
        sys.argv = ["fetch_wiki"] + sys.argv[2:]
        return fetch_wiki.main()

    # FIRST thing the app proper does. Everything below prints -- attaches, drops,
    # reconnects, probe results, flash output -- and in a windowed build (pythonw,
    # or the frozen console=False exe) stdout is attached to nothing, so all of it
    # was being written into a void. That made "what did it actually do?"
    # unanswerable for the one build people actually run.
    #
    # After the sentinels above on purpose: those hand the process to esptool or a
    # fetcher and exit, and each would otherwise open a log of its own.
    _log = applog.start()

    ap = argparse.ArgumentParser(description="Intellex desktop app")
    ap.add_argument("--port", type=int, default=hostmod.DEFAULT_PORT)
    ap.add_argument("--serial", help="attach this port at launch and skip the chooser")
    ap.add_argument("--ws", help="attach this droid at launch and skip the chooser")
    ap.add_argument("--ssid", default="NaviCore")
    ap.add_argument("--no-auto-bounce", action="store_true")
    ap.add_argument("--dev", action="store_true",
                    help="enable devtools and the right-click menu in the window")
    ap.add_argument("--inspect", type=int, metavar="PORT", nargs="?", const=9333,
                    help="open a DevTools protocol port on the window (default 9333) so a "
                         "freeze can be profiled from outside while it is happening")
    ap.add_argument("--browser", action="store_true",
                    help="open the default browser instead of an app window")
    a = ap.parse_args()

    hostmod.auto_bounce = not a.no_auto_bounce

    # WHY AN ENV VAR AND NOT A pywebview OPTION
    # pywebview's debug flag turns on the F12 devtools UI, which is no use when the
    # window is the thing that has stopped responding -- you cannot click it. The
    # WebView2 runtime reads WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS at STARTUP, so
    # it has to be set before the window is created and cannot be turned on later.
    # That is the whole reason this is a launch flag: a freeze is only debuggable
    # if you decided to make it debuggable before it happened.
    #
    # Loopback only, and off unless asked for -- it is an unauthenticated debug
    # port into the page.
    if a.inspect:
        os.environ["WEBVIEW2_ADDITIONAL_BROWSER_ARGUMENTS"] = (
            f"--remote-debugging-port={a.inspect}")
        print(f"devtools protocol on http://127.0.0.1:{a.inspect}/json")

    # BEFORE anything else. A failed bind here means another Intellex owns the
    # port; carrying on would open a window onto that one and -- because the
    # command-line attach below runs first -- would also grab the serial port the
    # live instance is trying to use, which it then retries forever with
    # "Access is denied".
    sock, bind_err = _bind(a.port)
    if sock is None:
        msg = (f"Intellex is already running on {hostmod.BIND_HOST}:{a.port}.\n\n"
               "Close the existing Intellex window, then start this one again.")
        print(msg, file=sys.stderr)
        print(f"  ({bind_err})", file=sys.stderr)
        _tell_user(msg)
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

    threading.Thread(target=_serve, args=(sock,), daemon=True, name="intellex-host").start()
    if not _wait_until_up(a.port):
        why = f": {_serve_error[0]}" if _serve_error else ""
        print(f"host did not start on {hostmod.BIND_HOST}:{a.port}{why}", file=sys.stderr)
        _tell_user(f"Intellex could not start its server on "
                   f"{hostmod.BIND_HOST}:{a.port}.{why}")
        # Release anything the early attach opened, or it stays held by this dying
        # process and no other instance can have it.
        with contextlib.suppress(Exception):
            hostmod.bridge.detach()
        return 1

    # Straight to the tool when a target was given; otherwise pick one first.
    landing = "/" if spec else "/_launcher"
    if _log:
        print(f"log       {_log}")
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
    # BEFORE the window exists, which matters on Windows: the taskbar identity is
    # read when the window is created and ignored afterwards. On macOS this is the
    # entire job. Window mode only -- in --browser mode there is nothing to put an
    # icon on, and on macOS asking for one would give this process a Dock presence
    # it has deliberately not got.
    _icon_msg = appicon.prepare()
    if _icon_msg:
        print(_icon_msg)

    # SIZED AND PLACED AGAINST THE SCREEN'S WORK AREA, not asked for flat. A bare
    # 1280x880 is 1100 physical px tall at 125% scaling, against a 1020px work
    # area, and pywebview's no-position fallback centres on the monitor's FULL
    # bounds -- so the bottom of the app sat under the taskbar, unreachable. See
    # src/winsize.py; it hands back the requested size unchanged when it cannot
    # measure, so the worst case is the old behaviour.
    geom = winsize.fit(1280, 880, min_size=(900, 640))
    try:
        webview.create_window("Intellex", url, text_select=True, **geom)
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
    # ── Let the shell open a second window ────────────────────────────────────
    # The two tools can be shown as tabs or side by side inside one window, but
    # sometimes you want the Wizard on a second monitor while the config tool
    # keeps the first. window.open() from inside a webview is unreliable -- it may
    # be blocked, or hand the page to the system browser, where it is a DIFFERENT
    # ORIGIN as far as nothing, but a different window with no app chrome.
    # pywebview can make a real one, so offer that and let the shell fall back.
    #
    # Registered only in window mode: under --browser or a bare host there is no
    # backend, and host.py answers 501 so the shell uses window.open() instead.
    def _open_window(title: str, path: str) -> None:
        # Measured per call rather than reusing `geom`: this one can be opened
        # long after startup, and the window it lands on may not be the one the
        # app started on -- a laptop docked to an external monitor mid-session is
        # the ordinary case, not a corner one.
        webview.create_window(title, f"http://{hostmod.BIND_HOST}:{a.port}{path}",
                              text_select=True,
                              **winsize.fit(1280, 880, min_size=(900, 640)))

    hostmod.open_window_hook = _open_window

    def _window_up() -> None:
        # Runs on pywebview's worker thread once the GUI is up -- the first moment a
        # window handle exists to hang an icon on. No-op on macOS.
        msg = appicon.attach()
        if msg:
            print(msg)

    try:
        webview.start(_window_up, debug=a.dev)
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
