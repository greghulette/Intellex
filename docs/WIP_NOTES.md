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
| 2026-08-31 | _(uncommitted)_ | **Recovery after a drop: `userClosed` was being set by the tool's TEARDOWN, not by the user.** The tool calls `port.close()` on both paths, so a lost link marked the session as user-disconnected and blocked the shim's retry. The cascade: `handleLinkLost()` → `disconnect()` → `close()`, then the tool's own one-shot `tryAutoReconnect()` fires immediately — before the host has re-attached to the droid — opens, sees no PONG, and disarms itself ("Auto-reconnect found no board"). Nothing retried after that. A close within 60 s of a loss is now treated as teardown; a deliberate Disconnect on a healthy link still sticks. Verified: drop → host reattaches → page reconnects itself in ~5 s and SBUS reports Receiving 24ch at 111 fps. |
| 2026-08-31 | _(uncommitted)_ | **The freeze: the shim closed the read stream gracefully.** `controller.close()` on WebSocket close made the tool's `read()` resolve `{done:true}` instead of rejecting, so its reader loop broke the inner `while`, released the lock, saw `port.readable` still non-null, re-acquired a reader and got `done:true` again — a tight loop with nothing to await and no exception, so none of its error handling or 50 ms backoff ran. Measured at 103% of a core with the JS heap thrashing 3-45 MB and layout frozen; three debugger samples all landed in that loop. A real serial port REJECTS with NetworkError on device loss, which the tool already treats as fatal and recovers from, so the shim now errors the controller instead. Fires on every ordinary droid drop, because the host closes the page socket on purpose so the tool re-handshakes after an OTA. |
| 2026-08-31 | _(uncommitted)_ | Added `/_api/identify` (serial PING → PONG) so the chooser names boards by asking instead of guessing from the USB descriptor. Records the trap found while building it: the USB-tethered mesh relay prints NaviCore `rc_hb` heartbeats — carrying a real firmware version — straight out of its own USB port, so any check looser than "a direct PONG reply to our own ping" labels the relay as a droid. Observed on COM16. |
| 2026-08-30 | _(uncommitted)_ | Logged the three NaviCore commits from the bug sweep (`a7e9b3e`, `55a097c`, `4d30c17`) and flagged the OTA one as NOT belonging to this effort — it fixes the pre-existing USB and mesh OTA paths and must survive a rip-out. Replaced the stale "nothing listens on the AP" gap, which the WS endpoint closed in `bdf476e`, with what is now actually unverified. |
| 2026-08-28 | _(uncommitted)_ | Created. Tracks the WiFi/desktop-app work on `main` in place of a feature branch: revert recipe, symbol-level inventory for when commits get tangled, what removal leaves behind (NVS keys, backup fields, boards already in the field), and the one-line kill switch. Records that the ESP-NOW-alongside-AP path is still unverified, and flags the coming `handleSerialInput()` extraction as the first non-additive change. |
