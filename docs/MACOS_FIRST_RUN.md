# macOS — first run

**A Mac has now run this** — 2026-09-01, macOS 15.4.1 on x86_64, python.org
Python 3.9.4. Items below are marked VERIFIED where they were actually exercised
on that machine and BROKEN where they were exercised and failed; anything still
unmarked was written from documentation and from simulating the platform branches
on Windows, and remains a test plan rather than a known-good instruction.

**No Apple Developer account is needed.** A repo you cloned has no
`com.apple.quarantine` attribute, so Gatekeeper does not get involved at all.
That only becomes a question if you later hand someone a downloaded `.app`.

## Run it

Double-click **`Intellex.command`** at the repo root. That is the whole answer —
it is the counterpart to `Intellex.bat`, and `.gitattributes` pins `*.command` to
LF so the shebang survives being authored on Windows (a CRLF one fails as
`bad interpreter: /bin/bash^M`, which reads like a broken script).

From a shell, if you prefer:

```bash
cd ~/Documents/GitHub/Intellex      # wherever you cloned it
./Intellex.command
```

First run builds the venv, installs dependencies, **and fetches the config tool**,
which takes a minute or two. It should then open a window on the chooser. The
Terminal window stays up while the app runs — there is no `pythonw` on macOS — and
closing it quits Intellex.

That fetch is not optional and it is easy to miss why: `src/webui/` is gitignored,
because the public NaviCore repo is the single source of truth for the UI. A fresh
clone therefore contains **no UI at all**, and before the launchers learned to fetch
it, the first thing every new machine showed was the app politely explaining that
there was nothing to configure with:

```
No bundled config tool.
Expected: .../src/webui/index.html
```

That message is still correct and still what you get if the fetch could not run —
the control API and the `/_link` byte pipe genuinely do work without the bundle. To
do it by hand at any time:

```bash
.venv/bin/python3 tools/fetch_webui.py          # or --check, or --force
```

If Finder refuses with "unidentified developer", this copy was **downloaded**
rather than cloned: a download carries `com.apple.quarantine`, a clone does not.
Right-click → Open once, or `xattr -d com.apple.quarantine Intellex.command`.

If the window is the part that fails, this still works and is the fastest way to
find out whether everything *else* is fine:

```bash
./Intellex.command --browser
```

## What is most likely to break, in order

**1. TLS certificates — BROKEN on a stock python.org Python, now handled.** This
was the first thing to actually go wrong, and it did not look like what it was. The
python.org macOS framework build does not read the system keychain; it looks for CAs
at `/Library/Frameworks/Python.framework/Versions/<x.y>/etc/openssl/cert.pem`, and
the installer ships that directory **empty**, leaving you to run
`/Applications/Python 3.x/Install Certificates.command` by hand afterwards. Nobody
does. Every HTTPS request then dies with `CERTIFICATE_VERIFY_FAILED`, and the app
reported it as *"cannot reach greghulette.github.io ... (offline is fine)"* on a
machine whose network was perfectly healthy.

`requirements.txt` now pulls in `certifi` — the same bundle that
Install Certificates.command installs — and `src/certs.py` uses it whenever the
platform store turns out to be empty. Verification is never disabled: an empty CA
store is a reason to go and find CAs, not to stop checking them. A cert failure is
also now told apart from being offline, everywhere it can surface, because the two
have completely different fixes and only one of them is "wait".

**2. pywebview / WKWebView — VERIFIED WORKING.** The window opened and rendered on
the first try, no fallback needed. `--browser` still sidesteps it entirely if it
ever misbehaves, and a backend failure falls back to the browser by itself rather
than killing the app — pywebview chooses its toolkit inside `start()`, and raises
`WebViewException`, not `ImportError`, so guarding only the import was not enough.
If it does fail, the useful detail is whatever pywebview prints about which backend
it tried.

**3. `_wifi_bounce_macos()` — was CONFIRMED BROKEN here; now rewritten.**
The stubbed test was checking the right logic against the wrong world. As first run:

```
>>> discover.wifi_bounce('NaviCore')
(False, 'no Wi-Fi interface is on "NaviCore" — join it once by hand so macOS remembers it')
```

…on a machine that was joined to the droid and pinging it fine. Two independent
causes, either of which alone is enough to break it:

*Not every 802.11 adapter is a "Wi-Fi" hardware port.* The droid was on `en6`, a
second adapter that `networksetup -listallhardwareports` names
`Hardware Port: 802.11ac NIC`. The parser only accepts a port whose name contains
"wi-fi" or "airport", so `en6` was never even a candidate — and it would not have
helped, because macOS itself refuses to introspect it:

