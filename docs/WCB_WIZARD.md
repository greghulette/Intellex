# The WCB Wizard inside Intellex

Intellex hosts **two** browser tools: the NaviCore config tool at `/` and the WCB Wizard at
`/wcb/Wizard/`. Both are unmodified copies of what the public repos publish, both are shimmed
at serve time, and both talk to the same droid link.

This page covers what is specific to the Wizard. The shared machinery — the byte pipe, the
transports, the shim's fake `navigator.serial` — is in [`../CLAUDE.md`](../CLAUDE.md) and the
header comments of `src/host.py` and `src/intellex_shim.js`.

---

## Why it works at all: the relay speaks NaviCore's transport

The MgmtRelay bridges WiFi to ESP-NOW, and its WebSocket endpoint was **deliberately built to
the same wire contract as NaviCore's**:

> *"NaviCore exposes `ws://<softAPIP>/ws` carrying newline-delimited UTF-8, reassembled per
> socket, with the reply stream being a CONSOLE MIRROR rather than strict request/response …
> This endpoint is the same URI, the same framing and the same mirror semantics, so a host app
> (NavLink) writes ONE client and points it at either box. The payloads differ — NaviCore
> speaks JSON verbs, this speaks the WCB serial grammar (`;w<id>,<cmd>`, bare text =
> broadcast) — but the transport does not."*
>
> — `WCBClient/examples/MgmtRelay/mgmt_wsserver.h`, header comment

So Intellex needed **no new transport** for the Wizard. `WebSocketTransport` already carries
it, and `src/discover.py` already finds a relay at `192.168.4.19` and attaches it with
`role: "relay"`. A WCB hosting its own AP is a doorway too, and takes more than an address to
recognise — see [Three doorways, one address](#three-doorways-one-address-identify-by-answer-never-by-address).

The Wizard, in turn, needed no new transport either: `BoardConnection.connect()`
(`Wizard/app.js`) uses `requestPort()` → `open({baudRate})` → `setSignals()` → read loop, and
the shim already provides all four. **The integration is almost entirely plumbing and UI**, not
protocol work.

## The two tools disagree about line endings

**The NaviCore tool ends every line with `\n`. The WCB Wizard ends every command with `\r` and
never sends `\n`** — `BoardConnection.send` is always called as `send(cmd + '\r')`, and
`sendAndCollect` does `this.send(command + '\r')`.

The shim's `writable` frames outgoing bytes one line per WebSocket message, because the
protocol is newline-delimited and a WebSocket is message-framed. It originally split on `\n`
alone, which was correct while the NaviCore tool was its only caller. With the Wizard, every
command contained no `\n`, the split loop never fired, and the command **sat in the buffer
forever without ever being sent**.

This failed in the worst available way — silently, and looking like a board fault:

- the page still received the relay's console mirror, so it appeared connected and healthy;
- every command it sent vanished with no error anywhere;
- the first config pull timed out as *"config pull incomplete — nothing was changed"*;
- so `?RELAY,1` was never seen, no relay card was created, and no mesh boards were offered —
  which reads as "the Wizard cannot see my WCBs" rather than "nothing is being transmitted".

The shim now splits on `\r`, `\n`, and treats `\r\n` as one terminator. Safe in both
directions: the relay's reader accepts either (`mgmt_wsserver.h` `feed()`:
`if (c == '\n' || c == '\r')`), and so does the WCB firmware's USB reader.

**Diagnosing this class of fault:** compare a raw `/_link` WebSocket against the page. A raw
socket that gets a reply while the page gets nothing means the page's write never left the
browser — look at the shim's framing, not at the droid.

## Cross-tab port sharing must be OFF

`establishConnection()` (`Wizard/app.js`) routes the **first** connect through `WcbSerialHub`
before anything else, then waits up to 3 s for `hub.portOpen` before proceeding.

That hub exists to work around one browser rule: *a Web Serial port can be open in exactly one
browsing context*, so the Wizard and the NaviCore tool cannot each hold their own connection to
the same USB board. It elects a leader over Web Locks and proxies the other tabs' bytes over a
BroadcastChannel.

**That rule does not apply inside Intellex, and the workaround breaks things.** The "port" is a
WebSocket to the host, every page opens its own, and the host fans the droid's bytes to all of
them. Both tools already have independent, first-class access to the same link. Left on, the hub:

- delays every connect by up to 3 s waiting for an election that cannot help;
- makes whichever tool loaded first the leader, so the other stops using its own socket and
  relays through the leader's BroadcastChannel instead;
- **disables flashing** — it explicitly needs the raw port, which a follower does not have.

