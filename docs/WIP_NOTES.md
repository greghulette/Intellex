# Desktop App / WiFi — change tracker

**Purpose: make this work cleanly removable.** It is being built on `main` rather than a
branch, so this page is the substitute for that branch — the record of every commit and every
touched site, kept current as the work proceeds.

**If you are here to rip it out, go to [§1](#1-how-to-remove-it).** Everything else is context.

Status: **in progress.** The SoftAP comes up, a laptop associates, and the WebSocket command
endpoint answers over it (PING -> PONG, verified 2026-08-28). The droid side is functional;
the desktop app is not written yet.

Related: [CONFIG_SCHEMA.md](CONFIG_SCHEMA.md) · [CONFIG_TOOL.md](CONFIG_TOOL.md) ·
[TROUBLESHOOTING.md](TROUBLESHOOTING.md)

---

## 1. How to remove it

### The clean path

Every commit in this effort is listed in [§2](#2-commits) and touches nothing else. Newest first:

```bash
git revert --no-commit 2c0e788 327ae1d 5958d70
git commit -m "Remove the WiFi/desktop-app work"
```

Revert in that order (newest → oldest); the reverse conflicts, because each commit builds on the
last.

### Do NOT revert these — same session, different work

The config-transfer throughput effort landed alongside this and is entirely unrelated to WiFi.
It touches the *mesh* path, which the WebSocket work does not use at all:

| Commit | What |
|---|---|
| `a2b64b8` | Fragment **pacing** floor (`FRAG_PACE_FLOOR_MS` = 100, derived per-line) + the mesh-channel 1–11 clamp + the fragment idle-timeout fix |
| `6c8ed16` | Config **download**: adaptive fragment fill (`_jsonEscCost`, `rc_telemetry.h`) |
| `30e81f1` | Config **upload**: adaptive fragment fill (`FRAG_ENV_TARGET_BYTES` = 180, `config_tool/index.html`) |
| `de9ad49`, `f7296b6` | Doc repairs for the above |

**These are the two independent levers on mesh transfer speed and they compound**: *pace* (how
long between fragments) and *chunk* (how full each fragment is). `FRAG_CHUNK_BYTES` = 80 is now
a worst-case floor that the adaptive code grows from, not the actual size. Reverting the WiFi
work must leave all of this alone.

Note also that `bdf476e` (WebSocket endpoint) **carries an unrelated `rc_telemetry.h` hunk**
swept in from a concurrent session in the same checkout — the adaptive-fill code, committed
mid-edit, which briefly broke CI until `6c8ed16`. A revert of `bdf476e` would take that with it.
Revert the file paths, not the whole commit.

**`e6a51c5` is deliberately absent from that list.** It splits `handleSerialInput()` into framing
plus `processInputLine()`, and it is a verified pure refactor with no WiFi in it — the wireless
work merely needed it first. **Leave it in.** It makes the input path reusable and better on its
own terms, and reverting it is a code *move* that will conflict with anything landed since. Only
revert it if you specifically want the old single-function shape back, and do it separately.

### If the commits have become tangled with later work

Remove by symbol instead. These names appear nowhere else in the tree, so a grep is exhaustive:

```
wifiEnabled   wifiSsid   wifiPassword          # firmware + tool
cfg-wifi-                                      # tool DOM ids
"wifi"  "wifien"                               # NVS keys
[WIFI]                                         # firmware boot log prefix
```

Per-site inventory is in [§3](#3-every-touched-site). Line numbers drift — the symbols do not.

### What removal leaves behind

- **NVS keys `wifien` and `wifi` stay on every board that ran this firmware.** Reverting the
  code does not erase them. They are inert once nothing reads them, and a `RESET_DEFAULTS` or a
  full wipe clears them. Harmless, but they exist.
- **`wifiEnabled` / `wifiSsid` / `wifiPassword` remain in saved config JSON and cloud backups**
  taken while this was live. Reverted firmware ignores unknown keys, so old backups still
  restore — they just carry three dead fields.
- **A board left with `wifiEnabled: true` keeps its AP until reflashed.** Reverting the source
  does nothing to a board in the field. Turn WiFi off *before* rolling back, or reflash it.

### One-line kill switch (no revert)

To disable everything without touching git, force the default off and ignore stored state —
`rc_config.h`, in `rcConfigDefaults()`:

```c
rcConfig.wifiEnabled = false;   // and delete the prefs.isKey("wifien") restore at ~2401
```

With the restore gone the flag can never become true, so the `if (rcConfig.wifiEnabled)` block
in `setup()` is dead and the AP cannot come up. Useful for bisecting whether WiFi is implicated
in some other symptom.

---

## 2. Commits

| Commit | What it did | Files |
|---|---|---|
| `bdf476e` | **The WebSocket command endpoint** (`navicore_wsserver.h`, +33.5 KB flash / +1.4 KB static RAM). Gated on `wifiEnabled`; feeds the same `processInputLine()` as USB. Handler on Core 0 enqueues only, command runs from `loop()` on Core 1. **Verified on hardware** — PING → PONG. NOTE: this commit also swept up an unrelated `rc_telemetry.h` change from a concurrent session (adaptive fragment fill), which briefly broke CI; fixed in `6c8ed16`. Neither belongs to this effort | `NaviCore.ino`, `navicore_wsserver.h`, `docs/PROTOCOLS.md` |
| `e6a51c5` | **First NON-additive change.** Split framing from dispatch: `handleSerialInput()` keeps the read loop, the ~430-line dispatch moves verbatim to `processInputLine(const String&)` so a non-serial transport can reuse it. Verified a pure refactor by normalised diff (274 executable lines, byte-identical). Reverting this is a code MOVE, not a deletion — see §1 | `NaviCore.ino`, `docs/PROTOCOLS.md` |
| `5958d70` | `wifiEnabled` bool, default false, six sites. Hidden toggle in the cloud-backup modal (reusing the existing 4×-wordmark gesture rather than adding a second secret). **Inert** — nothing read the flag | `rc_config.h`, `config_tool/index.html`, `docs/CONFIG_SCHEMA.md`, `docs/CONFIG_TOOL.md` |
| `327ae1d` | `wifiSsid[33]` + `wifiPassword[64]` — the flag alone could not name or secure an AP. Own NVS key `wifi`. AP name/password fields beside the toggle. Still inert | `rc_config.h`, `config_tool/index.html`, `docs/CONFIG_SCHEMA.md`, `docs/CONFIG_TOOL.md` |
| `2c0e788` | **The flag became live.** `setup()` raises the SoftAP before `wcb->begin()`. Removed the now-false "No WiFi AP or web server" comment | `NaviCore.ino`, `config_tool/index.html`, `docs/CONFIG_SCHEMA.md`, `docs/TROUBLESHOOTING.md` |

| `a7e9b3e` | **Per-client line accumulators in the WS handler** (`WsAcc`, `accFor()`), plus `cfg.close_fn = wsClose` for session teardown — which also gave `WsSink::drop()` its first caller. Fixes cross-client command corruption reproduced on hardware. Confined to `navicore_wsserver.h`; removing the endpoint removes this too | `navicore_wsserver.h`, `docs/PROTOCOLS.md`, +4 docs (restored a missing markdown separator row) |
| `55a097c` | **Three WS sink fixes**: UTF-8-safe frame boundaries (`_utf8SafeLen`), the `_dropping` latch that survived a disconnect, and queue depth 3 → 8 with a 50 ms enqueue wait instead of silent drops. All reproduced on hardware first | `navicore_wsserver.h`, `docs/PROTOCOLS.md` |
| `4d30c17` | **NOT part of this effort — keep on rip-out.** Both OTA END waits accepted a late DATA-phase ACK: the USB path reported failure on a successful update, the WCB path reported "Verified" for an image the target never verified. Pre-existing on the USB/mesh paths and unrelated to WiFi | `config_tool/index.html`, `docs/PROTOCOLS.md`, `docs/CONFIG_SCHEMA.md` |

`fw_version.h` also changes in each — that is the pre-commit DTG stamp, not part of this work.

---

## 3. Every touched site

### `rc_config.h` — the config field (all of it additive)

| Area | What |
|---|---|
| struct `RcConfig` | `wifiEnabled`, `wifiSsid[33]`, `wifiPassword[64]` |
| `rcConfigDefaults()` | all three zeroed / false |
| `rcConfigToJSON()` | three `doc[...]` writes |
| `rcConfigFromJSON()` | three reads; `wifiEnabled` uses `\| false` so an older config can never enable the radio |
| NVS save | `putBool("wifien")` + a `"wifi"` JSON blob (`ssid`, `pw`) |
| NVS load | `isKey("wifien")` restore + `isKey("wifi")` blob restore |

Modelled site-for-site on `sbusOutEnabled` — diff the two if anything looks out of place.

### `NaviCore.ino` — the bring-up (one block)

One `if (rcConfig.wifiEnabled) { … }` in `setup()`, immediately before `wcb = new WCB_Client(...)`.
Deleting the block restores the previous behaviour exactly; nothing else in the file references it.

Three properties are load-bearing, and matter to anyone moving rather than deleting this:

1. **It must run before `wcb->begin()`.** `WCB_Client` checks `WiFi.getMode()` on entry and,
   finding an AP up, selects `WIFI_AP_STA` and keeps it instead of forcing `WIFI_STA` and tearing
   the AP down (`WCB_Client.cpp` ≈89-110).
2. **The channel is passed explicitly** from `wcbNetwork.channel`. `softAP()`'s 3rd parameter
   defaults to 1, and once an AP owns the radio `WCB_Client` only *warns* on a mismatch — so a
   defaulted channel against a mesh on any other channel is a silent total blackout.
3. **It fails closed.** Empty or <8-char password refuses to start rather than falling back to an
   open AP. This command surface has no per-command auth (`REBOOT`, `RESET_DEFAULTS` dispatch on a
   bare `type`), so an open AP is an unauthenticated command channel to the whole mesh.

### `config_tool/index.html` — three clusters

| Cluster | What |
|---|---|
| default `config` object | `wifiEnabled` / `wifiSsid` / `wifiPassword` |
| `applyConfig()` | three reads from the board's `CONFIG` |
| `cfgOpenCloudModal()` | the hidden block: markup (`cfg-wifi-enable`, `-ssid`, `-pw`, `-pw-eye`, `-note`), the `_wifiNote()` helper, and four listeners |

No existing function was modified beyond those insertions — no transport code, no save path, no
message dispatch. `_diffConfigBranches()` picks the fields up generically; nothing was special-cased
for them.

### Deliberately NOT touched

- **The CSV cheat-sheet export.** A user-facing artifact; listing the field there would defeat the
  hiding. Consequence: a CSV round-trip does not carry the WiFi settings.
- **The Via-WCB strip in `saveConfigToBoard()`.** It strips `wcbNetwork` transport fields only, so
  WiFi settings save over either transport. Deliberate — they do not define the transport in use.
- **The ArduinoJson filter whitelist.** Not needed: `SET_CONFIG` deserialises un-filtered
  (`NaviCore.ino` ≈3725). The whitelist governs fields read by *non*-`SET_CONFIG` handlers.

---

## 4. Verified vs assumed

| | |
|---|---|
| **Verified on hardware** | Save → NVS → boot → `GET_CONFIG` round-trip of all three fields. SoftAP comes up; a laptop associates |
| **Verified by compiler only** | Everything else. 1,128,539 B, 57% of the 1,966,080 B app slot |
| **Verified on hardware (2026-08-28)** | **ESP-NOW survives alongside the AP — the design's biggest risk, retired.** SoftAP up with a laptop associated, two runs. Direct USB, 8 min: WCB1 27/27, WCB2 15/15, 100%, no retries. Then bridged **through** the mgmt relay, 10 min: 264 sends, 98.9% ack, WCB1 34/34 and WCB2 19/19 at 100%, relay itself 209/211. Crucially the retry/fail counters were **identical in both snapshots** (9 retry, 3 fail) — every failure predates the steady state, and ~206 later sends produced none. The `WCB_Client` coexistence path (`WIFI_AP_STA`, `WIFI_PS_NONE`, channel deferral) works under real bridge load, not just idle |
| **Verified on hardware (2026-08-30)** | The WS endpoint under adversarial load: two clients interleaving partial lines, a client disconnecting mid-line, an 8-command burst in one frame, CRLF framing, and a 13,764 B `GET_CONFIG` arriving whole and parsing — including immediately after a client is killed mid-reply. `_utf8SafeLen` is unit-tested on the host across every split/complete boundary |
| **Known gap** | Non-ASCII output crossing a frame boundary is fixed by construction and unit-tested, but has not been exercised end-to-end on hardware — that needs a config containing a multi-byte character |

---

## 5. Decisions worth not relitigating

- **Toggle lives in the cloud-backup modal** — that modal is already behind the 4×-wordmark
  gesture, so this inherits an existing hiding place instead of adding a second secret.
- **Hiding is obscurity, not security.** The tool is public and readable. The real gate is
  `wifiEnabled` defaulting false with the bring-up unreachable while it is. Corollary: a droid
  *reporting* WiFi on should show that in visible chrome even to a user who cannot see the switch —
  hide the control, never the state.
- **Never reuse `wcbNetwork.password` as the AP password.** It rides in cleartext in every ESP-NOW
  packet the droid emits; it is public by construction. Hence the separate NVS key.
- **Empty SSID derives `NaviCore-<deviceId>`** so a droid always has a distinguishable name.

---

## 6. Still to come

Each will be added to [§2](#2-commits) as it lands.

1. **The comms server** — `esp_http_server` (ships in the pinned core, `CONFIG_HTTPD_WS_SUPPORT=y`,
   measured +32.8 KB flash / +8 B static RAM). Gated on the same flag.
2. **A firmware transport seam.** `handleSerialInput()` (≈3692-4172) reads `Serial` directly and has
   no line-level entry point, so a WebSocket cannot reuse its dispatcher. Extracting
   `processInputLine(const String&)` is the fix — but it modifies the primary input path rather than
   adding to it, which makes it the *first* change in this effort that is not cleanly revertible.
   Land it as its own commit, separate from anything else, for exactly that reason.
3. **The desktop app** — Python (reusing the ESP-Flasher-Companion pipeline), talking to a local
   WebSocket so serial and board-WiFi are one code path in the page.

---

## Revision log

| Date | Commit | Change |
|---|---|---|
| 2026-09-10 | _(uncommitted)_ | **OTA over a WCB doorway aimed a NaviCore image at the WCB.** An *Update over USB (OTA)* failed with `[OTA:BEGIN,ERR,0]` while Intellex was attached to WCB *Body* at `192.168.4.1`. The chain, each link checked: the NaviCore tool decides direct-versus-bridged from whether a PING draws a PONG and **accepts any PONG** (`index.html` ~9515, no `id` or `sys` check); through a doorway the NaviCore's mesh PONG is mirrored straight back, so the tool concluded it was wired to the droid and never fell back to Via WCB; *Update over USB (OTA)* is enabled only when not Via WCB, so it ran; it sends `?OTALOCAL,BEGIN,<size>,1`, and a `?` command is run by the doorway **itself** -- proven by the outcome, since `WCB_OTA.cpp` answered in its own format and its own guard fired. *Body* is HW 1.0, a classic ESP32 (family 0) against the tool's hardcoded family 1, so the **chip-family brick guard** refused it. The `0` is `otaWrittenOffset()`, not an error code; the real reason prints on the `[OTA] ...` line just before it, which the tool sends to its Terminal pane rather than the firmware log, because it only intercepts `[OTA:` markers. **That guard was all that stood in the way: an ESP32-S3 WCB (HW 3.1/3.2) shares the image's family, and `otaBegin()` checks only family, slot and size**, with `esp_ota_end` validating just magic and SHA -- so it would have taken NaviCore firmware. There was no manual escape: the tool's Via-WCB checkbox was removed in NaviCore `99536e2` (2026-07-28), and the NaviCore wiki still describes it. **Fixed here, in the shim:** `autoConnect()` calls the tool's own `onViaWcbToggle(true)` when the host reports role `wcb` or `relay` -- *after* `connectDirect()`, since `openPortAndStart()` resets the flag at its start, and safely so, because the handshake sends only JSON (`GET_CONFIG`, `START_MONITOR`, `SET_DEBUG_FLAGS`), which a doorway forwards. Not for role `navicore`, whose own access point is a direct link. `tools/smoke_doorway_via_wcb.js` extracts the real `autoConnect()` and fails against both a shim without the switch and one that switches before the handshake. **Not fixed, because it belongs to the other repos:** the tool trusting any PONG, WCB OTA not checking what image it is handed, and the stale wiki. **One visible consequence, and it is the designed one:** in Via WCB mode the tool strips the `wcbNetwork` transport fields (`deviceId`, `macOct2`, `macOct3`, `password`, `quantity`, `channel`) from a Save and says so plainly, because changing them over a bridge would cut the link the Save travelled on. The misdetection had been bypassing that; a doorway user editing those fields now gets a warning instead of a Save that appears to work. Unverified on hardware until the app is rebuilt. |
| 2026-09-10 | _(uncommitted)_ | **Review fixes on the 2026-09-08..10 work.** **The docs viewer ran script from its own URL:** a page not on disk is titled with the name it was asked for, and that went into `<title>` unescaped, so `/wiki/wcb/</title><script>…` executed on the app's origin -- where a POST to `/_api/attach`, `/_api/flash` or `/_api/wifi-bounce` passes `_guard_origin`, because the Origin is ours. Any website could send the browser there; reproduced with aiohttp's test client. Titles are escaped, the template fills in one pass, and wiki pages carry a CSP that runs only the viewer's nonce'd script (files served from a wiki's tree get `sandbox`) -- the markdown renders raw HTML, so that is also what keeps a script inside a wiki page inert. **`/_api/branch-list` ran `reachable()` on the event loop**, stalling `/_link` for up to its 9 s budget on every launcher open on a droid's AP; now in a thread. **`ghproxy.contents` accepted any path** and cached the reply as the firmware listing before checking it was one; now only the product's bin directory, stored after parsing. **`fetch_wiki._swap_in` deleted the old copy before the new one arrived** from a temp dir that is often another volume; it now moves beside the destination first and restores on a failed rename, and a failed swap no longer aborts the other wikis. Its crawl also filed dotted page names (`version3.2_build`) as assets. **`winsize` measured only the primary monitor** (and on macOS returned global coordinates that pywebview re-offsets by the screen origin), so later windows opened on the wrong display. Launcher: the **Log** button's first click did nothing (its hidden state moved into CSS, the check still read the inline style); "No droid found" was announced before WiFi had been scanned and rewritten every 4 s while watching; Refresh said "updated" whatever happened, and a second unforced request wiped its error. `branchlist` no longer pins a stale list for the whole run after a total failure. The sidebar filter no longer hides a paragraph whose own link matches. Compiles; offline smoke tests pass; not tested on hardware or on a multi-monitor setup. |
| 2026-09-09 | `ba50ecd` | **A NEW HTTPS CALLER MUST GO THROUGH `src/certs.py`.** `fetch_wiki.py` and `branchlist.py` used `urlopen`'s default SSL context; the python.org macOS framework build ships an **empty CA store**, so that context can verify nothing and every request died with `CERTIFICATE_VERIFY_FAILED` on a machine whose network was fine. `flash.py`, `host.py` and `fetch_webui.py` already routed through `certs.context()` — these two were written later and did not, and Windows hid it because it has a system CA store. **The symptoms named everything except the cause:** `reachable()` passed, because it is a TCP probe that never completes a handshake; `page_index()` swallowed the SSL error in a bare `except` and said "page index unreadable"; the crawl fallback then failed on `Home.md` and said "nothing downloaded". Three messages, all pointing at GitHub and at the wiki's markup rather than at the interpreter. Diagnosing it from the Windows box cost two round trips and two real-but-unrelated fixes to `reachable()` before a session on the Mac read the actual exception. So the failure now names itself: cert errors are recorded and `certs.ADVICE` is printed once, and the page loop breaks on the first one rather than proving the same point against several hundred queued names. **Audited after:** all seven HTTPS callers in `src/` and `tools/` now pass a context; the eighth, `app.py`'s readiness poll, is plain HTTP to loopback and correctly does not. Verified on both platforms — 9/9 intellex, 24/24 navicore, 42/42 wcb. |
| 2026-09-09 | _(uncommitted)_ | **`reachable()` cached one answer for three different hosts.** Callers probe `api.github.com` (flashing, the proxy, the branch list), `greghulette.github.io` (the tool update) and `github.com` (the docs), but the cache was two module globals with no key — so one failing probe answered for all of them for the next 20 s. In the app that turns a single failure into "tools, firmware AND docs are all unreachable", three messages with one cause. Now keyed by host. **And a failure records WHY**: `unreachable_reason()` distinguishes a DNS error from a refused connection from *no answer within the budget* — the last being what a droid's AP looks like. Every "not reachable" message now carries it, because the bare sentence is true and useless: it cannot tell a laptop with no resolver from a board's access point from a firewall, and diagnosing the macOS failure without it meant guessing. **Note this does NOT explain the failing macOS build** — each fetcher there is a separate process, so nothing is cached between them. The reason string is what will identify that. |
| 2026-09-09 | _(uncommitted)_ | **Nothing the pages remembered survived a restart.** `webview.start()` defaults to `private_mode=True`, documented as *"cookies and local storage are not preserved"*, and `app.py` never passed otherwise — so `localStorage` was wiped on every launch, on both platforms. It surfaced as "how do I make the Mac default to dark", but the theme was the least of it: the chosen layout (`intellex_view`), the side-by-side divider (`intellex_split`) and **the two tools' own saved preferences** went with it, including the `wcb_fw_branch` / `rc_fw_branch` keys their own repos document as the branch escape hatch. Now `private_mode=False` with an explicit `storage_path` under `paths.user_data_dir()`, so the state lands with everything else the user has accumulated and deleting that one directory is still a clean reset. Verified by launching, quitting, and reading the key back out of WebView2's leveldb: `intellex_theme -> dark`. |
| 2026-09-09 | _(uncommitted)_ | **`reachable()` reported a healthy network as offline on macOS**, so the first Mac build fetched no tools, no docs and no firmware, and the app then refused to download any — while sitting on the very connection git had just cloned through. Two causes, both in one function. **The wall-clock budget was the same 3 s as the connect timeout**, but it has to cover the NAME LOOKUP too: `getaddrinfo` takes no timeout argument at all, and the function's own comment already recorded *"a 3 s probe took 13.6 s, nearly all of it DNS"* — so on a cold resolver the thread was abandoned with no answer and that read as unreachable. There are now two budgets: `timeout` bounds one connect, `budget` (9 s) bounds the whole answer. **And each address gets its own attempt** — a network advertising IPv6 it cannot route answers AAAA first, and `socket.create_connection` gives that address the full timeout before trying IPv4, so one dead address consumed everything. macOS prefers IPv6 more eagerly than Windows, which is why it surfaced there and not here. Measured after: 27–64 ms online, with a DNS failure and an unroutable address both still bounded. |
| 2026-09-09 | _(uncommitted)_ | The launcher opens the **WCB Wizard** by default on a first run rather than the NaviCore tool — a WCB or a relay is what the WiFi list usually turns up, and it is the doorway to everything behind it. Only a default: the choice is remembered the moment one is made. "NaviCore tool" is now just **NaviCore**. |
| 2026-09-09 | _(uncommitted)_ | **`scripts/build-macos.command` was committed 100644**, so the Mac refused it: *"cannot be executed because you do not have the appropriate access privileges"* — which reads like Gatekeeper or an ownership problem and is neither. `core.fileMode` is false on the Windows box, so Git ignores working-tree permissions there and a new `.command` silently lands without the exec bit; `chmod` locally does nothing, `git update-index --chmod=+x` is what works. The other two `.command` files were already 100755, which is why this had not been seen. |
| 2026-09-09 | _(uncommitted)_ | **Neither build script fetched the wikis**, so a build from a fresh clone shipped an app whose docs viewer was empty. It only ever looked right here because src/wiki/ already existed from a manual fetch -- the exact failure a second machine finds first. Both scripts now fetch the docs alongside the tools and the firmware, and both **install requirements.txt on every run** rather than only when creating the venv: markdown-it-py arrived with the offline docs, and a venv predating it would otherwise build an app that dies the first time the docs are opened. Verified by deleting src/wiki/ and building: 75 pages fetched, and the frozen exe serves all three wikis. |
| 2026-09-08 | _(uncommitted)_ | **The launcher is rebuilt to the Claude Design mock** (`design/Intellex Launcher.dc.html`): a two-column chooser — devices left, a panel right saying what is selected, which layout it opens and what needs maintenance — plus filter tabs with counts, a header attention pill, and a **light theme**. Three things the mock did not have to survive, each found by running it against real data rather than placeholder rows. **A row now selects rather than connects:** connecting from the list meant choosing the layout *before* the droid, in a control unrelated to it, and a misclick was an attach plus a page load rather than a change of mind. **Auto-select runs once, at the end of a full scan, in document order** — serial renders first because enumerating ports is instant, so selecting the first row on every list change always landed on a COM port even with a droid on the WiFi; it never moves a selection the user has made. **The action and the maintenance lines are pinned, and only the selection detail and layout list scroll** — pinning just the maintenance block, which exists to promise nothing is hiding behind a collapsed panel, pushed *Connect and open* below the fold instead. Fonts are **bundled** (`src/assets/fonts/`, 60 KB; IBM Plex Sans is a variable font so one file covers 400–700): the mock loaded them from Google Fonts, which would mean one typeface at the bench and another on a con floor. Both marks ship — the dark one's near-white housing outline disappears on a white panel. The full maintenance panel keeps every control and moves full-width below the columns, since two dropdowns, four buttons and a log tail do not fit a 340px sidebar; the side panel summarises it in three lines and expands it on demand. |
| 2026-09-08 | _(uncommitted)_ | **OTA could not reach the firmware cache, which made the con-floor case the one case it did not cover.** A cabled flash was fine — the shim intercepts the flash buttons and the host runs esptool, reading the cache directly. **OTA never goes near that path:** both OTA buttons fetch the image *inside the page* (`index.html` ~17441 USB, ~17697 **over a relay**, both → `fetchFirmwareImages()` → `api.github.com`), and the Wizard does the same for its own boards (`app.js` ~167, `flasher.js` ~117). So on a droid's AP — no route to GitHub, firmware cached, relay online, OTA button enabled — the transfer could not start, and nothing on screen said why. New `src/ghproxy.py` + `/_api/gh/contents` and `/_api/gh/raw`, with the shim rewriting the tools' GitHub REST calls to them. **The request is rewritten, not the functions:** swapping `fetchFirmwareImages` would fix one tool and leave the Wizard needing its own swap, against names their repos are free to change, whereas both agree on the GitHub REST shape. `download_url` is rewritten to point back at the host so the page's follow-up byte fetch lands here too; online the answer is stored on the way past, so merely opening the Firmware tab fills the cache. **Not an open proxy** — only the two firmware repos are served, since this process can reach the internet and the page cannot. Verified offline with GitHub forced unreachable: both listings and both app images served from cache. `tools/smoke_ghproxy.py` guards it and was checked to fail when the `download_url` rewrite is removed. |
| 2026-09-08 | _(uncommitted)_ | **The wikis are downloadable and readable offline** — `tools/fetch_wiki.py` → `src/wiki/<product>/`, rendered by `src/wikidocs.py`, served at `/wiki/`, with **Download docs** and **Open docs** in the launcher. Documentation was the last piece of the con-floor workflow still needing a network, and it is the piece you reach for exactly when something is not going to plan. **There is no API for wikis and codeload 404s on them** (measured), so the list comes from scraping the wiki's own `_pages` index and the content from `raw.githubusercontent.com/wiki/<owner>/<repo>/<Page>.md`. Scraping is brittle, so a **link crawl is the fallback** — measured at 24/24 NaviCore, 9/9 Intellex, 40/42 WCB, the two misses being orphans no reader could navigate to either. **Both link dialects are parsed**, because the wikis disagree: NaviCore and Intellex use `[[Page]]` (285 vs 0), the WCB wiki uses `[Text](Page)` (4 vs 485) — a crawler that knew one style found two pages of forty-two. **Images are capped at 1 MB**: whole, the WCB wiki alone is 123 MB of assembly photographs; capped, all three wikis are 10.4 MB, and a skipped image renders as a link to the online copy rather than a broken picture. Three traps found while building it, all now guarded by `tools/smoke_wiki.py` (which was verified to FAIL against each): rendering rewrites links in markdown-it **renderer rules**, not by regex over the source, so URLs inside the wikis' many command examples survive; raw `<img>` tags (37 across the three) are invisible to those rules and need a separate pass over the finished HTML, where a fenced example is already escaped and so cannot be corrupted; and page-vs-asset is decided by **what exists**, not by whether the name looks like it has an extension — the WCB page `version3.2_build` has an apparent suffix of `.2_build` and 404'd. **A fourth surfaced only in the frozen build:** `paths.data_dir()` decides user-copy-versus-bundled once for a whole directory, which is right for `webui/` (half a config tool is not useful) and wrong for `wiki/`, which holds three independently downloaded wikis. Fetching just one left the other two reporting `have=False` while sitting inside the app, perfectly good — and a partial download is a normal outcome, not an edge case, since one wiki can fail while the others succeed. New `paths.data_subdir()` makes that choice **per product**. |
| 2026-09-08 | _(uncommitted)_ | **The WiFi self-heal was disabled by a guessed SSID.** The launcher inferred the network name from what discovery found — a WCB meant `"WCB"` — but all three firmwares build `<prefix>-<id>` from a *different* prefix (`WCB_WiFi.cpp` `wcbWifiDefaultSsid`), so a real bench shows **WCB1** and **WCB2**. `wifi_bounce()` locates its adapter **by SSID**, so a wrong name does not weaken the recovery, it **turns it off**: observed in the log as `re-associating WCB: FAILED (no wireless interface is associated with "WCB")` while sitting on a healthy WCB2. New `discover.ssid_for_host()` asks the OS instead — it pairs `netsh wlan show interfaces` with `netsh interface ip show addresses` and returns the SSID of the adapter holding an address on the droid's subnet. Resolved **at attach time**, because that is the one moment it is knowable: once the lease drops the question has no answer, which is exactly when the bounce needs it. Re-asked at bounce time too, since moving between two boards' APs keeps the address (every SoftAP is `192.168.4.1`) but changes the network, so the recorded name would be the previous one. The page now sends **no** `ssid` at all — it cannot know and must not guess. `None` means "do not bounce", never "fall back to a guess". |
| 2026-09-08 | _(uncommitted)_ | **"Update firmware" read as "push firmware to my boards".** It downloads images for later offline flashing and touches no hardware, but the label said otherwise to the person whose app this is — so the maintenance panel is rebuilt around what each control actually does. Renamed **Download firmware**, under the heading *Firmware, for flashing later*, with the reassurance stated in the panel rather than hidden in a `title=` tooltip, which cannot correct a misreading nobody knows they are having. The branch **free-text box is now a dropdown** (`src/branchlist.py`, `/_api/branch-list`): a branch is something you pick, not a git ref you retype from memory, and a typo used to validate fine and fail much later at flash time with nothing on screen connecting the two. The list follows the same offline discipline as everything else — `flash.reachable()` asked **once** before any request, the result cached under the user data dir with a 6 h TTL, and the current branch plus `main` **always** present so the control can render its own value and can always be put back to released firmware. `gh-pages` is filtered out: it is the published site, never a firmware source. "Other…" keeps a text box for a branch too new to be listed. |
| 2026-09-08 | _(uncommitted)_ | **The repo is public**, opened deliberately once the app worked end to end — and the whole history went with it, so every commit made while it was private is readable. `CLAUDE.md`'s § Discretion is retired: NaviCore-side commit messages and identifiers no longer need to be coy about naming a desktop app. **One rule outlived it for a different reason** — still never put an Intellex-specific string in `config_tool/index.html`, not for secrecy but because the tool is not forked and that file ships from Pages to every NaviCore user. Also corrected two stale claims a public reader hits first: the README said "Scaffold. Nothing runs yet." directly above a table of hardware-verified rows, and listed packaging as "Not written" when `scripts\build-windows.bat` produces a working `.exe` (the macOS half is still unbuilt). A **user-facing wiki** now lives at `greghulette/Intellex.wiki` — seven pages covering the app only, linking out to the NaviCore and WCB wikis rather than duplicating them. GitHub Free does not allow wikis on private repos, which is why it could not exist before; the API silently ignores `has_wiki=true` rather than erroring, so the block is invisible unless you read the value back. |
| 2026-09-08 | _(uncommitted)_ | **A WCB hosting the AP flapped between "Body, a WCB" and "NaviCore" on successive scans of the same address.** Not a timeout or a flaky board — `probe()` returned on the FIRST PONG it saw, and whether that PONG arrived inside the first read window was a race. **Every endpoint here is a console mirror and a WCB mirrors the whole mesh**, so the PING reached the NaviCore behind the WCB over ESP-NOW and its PONG came back through the mirror looking local. Captured on the bench: the PONG says `id=20` while the WDP SELF row says `N=1,ALIAS=Body` — the PONG was never the host's. **The two PONGs are not the same message.** A direct host ping is answered by `NaviCore.ino` ~3828 with a bare `{"type":"PONG","version":…}` over the console — **no id**; a ping arriving over the MESH is answered by `rc_telemetry.h` ~2189 with `{"sys":1,"type":"PONG","id":<deviceId>,…}` back over ESP-NOW (the `sys:1` marker exists so the Wizard can mute exactly this traffic — `NaviCore.ino` ~4219). So an id-less PONG is decisive on its own, and an id-bearing one counts only when the id **is** the host's own SELF row. All three questions are now sent up front and the answer is settled after ONE read, so it cannot depend on arrival order; the read still breaks on `[WDP:END`, and on a direct PONG, so neither common case pays the timeout. **This also fixes the same latent bug for a MgmtRelay doorway** with a droid behind it, which would have been misreported identically. Note `SELF`-row presence alone could never have discriminated: `WCB_Client` (which NaviCore runs) emits `CLIENT=0,PEER=3` byte-compatibly with a real board (`WCB_Mgmt.h` ~205). **The flap can hide one level down, so the no-dump case matters as much:** when a mesh PONG lands but `?WDP,DUMP` does not, there is no SELF row to compare against — and answering "navicore" there would merely MOVE the race, showing "WCB" on scans where the dump arrives and "NaviCore" on scans where it does not. Only something that mirrors mesh traffic can put a mesh PONG on that socket, so that case is a doorway too, resolved by the same `[relay]` marker. New `tools/smoke_probe_identity.py` replays the recorded streams through the real `probe()` — no hardware — and fails against the pre-fix code. |
| 2026-09-08 | _(uncommitted)_ | Shell bar read **INTEL LEX**, split down the middle. `#bar .brand` is a flex container, and a flex container makes every run of bare text an **anonymous flex item** — so `INTEL<i>LEX</i>` was two items, not one, and the `gap: 6px` meant to separate the mark from the wordmark landed between INTEL and LEX as well. The wordmark is now wrapped in its own `<span class="word">`, giving the container exactly two children; tracking moved onto that span too, since on the container it also applied to the last letter and padded dead space in before the buttons. The launcher masthead is not affected: its flex children are the `<img>` and a `<div>`, and the wordmark is inline inside the `<h1>` within it. |
| 2026-09-08 | _(uncommitted)_ | **The window no longer opens with its bottom under the taskbar.** New `src/winsize.py`; both `create_window` calls go through it. TWO causes, and fixing either alone still leaves the window cut off. (1) **Size** — pywebview's `width`/`height` are LOGICAL pixels and its Windows backend multiplies them by the monitor scale (`winforms.py`: `Size(initial_width * scale, …)`), so a flat `height=880` is **1100 physical px at 125% scaling against a 1020px work area**. (2) **Position** — given no `x`/`y`, pywebview falls back to WinForms `FormStartPosition.CenterScreen`, which centres on the monitor's FULL BOUNDS and knows nothing about the taskbar; a window clamped to exactly the work area's height still comes out centred in 1080 rather than 1020, putting its bottom 30px behind the taskbar. **Measuring DPI before the window exists is the subtle part:** this runs before `webview.start()`, and pywebview does not call `SetProcessDPIAware` until inside it, so the process is normally DPI-UNAWARE here and every metric is pre-divided by the scale — while a frozen build could arrive already aware via a manifest. Normalising by `GetDpiForSystem()` (a flat 96 when unaware, the true DPI when aware) makes `physical * 96 / dpi` land on the same logical number either way: measured on this 1920x1080 at 125%, unaware reports 1536x816 with DPI 96 and aware reports 1920x1020 with DPI 120, and **both normalise to 816**. It deliberately does NOT call `SetProcessDpiAwareness` to make measuring easier — that would silently change how WinForms renders every window for the rest of the run. `min_size` is clamped too, because WinForms grows a form back up to `MinimumSize` and would undo the fix on a small screen. Verified on hardware: the window now lands at 160,15..1760,1005 against a 0,0..1920,1020 work area. Unmeasurable screens fall back to the requested size and no position — the old behaviour. |
| 2026-09-08 | _(uncommitted)_ | **The app wears the Intellex logo.** The master set is committed at `Intellex logo design/`; the six files the app and its build scripts read are copied to `src/assets/`. `tools/make_icon.py` and the three `navicore-icon.*` files are **deleted**, and nothing replaces them — **do not add a build step that cuts these icons from one vector.** The `.ico` carries THREE DIFFERENT DRAWINGS chosen by size (full instrument at 48px+, a heavier ring with the ticks and hexagon dropped at 24–32, the R2 alone at 16), which is precisely what a rasteriser pointed at a single `.svg` cannot reproduce; the old generator's tick-dropping crop was a cruder approximation of the same idea. Edit the design set and copy across. **Every `.svg` in that set is broken and the app uses none of them:** the `<image>` element holding the R2 carries no `href` at all — not `xlink:href`, not `href`, no data URI — so ring and hexagon draw and the centre comes out empty. It renders as a deliberately hollow logo rather than a missing file, and the set's own README claims the R2 is embedded as base64 (the base64 in those files is a C2PA manifest: 7.7 KB of metadata over 0.9 KB of drawing). The PNG and `.ico` builds are complete. New route `/_assets/` serves the app's own chrome, mounted **before** `/` for the same reason `/wcb/` is — the `/` static mount matches every path and would answer 404 from the NaviCore bundle. macOS `.icns` now cuts from `intellex-dock-1024.png`: it needs a 1024 entry, was upscaling one from a 512 source, and the Dock shows that entry on every Retina Mac — so the size most people see was the softest in the file. It is the squircle tile deliberately, not the transparent mark, because macOS does not add the tile shape itself. Also fixed: the launcher masthead still read **NaviLink**, the last user-visible miss from the rename. |
| 2026-09-08 | _(uncommitted)_ | **Renamed NaviLink -> Intellex**, after the Industrial Automaton droid brain fitted to an R2-series astromech. Mechanical everywhere except two places that had to keep knowing the old name. (1) `src/paths.py` gained a one-shot `_migrate_legacy()`: the user data directory moved from `NaviLink` to `Intellex`, and a bare rename would have orphaned the branch settings, both updated tool bundles and **the offline firmware cache** — the last of which only matters on a con floor with no network, so the loss would have surfaced as "flashing broke" long after and nowhere near the cause. It moves the old directory only when the new one does not exist, and fails silently to shipped defaults. (2) `src/applog.py` still prunes `navilink-*.log`, sorted on the timestamp rather than the filename — two prefixes sorted lexically group by name before date, and that list is ordered oldest-first so the tail survives, which would have deleted new logs and kept old ones. Renamed on disk: `Intellex.bat`, `Intellex.command`, `Intellex.spec`, `src/intellex_shim.js`; the shim route moved to `/_intellex.js` and the Windows AppUserModelID to `NaviCore.Intellex`, so a pinned taskbar shortcut from before the rename will not group with the new build. `localStorage` keys became `intellex_view` and `intellex_split`, resetting the saved pane layout once, deliberately un-migrated — a split percentage is not worth permanent fallback code. **Not renamed:** the GitHub remote, the clone directory, and all git history. Revision-log rows here were swept with everything else, so rows above this one say "Intellex" for work done under the old name. |
| 2026-09-02 | _(uncommitted)_ | **The WCB Wizard now runs inside Intellex, over the relay as well as USB.** No new transport was needed: MgmtRelay's `/ws` was deliberately built to NaviCore's wire contract (`mgmt_wsserver.h` — same URI, same newline-delimited UTF-8, same console-mirror semantics; only the payload grammar differs), so `WebSocketTransport` already carried it and the Wizard's `requestPort`/`open`/`setSignals` path already fitted the shim. The real work was elsewhere: `Bridge` now fans the link to a **set** of pages (one slot meant the second tool silently stole the pipe and showed "Connected" while deaf), the Wizard is bundled to `src/webui_wcb/` as `Wizard/` + `Images/` siblings and mounted at `/wcb/` so its `../Images/` resolves untouched, and `src/wcb_flash.py` flashes a WCB natively — detecting chip family and flash size *before* downloading, and refusing a full flash when no size-matched S3 bootloader exists, because a mismatched S3 bootloader header silently corrupts NVS rather than failing. Full design in [WCB_WIZARD.md](WCB_WIZARD.md). |
| 2026-09-02 | _(uncommitted)_ | **The chooser can now find and pick a MgmtRelay as well as the droid.** Discovery probed one address (`.1`) and demanded a direct PONG, so a relay showed as "answers on port 80 but did not PONG" with nothing to click. It now probes `.1` and `.19` (the relay pins itself to `192.168.4.<DEVICE_ID>` in BOTH its AP and join modes, so one pair covers both deployments) and asks each what it is: JSON `PING`→`PONG` identifies a NaviCore, `?version` containing `?RELAY,1` identifies a relay. Asking the relay directly — rather than bouncing a ping off a droid behind it — means it is still identified when no droid is powered. `?version` also prints `?EPASS,<mesh password>`; that line is explicitly skipped and never stored or returned. |
| 2026-08-31 | _(uncommitted)_ | **Why the link kept dying:** two firmware tasks writing one socket (fixed in NaviCore `66ba60a`), plus a keepalive fuse far too short for the hardware. The droid stalls for seconds — a 220k-packet ICMP run measured a 3.8 s maximum against a 6 ms average — so `ping_timeout=3` was killing healthy links. Now 5 s interval / 10 s timeout: still ~15 s worst-case detection versus ~34 s for a dead socket to notice itself, and the idle-gated TCP probe covers the fast case. Verified 8 minutes clean with pings on, where it previously dropped every ~90 s. |
| 2026-08-31 | _(uncommitted)_ | **Recovery after a drop: `userClosed` was being set by the tool's TEARDOWN, not by the user.** The tool calls `port.close()` on both paths, so a lost link marked the session as user-disconnected and blocked the shim's retry. The cascade: `handleLinkLost()` → `disconnect()` → `close()`, then the tool's own one-shot `tryAutoReconnect()` fires immediately — before the host has re-attached to the droid — opens, sees no PONG, and disarms itself ("Auto-reconnect found no board"). Nothing retried after that. A close within 60 s of a loss is now treated as teardown; a deliberate Disconnect on a healthy link still sticks. Verified: drop → host reattaches → page reconnects itself in ~5 s and SBUS reports Receiving 24ch at 111 fps. |
| 2026-08-31 | _(uncommitted)_ | **The freeze: the shim closed the read stream gracefully.** `controller.close()` on WebSocket close made the tool's `read()` resolve `{done:true}` instead of rejecting, so its reader loop broke the inner `while`, released the lock, saw `port.readable` still non-null, re-acquired a reader and got `done:true` again — a tight loop with nothing to await and no exception, so none of its error handling or 50 ms backoff ran. Measured at 103% of a core with the JS heap thrashing 3-45 MB and layout frozen; three debugger samples all landed in that loop. A real serial port REJECTS with NetworkError on device loss, which the tool already treats as fatal and recovers from, so the shim now errors the controller instead. Fires on every ordinary droid drop, because the host closes the page socket on purpose so the tool re-handshakes after an OTA. |
| 2026-08-31 | _(uncommitted)_ | Added `/_api/identify` (serial PING → PONG) so the chooser names boards by asking instead of guessing from the USB descriptor. Records the trap found while building it: the USB-tethered mesh relay prints NaviCore `rc_hb` heartbeats — carrying a real firmware version — straight out of its own USB port, so any check looser than "a direct PONG reply to our own ping" labels the relay as a droid. Observed on COM16. |
| 2026-08-30 | _(uncommitted)_ | Logged the three NaviCore commits from the bug sweep (`a7e9b3e`, `55a097c`, `4d30c17`) and flagged the OTA one as NOT belonging to this effort — it fixes the pre-existing USB and mesh OTA paths and must survive a rip-out. Replaced the stale "nothing listens on the AP" gap, which the WS endpoint closed in `bdf476e`, with what is now actually unverified. |
| 2026-08-28 | _(uncommitted)_ | Created. Tracks the WiFi/desktop-app work on `main` in place of a feature branch: revert recipe, symbol-level inventory for when commits get tangled, what removal leaves behind (NVS keys, backup fields, boards already in the field), and the one-line kill switch. Records that the ESP-NOW-alongside-AP path is still unverified, and flags the coming `handleSerialInput()` extraction as the first non-additive change. |
