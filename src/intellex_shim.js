// =============================================================================
//  intellex_shim.js — present the host's byte pipe as a Web Serial port
// =============================================================================
//
//  Injected by host.py BEFORE the config tool's own scripts run. It defines
//  navigator.serial, so the tool's existing serial code works untouched and the
//  bundled index.html stays BYTE-IDENTICAL to the copy published on Pages.
//
//  That is the whole reason for doing it this way. The alternative -- adding a
//  transport branch inside index.html -- means every future change to the tool's
//  connect path has to keep an app-specific branch working. A fork in all but
//  name, and lock-step with the public tool is a hard requirement here.
//
//  Underneath, "the port" is one same-origin WebSocket to /_link. Whether the
//  host has a COM port or the droid's SoftAP on the far end is invisible from
//  here, which is the same property the host itself is built around.
//
//  ── WHAT THIS DELIBERATELY DOES NOT DO ────────────────────────────────────
//  Flash through the port it fakes. esptool-js drives a real one hard: it polls
//  readable.locked / writable.locked in waitForUnlock(), cancels readers
//  mid-stream, and needs setSignals() to toggle DTR/RTS in a timing-sensitive
//  reset dance. Faking that well enough over a WebSocket is a bad trade when the
//  native host can run esptool directly. setSignals() below is therefore a real
//  call to the host, not a lie.
//
//  So the flash buttons are NOT dead here: they are intercepted (capture phase)
//  and routed to the host's /_api/flash, which owns the real device and runs
//  esptool as a Python package. See "Flashing: hand it to the host" below.
// =============================================================================
(() => {
  'use strict';

  if (window.__intellexShimInstalled) return;
  window.__intellexShimInstalled = true;

  const LINK_URL = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/_link';

  // ── Link state, so the page can get itself BACK ───────────────────────────
  // The host deliberately closes the page socket whenever the droid link drops
  // (that is what makes the tool re-handshake and pick up a new firmware version
  // after an OTA). The tool then has no port, and autoConnect() only ever ran on
  // `load` -- so the page sat dead until the whole app was closed and reopened.
  // Every droid reboot, every config save that restarts, every transient did it.
  //
  // linkOpen tracks OUR socket. userClosed separates "the tool asked to
  // disconnect", which must be respected, from "it dropped underneath us", which
  // is the case worth recovering from.
  let linkOpen = false;
  let userClosed = false;
  let reconnectAt = 0;      // earliest next attempt (ms epoch), for backoff
  // When the link was last lost underneath us. close() consults this to tell a
  // TEARDOWN from a user pressing Disconnect -- see the note there.
  let lastLossAt = 0;
  const LOSS_GRACE_MS = 60000;

  class IntellexPort {
    constructor() {
      this._ws = null;
      this.readable = null;
      this.writable = null;
      this._info = { usbVendorId: 0x303a, usbProductId: 0x1001 };  // Espressif; cosmetic only
    }

    getInfo() { return this._info; }

    async open(_options) {
      // _options (baudRate etc.) is intentionally ignored: the HOST owns the real
      // port parameters. Baud is meaningless for a WebSocket, and on a NaviCore it
      // is meaningless anyway -- native USB-CDC ignores it.
      if (this._ws) return;

      const ws = new WebSocket(LINK_URL);
      ws.binaryType = 'arraybuffer';
      await new Promise((resolve, reject) => {
        ws.addEventListener('open', resolve, { once: true });
        ws.addEventListener('error', () => reject(new Error('cannot reach the Intellex host')), { once: true });
      });
      this._ws = ws;
      linkOpen = true;
      userClosed = false;
      // (the stream's own close handler below clears linkOpen and reports the
      //  loss to the tool; watchLink() then brings the page back)

      // REAL WHATWG streams, not duck-typed objects. The tool locks and releases
      // readers/writers and checks .locked; a hand-rolled stand-in without a real
      // lock lifecycle hangs code that waits on it.
      this.readable = new ReadableStream({
        start(controller) {
          ws.addEventListener('message', (ev) => {
            const data = ev.data;
            if (data instanceof ArrayBuffer) {
              controller.enqueue(new Uint8Array(data));
            } else if (typeof data === 'string') {
              controller.enqueue(new TextEncoder().encode(data));
            }
            // Bytes stay bytes. The tool keeps ONE streaming TextDecoder alive so a
            // multi-byte character split across two frames reassembles; decoding
            // here would mangle exactly those.
          });
          ws.addEventListener('close', () => {
            // ERROR IT, DO NOT CLOSE IT. This is the freeze.
            //
            // controller.close() ends the stream GRACEFULLY, so the tool's
            // pending read() resolves {done:true} instead of rejecting. Its
            // reader loop is:
            //
            //   while (port === myPort && myPort.readable && !disconnecting) {
            //     r = myPort.readable.getReader();
            //     while (true) { const {value,done} = await r.read();
            //                    if (done) break; ... }
            //     ... releaseLock()
            //   }
            //
            // done:true breaks the inner loop, the lock is released, and the
            // OUTER condition is still true -- port.readable is non-null, it is
            // merely closed -- so it immediately acquires another reader, reads,
            // and gets done:true again. No exception, so none of the error
            // handling runs and none of its 50 ms backoff applies. It is a tight
            // loop with nothing to await, and it pegs a core: measured at 103%
            // with the JS heap thrashing 3-45 MB, layout frozen, and the window
            // unresponsive until the app is killed. Three debugger samples all
            // landed on the same two lines of that loop.
            //
            // A real serial port never does this. When a device disappears
            // Web Serial REJECTS the read with a NetworkError, which the tool
            // already classifies as fatal (_isFatalSerialError) and handles by
            // tearing down and calling handleLinkLost() -- which reconnects.
            // So report the loss the way the platform would.
            //
            // This fires on every ordinary droid drop, because the host closes
            // the page socket deliberately on a drop so the tool re-handshakes
            // and picks up a new firmware version after an OTA.
            linkOpen = false;
            lastLossAt = Date.now();
            const e = new Error('The device has been lost.');
            e.name = 'NetworkError';
            try { controller.error(e); } catch (_) {}
          });
          ws.addEventListener('error', () => {
            // Match Web Serial's vocabulary. The tool classifies fatal vs
            // recoverable by DOMException .name and treats NetworkError as
            // "device lost"; anything else it does not recognise is retried
            // forever, which is the 16.7-hour frozen-panel bug.
            const e = new Error('The device has been lost.');
            e.name = 'NetworkError';
            try { controller.error(e); } catch (_) {}
          });
        },
        cancel() { try { ws.close(); } catch (_) {} },
      });

      // ONE WEBSOCKET MESSAGE PER LINE — not per write().
      //
      // The tool splits any line over 512 B into chunks (sendLine's USB_CHUNK
      // pacing, which exists to protect the board's USB-CDC RX buffer). Sending
      // each chunk as its own WebSocket message makes the droid see three
      // separate "commands" for one line, because a message boundary is not a
      // line boundary. Short commands survived that; a ~1391 B OTA DATA line did
      // not, and failed as "base64 error -44 (chunk too big?)" — the decoder was
      // handed a fragment, not a line.
      //
      // A WebSocket is message-framed and the protocol is newline-delimited, so
      // something has to bridge the two. Doing it here means the droid receives
      // exactly one complete line per frame, which is the contract its handler
      // already assumes.
      let pending = '';
      const dec = new TextDecoder();
      this.writable = new WritableStream({
        write(chunk) {
          if (ws.readyState !== WebSocket.OPEN) {
            const e = new Error('The device has been lost.');
            e.name = 'NetworkError';
            throw e;   // a failing write is the tool's ONLY liveness proof
          }
          const bytes = chunk instanceof Uint8Array ? chunk : new Uint8Array(chunk);
          // ONE decoder for the life of the port. A fresh TextDecoder per write
          // makes { stream: true } meaningless: the parked partial sequence dies
          // with the discarded decoder and the continuation bytes come back as
          // U+FFFD. sendLine() cuts at 512 BYTES, so any longer line can split a
          // character mid-sequence -- SET_CMDLIB ships the whole command library as
          // one ~10 KB line, and a corrupted copy is persisted to the droid and then
          // adopted as "in sync", so it is never re-pushed.
          pending += dec.decode(bytes, { stream: true });
          // BOTH TERMINATORS. This split on '\n' alone, which was correct while the
          // NaviCore tool was the only caller -- it ends every line with '\n'.
          //
          // The WCB Wizard ends every command with '\r' and never sends '\n'
          // (BoardConnection.send is called as `send(cmd + '\r')`, and
          // sendAndCollect does `this.send(command + '\r')`). So a Wizard command
          // contained no '\n', this loop never fired, and the command sat in
          // `pending` FOREVER -- never reaching the host, let alone the board.
          //
          // The symptom was silent and looked like a board fault: the page still
          // received the relay's console mirror, so it appeared connected and
          // healthy, while every command it sent vanished. The first pull timed out
          // as "config pull incomplete", so a MgmtRelay was never identified and no
          // mesh boards were ever offered.
          //
          // Splitting on either is safe in both directions: the relay's own reader
          // treats '\n' and '\r' identically (mgmt_wsserver.h feed(): `if (c ==
          // '\n' || c == '\r')`), and so does the WCB firmware's USB reader.
          let i;
          while ((i = pending.search(/[\r\n]/)) >= 0) {
            let end = i + 1;
            // CRLF is ONE terminator, not two. Splitting it would emit a trailing
            // empty line; harmless (both readers skip blank lines) but it doubles
            // the frames and makes a capture confusing to read.
            if (pending[i] === '\r' && pending[end] === '\n') end++;
            const line = pending.slice(0, end);      // keep the terminator
            pending = pending.slice(end);
            ws.send(line);
          }
          // A tail with no terminator stays buffered until the rest arrives -- a
          // genuine mid-line chunk boundary, which is what this is for.
        },
        close() {
          if (pending) { try { ws.send(pending); } catch (_) {} pending = ''; }
          try { ws.close(); } catch (_) {}
        },
        abort() { try { ws.close(); } catch (_) {} },
      });
    }

    async close() {
      // Is this the user pressing Disconnect, or the tool tearing down after a
      // loss? It calls close() for BOTH, so the flag cannot simply be set here.
      //
      // The cascade that matters: handleLinkLost() -> disconnect() -> close(),
      // then its own one-shot tryAutoReconnect() fires immediately -- before the
      // host has re-attached to the droid -- opens, sees no PONG, and calls
      // disconnect() again ("Auto-reconnect found no board"), disarming itself.
      // Marking either of those closes as user intent blocks watchLink() from
      // ever retrying, which is exactly the "loses connection and never comes
      // back" state.
      //
      // So: a close within LOSS_GRACE_MS of a loss is teardown, not intent. A
      // deliberate Disconnect long after things are healthy still sticks, which
      // is the case the flag exists for.
      userClosed = (Date.now() - lastLossAt) > LOSS_GRACE_MS;
      linkOpen = false;
      const ws = this._ws;
      this._ws = null;
      this.readable = null;
      this.writable = null;
      if (ws) { try { ws.close(); } catch (_) {} }
    }

    async setSignals(signals) {
      // Forwarded for real -- on a serial link the host drives the actual lines.
      // The tool deasserts DTR/RTS right after open() to avoid resetting the board;
      // silently dropping that would reintroduce the reboot this project exists to
      // fix, so it must reach the host rather than be a no-op.
      try {
        await fetch('/_api/signals', {
          method: 'POST',
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify(signals || {}),
        });
      } catch (_) { /* a WebSocket target has no control lines; not an error */ }
    }
  }

  const thePort = new IntellexPort();
  const listeners = { connect: [], disconnect: [] };

  const shim = {
    // The host decides what it is attached to, so there is nothing to pick here.
    // A real chooser belongs in the app shell (which can show COM ports AND
    // discovered droids), not behind a browser API that only understands ports.
    async requestPort() { return thePort; },
    async getPorts() { return [thePort]; },
    addEventListener(type, fn) { (listeners[type] ||= []).push(fn); },
    removeEventListener(type, fn) {
      const a = listeners[type];
      if (a) { const i = a.indexOf(fn); if (i >= 0) a.splice(i, 1); }
    },
    dispatchEvent() { return true; },
  };

  // MUST be defineProperty, not assignment.
  //
  // Chrome exposes navigator.serial as a GETTER on Navigator.prototype, not as an
  // own data property. Under 'use strict' a plain `navigator.serial = shim` throws
  // "Cannot set property serial of #<Navigator> which has only a getter", which
  // aborted this whole IIFE -- leaving the REAL Web Serial in place, so the tool
  // silently went on asking for a physical port and auto-connect never ran. It
  // failed in exactly the way that looks like the app doing nothing at all.
  try {
    Object.defineProperty(navigator, 'serial', {
      value: shim, configurable: true, writable: true, enumerable: false,
    });
  } catch (e) {
    console.error('[Intellex] could not install the serial shim:', e);
    return;   // real Web Serial stays; the tool still works with a cable
  }
  if (navigator.serial !== shim) {
    console.error('[Intellex] serial shim did not take effect — the tool will use real Web Serial');
    return;
  }

  console.info('[Intellex] navigator.serial is backed by', LINK_URL);

  // ── Which tool is this? ───────────────────────────────────────────────────
  // One shim serves both. Everything above is genuinely shared -- the fake port,
  // the reconnect handling, the reload keys -- because both tools talk to the same
  // byte pipe and neither knows or cares what is on the far end. Below, the two
  // diverge: different connect entry points, different flash functions, different
  // status elements.
  //
  // Keyed off the path because that is what host.py actually mounts (the Wizard at
  // /wcb/Wizard/), not off page content, which would mean guessing from markup that
  // is not ours and may change upstream at any time.
  const IS_WCB = location.pathname.startsWith('/wcb/');
  console.info('[Intellex] tool:', IS_WCB ? 'WCB Wizard' : 'NaviCore config tool');

  // ── Tell the tool which firmware branch Intellex is set to ────────────────
  // Both tools resolve a branch themselves and default to 'main': the Wizard's
  // getFirmwareBranch() reads localStorage 'wcb_fw_branch', the config tool's
  // reads 'rc_fw_branch'. Each is a documented escape hatch, which makes it the
  // right seam -- setting it is using the page's own mechanism, not patching it.
  //
  // Without this the branch setting is only half applied: the HOST flashes the
  // branch build, while the PAGE still reports "latest on GitHub" from main and
  // its own firmware panel disagrees with what a flash would actually write.
  // Two sources of truth about the same thing, which is worse than either alone.
  //
  // Read from a value host.py prepends to this file, NOT fetched: the tools read
  // their key during init, and an async fetch would resolve after they had
  // already looked. Set before their scripts run, which is what the injection
  // point buys us.
  //
  // 'main' REMOVES the key rather than writing it. The tools treat absent and
  // 'main' identically, and leaving no residue means an override is always a
  // deliberate mark of a non-default branch rather than something that merely
  // accumulated.
  try {
    const br = (window.__intellexBranches || {})[IS_WCB ? 'wcb' : 'navicore'] || 'main';
    const key = IS_WCB ? 'wcb_fw_branch' : 'rc_fw_branch';
    if (br && br !== 'main') {
      localStorage.setItem(key, br);
      console.info('[Intellex] firmware branch for this tool:', br);
    } else {
      localStorage.removeItem(key);
    }
  } catch (_) { /* storage unavailable — the host still flashes the right branch */ }

  // ── Point the Wizard's cross-link at OUR NaviCore tool ────────────────────
  // The Wizard offers "open the RC Config Tool", defaulting to the copy on GitHub
  // Pages (RC_TOOL_URL_DEFAULT in its app.js). Inside Intellex that default is
  // wrong twice over: it needs the internet, which a con floor does not have, and
  // it lands on a DIFFERENT ORIGIN that cannot reach this host's byte pipe -- so
  // the tool it opens would be unable to talk to the droid at all.
  //
  // The Wizard already reads this from localStorage and treats it as user-editable
  // (rc_config_tool_url), so setting it is using a documented seam rather than
  // patching the file. Set before app.js runs, since it reads the value at load.
  //
  // Only when unset or still pointing at Pages: a user who has deliberately typed
  // their own URL keeps it.
  if (IS_WCB) {
    try {
      const KEY = 'rc_config_tool_url';
      const cur = localStorage.getItem(KEY);
      if (!cur || /greghulette\.github\.io/.test(cur)) localStorage.setItem(KEY, '/');
    } catch (_) { /* private mode / storage disabled — the link just stays remote */ }
  }

  // ── Reload keys ───────────────────────────────────────────────────────────
  // The app window has no browser chrome, so F5 and Ctrl+R do nothing — which
  // makes "just reload" unavailable exactly when it is the cheap fix. The UI
  // (this shim, the launcher, the bundled tool) is all served no-store, so a
  // reload picks up edits without restarting the app; only Python changes need a
  // restart. Ctrl+Shift+R additionally re-fetches, for when a stale asset is
  // suspected despite the headers.
  window.addEventListener('keydown', (e) => {
    if (e.key === 'F5' || ((e.ctrlKey || e.metaKey) && (e.key === 'r' || e.key === 'R'))) {
      e.preventDefault();
      location.reload();
    }
  });

  // ── Auto-connect ──────────────────────────────────────────────────────────
  // Without this the user must click Connect and choose "Direct USB" — a label
  // that is now actively misleading, because the host may well be on WiFi with no
  // cable in sight. The tool's transport names describe how IT reaches a board,
  // and the shim has made that one thing regardless of what is on the far end.
  //
  // Relabelling would mean editing index.html, which is the fork this whole
  // approach exists to avoid. So instead: the host already knows what it is
  // attached to, so just drive the tool's own connect path and skip the question.
  //
  // Calls the page's connectDirect(), which is the branch that ends up in
  // navigator.serial. Feature-detected and non-fatal: if the tool ever renames it,
  // auto-connect quietly stops and the manual button still works — a stale
  // assumption here must not make the app unusable.
  async function autoConnect() {
    try {
      const r = await fetch('/_api/status');
      const st = await r.json();
      if (!st.attached) {
        console.info('[Intellex] host has no transport attached — not auto-connecting');
        return;
      }
      if (typeof window.connectDirect !== 'function') {
        console.warn('[Intellex] connectDirect() not found; click Connect manually');
        return;
      }
      console.info('[Intellex] auto-connecting to', st.target);
      await window.connectDirect();
    } catch (e) {
      console.warn('[Intellex] auto-connect skipped:', e && e.message);
    }
  }

  // ── The tools' GitHub firmware calls, answered locally ────────────────────
  // Intercepting the flash BUTTONS was not enough. Both tools also fetch firmware
  // FROM INSIDE THE PAGE, and that is the only path OTA has at all:
  //
  //   index.html:17441  Update over USB (OTA)   -> fetchFirmwareImages()
  //   index.html:17697  Update over WCB (OTA)   -> fetchFirmwareImages()
  //   Wizard app.js:167 / flasher.js:117        -> the same REST call
  //
  // OTA over a relay IS the con-floor case -- on the droid's AP, no route to
  // GitHub, firmware cached, the relay right there -- and the page could not
  // reach the one copy of the image already on the machine.
  //
  // THE REQUEST IS REWRITTEN, NOT THE FUNCTIONS. Swapping fetchFirmwareImages
  // would fix one tool and leave the other needing its own swap, against function
  // names their own repos are free to change. Both tools agree instead on the
  // GitHub REST shape, which is far more stable, so that is what gets answered.
  // The host serves it from the cache when offline and from GitHub when not --
  // storing it on the way past, so merely opening the Firmware tab fills the
  // cache. See src/ghproxy.py.
  //
  // The reply's download_url values already point back at the host, so the page's
  // own follow-up fetch for the bytes needs no rewriting here.
  const GH_CONTENTS =
    /^https:\/\/api\.github\.com\/repos\/([^/]+)\/([^/]+)\/contents\/([^?]+)(?:\?(.*))?$/;

  (function proxyGithubFirmware() {
    const nativeFetch = window.fetch.bind(window);
    window.fetch = function (input, init) {
      try {
        const url = typeof input === 'string' ? input
                  : (input && input.url) ? input.url : '';
        const m = GH_CONTENTS.exec(url);
        if (m) {
          const owner = m[1], repo = m[2], path = m[3];
          const ref = new URLSearchParams(m[4] || '').get('ref') || 'main';
          const q = new URLSearchParams({ owner: owner, repo: repo,
                                          path: decodeURIComponent(path), ref: ref });
          // Only the URL is swapped; the caller's init (headers, signal) rides
          // along untouched, so an abort or a timeout still behaves.
          return nativeFetch('/_api/gh/contents?' + q.toString(), init);
        }
      } catch (_) { /* fall through to the real fetch */ }
      return nativeFetch(input, init);
    };
    console.info('[Intellex] firmware listings now come from the host');
  })();

  // ── Flashing: hand it to the host ─────────────────────────────────────────
  // esptool-js genuinely CANNOT work through this shim. It drives a real port:
  // toggling DTR/RTS in a timing-sensitive reset dance to enter the bootloader,
  // polling readable.locked / writable.locked, cancelling readers mid-stream. Over
  // a WebSocket there are no control lines at all (set_signals is a documented
  // no-op on that transport), so the board never enters download mode and esptool
  // sits printing "... ___ ..." forever. These buttons used to be disabled for
  // exactly that reason.
  //
  // But the HOST can flash, and better: it holds the real serial device and has
  // esptool as a Python package -- the same one ESP-Flasher-Companion ships, and
  // the approach CLAUDE.md specifies. So intercept the tool's own buttons and run
  // it there. Capture phase + stopImmediatePropagation, so the page's esptool-js
  // handler never fires; same buttons, same wording, same progress elements, only
  // the engine underneath changes. Nothing in index.html is touched, which is the
  // whole point of doing it from the shim.
  let flashBusy = false;

  // ── Is the HOST new enough to flash? ──────────────────────────────────────
  // This file is re-read from disk on every request; host.py is loaded once, at
  // startup. So a Intellex left running across an update serves a NEW page to an
  // OLD host -- the page POSTs /_api/flash, that route does not exist yet, the
  // static handler takes the request and answers 405, and the user is told "the
  // host refused" when the true answer is "restart Intellex". That happened, so
  // ask first and say the useful thing instead.
  //
  // Only a positive result is cached: a failed probe may just be a host that is
  // briefly busy, and the 3 s re-assert below will ask again.
  let hostCanFlash = false;
  async function hostSupportsFlash() {
    if (hostCanFlash) return true;
    try {
      hostCanFlash = (await fetch('/_api/flash-status')).ok;
    } catch (_) {
      hostCanFlash = false;
    }
    return hostCanFlash;
  }

  const STALE_HOST_MSG = 'This page is newer than the running Intellex host — '
                       + 'quit and reopen Intellex to enable flashing.';

  const FLASH_BTNS = [
    ['btn-fw-flash', false, 'Update Firmware',
     'Update via the host\'s native esptool. Saved configuration (NVS) is preserved.'],
    ['btn-fw-wipe',  true,  'Full Wipe & Flash',
     'Full wipe via the host\'s native esptool. ERASES saved configuration (NVS).'],
  ];

  // ── USB-only, and the buttons must SAY so ─────────────────────────────────
  // esptool needs the real serial line to walk the board into its bootloader. A
  // WiFi session -- to a droid's AP or through a management relay -- has no
  // control lines at all, so /_api/flash refuses it outright.
  //
  // The buttons were still offered on such a session. They looked perfectly
  // available, and only on click did the host answer 409 with an explanation. On
  // a page whose whole job is flashing, an enabled button that cannot work is
  // worse than a disabled one: it invites the click that puts a board into
  // bootloader mode for a flash that was never going to start.
  //
  // So ask what the link IS, not merely whether the host can flash at all.
  let linkKind = '';
  async function refreshLinkKind() {
    try {
      const st = await (await fetch('/_api/status')).json();
      // From the SPEC, not gated on being attached right now. The spec is what
      // the user chose, and it does not stop being WiFi because the link dropped
      // for a moment -- gating on `attached` made the buttons flap back to
      // enabled on every transient, which is when a hopeful click is most likely.
      linkKind = st.kind || '';
    } catch (_) { /* keep the last answer rather than flapping the buttons */ }
  }

  const NOT_USB_MSG = 'Flashing needs a direct USB connection. This session is over '
                    + 'WiFi, which has no DTR/RTS lines to enter the bootloader. '
                    + 'Attach the board over USB, or use OTA.';

  async function wireNativeFlash() {
    const ok = await hostSupportsFlash();
    await refreshLinkKind();
    // Only "serial" can flash. An empty kind means nothing is attached yet, and
    // disabling then would be wrong the other way -- the tool has its own
    // not-connected handling and this must not fight it.
    const usb = linkKind === 'serial';
    for (const [id, eraseNvs, label, title] of FLASH_BTNS) {
      const b = document.getElementById(id);
      if (!b) continue;
      if (!ok) {
        // Leave them disabled rather than wired to a route that is not there.
        if (!flashBusy) { b.disabled = true; b.title = STALE_HOST_MSG; }
        continue;
      }
      if (linkKind && !usb) {
        if (!flashBusy) { b.disabled = true; b.title = NOT_USB_MSG; }
        continue;
      }
      if (!b.__intellexWired) {
        b.__intellexWired = true;
        b.addEventListener('click', ev => {
          ev.preventDefault();
          ev.stopImmediatePropagation();   // the page's esptool-js path must not run
          startNativeFlash(eraseNvs, label);
        }, true);
      }
      // Re-asserted on a timer because the tool rewrites this whenever its
      // transport state changes -- but never while we are mid-flash, or its
      // bookkeeping would re-enable a button the flash is deliberately holding.
      if (!flashBusy) { b.disabled = false; b.title = title; }
    }
  }

  const FLASH_POLL_MS = 700;

  async function startNativeFlash(eraseNvs, label) {
    if (flashBusy) return;
    if (eraseNvs && !window.confirm(
          'Full Wipe & Flash will ERASE all saved settings on the board (NVS) '
        + 'and write a factory-fresh firmware image.\n\nContinue?')) return;

    const $ = id => document.getElementById(id);
    const statusEl = $('fw-status'), logEl = $('fw-log');
    const wrap = $('fw-progress-wrap'), bar = $('fw-progress-bar'), pct = $('fw-progress-pct');
    const setStatus = m => { if (statusEl) statusEl.textContent = m; };
    const seen = new Set();

    // Everything that could seize the port or the board while esptool owns it.
    const locked = ['btn-fw-flash', 'btn-fw-wipe', 'btn-fw-ota', 'btn-fw-ota-wcb', 'btn-connect']
      .map($).filter(Boolean);

    flashBusy = true;
    locked.forEach(b => { b.disabled = true; });
    if (logEl) logEl.textContent = '';
    if (wrap) wrap.style.display = 'block';
    if (bar)  bar.style.width = '0%';
    if (pct)  pct.textContent = '0%';
    setStatus(label + ': starting…');

    try {
      const r = await fetch('/_api/flash', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ eraseNvs }),
      });
      if (r.status === 404 || r.status === 405) {
        // The route is absent, so aiohttp's static handler answered instead.
        hostCanFlash = false;
        throw new Error(STALE_HOST_MSG);
      }
      const j = await r.json().catch(() => ({}));
      if (!r.ok || !j.ok) throw new Error(j.error || `the host refused (HTTP ${r.status})`);

      // Poll rather than stream: a flash outlives any request held open across the
      // board reset that ends it, and the log is small enough that re-reading it is
      // cheaper than another socket.
      for (;;) {
        await new Promise(res => setTimeout(res, FLASH_POLL_MS));
        let st;
        try {
          st = await (await fetch('/_api/flash-status')).json();
        } catch (_) {
          continue;                    // the host is briefly busy; keep waiting
        }
        for (const line of (st.log || [])) {
          if (seen.has(line)) continue;
          seen.add(line);
          if (logEl) { logEl.textContent += line + '\n'; logEl.scrollTop = logEl.scrollHeight; }
        }
        const p = st.percent || 0;
        if (bar) bar.style.width = p + '%';
        if (pct) pct.textContent = p + '%';
        if (st.running) { setStatus(`${label}: ${p}%`); continue; }
        if (st.ok) setStatus(`${label} complete ✓ — board is running ${st.version}`);
        else       setStatus(`${label} failed: ${st.error || 'see the log'}`);
        break;
      }
    } catch (e) {
      setStatus(label + ' failed: ' + ((e && e.message) || e));
    } finally {
      flashBusy = false;
      locked.forEach(b => { b.disabled = false; });
    }
  }

  // ══ The WCB Wizard ═══════════════════════════════════════════════════════
  // Everything from here to the next banner runs ONLY in the Wizard.

  // ── Auto-connect board slot 1 ─────────────────────────────────────────────
  // Same reasoning as the NaviCore auto-connect above: the host already knows what
  // it is attached to, so making the user re-answer "which port?" in a modal is
  // asking a question we have the answer to -- and the honest answer may well be a
  // relay over WiFi, which the modal has no way to express.
  //
  // Slot 1 specifically, and only when it is free. The Wizard is a MULTI-BOARD
  // tool: slots 2..n are for other WCBs, and quietly filling one the user was
  // about to assign themselves would be worse than doing nothing. Slot 1 on a
  // fresh page is the unambiguous case.
  //
  // _modalDoConnect() is the tool's own "connect this slot to this port" path, the
  // one its manual and authorize buttons both end in, so the entire rest of the
  // connect sequence (pull, UI, terminal wiring) happens exactly as it always does.
  // ── Turn OFF the Wizard's cross-tab port sharing ──────────────────────────
  // WcbSerialHub exists to work around one browser rule: a Web Serial port can be
  // open in exactly ONE browsing context, so the Wizard and the NaviCore tool
  // cannot each hold their own connection to the same USB board. It solves that
  // with a BroadcastChannel + Web Locks election — one tab owns the port, the
  // others proxy their bytes through it.
  //
  // THAT RULE DOES NOT APPLY HERE, and the workaround is actively harmful.
  // Under Intellex the "port" is a WebSocket to the host, every page opens its own,
  // and the host fans the droid's bytes to all of them (Bridge._pages). Both tools
  // already have independent, first-class access to the same link.
  //
  // Left on, the hub layers a second election and mirroring scheme on top of that:
  //   - establishConnection() auto-shares on the FIRST connect and then waits up to
  //     3 s for hub.portOpen before it will proceed, delaying every connect;
  //   - whichever tool loads first becomes leader, and the other stops using its own
  //     socket and relays through the leader's BroadcastChannel instead;
  //   - flashing is explicitly unavailable in shared mode ("it needs the raw port"),
  //     which would disable the native flash path for no reason.
  //
  // allowShare=false is the tool's OWN documented opt-out — its bulk auto-detect
  // passes it for a related reason ("so every board connects direct and a busy port
  // is reported rather than silently shared"). Forcing it is using a supported seam,
  // not defeating one. Deliberately NOT done by faking WcbSerialHub.supported:
  // getSharedHub() THROWS when unsupported and establishConnection has no catch
  // around it, so that would turn a connect into "Port sharing needs a Chromium
  // browser".
  function disableWcbPortSharing() {
    const orig = window.establishConnection;
    if (typeof orig !== 'function' || orig.__intellexNoShare) return;
    const wrapped = function (n, port, usedPorts, _allowShare) {
      return orig.call(this, n, port, usedPorts, false);
    };
    wrapped.__intellexNoShare = true;
    window.establishConnection = wrapped;
    console.info('[Intellex] cross-tab port sharing disabled — the host already '
               + 'serves every page its own link');
  }

  // ── Retire the branch-override warning after a few seconds ────────────────
  // The Wizard raises a PERSISTENT toast when a firmware branch override is set,
  // and it is right to: flashing a dev build unknowingly is exactly the mistake
  // worth shouting about. Its own comment says it "must not vanish before the
  // user flashes".
  //
  // Inside Intellex two things change. The advice is WRONG -- it says to run
  // localStorage.removeItem('wcb_fw_branch') in the console, but that key is set
  // from the launcher's branch field on every load, so doing that reverts on the
  // next reload and the real control is somewhere else entirely. And the warning
  // is not the only one: the launcher shows "not on main: WCB -> WIFI" in orange,
  // permanently, next to the field that actually changes it.
  //
  // So let it be seen and then let it go. A persistent toast that cannot be
  // resolved by following its own instructions stops being a warning and becomes
  // furniture -- and furniture is what people stop reading.
  const BRANCH_TOAST_MS = 9000;
  function retireBranchToast() {
    const until = Date.now() + 30000;
    const tick = setInterval(() => {
      if (Date.now() > until) { clearInterval(tick); return; }
      for (const t of document.querySelectorAll('#toast-container .toast.warning')) {
        if (!/Firmware source is overridden/i.test(t.textContent || '')) continue;
        if (t.__intellexTimed) continue;
        t.__intellexTimed = true;
        // Dismiss with the toast's OWN button, so whatever teardown it does --
        // transition, removal, bookkeeping -- happens exactly as it would if the
        // user had clicked it.
        setTimeout(() => {
          const btn = t.querySelector('.toast-dismiss');
          if (btn) btn.click(); else t.remove();
        }, BRANCH_TOAST_MS);
        clearInterval(tick);
        return;
      }
    }, 200);
  }

  // ── Dismiss the Wizard's "Setup Wizard / Config Tool" splash ──────────────
  // It asks which way you want to start, every single load. Inside Intellex that
  // question has already been answered by opening the WCB pane at all, and the
  // shell PRELOADS that pane — so the modal appears over a tool nobody has looked
  // at yet, and again in a second window, for a choice nobody asked to make.
  //
  // Answered with the tool's OWN splashGoConfig(), which is exactly what its
  // "Config Tool" button calls — the seam the page already offers, not a reach
  // into its internals and not an edit to index.html. There is no flag or URL
  // parameter to do this with; showSplash() is called unconditionally at init.
  //
  // Config Tool, not Setup Wizard, because that is the branch for "updating an
  // existing system" — its own words — and the guided setup is a thing to choose
  // deliberately, never something to land in by default.
  //
  // NOTHING IS LOST. The header keeps its "✦ Wizard" button, so the guided setup
  // is still one click away; only the question up front goes.
  //
  // Polled rather than called once: the splash is raised during the tool's own
  // init, which can land after this shim's load handler. Watches only
  // #splash-overlay, so a Setup Wizard the user opens deliberately (a different
  // modal, #wizard-modal) is never touched.
  const SPLASH_WATCH_MS = 10000;
  let splashWatching = false;
  function dismissWcbSplash() {
    if (splashWatching) return;
    splashWatching = true;
    const until = Date.now() + SPLASH_WATCH_MS;
    const tick = setInterval(() => {
      const el = document.getElementById('splash-overlay');
      if (el && el.classList.contains('open')) {
        try {
          if (typeof window.splashGoConfig === 'function') window.splashGoConfig();
          else el.classList.remove('open');   // the function was renamed; still dismiss
          console.info('[Intellex] dismissed the Wizard start-up splash');
        } catch (e) {
          console.warn('[Intellex] could not dismiss the splash:', e && e.message);
        }
        clearInterval(tick);
        return;
      }
      if (Date.now() > until) clearInterval(tick);
    }, 150);
  }

  // What the far end is decides how the Wizard must be set up, so remember it.
  let wcbRole = '', wcbRelayId = null;

  async function autoConnectWcb() {
    try {
      const r = await fetch('/_api/status');
      const st = await r.json();
      if (!st.attached) {
        console.info('[Intellex] host has no transport attached — not auto-connecting');
        return;
      }
      wcbRole    = st.role || '';
      wcbRelayId = st.relayId || null;
      if (typeof window._modalDoConnect !== 'function') {
        console.warn('[Intellex] _modalDoConnect() not found; connect board 1 manually');
        return;
      }
      // Already connected (a reload that raced us, or the user was quicker)? Leave it.
      const conns = window.boardConnections || {};
      if (conns[1] && conns[1].isConnected && conns[1].isConnected()) return;
      console.info('[Intellex] auto-connecting WCB slot 1 to', st.target,
                   wcbRole ? `(role: ${wcbRole})` : '');
      await window._modalDoConnect(1, await navigator.serial.requestPort());
      // _modalDoConnect schedules its own pull 3 s out. That pull is what reveals
      // ?RELAY,1 and turns slot 1 into a relay card, so wait for the outcome
      // rather than assuming it.
      //
      // RUN THIS WHATEVER THE HOST SAID IT ATTACHED TO. It used to be gated on
      // role === 'relay', which was wrong the moment NaviCore itself became a
      // doorway to the mesh: the host reports that link as role "navicore", so
      // the gate would skip routing and leave every WCB unmanaged behind a board
      // that was perfectly capable of relaying to them.
      //
      // What actually matters is whether a RELAY CARD appears, which is the
      // page's own verdict on the backup it got back — and something only a
      // device advertising ?RELAY,1 produces. routeMeshThroughRelay() polls for
      // exactly that and does nothing if none ever shows, so running it
      // unconditionally costs a timer against a directly-cabled WCB and gets the
      // answer right for every kind of doorway, including ones added later.
      routeMeshThroughRelay();
      // ...and its counterpart for a doorway that is an ordinary WCB, which
      // produces no relay card by definition. Both start; they are mutually
      // exclusive at runtime, because this one stands down the moment a relay
      // card exists and the other does nothing until one does. Starting both
      // rather than choosing means neither has to predict which kind of box the
      // pull is about to reveal — the same reason the call above is ungated.
      routeMeshThroughBoard();
    } catch (e) {
      console.warn('[Intellex] WCB auto-connect skipped:', e && e.message);
    }
  }

  // ── A relay is NOT a board, and the Wizard already knows that ─────────────
  // Connecting the Wizard to a MgmtRelay and stopping there leaves it looking
  // like a broken WCB: "config pull incomplete", no boards, nothing to manage.
  // That is not a bug in either tool — it is a setup step nobody performed.
  //
  // The Wizard's own model is exactly right for this and needs no changes. A
  // device whose backup carries ?RELAY,1 is short-circuited out of the numbered
  // grid into a dedicated RELAY CARD (app.js, the config.isRelay branch), the
  // mesh boards it hears via ?WDP,DUMP land in _relayNodes, and relayRouteAll()
  // binds each one for remote management and pulls its config SEQUENTIALLY --
  // sequentially because the relay reassembles one config reply at a time and
  // overlapping pulls cross-assign configs to the wrong board.
  //
  // All of that is reached by clicking "Manage all" on the relay card. The only
  // thing missing was anyone clicking it, so do that: the host already knows it
  // attached a relay, which is the fact the page was lacking.
  //
  // WAIT FOR THE CARD, do not race it. The chain is connect -> 3 s -> pull ->
  // parse ?RELAY,1 -> relay card -> a WDP mesh sweep populates the board list.
  // Calling relayRouteAll() before the sweep finds no targets and does nothing,
  // silently, which is indistinguishable from the bug it is meant to fix.
  // Which relay card the Wizard rendered. Prefer the id the host gave us; fall
  // back to whatever card exists, because a relay whose DEVICE_ID was changed
  // still renders one and is still the thing we want.
  function relayCardSlot() {
    if (wcbRelayId != null && document.getElementById(`relay-card-${wcbRelayId}`)) return wcbRelayId;
    const card = document.querySelector('#relay-cards [id^="relay-card-"]');
    if (!card) return null;
    const n = parseInt(card.id.slice('relay-card-'.length), 10);
    return Number.isNaN(n) ? null : n;
  }

  // Boards on the card that are heard but NOT yet managed. renderRelayCard gives
  // each unbound board a "Manage via relay" button and each bound one a plain
  // "managed" label, so counting the buttons asks the page what it actually shows
  // rather than reaching into the tool's module-scoped state.
  function unmanagedOnCard(slot) {
    return document.querySelectorAll(
      `#relay-card-${slot} button[onclick^="relayManageOne"]`).length;
  }

  // KEEP WATCHING, do not race the mesh sweep once and give up.
  //
  // The chain is: connect -> pull -> relay card -> a ?WDP,DUMP sweep populates the
  // board list -> relayRouteAll binds them. That sweep runs on the Wizard's own
  // timer, so how long it takes is not ours to predict — and a board that is
  // powered on later, or that misses a sweep, appears minutes afterwards. A
  // one-shot window measured this wrong: it expired before the first sweep landed
  // and then never looked again, leaving every board unmanaged with no way back
  // except noticing the button.
  //
  // So poll, act whenever there is something unmanaged, and keep polling. Calling
  // relayRouteAll again is safe and cheap: it guards itself with _relayRouteAllBusy
  // against overlapping runs, and skips boards that already have a baseline.
  const RELAY_POLL_MS  = 2000;
  const RELAY_WATCH_MS = 300000;   // 5 min of attention, then stop nagging
  async function routeMeshThroughRelay() {
    if (typeof window.relayRouteAll !== 'function') {
      console.warn('[Intellex] relayRouteAll() not found — click "Manage all" on the relay card');
      return;
    }
    const deadline = Date.now() + RELAY_WATCH_MS;
    let sawCard = false, bound = 0;
    while (Date.now() < deadline) {
      await new Promise(res => setTimeout(res, RELAY_POLL_MS));
      const slot = relayCardSlot();
      if (slot == null) continue;
      if (!sawCard) {
        sawCard = true;
        console.info('[Intellex] relay card is up at slot', slot);
      }
      const n = unmanagedOnCard(slot);
      if (!n) continue;
      bound += n;
      console.info(`[Intellex] ${n} mesh board(s) unmanaged — routing them through relay ${slot}`);
      try { window.relayRouteAll(slot); } catch (e) {
        console.warn('[Intellex] relayRouteAll failed:', e && e.message);
      }
    }
    // Say WHICH half fell short — they need different things looked at.
    if (!sawCard) {
      console.warn('[Intellex] no relay card appeared — the config pull never returned '
        + 'a backup. Check the terminal pane for what came back.');
    } else if (!bound) {
      console.warn('[Intellex] the relay card is up but no mesh boards were ever heard. '
        + 'Are the WCBs powered and on the same mesh channel?');
    }
  }

  // ── The same thing again, for a doorway that is an ORDINARY WCB ───────────
  //
  // A relay card is created only for a device whose backup carries ?RELAY,1
  // (parser.js:499), and a real WCB's backup has no such line -- that absence is
  // exactly what identifies it as a board rather than a relay (see discover.py).
  // So attaching through a WCB hosting its own AP, or through one on USB, renders
  // NO relay card, routeMeshThroughRelay() above finds nothing, and the other
  // boards on the mesh sit surfaced-but-unmanaged forever. The WDP sweep DOES
  // list them and give each a section; nothing pulls their config.
  //
  // ROUTING THROUGH A PLAIN WCB IS NOT A TRICK -- it is the firmware's original
  // design. WCB.ino implements the whole relay half of the management protocol
  // ("Relay side: handle ?MGMT,PULL,<targetWCB>", WCB.ino:3969), which is how the
  // Wizard has always managed a mesh through one USB-cabled board. MgmtRelay is a
  // second implementation of that surface, not the only one.
  //
  // The page side is equally general: remoteBoardPull(parent, target) needs only
  // boardConnections[parent] to be live, and setRemoteConnected(target, parent)
  // arms the terminal, the ETM listener and the buttons with no relay check in
  // it. Only the ENTRY POINT was relay-shaped.
  //
  // WHY NOT JUST CALL relayRouteAll() WITH THE WCB'S SLOT: two reasons, both
  // fatal. It reads its targets from _relayNodes[slot], which the sweep fills
  // only `for (const rs of _relaySlots)` (app.js:13124) -- empty for a board. And
  // it routes through relayManageOne(), which calls renderRelayCard()
  // unconditionally, so it would draw a relay card for a device that is a real
  // board and is already in the numbered grid: the duplicate-card bug the Wizard
  // fixed once already.
  //
  // So drive the two generic functions directly, keeping relayRouteAll's own
  // disciplines, which are firmware constraints and not relay ones:
  //   SEQUENTIALLY, awaiting each pull. The parent reassembles one config reply
  //     at a time and the [MGMT:CONFIG,] listener is not target-filtered, so
  //     overlapping pulls cross-assign configs to the WRONG board.
  //   ONCE PER BOARD. The Wizard deliberately stopped auto-pulling every newly
  //     heard peer because it "fought live traffic and surprised the user"
  //     (app.js:13139). Skipping boards that already have a baseline is what
  //     keeps this a one-time catch-up rather than a re-pull on every sweep.
  const _meshRoutedOnce = new Set();

  // ── Which nodes are actually BOARDS ──────────────────────────────────────
  // Not every device on the mesh is a configurable WCB. A WDP row carries
  // CLIENT=1 for a WCB_Client host -- a MgmtRelay, or NaviCore itself -- and
  // those have no WCB config to pull at all. The Wizard gives them a lightweight
  // client card (cfg.type = 'client'), never a board section's config.
  //
  // _meshBoards is NOT the set of boards, which is the trap this fell into. It
  // means "has a numbered section", and upsertClientCard() calls
  // addDiscoveredBoards() for clients too (app.js:13045) -- so a relay at 19 and
  // a NaviCore at 20 sat in it looking exactly like boards, and got pulled.
  //
  // The authoritative flag lives on the sweep's own parsed nodes
  // (`client: m[2] === '1'`, app.js:12803), so take it from there: wrap the
  // renderWdpMesh() the sweep already calls on every tick and keep the list. No
  // extra ?WDP,DUMP of our own -- that would fight the tool's sweep for the same
  // connection, and the tool guards its own with _meshDiscoverBusy for a reason.
  let _lastWdpNodes = null;
  function watchWdpSweeps() {
    const orig = window.renderWdpMesh;
    if (typeof orig !== 'function' || orig.__intellexWrapped) return;
    const wrapped = function (nodes) {
      if (Array.isArray(nodes) && nodes.length) _lastWdpNodes = nodes;
      return orig.apply(this, arguments);
    };
    wrapped.__intellexWrapped = true;
    window.renderWdpMesh = wrapped;
  }

  // The Wizard's state lives in top-level `let`s. Those are reachable here by
  // BARE NAME -- the shim is a classic <script> in the same global scope -- but
  // NOT as window.x, because a top-level let/const goes in the global lexical
  // environment and never becomes a property of window. (Its top-level
  // `function`s do, which is why the calls below use window. and the reads do
  // not.) Grabbed in one guarded shot so a rename in the tool degrades to a
  // single clear warning instead of a ReferenceError mid-pass.
  function wizState() {
    try {
      return {
        conns:     boardConnections,
        baselines: boardBaselines,
        relayFor:  remoteRelayForBoard,
        pulling:   _pullingBoards,
        configs:   typeof boardConfigs   !== 'undefined' ? boardConfigs   : {},
        clients:   typeof _meshClients   !== 'undefined' ? _meshClients   : null,
        heard:     typeof _meshBoards    !== 'undefined' ? _meshBoards    : null,
      };
    } catch (_) { return null; }
  }

  async function routeMeshThroughBoard() {
    // Before the first poll, so no sweep goes uncaptured. Idempotent.
    watchWdpSweeps();
    const deadline = Date.now() + RELAY_WATCH_MS;
    let warned = false;
    while (Date.now() < deadline) {
      await new Promise(res => setTimeout(res, RELAY_POLL_MS));
      // A relay card means the relay path above owns this link. Standing down is
      // not politeness: both loops pulling would interleave two config replies
      // through one non-filtered listener, which is the cross-assignment above.
      if (relayCardSlot() != null) return;

      const parent = typeof window._wdpMeshConn === 'function' ? window._wdpMeshConn() : null;
      if (!parent) continue;                       // nothing connected yet
      if (typeof window.setRemoteConnected !== 'function'
          || typeof window.remoteBoardPull !== 'function') {
        if (!warned) {
          warned = true;
          console.warn('[Intellex] setRemoteConnected/remoteBoardPull not found — mesh '
            + 'boards will need their own Connect button.');
        }
        continue;
      }
      const st = wizState();
      if (!st) {
        if (!warned) {
          warned = true;
          console.warn('[Intellex] cannot see the Wizard\'s board state — mesh boards will '
            + 'need their own Connect button. (Wizard internals renamed?)');
        }
        continue;
      }

      // PREFER the sweep's own nodes: they carry CLIENT, so a relay or a NaviCore
      // is excluded on the authoritative flag rather than on a guess.
      const parentNum = parseInt(parent.wcbNum, 10);
      let seen;
      if (_lastWdpNodes) {
        seen = _lastWdpNodes.filter(nd => !nd.client).map(nd => nd.n);
      } else {
        // No sweep captured yet. _meshBoards mixes clients in, so subtract every
        // way the page knows one: its client roster, and the slot type it flipped
        // the card to. Belt and braces because they populate at different moments
        // -- upsertClientCard bails before _meshClients.set() on the first sweep,
        // when the section exists but boardConfigs[n] does not yet.
        const raw = st.heard ? [...st.heard]
          : [...document.querySelectorAll('[id^="section-board-"]')]
              .map(el => parseInt(el.id.slice('section-board-'.length), 10));
        seen = raw.filter(n =>
          !(st.clients && st.clients.has(n)) && st.configs[n]?.type !== 'client');
      }

      const targets = seen.filter(n =>
        Number.isInteger(n) && n >= 1 && n <= 20
        && n !== parentNum && String(n) !== String(parent.slot)
        && !st.conns[n]?.isConnected?.()   // a directly-cabled board is not ours to route
        && !st.baselines[n]                // already pulled — do not re-pull on every sweep
        && !st.pulling.has(n)
        && !_meshRoutedOnce.has(n));
      if (!targets.length) continue;

      console.info(`[Intellex] ${targets.length} mesh board(s) unmanaged behind WCB `
        + `${parentNum} — arming and pulling through it`);
      for (const n of targets) {
        _meshRoutedOnce.add(n);            // claim it before awaiting, so the next
                                           // tick cannot pick the same board up again
        try {
          if (st.relayFor[n] !== parent.slot) window.setRemoteConnected(n, parent.slot);
          // remoteBoardPull signals through its onComplete CALLBACK, not by
          // resolving — the same contract relayRouteAll relies on.
          //
          // Raced against a watchdog, which relayRouteAll does not need and this
          // does: it is user-triggered and reports through toasts, while this
          // runs unattended in the background. A pull that never calls back would
          // park this loop forever on board one and silently strand every board
          // behind it, looking exactly like "it only pulled the first one".
          // 45 s clears the tool's own worst case with room to spare — 3 attempts
          // x PULL_TIMEOUT_MS 6 s, plus 2 x PULL_RETRY_MS 2.5 s = 23 s.
          const PULL_WATCHDOG_MS = 45000;
          let timer;
          const done = await Promise.race([
            new Promise(res => {
              const p = window.remoteBoardPull(parent.slot, n, 1, 3, res);
              if (p && p.catch) p.catch(() => res(false));
            }),
            new Promise(res => { timer = setTimeout(() => res('timeout'), PULL_WATCHDOG_MS); }),
          ]);
          clearTimeout(timer);
          if (done === 'timeout') {
            console.warn(`[Intellex] pull of WCB${n} through ${parentNum} never reported back `
              + `after ${PULL_WATCHDOG_MS / 1000}s — moving on to the next board. `
              + 'Use that board\'s own Pull Config button to retry it.');
          }
          await new Promise(r => setTimeout(r, 250));   // let reassembly clear
        } catch (e) {
          console.warn(`[Intellex] pull of WCB${n} through ${parentNum} failed:`, e && e.message);
        }
      }
    }
  }

  // ── Native flashing for the Wizard ────────────────────────────────────────
  // REPLACE THE FUNCTION, DO NOT INTERCEPT THE BUTTONS.
  //
  // The NaviCore tool has two fixed flash buttons, so intercepting clicks works
  // there. The Wizard does not: flashing is one branch of boardGo() for any of N
  // board slots, wrapped in its own bookkeeping -- it saves the port, closes the
  // connection, sets "Flashing…" on that card, drives a per-board progress bar,
  // then reconnects and re-shares the port and re-pushes config afterwards. All of
  // that is correct and none of it is ours to reimplement.
  //
  // But every path through it funnels into exactly one call: flashFirmware(port,
  // hwVersion, {onProgress, onLog, onStatus, appOnly, eraseNvs}). Swapping THAT
  // leaves all the surrounding behaviour untouched and running, and the tool cannot
  // tell the difference: it passes the same callbacks and gets the same
  // resolve-or-throw contract back.
  //
  // `port` is ignored on purpose -- it is our fake one, and the host has the real
  // device. So is hwVersion: flasher.js calls it a "pre-fetch guess" and says chip
  // auto-detection is authoritative, and the host detects for real before it
  // downloads anything, so passing the guess along could only make things worse.
  function installWcbFlash() {
    if (window.__intellexWcbFlash) return;
    window.__intellexWcbFlash = true;

    window.flashFirmware = async function (_port, _hwVersion, opts) {
      const o = opts || {};
      const onLog      = o.onLog      || (() => {});
      const onStatus   = o.onStatus   || (() => {});
      const onProgress = o.onProgress || (() => {});
      const appOnly    = !!o.appOnly;
      const eraseNvs   = !!o.eraseNvs;

      if (!(await hostSupportsFlash())) throw new Error(STALE_HOST_MSG);

      // Refuse BEFORE boardGo() tears the board's connection down. The host would
      // reject a WiFi session anyway (409), but by then the Wizard has already
      // closed the port, set the card to "Flashing…" and started its own
      // bookkeeping -- so the failure arrives after a disruption that achieved
      // nothing. Checking first turns it into a message and no disruption at all.
      await refreshLinkKind();
      if (linkKind && linkKind !== 'serial') throw new Error(NOT_USB_MSG);

      onStatus('Starting native flash…');
      onLog('Intellex: flashing through the host (esptool), not the browser.');
      onLog(appOnly ? 'Mode: Update — app only, configuration preserved.'
                    : eraseNvs ? 'Mode: Factory Reset — NVS will be erased.'
                               : 'Mode: full flash — bootloader + partitions + app.');

      const r = await fetch('/_api/flash-wcb', {
        method:  'POST',
        headers: { 'Content-Type': 'application/json' },
        body:    JSON.stringify({ appOnly, eraseNvs }),
      });
      if (r.status === 404 || r.status === 405) {
        // The route is absent, so aiohttp's static handler answered instead —
        // a page newer than the running host. Same trap as the NaviCore path.
        hostCanFlash = false;
        throw new Error(STALE_HOST_MSG);
      }
      const j = await r.json().catch(() => ({}));
      if (!r.ok || !j.ok) throw new Error(j.error || `the host refused (HTTP ${r.status})`);

      // Poll, for the reason the NaviCore path polls: a flash outlives any request
      // held open across the board reset that ends it.
      const seen = new Set();
      for (;;) {
        await new Promise(res => setTimeout(res, FLASH_POLL_MS));
        let st;
        try {
          st = await (await fetch('/_api/flash-status')).json();
        } catch (_) {
          continue;                    // the host is briefly busy; keep waiting
        }
        for (const line of (st.log || [])) {
          if (seen.has(line)) continue;
          seen.add(line);
          onLog(line);
        }
        const p = st.percent || 0;
        // The Wizard's bar takes (written, total) and derives the percentage, so
        // feed it p/100 rather than inventing byte counts we do not have.
        onProgress(p, 100);
        if (st.running) { onStatus(`Flashing… ${p}%`); continue; }
        // THROW on failure. boardGo() uses the exception as its only failure
        // signal -- it catches, toasts, and skips the post-flash reconnect and
        // config push. Returning quietly would have it report success and then
        // push config at a board that never got new firmware.
        if (!st.ok) throw new Error(st.error || 'flash failed — see the log');
        onStatus('Flash complete');
        onLog(`Done — board is running ${st.version}.`);
        // The tool reads this to label the card after a flash. Setting it keeps
        // that display honest instead of leaving it on the pre-flash build.
        try { window.latestFirmwareVersion = st.version; } catch (_) {}
        return;
      }
    };
    console.info('[Intellex] flashFirmware() now runs on the host');
  }

  // ── Say which transport is actually in use ────────────────────────────────
  // The tool's status reads "Connected" whatever is behind navigator.serial, so a
  // WiFi session looks identical to a cable. That is not cosmetic: what you do
  // when it stops responding differs completely — check the cable, or check the
  // adapter and whether the AP came back after a reboot.
  //
  // IN OUR OWN ELEMENT, NOT THE TOOL'S TEXT.
  //
  // This used to append to #status-text and re-apply on a 2 s timer, because the
  // tool rewrites that span on every state change. On a direct link you never
  // notice. Through the relay you get the whole mesh mirror, and the tool
  // unconditionally rewrites the span on EVERY rc_hb heartbeat (index.html, the
  // rc_hb branch of handleBoardMessage) -- which arrive about every 2 s, beating
  // against our own 2 s re-apply. The label appeared and vanished, over and over.
  //
  // Fighting a timer with a timer cannot win. Own a sibling span instead: the tool
  // can rewrite its element as often as it likes and never touches ours.
  let lastTarget = '';

  function labelEl() {
    let el = document.getElementById('intellex-transport');
    if (el) return el;
    const host = document.getElementById('status-text');
    if (!host || !host.parentNode) return null;
    el = document.createElement('span');
    el.id = 'intellex-transport';
    // Inherit, so it reads as part of the status line rather than a bolt-on.
    el.style.cssText = 'font:inherit;color:inherit;opacity:.85;cursor:pointer';
    el.title = 'Change connection (USB / WiFi)';
    // THE WAY BACK. The window has no browser chrome, so once a transport is
    // picked there is otherwise no route to the chooser -- you would have to close
    // and relaunch to switch from USB to WiFi. The label naming the current
    // connection is where you look when you want to change it.
    el.addEventListener('click', openChooser);
    host.parentNode.insertBefore(el, host.nextSibling);
    return el;
  }

  async function refreshTarget() {
    try {
      const st = await (await fetch('/_api/status')).json();
      lastTarget = st.attached ? String(st.target || '') : '';
    } catch (_) { lastTarget = ''; }
  }

  function labelFor(target) {
    if (!target) return '';
    // "ws://192.168.4.1/ws" -> "WiFi 192.168.4.1"; "serial COM5" -> "USB COM5"
    const m = /^ws:\/\/([^/:]+)/.exec(target);
    if (m) return 'WiFi ' + m[1];
    return target.replace(/^serial\s+/, 'USB ');
  }

  function annotateStatus() {
    const host = document.getElementById('status-text');
    const el = labelEl();
    if (!host || !el) return;
    const label = labelFor(lastTarget);
    // Only show it once the tool itself says it is connected — labelling
    // "Disconnected" or "Waiting for board…" would be actively misleading.
    const base = host.textContent || '';
    const want = (label && /connect/i.test(base) && !/disconnect/i.test(base))
      ? ' · ' + label + ' ▾'
      : '';
    // Write only on change: this runs every 2 s and the element is in the layout
    // path of a status bar that is already being repainted by telemetry.
    if (el.textContent !== want) el.textContent = want;
  }

  // ── The Wizard's version of that label ────────────────────────────────────
  // The Wizard has no #status-text to annotate -- its connection state lives per
  // board card, and there is no one place that means "the link". So it gets its
  // own chip instead of a borrowed element.
  //
  // The information matters more here than in the NaviCore tool, not less: a WCB
  // reached over the relay is on the far side of a mesh hop, so "it stopped
  // answering" has a different cause and a different fix than it does on a cable.
  // The Wizard's own UI cannot say which, because through the shim both look like
  // an ordinary serial port.
  //
  // SUPPRESSED INSIDE THE SHELL, which already shows the transport in its bar.
  // Two chips saying the same thing in one window reads as two connections.
  const IN_SHELL = (() => {
    try { return window.parent !== window; } catch (_) { return false; }
  })();

  // ── "Change connection", without throwing the session away ────────────────
  // Standalone this is a plain navigation. Inside the shell it must NOT be: this
  // document is a tool frame, so navigating it to /_launcher unloads the tool and
  // discards everything it holds — the connection, every board's pulled config,
  // the terminal scrollback. Coming back then re-pulls the lot, which through a
  // relay is a slow sequential pull per board.
  //
  // So ask the shell to put the chooser over the top instead. It stays a
  // navigation when there is no shell to ask.
  function openChooser() {
    if (IN_SHELL) {
      try {
        window.parent.postMessage({ intellex: 'open-chooser' }, location.origin);
        return;
      } catch (_) { /* fall through to navigating */ }
    }
    location.href = '/_launcher';
  }

  function wcbChip() {
    if (IN_SHELL) return null;
    let el = document.getElementById('intellex-chip');
    if (el) return el;
    if (!document.body) return null;
    el = document.createElement('div');
    el.id = 'intellex-chip';
    el.style.cssText = 'position:fixed;right:12px;bottom:12px;z-index:99998;'
      + 'background:#1d2733;color:#cfe3ff;border:1px solid #35506e;border-radius:14px;'
      + 'padding:5px 11px;font:12px/1.3 system-ui,sans-serif;cursor:pointer;'
      + 'box-shadow:0 2px 10px rgba(0,0,0,.35);opacity:.92';
    el.title = 'Intellex connection — click to change it';
    // THE WAY BACK, same problem as the NaviCore label solves: an app window has
    // no browser chrome, so without this there is no route from the Wizard to the
    // chooser and switching from USB to WiFi means restarting the app.
    el.addEventListener('click', openChooser);
    document.body.appendChild(el);
    return el;
  }

  function annotateWcb() {
    const el = wcbChip();
    if (!el) return;
    const label = labelFor(lastTarget);
    const want = label ? 'Intellex · ' + label + ' ▾' : 'Intellex · not attached ▾';
    if (el.textContent !== want) el.textContent = want;
  }

  setInterval(() => {
    refreshTarget().then(IS_WCB ? annotateWcb : annotateStatus);
  }, 2000);

  // ── "Still trying" banner ─────────────────────────────────────────────────
  // A reboot costs ~6 s of dead air and the host recovers on its own, so a banner
  // that appears instantly would be noise. But when it does NOT come back the
  // cause is almost always this machine's WiFi rather than the droid — and there
  // is nothing on screen to say so, or to say what to do about it.
  //
  // So: stay quiet through the normal outage, then be specific.
  const QUIET_S = 20;    // longer than any healthy reboot recovery observed
  let downSince = 0;

  function banner(html) {
    let b = document.getElementById('intellex-banner');
    if (!html) { if (b) b.remove(); return; }
    if (!b) {
      b = document.createElement('div');
      b.id = 'intellex-banner';
      b.style.cssText = 'position:fixed;left:0;right:0;top:0;z-index:99999;'
        + 'background:#4a3a1e;color:#ffd79a;border-bottom:1px solid #7a5f2e;'
        + 'padding:9px 14px;font:13px/1.45 system-ui,sans-serif;text-align:center';
      document.body.appendChild(b);
    }
    b.innerHTML = html;
  }

  async function watchLink() {
    let st;
    try { st = await (await fetch('/_api/status')).json(); } catch (_) { return; }

    // GET THE PAGE BACK. The host is attached but our socket is not open, and the
    // tool was not the one who closed it -- so the droid dropped and came back
    // while the page stayed disconnected. autoConnect() only runs on `load`, which
    // is why the only known cure used to be closing and reopening the app.
    //
    // Backed off, because connectDirect() is not instant and the tool needs a
    // moment to finish tearing the old port down; retrying every 2 s would stack
    // half-finished connects on top of each other.
    if (st.attached && !linkOpen && !userClosed && Date.now() >= reconnectAt) {
      reconnectAt = Date.now() + 5000;
      console.info('[Intellex] host is attached but the page is not — reconnecting');
      // KEEP RETRYING. The tool's own auto-reconnect is deliberately one-shot and
      // disarms after a single failure, which is the right call for real hardware
      // (it must never grab the wrong serial device). Here the target is not
      // ambiguous -- there is exactly one host and it has already told us it is
      // attached -- so retrying is safe, and necessary: the first attempt usually
      // lands while the droid is still rebooting and sees no PONG.
      (IS_WCB ? autoConnectWcb : autoConnect)();
    }

    if (st.attached || !st.wantsLink) { downSince = 0; banner(''); return; }
    if (!downSince) downSince = Date.now();
    const secs = Math.round((Date.now() - downSince) / 1000);
    if (secs < QUIET_S) { banner(''); return; }   // still within a normal reboot
    banner(
      `<b>Still trying to reach the droid</b> — ${secs}s. `
      + `The app keeps retrying, so no action is needed if it is rebooting.<br>`
      + `If it does not come back: <b>reconnect this computer to the "NaviCore" WiFi network</b>, `
      + `or <a href="/_launcher" style="color:#ffd79a">choose a different connection</a>.`
    );
  }
  setInterval(watchLink, 2000);

  // AS EARLY AS THE FUNCTION EXISTS, which is DOMContentLoaded: every <script> has
  // run by then, so app.js has defined establishConnection, but the Wizard's own
  // init (and any auto-detect it starts) has not yet had a chance to connect. Doing
  // this only at load+400 ms leaves a window in which a connect could still go
  // through the shared hub. Idempotent, and repeated below as a backstop.
  if (IS_WCB) {
    document.addEventListener('DOMContentLoaded', () => {
      disableWcbPortSharing();
      // Started HERE rather than in the load handler below: the splash goes up
      // during the tool's init, so waiting for load+400ms left it on screen for
      // ~2 s -- measured -- which is a flash of a modal answering itself. The
      // poller catches it within 150 ms of it appearing instead.
      dismissWcbSplash();
    }, { once: true });
  }

  // After load, so the tool has defined its functions and wired its UI. The delay
  // is for its own connect-modal setup, not the socket.
  window.addEventListener('load', () => setTimeout(() => {
    if (IS_WCB) {
      disableWcbPortSharing();   // BEFORE any connect — it wraps the connect path
      dismissWcbSplash();        // backstop; normally already running from DOMContentLoaded
      retireBranchToast();
      installWcbFlash();
      autoConnectWcb();
    } else {
      wireNativeFlash();
      autoConnect();
    }
  }, 400), { once: true });
  // The NaviCore tool rewrites its flash buttons whenever the transport changes,
  // so re-assert after any connect settles rather than only once at load. The
  // Wizard needs no equivalent: replacing flashFirmware() is a one-time swap that
  // nothing in the page overwrites.
  if (!IS_WCB) setInterval(wireNativeFlash, 3000);
})();