The shim therefore wraps `window.establishConnection` to force `allowShare = false` — the
tool's **own** documented opt-out, which its bulk auto-detect already passes for a related
reason. Do **not** implement this by faking `WcbSerialHub.supported`: `getSharedHub()` *throws*
when unsupported and `establishConnection` has no `catch` around it, so that turns a connect
into "Port sharing needs a Chromium browser".

## A relay is not a board

Attaching the Wizard to a MgmtRelay and stopping there looks like a broken WCB — "config pull
incomplete", no boards, nothing to manage. Nothing is broken; a setup step is missing.

The Wizard's own model already handles this and needs no changes:

1. A device whose backup carries `?RELAY,1` is short-circuited out of the numbered grid into a
   dedicated **relay card**, filed under its `?WCB,<id>` rather than the slot it landed on.
2. A `?WDP,DUMP` sweep fills `_relayNodes` with the mesh boards it hears.
3. `relayRouteAll(slot)` binds each one for remote management and pulls its config
   **sequentially** — sequentially because the relay reassembles one config reply at a time and
   overlapping pulls cross-assign configs to the wrong board.

Step 3 is the "Manage all" button. The only thing missing was anyone pressing it, so the shim
does — but it **polls rather than racing the sweep once**. It watches the relay card for boards
that are heard but unmanaged (`renderRelayCard` gives those a "Manage via relay" button and
bound ones a plain "managed" label, so counting those buttons asks the page what it actually
shows) and calls `relayRouteAll` whenever it finds any, for five minutes.

A single timed attempt is not enough, and was measured failing: the WDP sweep runs on the
Wizard's own timer, so a one-shot 45 s window expired before the first sweep landed and then
never looked again — every board left unmanaged with no way back except noticing the button.
Polling also picks up a board powered on later, or one that missed a sweep. Re-calling is safe:
`relayRouteAll` guards itself with `_relayRouteAllBusy` and skips boards that already have a
baseline.

The relay's id reaches the page through `discover` → the launcher's attach body → the spec →
`/_api/status` (`role`, `relayId`). The Wizard cannot derive it: a relay is filed under its
`DEVICE_ID`, not its slot.

**The routing is NOT gated on that role, though**, and must not be. It once was, and that was
wrong the moment NaviCore itself became a doorway to the mesh: the host reports that link as
role `navicore`, so the gate skipped routing and left every WCB unmanaged behind a board that
could relay to them perfectly well. What decides it is whether a **relay card appears** — the
page's own verdict on the backup it got back, which only a device advertising `?RELAY,1`
produces. `routeMeshThroughRelay()` therefore runs after every connect and does nothing when no
card shows, which costs a timer against a directly-cabled WCB and gets the answer right for
every kind of doorway, including ones added later. `relayId` stays a hint; the DOM is the
fallback and the authority.

### Verified on hardware, 2026-09-02

Probed through the live `/_link` pipe against a relay at `192.168.4.19`, with a laptop on its
network. Every layer answered:

| Sent | Got back |
|---|---|
| `WCB_WEBTOOL_CONFIG_PULL` | the full backup — `?WCB,19`, `?RELAY,1`, `?ALIAS,Mgmt Relay`, `--------- End of Backup ---------` |
| `?version` | `Software Version: 1.2` / `End of Version` |
| `?WDP,DUMP` | `N=19` self + `N=1 Body` (HW 1) + `N=2 Dome` (HW 24), both FW `6.2.1_021242RSEP2026`, `[WDP:END,count=2]` |
| `?MGMT,PULL,1` | `[MGMT:CONFIG,1][VER:6.2.1_…]?HW,1^…^?ALIAS,Body^…` |
| `?MGMT,PULL,2` | `[MGMT:CONFIG,2][VER:6.2.1_…]?HW,24^…^?ALIAS,Dome^…` |

So the transport, the relay's Wizard command surface and the whole remote-management path are
sound. **A failed pull in the Wizard is a page-side setup problem, not a wire problem** — check
the two sections above before suspecting the link.

Note the remote config encoding: `[MGMT:CONFIG,<n>]` with `^` separators, **not** a newline
backup with an `End of Backup` marker. The remote pull uses the `[MGMT:CONFIG,` tag as its
sentinel; only the *direct* pull looks for `End of Backup`.

## Auto-pull behind a doorway that is an ordinary WCB

The section above gets the *mesh boards surfaced*. Pulling their configs is a separate
mechanism, and it only ever covered half the cases.

