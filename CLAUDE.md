# NaviLink — Working Notes

Desktop companion for NaviCore (Windows + macOS). Talks to a droid over **native serial**
or **WiFi**, and hosts the existing NaviCore config tool UI rather than reimplementing it.

**This repo is PRIVATE and the work is unannounced.** See [§ Discretion](#discretion) before
writing anything user-visible or pushing to a public repo.

Ecosystem context: [`../NaviCore/CLAUDE.md`](../NaviCore/CLAUDE.md) and
`C:\Users\ghulette\.claude\CLAUDE.md`. This file is the authority for **this** repo only.

---

## This repo is worked on from more than one machine

**`git fetch` and check for incoming commits BEFORE making any change.** The same repo is
worked on from a Windows box and a Mac, so the local clone may be behind even when nothing
here has changed. Editing on top of a stale checkout produces conflicts at push time at
best, and silently reverts the other machine's work at worst.

```bash
git fetch origin && git status -sb          # "behind N" means stop and pull first
git log --oneline HEAD..origin/main         # what arrived while you were away
```

Pull before you start, not when the push is rejected. If a push IS rejected, rebase onto
what arrived and re-read anything you were about to edit — do not force.

## The one idea this whole thing rests on

**The page talks to one WebSocket. The host process decides what's on the other end.**

```
  config tool UI  ──ws://127.0.0.1:PORT──►  NaviLink (Python)  ──►  COM port   (pyserial)
   (unmodified)                                                └──►  ws://192.168.4.1  (droid AP)
```

Serial and droid-WiFi become the *same code path* in the page. The difference lives entirely
in the host — which endpoint it attaches to. Two requirements collapse into one implementation.

Two consequences worth stating plainly:

- **The page needs no native bridge beyond that socket.** No custom IPC, no `window.pywebview.api`
  shims for the hot path.
- **The host serves the page too**, over `http://127.0.0.1:PORT`. Same origin as the WebSocket,
  which kills the CORS/mixed-content problem that rules out a hosted page talking to a board.

## Why native serial, not Web Serial

Not preference. `NaviCore/config_tool/index.html` ≈4468 records a measured finding: Chrome
asserts DTR/RTS inside `open()` and Web Serial exposes no way to suppress it, so **the droid
reboots on every page refresh** (`Reset reason: 11 - USB peripheral`). No browser-side work can
fix it — including Electron, which is the same Chromium implementation. `pyserial` can open a
port without touching the control lines.

That is the strongest technical justification for this project, and it is not "we wanted an app".

## Stack, and why

Python + `pyserial` + `esptool` + PyInstaller. Chosen because **`ESP-Flasher-Companion` already
proves this exact pipeline on both platforms** — frozen standalone executables, no Python needed
by users, `collect_all('esptool')` / `collect_all('serial')` in its `.spec`, and working
`scripts/build-windows.bat` + `scripts/build-macos.command`. Read that repo before solving any
packaging problem here; it has probably already been solved.

Rejected: Electron (~150 MB Chromium to maintain, and does not fix the DTR bug), Tauri (Rust
toolchain plus an unofficial community serial crate on the critical path, for a solo maintainer).

## The UI is NOT forked

**`config_tool/index.html` stays the single source of truth in the public NaviCore repo.** This
app *bundles* a copy and serves it. It does not fork it, and it does not diverge from it.

That is a hard constraint, not a preference — a fork drifts within a week and the whole point is
that a fix to the web tool is a fix here too.

- Bundle for offline (a con floor has no internet).
- An explicit **Update** button pulls the current version from GitHub Pages.
- Compare versions using the tool's own footer stamp: `<span id="footer-dtg">` in `index.html`,
  set by NaviCore's pre-commit hook on every commit.

**The tool is not one file.** A correct update fetches `index.html` + `flasher.js` +
`serial-hub.js` + `cmdlib/` — ~1.55 MB across ~30 files. `cmdlib/` is fetched at *relative paths
at runtime*, so pulling only `index.html` leaves a stale command library silently mismatched
against a newer UI. Skip `index-Old.html` and `index1.html` (frozen, nothing loads them).

Source: `https://greghulette.github.io/NaviCore/config_tool/`. Per-branch previews exist at
`/dev/<branch>/config_tool/` — a free test channel.

Updates must be **atomic and revertible**: download to a temp dir, verify, swap, and keep the
bundled copy permanently as a fallback. A bad pull otherwise leaves a broken UI and no way back,
and this is the only way to configure the droid.

## Rules that are easy to break

1. **Deliver RAW BYTES to the page, never strings and never pre-split lines.** The tool keeps one
   `TextDecoder({stream:true})` alive so a UTF-8 character split across two reads reassembles. A
   host that helpfully decodes per chunk produces mojibake at chunk boundaries.
2. **A write must REJECT on device loss, not swallow the error.** `index.html` ≈5316 uses a
   failing write as the only liveness proof — a `read()` on a vanished device can hang forever
   without rejecting. Fire-and-forget writes reinstate a measured 16.7-hour "Connected but frozen"
   hang.
3. **Do not reimplement `sendLine()`'s pacing.** Its 512-byte chunking and inter-chunk delay exist
   because a ~10 KB `SET_CONFIG` overran the board's USB-CDC RX buffer. That logic lives in the
   page and must stay there.
4. **`setSignals`/DTR-RTS control is mandatory**, not optional — it is both the flasher's
   bootloader entry and the fix for the reboot-on-open bug.
5. **Flashing: reuse, do not port.** `flasher.js` is esptool-js over Web Serial. The native answer
   is `esptool` as a Python package, exactly as ESP-Flasher-Companion does it. Note that repo's
   `--run-esptool` self-reinvocation trick: a frozen build cannot spawn `python -m esptool`
   because `sys.executable` is the app.

## Firmware counterpart

The droid side is `wifiEnabled` / `wifiSsid` / `wifiPassword` in `NaviCore/rc_config.h`, off by
default, with the SoftAP raised in `setup()`. Tracked in [`docs/WIP_NOTES.md`](docs/WIP_NOTES.md),
which also carries the recipe for removing it.

**Verified on hardware 2026-08-28: ESP-NOW survives alongside the SoftAP.** Two runs with the
AP up and a laptop associated — direct USB, then bridged through a mgmt relay: 264 sends,
98.9% ack, both WCBs at 100% with zero retries, and every failure predating the steady state.
The shared-radio coexistence path works under real load. This was the assumption the whole
WiFi transport rested on.

## Discretion

The desktop app is **not announced**. The NaviCore repo and its GitHub Pages tool are public.

- Keep NaviCore-side commit messages and identifiers **neutral** — "optional SoftAP", "comms
  endpoint". Not "desktop app".
- **Never** put an app-revealing string in `config_tool/index.html`; it ships to every user via
  Pages and is readable with view-source.
- This is discretion against casual browsing, not secrecy against inspection. `wifiEnabled` is
  readable in the public repo by anyone who looks. Do not mistake one for the other.

## Verifying

```bash
python -m py_compile src/*.py          # no test suite yet — this is the bar
```

Say "compiles" and mean it; do not imply testing that did not happen.

## Conventions

Follow the NaviCore house style: comments explain **why**, especially where a naive change would
reintroduce a fixed bug. Docs update in the same commit as the code. Every doc ends with a
revision log.
