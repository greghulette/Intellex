# NaviLink

Desktop companion for NaviCore — Windows and macOS. Native serial or WiFi, hosting the
existing NaviCore config tool UI rather than reimplementing it.

**Private / unreleased.** See [CLAUDE.md](CLAUDE.md) before making anything user-visible.

## Status

Scaffold. Nothing runs yet.

| Piece | State |
|---|---|
| Firmware SoftAP (`wifiEnabled`, off by default) | Landed in NaviCore; AP verified on hardware |
| Droid-side comms server | **Verified on hardware** — WS endpoint answers PING with PONG |
| Transport contracts | Defined and honoured — **the DTR-on-open reset is fixed** (no board reboot when the port opens) |
| Serial / WebSocket transports | **Both verified on hardware** — `tools/smoke_transport.py serial|ws` |
| Local HTTP + WS host | **Verified on hardware** — same client code reaches the droid over serial AND WiFi |
| App shell (window + chooser) | **Working** — `scripts/run-windows.bat` opens a pywebview window; picks a droid or port, updates the tool |
| UI bundling + update-from-Pages | Working — `tools/fetch_webui.py`, verified byte-identical to Pages |
| Packaging | Not written — port ESP-Flasher-Companion's proven pipeline |

## Layout

```
src/transport.py   the byte-pipe seam; read its docstring first
src/webui/         bundled NaviCore config tool — fetched, never committed
src/webui/Images/  footer art the tool loads as "../Images/<name>" (see below)
NaviLink.bat       double-click launcher — Windows
NaviLink.command   double-click launcher — macOS
docs/WIP_NOTES.md  firmware-side change tracker + removal recipe
scripts/           cross-platform build scripts
```

The tool reaches its footer images with `../Images/<name>`, which resolves on
GitHub Pages because the site root holds `config_tool/` and `Images/` side by
side. Here `index.html` is served **at** the root, so the browser clamps the
`..` and asks for `/Images/<name>` — which works, but only because
`fetch_webui.py` bundles them. Anything else the tool loads from outside
`config_tool/` needs adding to `IMAGES` (or its own list) the same way, or it
will 404 in the app while looking perfect on the published site.

## Running it

Double-click **`NaviLink.bat`** (Windows) or **`NaviLink.command`** (macOS), both at
the repo root. First run creates the venv and installs dependencies; after that it
just opens.

    NaviLink.bat                     window + chooser
    NaviLink.bat --ws 192.168.4.1    skip the chooser
    NaviLink.bat --serial COM5
    NaviLink.bat --browser           no window, use the default browser

    ./NaviLink.command               same flags, macOS paths
    ./NaviLink.command --serial /dev/cu.usbmodem1101

The chooser also has **Open the tool anyway** — the config tool does not need a
board to be useful. Panels, saved configs and editing all work offline; attach
later from the status line at the top of the tool.

On macOS there is no `pythonw`, so a double-click opens a Terminal window that
stays up while the app runs — closing it quits NaviLink. That is the same
relationship the `.bat` has with its console; here it is just visible.

If Finder refuses with "unidentified developer", that copy was **downloaded**
rather than cloned — a download carries `com.apple.quarantine` and a clone does
not. Right-click → Open once, or `xattr -d com.apple.quarantine NaviLink.command`.

`scripts/run-windows.bat` and `scripts/run-macos.command` still exist and do the
same thing; the root launchers are the ones to hand somebody.

To change connection later, click the transport label in the tool's status bar
("Connected · USB COM5 ▾") — that returns to the chooser.

## Platform status

| | Windows | macOS |
|---|---|---|
| App, chooser, transports, OTA | **verified on hardware** | written, **never run** |
| Serial port naming | `COM*` | `/dev/cu.*` — pyserial handles it; labels match both |
| Auto re-associate after a droid reboot | `netsh`, measured | `networksetup`, **unverified** |
| Window | WebView2 | WKWebView (needs pyobjc, installed by marker) |

First run on a Mac: [docs/MACOS_FIRST_RUN.md](docs/MACOS_FIRST_RUN.md).

**No Mac has run any of this.** The macOS paths are written from documentation, not
from a session at a machine. Expect the first run to need fixes — most likely in
`_wifi_bounce_macos()` and in whether pywebview picks up WKWebView cleanly. The app
itself does not depend on either: `--browser` skips the window, and the auto-bounce
is a convenience that reports a clear error when it cannot run.

## After a code change — reload or restart?

| Changed | What to do |
|---|---|
| `navilink_shim.js`, `launcher.html`, the bundled tool | **F5** in the window — everything UI is served `no-store` |
| `host.py`, `discover.py`, `*_transport.py`, `app.py` | **Restart the app** — Python is loaded once at startup |

The window has no browser chrome, so F5 / Ctrl+R are wired up in the page itself.
`NaviLink.bat --dev` adds devtools and a right-click menu for when a reload is not
enough.

## Development

```bash
python -m venv .venv && .venv/Scripts/activate    # Windows
pip install -r requirements.txt
python -m py_compile src/*.py                      # no test suite yet
```

## Environment

Python **3.14.7**, installed via the Python Install Manager at
`%LOCALAPPDATA%\Python\pythoncore-3.14-64`. Its shim directory
(`%LOCALAPPDATA%\Python\bin`) was missing from PATH — added to the **User** PATH on
2026-08-28, so a new shell finds `python` and `pip`.

```bash
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe -m py_compile src/*.py     # no test suite yet — this is the bar
```

All dependencies resolve on 3.14: pyinstaller 6.22.2, esptool 5.3.1, pyserial 3.5,
websockets 17.1, aiohttp 3.14.3.

`transport.list_serial_ports()` is verified working against real hardware. Everything else
in `transport.py` is a contract with no implementation yet.