**A relay card is created only for a device whose backup carries `?RELAY,1`**
(`parser.js:499`) — and a real WCB's backup has no such line. That absence is exactly what
identifies it as a board rather than a relay. So through a WCB doorway there is no relay card,
`routeMeshThroughRelay()` finds nothing, and the other boards sit surfaced-but-unmanaged: the
WDP sweep lists them and gives each a section, but nothing pulls their config. The sweep says
so in as many words — *"Detection just SURFACES the board … We do NOT auto-connect or
auto-pull"* (`app.js:13139`).

**Routing through a plain WCB is not a workaround — it is the firmware's original design.**
`WCB.ino` implements the entire relay half of the management protocol (*"Relay side: handle
`?MGMT,PULL,<targetWCB>`"*, `WCB.ino:3969`), which is how the Wizard has always managed a mesh
through one USB-cabled board. MgmtRelay is a *second* implementation of that surface, not the
only one. The page side is equally general: `remoteBoardPull(parent, target)` needs only
`boardConnections[parent]` live, and `setRemoteConnected(target, parent)` has no relay check in
it at all. **Only the entry point was relay-shaped.**

### Why not just call `relayRouteAll()` with the WCB's slot

Two reasons, both fatal:

- It reads its targets from `_relayNodes[slot]`, which the sweep fills only
  `for (const rs of _relaySlots)` (`app.js:13124`) — always empty for an ordinary board.
- It routes through `relayManageOne()`, which calls `renderRelayCard()` **unconditionally**. That
  would draw a relay card for a device that is a real board and already in the numbered grid —
  the duplicate-card bug the Wizard fixed once already.

So `routeMeshThroughBoard()` drives the two generic functions directly, keeping
`relayRouteAll`'s disciplines, which are **firmware constraints, not relay ones**:

| Discipline | Why |
|---|---|
| **Sequentially**, awaiting each pull | The parent reassembles one config reply at a time and the `[MGMT:CONFIG,]` listener is not target-filtered, so overlapping pulls cross-assign configs to the **wrong board**. |
| **Once per board** | The Wizard deliberately stopped auto-pulling every newly heard peer because it *"fought live traffic and surprised the user"*. Skipping boards that already have a baseline keeps this a one-time catch-up, not a re-pull on every sweep. |
| **Boards only, never clients** | See below — this one shipped broken. |

### `_meshBoards` is not the set of boards

It means **"has a numbered section"**. `upsertClientCard()` calls `addDiscoveredBoards()` for
clients too (`app.js:13045`), so a MgmtRelay at 19 and a NaviCore at 20 sit in it looking
exactly like boards. Trusting it pulled both on hardware. A `CLIENT=1` device is a `WCB_Client`
host with **no WCB config to pull at all** — the Wizard gives it a lightweight client card
(`cfg.type = 'client'`), never a board config — so the pull can only fail.

The authoritative flag is on the sweep's own parsed nodes (`client: m[2] === '1'`,
`app.js:12803`, straight from `CLIENT=` in the WDP row). The shim wraps the `renderWdpMesh()`
that the sweep already calls every tick and keeps the list. **Not** by issuing its own
`?WDP,DUMP`: that would fight the tool's sweep for the same connection, which is why the tool
guards its own with `_meshDiscoverBusy`.

The fallback, for the window before any sweep is captured, subtracts clients two ways —
`_meshClients` and `boardConfigs[n].type` — because they populate at different moments:
`upsertClientCard` returns early *before* `_meshClients.set()` on the first sweep, when the
section exists but `boardConfigs[n]` does not yet.

Both loops start after every connect and are mutually exclusive at runtime: this one stands
down the moment a relay card exists, the other does nothing until one does. Starting both
rather than choosing means neither has to predict which kind of box the pull is about to
reveal — the same reason `routeMeshThroughRelay()` is ungated.

**One thing this has that `relayRouteAll` does not: a watchdog.** `remoteBoardPull` signals
through an `onComplete` **callback**, not by resolving. `relayRouteAll` can rely on that
because it is user-triggered and reports through toasts; this runs unattended, so a pull that
never calls back would park the loop forever on board one and silently strand every board
behind it — indistinguishable from *"it only pulled the first one"*. 45 s clears the tool's own
worst case (3 × `PULL_TIMEOUT_MS` 6 s + 2 × `PULL_RETRY_MS` 2.5 s = 23 s) with room to spare.

`tools/smoke_mesh_route.js` covers all of this with no hardware, no browser and no droid — it
*extracts the function's real source text* from the shim rather than restating it, so the test
cannot drift from what ships. Its last case is that hang, which is not hypothetical: it is what
the test hit on its own first run.

## Three doorways, one address: identify by answer, never by address