```
$ networksetup -getairportnetwork en6
en6 is not a Wi-Fi interface.
$ networksetup -setairportpower en6 off
en6 is not a Wi-Fi interface.
```

So for that adapter, both the *detection* step and the *bounce* step are
unavailable through `networksetup`'s airport verbs.

*SSID lookup is gated on Location Services from macOS 14 on.* Even for the genuine
built-in `Wi-Fi`/`en0`, an unprivileged process gets:

```
$ networksetup -getairportnetwork en0
You are not associated with an AirPort network.
```

which is simply false — `ipconfig getsummary en0` showed `SSID : <the house
network>` at the same moment. Any design that identifies the adapter *by SSID* is
therefore unreliable on a modern macOS regardless of the hardware.

The fix is to stop asking about SSIDs. The routing table already knows which
adapter carries the droid, needs no entitlement, and is the actual decision the
traffic follows:

```
$ route -n get 192.168.4.1
  interface: en6
```

and a service that has no airport verbs can still be cycled by name, without sudo:

```
$ networksetup -listnetworkserviceorder     # (19) 802.11ac NIC -> en6
$ networksetup -setnetworkserviceenabled "802.11ac NIC" off
$ networksetup -setnetworkserviceenabled "802.11ac NIC" on
```

That is what it now does. Detection asks the routing table first and only falls
back to the SSID scan when no interface specifically carries the droid; the bounce
uses `-setairportpower` where macOS allows it and the service toggle where it does
not. On this machine it now resolves correctly:

```
>>> discover.wifi_bounce('NaviCore')
(True, 'en6 (carrying 192.168.4.1): service "802.11ac NIC" cycled, reconnecting')
```

**The safety property matters more than the fix.** `route -n get` *always*
answers — with no route to the droid it hands back the **default gateway**, which
is the user's real network. Bouncing that to fix the droid link is the exact
failure this module scopes to one interface to avoid, and it is the normal state
whenever the droid is simply switched off. So the default route is rejected
outright, and the adapter must additionally hold an address in the droid's own
`/24`. Verified: `8.8.8.8`, `1.1.1.1` and an unused `192.168.99.1` all resolve to
`None` and issue no commands at all.

Still unverified: whether `-setnetworkserviceenabled` needs admin rights on your
machine. The decision path was exercised with the mutating commands intercepted,
so the adapter has never actually been cycled — run it once for real to confirm.

**4. Serial port naming — VERIFIED.** The chooser labels ports by USB **vendor ID**, which is
the same number on every platform, so nothing here depends on how macOS words a
description: `303A` Espressif native USB, `1A86` CH9102, `10C4` CP210x, `0403` FTDI.

It does not guess a product from that. Espressif ports are then **asked** — one
`PING`, and only a direct `PONG` counts — so a real NaviCore reports its firmware
version and anything else says it did not answer. That matters on a Mac for the
same reason it does on Windows: a NaviCore and an SBUS controller are both
ESP32-S3 with native USB and enumerate identically. If a port shows up unlabeled,
send its path, VID and description from:

```bash
.venv/bin/python3 -c "import sys;sys.path.insert(0,'src');import transport,json;print(json.dumps(transport.list_serial_ports(),indent=1))"
```

**Use `cu.*`, never `tty.*`** — opening a `tty.` device blocks waiting for carrier
detect, which a USB CDC device never asserts, so the connect hangs with no error.
pyserial lists both; the chooser now filters `tty.` out, so you should only ever be
offered the `cu.` side. Confirmed on the real machine: `/dev/tty.usbmodem141301`
and friends exist and none of them reach the chooser.

Two things worth knowing that the list makes obvious:

- VIDs decode as designed — `12346` = `0x303A` Espressif, `4292` = `0x10C4` CP210x.
- **A CP210x appears twice.** `/dev/cu.usbserial-141210` and
  `/dev/cu.SLAB_USBtoUART` are one physical board under two names — the Silicon Labs
  VCP driver adds the `SLAB_` alias — and they carry an identical `serial_number`.
  Either one connects to the same hardware, so this is cosmetic, but the duplicate
  in the chooser is real and the serial number is what tells you they are the same
  device.

## Which Python you actually got

