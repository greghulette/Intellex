# macOS — first run

No Mac has run this. Everything here is written from documentation and from
simulating the platform branches on Windows, so treat it as a test plan rather
than instructions that are known to work.

**No Apple Developer account is needed.** A repo you cloned has no
`com.apple.quarantine` attribute, so Gatekeeper does not get involved at all.
That only becomes a question if you later hand someone a downloaded `.app`.

## Run it

```bash
cd ~/Documents/GitHub/NaviLink      # wherever you cloned it
chmod +x scripts/run-macos.command  # once, if git did not carry the bit
./scripts/run-macos.command
```

First run builds the venv and installs dependencies, which takes a minute. It
should then open a window on the chooser.

If the window is the part that fails, this still works and is the fastest way to
find out whether everything *else* is fine:

```bash
./scripts/run-macos.command --browser
```

## What is most likely to break, in order

**1. pywebview / WKWebView.** The window is the least-tested piece. `--browser`
sidesteps it entirely and the app is fully functional that way, so this is an
annoyance rather than a blocker. A backend failure now falls back to the browser by
itself rather than killing the app — pywebview chooses its toolkit inside
`start()`, and raises `WebViewException`, not `ImportError`, so guarding only the
import was not enough. If it fails, the useful detail is whatever pywebview prints
about which backend it tried.

**2. `_wifi_bounce_macos()`.** Auto re-association after a droid reboot. It looks
for a Wi-Fi hardware port via `networksetup -listallhardwareports`, then the one
whose current network matches the SSID.

The parser is now exercised by a stubbed test (real `networksetup` output shapes,
asserted decisions: default-named AP, explicitly-named AP, the `NaviCore_Guest`
false positive, no Wi-Fi port at all) — so the *logic* is checked even though the
commands have never actually run on a Mac. What is still unverified is whether
`networksetup` on your machine prints what the stub assumes. Worth confirming by
hand, because if these two commands do not say what the code expects, nothing else
will hint at it:

```bash
networksetup -listallhardwareports
networksetup -getairportnetwork en0     # or whichever device the first lists
```

A failure here costs you the automatic reconnect, not the connection. Everything
else keeps working and the app reports a clear reason.

**3. Serial port naming.** A NaviCore should appear as `/dev/cu.usbmodem*`, a
bridge WCB as `/dev/cu.usbserial*` or `/dev/cu.wchusbserial*`. The chooser labels
them "probably a NaviCore" / "probably a WCB bridge". If a port shows up unlabeled,
send its path and description from:

```bash
.venv/bin/python3 -c "import sys;sys.path.insert(0,'src');import transport,json;print(json.dumps(transport.list_serial_ports(),indent=1))"
```

**Use `cu.*`, never `tty.*`** — opening a `tty.` device blocks waiting for carrier
detect, which a USB CDC device never asserts, so the connect hangs with no error.
pyserial lists both; the chooser now filters `tty.` out, so you should only ever be
offered the `cu.` side.

## Checking it end to end without the UI

```bash
.venv/bin/python3 tools/smoke_ws.py                       # straight to the droid
.venv/bin/python3 tools/smoke_transport.py list           # what ports are visible
.venv/bin/python3 tools/smoke_transport.py ws             # via the WebSocket transport
.venv/bin/python3 tools/smoke_transport.py serial --port /dev/cu.usbmodem1101
```

Each exits non-zero on failure and names the likely cause, so they are worth
running before debugging the window.

## Revision log

| Date | Commit | Change |
|---|---|---|
| 2026-08-30 | _(uncommitted)_ | Three of the four macOS risks on this page are now addressed in code rather than only described. The Wi-Fi bounce matched the SSID as a substring of the whole `networksetup` reply, so a house network called `NaviCore_Guest` would have power-cycled the wrong radio every ~10 s — it now parses the network name out and matches exactly, or as `<ssid>-<suffix>` for a default-named AP (the firmware derives `NaviCore-<deviceId>` when `wifiSsid` is blank, which the old exact-match Windows path never matched either). A window-backend failure falls back to the browser instead of killing the app. The chooser filters `/dev/tty.*`, whose open blocks on carrier detect. The parser logic is covered by a stubbed test; whether real `networksetup` output matches the stub is still unverified. |
| 2026-08-30 | _(uncommitted)_ | Created. First-run test plan for macOS, written without a Mac to verify against: how to run it, the three things most likely to fail and what each costs, and the headless checks that isolate the app from the window. Records that no Apple Developer account is needed for a locally cloned repo, since Gatekeeper only acts on quarantined downloads. |