The WCB firmware can now host its own AP, so **three** kinds of box can be the thing you
attach to — NaviCore, a MgmtRelay, or a WCB — and discovery has to say which.

**The address cannot tell you.** NaviCore never calls `softAPConfig()`, and the WCB firmware
deliberately does not either: pinning itself to `192.168.4.<board>` was tried and *breaks
DHCP* — clients associate, no lease ever arrives, they land on `169.254.x` and cannot reach
the board at all (`WCB_WiFi.cpp:122`, measured on hardware, not a startup race). So every
AP-hosting box is at `192.168.4.1`. Only MgmtRelay pins itself, to `.19`.

**`?WDP,DUMP` cannot tell you either.** Its `PEER=3` SELF row says where a device sits on the
mesh, and a WCB and a relay emit it identically — the relay's row was byte-matched to the
firmware's on purpose (`WCB_WDP.cpp:1127`). Both are doorways; the sweep proves only that.

Two discriminators look right and are not:

| Tried | Why it fails |
|---|---|
| The SELF row's `HW` field | MgmtRelay reports `HW=32` to mean *"not a real board"* — but `32` is **also a genuine hardware version, WCB 3.2** (`Wizard/parser.js:217` `HW_VERSION_MAP`). A v3.2 board is exactly the case being identified, so this misreads every one. |
| `?RELAY,1` in `?backup` | It is a true marker, but `?backup` dumps the whole config **including `?EPASS`, the mesh password in clear**. A discovery probe has no business pulling that on every scan. |

**`?RELAY,WIFI` is the discriminator.** It is a MgmtRelay command with no handler in the WCB
firmware, so the *presence of a reply* is the whole test, and its report names the mode and
**deliberately never a password** (`MgmtRelay.ino:1094`).

It is sent **before** `?WDP,DUMP` and read in the **same loop**, so it costs no extra round
trip and needs no timeout of its own: the far end handles lines in order, the `[relay]` reply
lands ahead of the dump, and `[WDP:END` still ends the read. A `?` command is handled locally
and never re-broadcast to the mesh (`WCB_Help.cpp:858`), so this asks nothing of other boards.

`probe()` returns `kind` of `navicore | relay | wcb | unknown`; `scan()` adds `isWcb` and
`isMesh` (either doorway). The launcher renders one row shape for both doorways — the decision
is the same, only the noun differs — and the noun has to be right, or a WCB labelled *"Mgmt
relay"* sends someone hunting for a board they never flashed.

One thing that **does** differ, and matters: the SSID to re-associate after a drop. All three
firmwares derive `<prefix>-<id>` from a *different* prefix — `NaviCore-<id>`,
`MgmtRelay-<id>`, `WCB-<alias|number>` (`WCB_WiFi.cpp` `wcbWifiDefaultSsid`) — and the bounce
matches on that prefix. Pass the wrong one and it hunts for a network that does not exist,
then reports the adapter as unassociated.

## NaviCore can be the WCB relay too

**It already is one, for OTA.** `?OTA,*` routes to `processOtaRelayCommand`, and
`navicore_ota.h` states the ESP-NOW wire format is identical to the WCB's *"so a WCB relay can
update a NaviCore and vice-versa"*. Relaying to WCBs is not a new role for that firmware.

**Over WiFi the relayed ACK has to be printed from `loop()`.** `rcSerial` mirrors only the
loop core to the WebSocket, so an `[OTA:ACK,…]` printed from the ESP-NOW callback reaches
USB alone, and the Wizard fails with *"no response from WCB<n> via relay — is it online &
on this firmware?"* while the target is answering. NaviCore queues it and prints it from
`drainOtaPackets()` (`navicore_ota.h` `handleOtaAckRelay`, NaviCore `079d195`); firmware older
than that cannot relay OTA to a WCB over WiFi.

What it already has, checked in the source rather than assumed:

| Needed | NaviCore today |
|---|---|
| `WCB_Client` mesh send/receive | linked; `sendRawPacket`, `getNeighbor`, `isLearnedPeer`, `send`, `broadcast` |
| One `?`-dispatcher for USB **and** WebSocket | `processInputLine()` (`NaviCore.ino:3752`), fed by the ws server as "the SAME dispatcher the USB path uses" |
| `?`-commands already parsed | `?OTALOCAL,` / `?OTA,` (`NaviCore.ino:3353-3354`) |
| A raw-packet hook | registered for OTA, and it **demuxes by length and ignores unknown sizes** (`navicore_ota.h:548`) — an extension point, not a conflict |

