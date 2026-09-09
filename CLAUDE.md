# Intellex — Working Notes

Desktop companion for NaviCore (Windows + macOS). Talks to a droid over **native serial**
or **WiFi**, and hosts the existing NaviCore config tool UI rather than reimplementing it.

**This repo is PUBLIC as of 2026-09-08**, history included. It was private and the work
unannounced until then; see [§ It is public now](#it-is-public-now) for what that retired
and the one rule that outlived it.

Ecosystem context: [`../NaviCore/CLAUDE.md`](../NaviCore/CLAUDE.md) and
`C:\Users\ghulette\.claude\CLAUDE.md`. This file is the authority for **this** repo only.

## The app was called NaviLink until 2026-09-08

Named for the Intellex, the droid brain Industrial Automaton ships in an R2-series
astromech. **The old name is still all over the places code cannot reach**, and none of
these are bugs to go fix:

- The **GitHub repo is now `greghulette/Intellex`**, but **the local clone directory is
  still `NaviLink`** on the Windows box — a directory cannot rename itself out from under
  a running session. GitHub redirects the old URL, so a clone whose origin still says
  `NaviLink.git` keeps working; the Mac's clone wants
  `git remote set-url origin https://github.com/greghulette/Intellex.git` all the same,
  because a redirect is not a promise.
- **Git history** is entirely under the old name. So is every commit message before the
  rename commit.
- **`docs/` revision-log rows were swept along with everything else**, so historical rows
  say "Intellex" for events that happened when it was NaviLink. That was chosen over
  leaving two names scattered through the docs; the rename's own row records it.
- **`src/paths.py` still knows the old name on purpose** — `_LEGACY_DIR_NAME`, a one-shot
  move of the user data directory. `src/applog.py` likewise still globs `navilink-*.log`
  when pruning. Deleting either silently orphans real user state.

---

## The exec bit is NOT automatic on the Windows box

`core.fileMode` is **false** here, so Git ignores permissions in the working tree
and a new `.command` or `.sh` gets committed **100644**. It looks fine on Windows
and fails on the Mac with *"cannot be executed because you do not have the
appropriate access privileges"* -- which reads like a Gatekeeper or ownership
problem and is neither. It happened to `scripts/build-macos.command`.

Chmod locally does nothing here. Set it in the index instead, and check:

```bash
git update-index --chmod=+x scripts/whatever.command
git ls-files -s -- '*.command' '*.sh'      # every one of these must read 100755
```

## This repo is worked on from more than one machine

**`git fetch` and check ALL BRANCHES before making any change.** The same repo is worked on
from a Windows box and a Mac, so the local clone may be behind even when nothing here has
changed.

**Checking only `origin/main` is not enough, and this has already gone wrong.** Work pushed
from the Mac landed on a branch called `macos-first-run`; `git status -sb` and
`git log HEAD..origin/main` both reported "nothing incoming" for two days while eleven
commits sat on the remote. The report was true and useless. Look at every branch:

```bash
git fetch --all --prune
git branch -r                                  # EVERY remote branch, not just main
git log --oneline --all --not main             # anything anywhere that main lacks
git status -sb                                 # then the usual "behind N" check
```

**Commit to `main`. Do not create feature branches in this repo.** The default Claude Code
guidance is to branch when on the default branch, and that is what produced the split above
— one session branched, another committed straight to `main`, and neither was wrong on its
own. This repo wants a single line of history. If you think a branch is genuinely warranted,
say so and get agreement first.

Pull before you start, not when the push is rejected. If a push IS rejected, rebase onto
what arrived and re-read anything you were about to edit — do not force.

## The one idea this whole thing rests on

**The page talks to one WebSocket. The host process decides what's on the other end.**

```
  config tool UI  ──ws://127.0.0.1:PORT──►  Intellex (Python)  ──►  COM port   (pyserial)
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

## Two tools, one link

Intellex hosts **both** browser tools and attaches them to the same droid link:

| Path | Tool | Source repo |
|---|---|---|
| `/` | NaviCore config tool | `NaviCore/config_tool/` |
| `/wcb/Wizard/` | WCB Wizard | `Wireless_Communication_Board-WCB/Wizard/` |
| `/_shell` | the window that holds one or both | this repo |

This works because **MgmtRelay's WebSocket endpoint was built to NaviCore's transport contract
on purpose** — same URI, same newline-delimited UTF-8, same console-mirror semantics, different
payload grammar (`mgmt_wsserver.h` header comment). So one transport carries both tools, and
`Bridge` fans the link to *every* attached page rather than one.

**Read [`docs/WCB_WIZARD.md`](docs/WCB_WIZARD.md) before touching the Wizard, the `/wcb/` mount,
the page fan-out, or WCB flashing.** The rules there are the ones that bite.

## The UI is NOT forked

**Neither tool is forked.** `config_tool/index.html` stays the single source of truth in the
public NaviCore repo, and `Wizard/` in the public WCB repo. This app *bundles* a copy of each
and serves it. It does not fork them, and it does not diverge from them.

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

**The Wizard is not one file either.** ~1.1 MB across 16 files: `index.html` + `app.js` +
`parser.js` + `flasher.js` + `device-labels.js` + `serial-hub.js` + `styles.css` + `vendor/`,
plus the sibling `Images/`. Its version stamp is **`UI_VERSION` in `app.js`**, not a footer
element — the WCB pre-commit hook writes it, so it plays the same role `footer-dtg` does.
Source: `https://greghulette.github.io/Wireless_Communication_Board-WCB/Wizard/`, with the same
`/dev/<branch>/Wizard/` preview channel. Firmware binaries are **not** part of a tool update —
they are a separate thing on a separate cadence, fetched by `tools/fetch_firmware.py` and kept
locally so flashing works with no network (see [§ Offline](#offline-is-the-normal-case-not-the-edge-case)).

Updates must be **atomic and revertible**: download to a temp dir, verify, swap, and keep the
bundled copy permanently as a fallback. A bad pull otherwise leaves a broken UI and no way back,
and this is the only way to configure the droid. The two tools update as **separate** atomic
swaps (`tools/fetch_webui.py --tool navicore|wcb|all`) — different repos, different cadences, so
one failing offline must not roll back the other.

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
6. **The bridge feeds MANY pages, not one.** Both tools can be open at once, and each holds its
   own `/_link`. Every chunk goes to every page **whole** — each page keeps its own streaming
   `TextDecoder`, so a chunk one page misses desynchronises a multi-byte character for it. A
   single page slot is what let the second tool silently steal the pipe, showing "Connected"
   while receiving nothing. `tools/smoke_fanout.py` guards this, no hardware needed.
7. **The S3 bootloader must match the board's flash size.** Its header *declares* the size, so
   writing the 16 MB build onto an 8 MB board does not fail — it boots and then silently
   corrupts NVS. `src/wcb_flash.py` detects chip and flash size before downloading anything and
   **refuses** a full flash when no size-matched bootloader exists. Never make that a warning.
8. **The two tools do not agree on a line terminator.** The NaviCore tool ends every line with
   `\n`; **the WCB Wizard ends every command with `\r` and never sends `\n`** (`send(cmd +
   '\r')`, and `sendAndCollect` likewise). The shim's writable frames outgoing bytes per line,
   so splitting on `\n` alone left every Wizard command parked in its buffer forever — never
   sent. It failed *silently and invisibly*: the page still received the relay's console
   mirror, so it looked connected and healthy while nothing it sent arrived. The first pull
   timed out as "config pull incomplete", so no relay was ever identified and no mesh boards
   appeared. **Split on `\r`, `\n` and `\r\n`.** Both readers on the far end already accept
   either (`mgmt_wsserver.h` `feed()`: `if (c == '\n' || c == '\r')`).
9. **Intercept `flashFirmware()`, not the Wizard's buttons.** Flashing there is one branch of
   `boardGo()` per board slot, wrapped in bookkeeping (save port, close, progress, reconnect,
   re-share, re-push config) that is correct and not ours to reimplement. Every path funnels
   into that one call, and the replacement **must throw on failure** — `boardGo()` uses the
   exception as its only failure signal, so returning quietly makes it report success and then
   push config at a board that never got new firmware.

## Offline is the normal case, not the edge case

The workflow this app is built around: **get everything current while you have a
network, disconnect, join the droid's AP, and configure the fleet from there.** On that
AP there is no route to GitHub — so anything that reaches for the internet at the moment
of use is broken by design.

- **The cache is keyed by (product, BRANCH), not product alone.** CI publishes binaries
  per branch, so a feature branch has a real flashable build — and a fallback that
  crosses branches is worse than no fallback. Keyed by product only, a throttled fetch
  for `WIFI` silently returned **main's** images under legitimate-looking names.
  Observed. Now a miss is a miss and says so.
- **Both flashers cache every image they download** (`src/fwcache.py`), and fall back to
  the cache when the network is gone. `tools/fetch_firmware.py` fills it deliberately —
  both WCB families *and* both S3 flash sizes, because which one a board needs is only
  known once esptool has answered, and that happens offline.
- **The wikis are downloaded too** (`tools/fetch_wiki.py` → `src/wiki/`,
  served at `/wiki/`). Documentation is the thing you reach for when something
  is not going the way you expected, which on a con floor is exactly when there
  is no route to GitHub. There is **no API for wikis** and codeload 404s on
  them; what works is the `_pages` index for the list plus
  `raw.githubusercontent.com/wiki/<owner>/<repo>/<Page>.md` for content, with a
  link-crawl as the fallback when the scrape breaks. **Images are capped at
  1 MB**: fetched whole the WCB wiki alone is 123 MB of assembly photographs,
  against 10.4 MB for all three wikis with the cap, and a skipped image renders
  as a link to the online copy rather than a broken picture.
- **THE PAGE'S OWN GitHub CALLS ARE ANSWERED LOCALLY TOO** (`src/ghproxy.py`,
  `/_api/gh/*`, redirected by the shim's `fetch` patch). Intercepting the flash
  buttons was not enough: **both OTA paths fetch the image inside the page**
  (`index.html` ~17441 and ~17697 → `fetchFirmwareImages()` → `api.github.com`),
  and OTA over a relay is the whole point of a con floor -- no cable, board in
  the droid. The request is rewritten rather than the functions, because both
  tools agree on the GitHub REST shape and neither agrees on a function name.
  `download_url` comes back pointing at the host, so the follow-up byte fetch
  lands here too. Only the two firmware repos are served: this process can reach
  the internet and the page cannot.
- **The GitHub file listing is cached too.** Caching only the images is not enough: both
  flashers call the Contents API *first* to discover what exists, and that call fails
  before any download is attempted — so the cache could never be reached.
- **Ask `flash.reachable()` ONCE before a batch, do not infer from exceptions.** On a
  droid's AP there *is* a default route — it just goes nowhere — so a connection does
  not fail, it **times out at ~40 s**, and `urlopen`'s own timeout does not bound it
  because DNS and connect retry underneath. Four attempts across a ~35-file tool update
  is tens of minutes of certain failure. One TCP probe answers in ~3 s. A timeout can
  never be treated as "offline" in general (a real network times out transiently), which
  is exactly why the question has to be asked separately.
- **`no_network()` in `flash.py` fails fast** on the errors that *are* unambiguous. The retry loop is built for GitHub's
  throttles (4 attempts, ~9 s). Offline that is pure waste: measured at **over two
  minutes** for one firmware set before falling back. A DNS or no-route error breaks out
  immediately; a *timeout* still retries, because that really can be transient.

## The branch picks the TOOL as well as the firmware

CI publishes the tools per branch to gh-pages under `/dev/<branch>/`, with the same
`Wizard/` + `Images/` sibling layout as the site root — so `../Images` resolution
carries over and `tools/fetch_webui.py` needs no special case. Verified against
gh-pages, not assumed.

They must match. A branch build that adds a config field is unreachable from a
main-branch UI with no widget for it, and that shows up as "the tool cannot see the new
setting" rather than as anything version-shaped.

- **Not every branch publishes a tool.** WCB deploys `/dev/<branch>/Wizard`; NaviCore's
  gh-pages has no `/dev/` tree at all today. `resolve_base()` probes and falls back to
  main *saying so* — otherwise a branch set for firmware reasons turns into "my config
  tool stopped updating", which nobody would connect back to it.
- **The tools resolve their own branch from `localStorage`** (`wcb_fw_branch`,
  `rc_fw_branch`) — documented escape hatches, so setting them is using the page's own
  mechanism. `host.py` prepends `window.__intellexBranches` to the served shim because
  the tools read that key during *init*: an async fetch resolves after they have already
  looked, and the page would report main's firmware while the host flashed the branch's.

## Firmware comes from a branch you choose

`src/settings.py` holds a branch **per product** — a WCB feature branch alongside
released NaviCore firmware is the normal case, not an edge case. Set it in the launcher;
both flashers and the offline cache read it.

- The branch is interpolated into a GitHub API URL, so it is **whitelisted**
  (`[A-Za-z0-9._/-]`, ≤100 chars) — the same guard, for the same reason, that
  `flasher.js` documents.
- Resolved **at call time**, never as a default argument: a default is evaluated once at
  import and would pin the branch for the life of the process.
- A non-main branch warns in the flash log and is called out in the launcher. Flashing a
  branch build should only ever happen on purpose, and a setting that persists silently
  is how you flash one months later having forgotten.

## Frozen builds write somewhere else

`src/paths.py` resolves every updatable directory (`webui`, `webui_wcb`, `firmware`) to
**the user data dir if a copy is there, else what shipped inside the app**.

A PyInstaller one-file build unpacks to a temp directory that is deleted on exit, so an
update written "beside the app" is gone the moment it closes — silently, with the button
reporting success. Reads prefer the user copy; the bundled copy is the permanent
fallback, which is what makes a bad update recoverable by deleting one directory.

**A frozen build has no python and no `tools/` directory.** Anything that shells out to a
script must re-invoke the app with a sentinel that `app.py` intercepts before startup —
`--run-esptool`, `--run-fetch-webui`, `--run-fetch-firmware`. Adding a fourth follows the
same pattern; calling `sys.executable` with a script path does not work.

## Firmware counterpart

The droid side is `wifiEnabled` / `wifiSsid` / `wifiPassword` in `NaviCore/rc_config.h`, off by
default, with the SoftAP raised in `setup()`. Tracked in [`docs/WIP_NOTES.md`](docs/WIP_NOTES.md),
which also carries the recipe for removing it.

**Verified on hardware 2026-08-28: ESP-NOW survives alongside the SoftAP.** Two runs with the
AP up and a laptop associated — direct USB, then bridged through a mgmt relay: 264 sends,
98.9% ack, both WCBs at 100% with zero retries, and every failure predating the steady state.
The shared-radio coexistence path works under real load. This was the assumption the whole
WiFi transport rested on.

## It is public now

Public since 2026-09-08, opened deliberately once the app worked end to end. The whole
history went public with it, so every commit made while it was private is readable.

**The discretion rules are retired.** NaviCore-side commit messages and identifiers no
longer need to be coy — "optional SoftAP" and "comms endpoint" were phrased that way to
avoid naming a desktop app that had not been announced. Name it.

**One rule outlived the secrecy, for a different reason.** Still never put an
Intellex-specific string in `config_tool/index.html`. Not to hide anything now, but because
**the tool is not forked** (see [§ The UI is NOT forked](#the-ui-is-not-forked)): that file is
NaviCore's, it ships from Pages to every user whether or not they have ever heard of this
app, and a string that only makes sense inside Intellex is divergence by another name.

## The launcher picks a droid, THEN acts

`src/launcher.html` is a two-column chooser: devices on the left, and a panel on
the right that says what is selected, which layout it will open, and what needs
maintenance. Three rules it is built on, all of which were bugs first:

- **A row selects; it does not connect.** Connecting straight from the list meant
  choosing the layout *before* the droid, in a control that had nothing to do with
  it — and a misclick was an attach, a reconnect and a page load rather than a
  change of mind.
- **Auto-select runs ONCE, at the end of a full scan, in document order.** Serial
  renders first because enumerating ports is instant, so "select the first row"
  run on every list change always landed on a COM port even with a droid on the
  WiFi. It never moves a selection the user has made.
- **The action and the maintenance lines are PINNED**, and only the selection
  detail and layout list scroll. Pinning just the maintenance block pushed
  "Connect and open" below the fold, which is worse than what it fixed.

The **theme** is remembered in `intellex_theme` and defaults to the OS preference.
Both marks ship: the dark one's near-white housing outline vanishes on a white
panel. Fonts are **bundled** (`src/assets/fonts/`, 60 KB) — IBM Plex Sans is a
variable font, so one file covers 400–700, and fetching it from Google would mean
one typeface at the bench and another on a con floor.

## pywebview throws away localStorage unless told not to

`webview.start()` defaults to **`private_mode=True`**, whose documented effect is
that *"cookies and local storage are not preserved"*. So everything the pages
remember was silently wiped on every launch — the light/dark choice, the layout
(`intellex_view`), the divider (`intellex_split`), **and the two tools' own
preferences**, including the `wcb_fw_branch` / `rc_fw_branch` keys their repos
document as the branch escape hatch.

It reads as "the app does not save my settings" and looks like a bug in whichever
setting you noticed first. `app.py` now passes `private_mode=False` and an explicit
`storage_path` under `paths.user_data_dir()`, so that state lands with everything
else the user has accumulated and deleting that one directory is still a clean
reset.

## Never spawn a bare subprocess

The app is windowed — `pythonw`, and the frozen build is `console=False`. A GUI
process has no console, so spawning a console program (`netsh`, `esptool`, `git`,
or the exe re-invoking itself) makes Windows **create one**, and a black window
flashes at the user. Use `src/proc.py` (`proc.run` / `proc.popen`), which adds
`CREATE_NO_WINDOW` on Windows and nothing elsewhere.

Not cosmetic: the reconnect loop bounces the adapter on a timer, three `netsh`
calls a go, so an unreachable droid produced a console flash every few seconds
indefinitely. It looks exactly like malware and names no culprit.

## An adapter bounce that fails is not a transient

`wifi_bounce` failing with *"no wireless interface is associated with X"* means the
profile is absent or the user is deliberately on another network. The thousandth
attempt fails exactly like the first. Unbounded it ran **4288 times in one
session** — flapping the adapter every 10 s and burying every other log line.

`BOUNCE_GIVE_UP` caps consecutive failures, then leaves the adapter alone and says
so; the link itself keeps retrying passively. The counter resets on success, so a
droid that genuinely comes and goes still gets the retries this exists for.

## Never bounce the user's WiFi from a test host

`reconnect_loop` has **`auto_bounce = True` by default**. When a `ws` target is wanted
and unreachable, after two failures it runs `netsh wlan disconnect` + `netsh wlan connect
<ssid>` on the adapter associated with that SSID — and `ssid` defaults to `"NaviCore"`
when the attach body omits it.

That is correct for the app: a droid reboot leaves Windows holding a half-dead
association, and re-associating is the fix. It is **not** correct for a throwaway host
started to poke an endpoint. Attaching a test instance to a `ws` target that happens to
be unreachable disconnects the user's laptop from their droid's AP, mid-session, for
reasons nothing on their screen explains. This has happened.

**So when starting a host for testing:**

```bash
python src/host.py --port 879x --no-auto-bounce    # ALWAYS
```

and prefer not to attach a `ws` target at all unless the test needs one. It bounces the
*laptop's* adapter, not the droid — the droid is fine, which makes it worse to diagnose,
because the board looks healthy and the link is simply gone.

## Verifying

```bash
python -m py_compile src/*.py tools/*.py     # the bar for everything else

# Packaging. Fetches both tools AND the firmware cache first, then builds.
scripts\build-windows.bat                    # -> dist\Intellex.exe
scripts/build-macos.command                  # -> dist/Intellex.app + .dmg
powershell -File scripts\install-windows.ps1 # Start Menu shortcut

# The one real test. Fake transport, real Bridge, real aiohttp, real WebSockets —
# proves both tools can hold the link at once. No droid needed, so there is no
# excuse for skipping it after touching Bridge or /_link.
python tools/smoke_fanout.py

# Discovery's "what IS this host" logic, replaying REAL recorded console
# streams from the bench through the real probe(). Guards the trap that a
# doorway mirrors the mesh, so a PONG on the wire is not proof the thing you
# are talking to sent it. Run it after touching discover.probe().
python tools/smoke_probe_identity.py

# The shim's mesh auto-pull, run against fakes. Extracts routeMeshThroughBoard's
# REAL source text out of intellex_shim.js, so it cannot drift from what ships.
# Run it after touching the mesh routing or the shim's connect path.
node tools/smoke_mesh_route.js

# Can the PAGE get firmware with no internet? OTA depends on it -- both OTA
# buttons fetch the image inside the page, and OTA over a relay is the only
# way to reprogram a board on a con floor. Forces GitHub unreachable and
# checks the cache answers. Run after touching ghproxy.py or the shim's fetch.
python tools/smoke_ghproxy.py

# The docs viewer, end to end: fetch nothing, render everything already on
# disk, and prove no page is left pointing at a relative image or an
# un-rewritten link. Run after touching fetch_wiki.py or wikidocs.py.
python tools/smoke_wiki.py

# Both browser tools have no build step, so a syntax slip silently breaks all
# event wiring. Run after editing shell.html, launcher.html or the shim.
node C:\Users\ghulette\tools\jscheck.js src/shell.html      # inline <script> blocks
node --check src/intellex_shim.js                           # bare .js — jscheck reads HTML only
```

Everything else needs hardware. Say "compiles" and mean it; do not imply testing that did not
happen.

## Conventions

Follow the NaviCore house style: comments explain **why**, especially where a naive change would
reintroduce a fixed bug. Docs update in the same commit as the code. Every doc ends with a
revision log.