Unresolved, and worth a decision rather than a discovery. Both launchers say
"Install Python 3.11+" — but only in the branch where no `python3` exists at all.
When one *does* exist they build the venv from it without looking at its version,
and on this Mac the first `python3` on PATH was the python.org **3.9.4** framework
build. So the venv is 3.9, the stated floor is 3.11, and nothing said a word.

In practice 3.9 ran everything: all dependencies installed, the host serves, the
window opens. The single exception was `tools/smoke_host.py`, which used
`asyncio.timeout()` (3.11+) and would have raised `AttributeError` — a bad failure
to have in the one check you reach for when nothing else works. It now uses
`asyncio.wait_for`, which is the same fence on every version.

Two honest options, and it should be one of them rather than the current silence:
either the launchers **enforce** the 3.11 floor they claim, or the floor is **3.9**
and the message stops saying otherwise.

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
| 2026-09-08 | _(uncommitted)_ | Renamed NaviLink -> Intellex, so the macOS launcher at the repo root is now `Intellex.command` and the build products are `Intellex.app` / `Intellex.dmg`. The quarantine command changes with it. Rows above this one were swept too and name the app Intellex for work done while it was still NaviLink. |
| 2026-09-01 | _(uncommitted)_ | **First real run on a Mac.** Four things, in the order they bit. (1) A fresh clone has no UI — `src/webui/` is gitignored — so the first screen was "No bundled config tool". All four launchers now fetch it when `src/webui/index.html` is absent, non-fatally. (2) The fetch then failed anyway: python.org Python ships an EMPTY CA store, so every HTTPS request died as `CERTIFICATE_VERIFY_FAILED` while reporting itself as *"cannot reach ... (offline is fine)"*. Added `certifi` to requirements and `src/certs.py`, shared by the fetcher and the app's update probe; a cert failure is now told apart from being offline everywhere it surfaces, including the launcher's version line, which had been saying "offline — cannot compare" on a networked machine. (3) `fetch_webui.py` had no retry, and Pages throttled the ~35-file burst with a 503 partway through cmdlib, aborting the whole update with a traceback that named `urlopen` but not the file. It now retries what is worth retrying, skips what is not (404s, cert failures), and names the URL that lost. (4) `_wifi_bounce_macos()` confirmed broken on real hardware — see item 3 above; diagnosed and written up, not yet fixed. Also: pywebview/WKWebView verified working first try, serial `cu.*`/VID handling verified, and `smoke_host.py` no longer needs 3.11. |
| 2026-08-31 | _(uncommitted)_ | Added `Intellex.command` at the repo root, so "how do I open it" has the same answer on both platforms — the Windows launcher was at the root while macOS users had to know `scripts/` existed. Added `.gitattributes` pinning `*.command`/`*.sh` to LF and `*.bat` to CRLF: these are authored on a Windows box with `core.autocrlf=true`, and a CRLF shebang fails on macOS as `bad interpreter: /bin/bash^M`, which does not look like a line-ending problem. Recorded the quarantine distinction (downloads carry it, clones do not) where someone hitting it will look. |
| 2026-08-31 | _(uncommitted)_ | The chooser no longer guesses a product from the USB description. It reports the chip from the VID (platform-independent, so the old Windows-vs-macOS description matching is gone) and ASKS Espressif ports what they are. The old heuristic was wrong on half a normal bench: a NaviCore and an SBUS controller are both ESP32-S3 native USB and enumerate identically as 303A:1001 "USB Serial Device", so both read as "probably a NaviCore", while a CP210x dev board running the mgmt relay read as "probably a WCB bridge". |
| 2026-08-30 | _(uncommitted)_ | Three of the four macOS risks on this page are now addressed in code rather than only described. The Wi-Fi bounce matched the SSID as a substring of the whole `networksetup` reply, so a house network called `NaviCore_Guest` would have power-cycled the wrong radio every ~10 s — it now parses the network name out and matches exactly, or as `<ssid>-<suffix>` for a default-named AP (the firmware derives `NaviCore-<deviceId>` when `wifiSsid` is blank, which the old exact-match Windows path never matched either). A window-backend failure falls back to the browser instead of killing the app. The chooser filters `/dev/tty.*`, whose open blocks on carrier detect. The parser logic is covered by a stubbed test; whether real `networksetup` output matches the stub is still unverified. |
| 2026-08-30 | _(uncommitted)_ | Created. First-run test plan for macOS, written without a Mac to verify against: how to run it, the three things most likely to fail and what each costs, and the headless checks that isolate the app from the window. Records that no Apple Developer account is needed for a locally cloned repo, since Gatekeeper only acts on quarantined downloads. |