Missing is only the Wizard's *config* surface — `?backup`, `?version`, `?WDP,DUMP`, `?MGMT,*`
and the `[MGMT:CONFIG]`/`[TERM]` reassembly. About 250 lines, all of it generic `WCB_Client`
usage with nothing that depends on being a bare board.

**It lives in `WCB_Client/src/WCB_Mgmt.h`, not copied into NaviCore.** This surface is a wire
protocol with three parties — the Wizard, the relaying device, and the WCB firmware — with
structs byte-matched to `WCB.ino` and replies byte-matched to the Wizard's parser. Two copies
drift the first time one side is fixed, and drift *silently*, because a stale copy still
answers. One module compiled by both firmwares is the answer `WcbCmd` already gives to this
exact problem.

The module **registers nothing** with `WCB_Client`. The host calls `begin()`, then feeds it
from its own dispatcher and its own raw-packet hook. That is not politeness: `onRawPacket()`
takes a single callback and NaviCore already owns it for OTA, so self-registering would have
silently replaced the OTA hook — with nothing visible until an update failed.

### Why the load objection does not apply

This is setup and configuration work — workshop or behind the table, not while animating.
Nobody pushes a config and flies a droid at the same time, so config traffic does not contend
with flight telemetry in practice. What does survive is *correctness*: the OTA demux must stay
right, because OTA is a workshop activity too, in the same session.

### Consequences

- One connection at `192.168.4.1` serves both tools at direct-HTTP speed, so the two-link host
  work is not needed.
- MgmtRelay becomes the **fallback**, not the primary: USB-only bench work, and reaching WCBs
  when NaviCore is down or mid-flash — exactly when it is most wanted.
- **Still to verify on hardware:** NaviCore advertising `?RELAY,1` files it as a relay card at
  slot 20 while the WDP sweep also lists it as `Client 20`. `relayRouteAll` skips clients and
  skips its own id, and `WCB_MAX = 20`, so it should be fine — but that interaction is untested.

## Bundling: `/wcb/` mirrors the published layout

`tools/fetch_webui.py --tool wcb` fetches from
`https://greghulette.github.io/Wireless_Communication_Board-WCB/Wizard/` into
`src/webui_wcb/`, laid out as:

```
src/webui_wcb/
  Wizard/          index.html app.js parser.js flasher.js device-labels.js
                   serial-hub.js styles.css favicon.png manifest.json
                   vendor/crypto-js/…  vendor/esptool-js/…
  Images/          r2logo.png navicore-icon.png kyberLogo.png qr-code.png
```

`host.py` mounts that directory at `/wcb/`, so the Wizard is served at `/wcb/Wizard/`.

**That nesting is load-bearing.** `index.html` reaches its logos with `../Images/<name>`, which
works on Pages because `/Wizard/` and `/Images/` are siblings. Reproducing the pair means the
published file is served *byte-identically*; flattening it would mean rewriting paths inside
`index.html`, which is a fork. It also keeps the two tools' images apart — both ship a
`qr-code.png` and an `r2logo.png`.

Firmware binaries are **not** bundled: `flasher.js` pulls them from the GitHub Contents API at
flash time, and so does `src/wcb_flash.py`. The bundle is ~1.1 MB, 16 files.

Version handle: `UI_VERSION` in `app.js` (`app.js:115`), stamped by the WCB repo's pre-commit
hook. It plays the role `footer-dtg` plays for the NaviCore tool. `/_api/webui-version` reports
both, under the top-level keys (NaviCore) and a `wcb` sub-object.

## One link, several pages

`Bridge` holds a **set** of page sockets and fans every chunk to all of them
(`host.py`, `Bridge._pages`). Before this it held exactly one, and a second page silently stole
the pipe: the robbed tool showed "Connected" and received nothing, with no error anywhere.

Fanning out matches what the far end already does — MgmtRelay serves 3 WebSocket clients and
mirrors its console to all of them (`mgmt_wsserver.h`, `WS_MAX_CLIENTS`), and NaviCore's `/ws`
is a console mirror too. Both tools reading one droid link is the normal shape of this
protocol.

Two rules the fan-out must keep:

- **Each chunk goes to every page, whole.** Each page keeps its own streaming `TextDecoder`, so
  a chunk delivered to one page and not another desynchronises a multi-byte character for
  whoever missed it.
- **A send failure is suppressed per page.** One page dying mid-send must not cost the others
  the rest of the chunk.

`tools/smoke_fanout.py` tests all of this against a fake transport — no droid required.

## Flashing a WCB

`src/wcb_flash.py` is a port of `Wizard/flasher.js`'s decisions, driving `esptool` as a Python
package. The browser cannot do this through the shim: esptool-js needs real DTR/RTS, and a
WebSocket has no control lines.

