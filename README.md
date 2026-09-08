# Intellex

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
| App shell (window + chooser) | **Working on Windows and macOS** — pywebview opens a window on both; picks a droid or port, updates the tool |
| UI bundling + update-from-Pages | Working — `tools/fetch_webui.py`, verified byte-identical to Pages; fetched automatically on first run |
| Flashing (Update Firmware / Full Wipe) | **Written, not yet run against a board** — `src/flash.py` drives native `esptool` (CLAUDE.md §5); the shim intercepts the tool's own buttons and posts to `/_api/flash`. Image selection, flash map and NVS rules verified against the real `firmware/` listing; the write itself is untested on hardware. |
| Packaging | Not written — port ESP-Flasher-Companion's proven pipeline |

## Layout

```
src/transport.py   the byte-pipe seam; read its docstring first
src/appicon.py     Dock / taskbar icon — pywebview cannot do this for us
src/assets/        app icon — the .svg comes from NaviCore; the .ico and .png
                   are BOTH generated from it by tools/make_icon.py, which
                   crops to the hexagon and drops the side ticks. Not a plain
                   rasterisation — read that header before regenerating
src/certs.py       TLS trust, because a stock macOS Python has none
src/flash.py       native esptool flashing — what the browser cannot do here
src/wcb_flash.py   the same for a WCB: chip + flash-size detection, then write
src/paths.py       where things live, run from source AND frozen — updates go to
                   the user data dir, because a one-file build's own directory is
                   a temp dir that is deleted on exit
src/fwcache.py     local copy of every firmware image, so flashing works with no
                   internet (join the droid's AP and there is no route to GitHub)
src/firmware/      that cache — fetched, never committed
Intellex.spec      PyInstaller: one .exe on Windows, one .app/.dmg on macOS
src/shell.html     the window that holds one or both tools (tabs / side by side)
src/webui/         bundled NaviCore config tool — fetched, never committed
src/webui/Images/  footer art the tool loads as "../Images/<name>" (see below)
src/webui_wcb/     bundled WCB Wizard — also fetched, also never committed.
                   Laid out as Wizard/ + Images/ SIBLINGS, mirroring gh-pages,
                   and mounted at /wcb/ so the Wizard's own "../Images/<name>"
                   resolves with no path rewriting
Intellex.bat       double-click launcher — Windows
Intellex.command   double-click launcher — macOS
docs/WCB_WIZARD.md how the Wizard is hosted, flashed and laid out — read first
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

The Wizard has the same `../Images/` habit, solved the other way round: instead
of flattening it and clamping the `..`, its bundle keeps `Wizard/` and `Images/`
as siblings under `src/webui_wcb/` and mounts the pair at `/wcb/`. The published
file is then served byte-identically. See
[docs/WCB_WIZARD.md](docs/WCB_WIZARD.md).

## Running it

Double-click **`Intellex.bat`** (Windows) or **`Intellex.command`** (macOS), both at
the repo root. First run creates the venv, installs dependencies **and fetches the
config tool** — `src/webui/` is gitignored, so a fresh clone has no UI until that
runs. After that it just opens. If the fetch cannot reach Pages the app still starts
and explains itself; `.venv/bin/python3 tools/fetch_webui.py` retries it.

    Intellex.bat                     window + chooser
    Intellex.bat --ws 192.168.4.1    skip the chooser
    Intellex.bat --serial COM5
    Intellex.bat --browser           no window, use the default browser

    ./Intellex.command               same flags, macOS paths
    ./Intellex.command --serial /dev/cu.usbmodem1101

The chooser also has **Open the tool anyway** — the config tool does not need a
board to be useful. Panels, saved configs and editing all work offline; attach
later from the status line at the top of the tool.

On macOS there is no `pythonw`, so a double-click opens a Terminal window that
stays up while the app runs — closing it quits Intellex. That is the same
relationship the `.bat` has with its console; here it is just visible.

If Finder refuses with "unidentified developer", that copy was **downloaded**
rather than cloned — a download carries `com.apple.quarantine` and a clone does
not. Right-click → Open once, or `xattr -d com.apple.quarantine Intellex.command`.

`scripts/run-windows.bat` and `scripts/run-macos.command` still exist and do the
same thing; the root launchers are the ones to hand somebody.

To change connection later, click the transport label in the tool's status bar
("Connected · USB COM5 ▾") — that returns to the chooser.

## Platform status

| | Windows | macOS |
|---|---|---|
| App, chooser, transports, OTA | **verified on hardware** | written, **never run** |
| Serial port naming | `COM*` | `/dev/cu.*` — pyserial handles it; labels match both |
| Auto re-associate after a droid reboot | `netsh`, measured | route-based, **rewritten after failing on real hardware** |
| Window | WebView2 | WKWebView (needs pyobjc, installed by marker) |

First run on a Mac: [docs/MACOS_FIRST_RUN.md](docs/MACOS_FIRST_RUN.md).

**A Mac has now run this** (macOS 15.4.1, 2026-09-01). pywebview picked up WKWebView
cleanly on the first try, and serial enumeration behaves. Two things did need fixes —
a fresh clone had no UI, and python.org's Python ships an empty CA store, so every
HTTPS fetch failed while claiming to be offline — and both are handled now.

`_wifi_bounce_macos()` is the prediction that came true: it is **confirmed broken**
on real hardware, for two reasons neither the code nor its stubbed test anticipated.
It is diagnosed but not yet fixed — see
[docs/MACOS_FIRST_RUN.md](docs/MACOS_FIRST_RUN.md). The app does not depend on it:
the auto-bounce is a convenience, and it reports a clear error when it cannot run.

## After a code change — reload or restart?

| Changed | What to do |
|---|---|
| `intellex_shim.js`, `launcher.html`, the bundled tool | **F5** in the window — everything UI is served `no-store` |
| `host.py`, `discover.py`, `*_transport.py`, `app.py` | **Restart the app** — Python is loaded once at startup |

The window has no browser chrome, so F5 / Ctrl+R are wired up in the page itself.
`Intellex.bat --dev` adds devtools and a right-click menu for when a reload is not
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