| | ESP32 | ESP32-S3 |
|---|---|---|
| 2026-09-07 | _(uncommitted)_ | **Auto-pull tried to pull CLIENTS: a MgmtRelay at 19 and a NaviCore at 20.** Reported from hardware. `_meshBoards` means "has a numbered section", not "is a board" -- `upsertClientCard()` calls `addDiscoveredBoards()` for clients too (`app.js:13045`), so both sat in it looking like boards. A `CLIENT=1` device is a WCB_Client host with no WCB config to pull, so the pull can only fail. Targets now come from the sweep's own parsed nodes, which carry the authoritative flag (`app.js:12803`), captured by wrapping the `renderWdpMesh()` the sweep already calls -- not by issuing a competing `?WDP,DUMP`. The pre-sweep fallback subtracts clients via BOTH `_meshClients` and `boardConfigs[n].type`, which populate at different moments. `smoke_mesh_route.js` now models the real mesh (2 boards + 2 clients) and reproduces the bug exactly without the fix. |
| 2026-09-07 | _(uncommitted)_ | **Mesh boards behind a WCB doorway were never auto-pulled.** A relay card exists only for a device advertising `?RELAY,1`, which a real WCB by definition lacks, so `routeMeshThroughRelay()` had nothing to act on and the boards stayed surfaced-but-unmanaged. Routing through a plain WCB is the firmware's original design (`WCB.ino:3969` implements the relay side), and `remoteBoardPull`/`setRemoteConnected` were already generic -- only the entry point was relay-shaped. New `routeMeshThroughBoard()` drives them directly, sequentially and once per board (both firmware constraints, not relay ones). Not via `relayRouteAll`: its targets come from `_relayNodes`, filled only for relay slots, and it renders a relay card unconditionally -- the duplicate-card bug again. Adds a 45 s watchdog `relayRouteAll` does not need: `remoteBoardPull` reports through a CALLBACK, so one that never fires would park an unattended loop forever and strand every board behind it. Covered by `tools/smoke_mesh_route.js`. |
| 2026-09-07 | _(uncommitted)_ | **A WCB hosting its own AP was reported as a MgmtRelay.** Both answer `?WDP,DUMP` with the same `PEER=3` SELF row -- the relay was byte-matched to the firmware on purpose -- and both sit at `192.168.4.1`, since pinning a WCB to `192.168.4.<board>` breaks DHCP (`WCB_WiFi.cpp:122`). `probe()` now sends `?RELAY,WIFI` ahead of the dump and reads both replies in one loop: a relay-only command, no password in its report, no extra round trip. `HW=32` was rejected as a discriminator -- MgmtRelay uses it for "not a real board" but it is also genuine WCB 3.2. New `kind="wcb"`, `isWcb`/`isMesh` on `scan()`, `role="wcb"` through the spec, and the re-associate SSID now uses the `WCB-` prefix rather than `MgmtRelay`. |
| bootloader | `0x1000` | `0x0` |
| partition table | `0x8000` | `0x8000` |
| app | `0x10000` | `0x10000` |
| nvs | `0x9000`, `0x5000` — erased only on Factory Reset | same |
| otadata | `0xE000`, `0x2000` — **always** reset | same |

otadata is always reset because the app is always written to `ota_0`; leaving a boot selector
that a previous OTA flipped to `ota_1` makes the bootloader try to boot a slot that was just
overwritten, and the rollback watchdog reboots the board forever.

**The S3 bootloader must match the flash size.** Its header declares that size, and writing the
16 MB build onto an 8 MB board (or the reverse) does not fail — it boots and then silently
corrupts NVS. `wcb_flash.detect()` asks esptool for the chip family and flash size *before
anything is downloaded*, and if no matching bootloader exists the module **refuses a full
flash** rather than writing a partial set.

Two places `wcb_flash.py` is deliberately **stricter than `flasher.js`**:

1. **Anchored filename matching**, not `endsWith()`. More than one matching app image is an
   error, not a first-match win — the build tags carry a DTG that does not sort
   chronologically, so "first" and "newest" are unrelated.
2. **Every image comes from one build.** The partition table and bootloader are looked up by
   the app's own build tag rather than searched for independently. A table from a different
   build flashes silently and only shows up later.

### How the page reaches it

The shim **replaces `window.flashFirmware`** rather than intercepting buttons. Flashing in the
Wizard is one branch of `boardGo()` for any of N board slots, wrapped in per-card bookkeeping —
save the port, close the connection, drive a progress bar, then reconnect, re-share the port
and re-push config. All of that is correct and none of it is Intellex's to reimplement. Every
path funnels into one call:

```js
flashFirmware(port, hwVersion, { onProgress, onLog, onStatus, appOnly, eraseNvs })
```

Swapping that leaves the surrounding behaviour running untouched. `port` is ignored (it is the
fake one; the host holds the real device) and so is `hwVersion` — `flasher.js` calls it a
"pre-fetch guess" and says chip auto-detection is authoritative, and the host detects for real.

The replacement **must throw on failure**: `boardGo()` uses the exception as its only failure
signal, and returning quietly would have it report success and then push config at a board that
never got new firmware.

`/_api/flash` and `/_api/flash-wcb` share one `_flash_state` and one claim, so a flash started
in one pane makes the other pane's button a clean 409 rather than two esptools racing for one
port.

## Window layouts

The launcher offers four, remembered in `localStorage.intellex_view`:

| | |
|---|---|
| `nc` | NaviCore tool, full window |
| `wcb` | WCB Wizard, full window |
| `split` | both in `/_shell`, side by side, draggable divider |
| `sep` | config tool here, Wizard in its own app window |

`src/shell.html` hosts the tools in same-origin iframes. Frames are created on first show and
**never destroyed** — removing an iframe kills its page, and losing an unsaved config by
clicking the wrong tab is not recoverable. Hiding is `display:none` and nothing more.

**Nothing that merely *looks* at the connection may unload a tool frame.** Navigating to
`/_launcher` discards the tool, its link and every board's pulled config — and through a relay
those configs are re-pulled one board at a time over the mesh. So the chooser opens as an
**overlay** over the live frames, and both the launcher's "back" and each tool's own "change
connection" affordance ask for it by `postMessage` instead of navigating.

**Panes auto-fit by scaling their viewport.** Both tools lay out for a full window — the
Wizard's header alone carries Simple/Advanced, Wizard, Push All, Load File and Export — so at
half width the header, the board tabs and the terminal selector run off the edge. Neither tool
is ours to restyle, so the shell gives each pane's document `DESIGN_W` (1180 px) to lay out in
and scales it down with CSS `zoom`, which in Chromium is a *layout* zoom: at zoom `z` the inner
document sees a viewport of `width / z`.

Two things that do not work here, both measured:

- **A CSS `transform`** scales the painted result but leaves the document laying out in the
  same narrow viewport, so the header clips exactly as before, only smaller.
- **A percentage width** (`width: 100/z %`) resolves against the parent and is *then* divided
  by the zoom, so it compounds — at `z = 0.5` in a 621 px pane it handed the document a
  **2482 px** viewport and rendered everything at quarter size. Size the frame in explicit
  pixels (`w / z`) instead.

`sep` asks `/_api/open-window`, which app.py backs with `webview.create_window`. `host.py` does
not import webview (it runs standalone), so the hook is registered by `app.py` and the route
answers 501 when there is none; the caller then falls back to `window.open()`. The route
accepts **local paths only** — an absolute URL would let a page render an arbitrary site inside
Intellex's own window, wearing its title.

## Things that would break it

- **Serving the Wizard anywhere but `/wcb/Wizard/`.** `../Images/` stops resolving and the
  logos 404, or worse, collide with the NaviCore tool's.
- **Registering the `/` static mount before `/wcb/`.** aiohttp resolves in registration order
  and `/` matches everything, so the Wizard's assets would 404 out of the wrong bundle.
- **Letting any spelling of the Wizard's URL reach the static mount.** `/wcb/Wizard/` and
  `/wcb/Wizard/index.html` are explicit routes through `wcb_index()`; served by the static
  mount instead they would be the *raw* `index.html` with no shim, and the Wizard would open
  against real Web Serial asking for a COM port the host is already holding. The third
  spelling, `/wcb/Wizard` with no slash, reaches the mount as a directory request and answers
  **403 Forbidden** (`show_index=False`) — measured — so it is redirected to the slashed form
  rather than left as a dead end.
- **Reverting the bridge to one page socket.** Silent, and it looks like a tool bug.

---

## Revision log

| Date | Commit | Change |
|---|---|---|
| 2026-09-10 | _(uncommitted)_ | **Wireless OTA to a WCB through NaviCore failed over WiFi, and the cause was in NaviCore.** *"Wireless OTA failed: no response from WCB2 via relay"*, attached to NaviCore's own AP. The Wizard's `?OTA,BEGIN`, NaviCore's relay parser and WCB2's target handlers all agreed on the wire, and WCB2 registers device 20 as a peer before it ACKs. NaviCore printed each relayed `[OTA:ACK]` from the ESP-NOW callback, which `rcSerial` mirrors to USB only, so the Wizard never saw one. Fixed in NaviCore `079d195`; the section above says why the print must come from `loop()`. Nothing in Intellex changed. The `NaviCore.ino` line numbers in its table were refreshed, and the 2026-09-08 rename row below is repaired: it held real CR and LF characters where the text `` / `
` / `
` was meant, which split the table. |
| 2026-09-08 | _(uncommitted)_ | Renamed NaviLink -> Intellex. Affects this page in two places worth knowing before grepping for either: the shim is `src/intellex_shim.js` and it is served at `/_intellex.js`. Behaviour is unchanged — the `\r` / `\n` / `\r\n` line splitting and the `flashFirmware()` interception are untouched. Rows above this one were swept too and name the app Intellex for work done while it was still NaviLink. |
| 2026-09-02 | _(uncommitted)_ | **Reversed the earlier "NaviCore cannot be the WCB relay" call — it already is one, for OTA.** `?OTA,*` relays to WCBs today, NaviCore already dispatches `?`-commands through one handler shared by USB and its WebSocket, and its `onRawPacket` hook already demuxes by length and ignores unknown sizes (so it extends cleanly rather than conflicting — the blocker first flagged here was wrong). The Wizard surface is therefore extracted to `WCB_Client/src/WCB_Mgmt.h` rather than copied: it is a wire protocol byte-matched to `WCB.ino` and to the Wizard's parser, so two copies would drift silently. The module registers nothing with `WCB_Client` — the host feeds it from its own dispatcher and hook, because `onRawPacket` takes one callback and NaviCore already owns it for OTA. Compiles standalone against a NaviCore-shaped host (ESP32-S3, 67% flash). |
| 2026-09-02 | _(uncommitted)_ | **The Wizard's commands were never being transmitted.** The shim frames outgoing bytes one line per WebSocket message and split on `
` alone — correct while the NaviCore tool was its only caller, but the Wizard ends every command with `
` and never sends `
`, so each command sat in the buffer forever. Silent and indistinguishable from a board fault: the page still received the console mirror, so it looked healthy while nothing it sent arrived, and the first pull failed as "config pull incomplete" — which is why no relay was identified and no mesh boards appeared. Now splits on `
`, `
` and `
`. Found by comparing a raw `/_link` socket (backup arrived) against the page (nothing did). Also: the relay auto-route now polls instead of racing the WDP sweep once and giving up; the chooser is an overlay so looking at the connection no longer re-pulls every config; and split panes scale their viewport to fit (explicit px, not percentages — those compound with `zoom`). Verified in headless Chrome against the live relay: relay card at 19, `remoteRelayForBoard {1:19, 2:19}`, baselines pulled for Body and Dome, no pane overflow. |
| 2026-09-02 | _(uncommitted)_ | **Wizard connected but could not pull: two page-side setup gaps.** (1) `establishConnection()` auto-shares the port through `WcbSerialHub` on the first connect and waits up to 3 s for an election — a workaround for a one-context-per-Web-Serial-port rule that does not apply here, and which also disables flashing. The shim now forces the tool's own `allowShare=false` opt-out. (2) Nothing triggered the Wizard's relay flow, so a MgmtRelay looked like a broken WCB; the shim now waits for the relay card **and** its WDP row, then calls `relayRouteAll()`. `relayId` and `role` are plumbed discover → launcher → spec → `/_api/status` because the Wizard files a relay under its DEVICE_ID, not its slot. Verified on hardware against the relay at `.19`: backup, `?version`, `?WDP,DUMP` (Body + Dome) and `?MGMT,PULL,1|2` all answer correctly through the pipe — the wire was never the problem. |
| 2026-09-02 | _(uncommitted)_ | Created, with the WCB Wizard integration. `Bridge` fans the droid link to a set of pages instead of one (the single slot let a second tool silently steal the pipe). Wizard bundled to `src/webui_wcb/` mirroring the published `Wizard/` + `Images/` sibling layout and mounted at `/wcb/`; `tools/fetch_webui.py` gained `--tool navicore\|wcb\|all` as independent atomic swaps. `src/wcb_flash.py` flashes a WCB natively via esptool, detecting chip family and flash size first and refusing a full flash when no size-matched S3 bootloader exists. The shim replaces `window.flashFirmware` rather than intercepting buttons, and points the Wizard's `rc_config_tool_url` at `/`. New `src/shell.html` offers tabs/side-by-side, and `/_api/open-window` a second app window. `tools/smoke_fanout.py` covers the fan-out without hardware. |
