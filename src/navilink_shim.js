// =============================================================================
//  navilink_shim.js — present the host's byte pipe as a Web Serial port
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

  if (window.__navilinkShimInstalled) return;
  window.__navilinkShimInstalled = true;

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

  class NaviLinkPort {
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
        ws.addEventListener('error', () => reject(new Error('cannot reach the NaviLink host')), { once: true });
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
          let nl;
          while ((nl = pending.indexOf('\n')) >= 0) {
            const line = pending.slice(0, nl + 1);   // keep the newline
            pending = pending.slice(nl + 1);
            ws.send(line);
          }
          // A tail without a newline stays buffered until the rest arrives. The
          // tool always terminates its lines, so this only holds a partial chunk.
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

  const thePort = new NaviLinkPort();
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
    console.error('[NaviLink] could not install the serial shim:', e);
    return;   // real Web Serial stays; the tool still works with a cable
  }
  if (navigator.serial !== shim) {
    console.error('[NaviLink] serial shim did not take effect — the tool will use real Web Serial');
    return;
  }

  console.info('[NaviLink] navigator.serial is backed by', LINK_URL);

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
        console.info('[NaviLink] host has no transport attached — not auto-connecting');
        return;
      }
      if (typeof window.connectDirect !== 'function') {
        console.warn('[NaviLink] connectDirect() not found; click Connect manually');
        return;
      }
      console.info('[NaviLink] auto-connecting to', st.target);
      await window.connectDirect();
    } catch (e) {
      console.warn('[NaviLink] auto-connect skipped:', e && e.message);
    }
  }

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
  // startup. So a NaviLink left running across an update serves a NEW page to an
  // OLD host -- the page POSTs /_api/flash, that route does not exist yet, the
  // static handler takes the request and answers 405, and the user is told "the
  // host refused" when the true answer is "restart NaviLink". That happened, so
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

  const STALE_HOST_MSG = 'This page is newer than the running NaviLink host — '
                       + 'quit and reopen NaviLink to enable flashing.';

  const FLASH_BTNS = [
    ['btn-fw-flash', false, 'Update Firmware',
     'Update via the host\'s native esptool. Saved configuration (NVS) is preserved.'],
    ['btn-fw-wipe',  true,  'Full Wipe & Flash',
     'Full wipe via the host\'s native esptool. ERASES saved configuration (NVS).'],
  ];

  async function wireNativeFlash() {
    const ok = await hostSupportsFlash();
    for (const [id, eraseNvs, label, title] of FLASH_BTNS) {
      const b = document.getElementById(id);
      if (!b) continue;
      if (!ok) {
        // Leave them disabled rather than wired to a route that is not there.
        if (!flashBusy) { b.disabled = true; b.title = STALE_HOST_MSG; }
        continue;
      }
      if (!b.__navilinkWired) {
        b.__navilinkWired = true;
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

  // ── Say which transport is actually in use ────────────────────────────────
  // The tool's status reads "Connected" whatever is behind navigator.serial, so a
  // WiFi session looks identical to a cable. That is not cosmetic: what you do
  // when it stops responding differs completely — check the cable, or check the
  // adapter and whether the AP came back after a reboot.
  //
  // Appended to the tool's own text rather than replacing it, and re-applied on a
  // timer because the tool rewrites that span on every state change. Guarded by a
  // marker so it cannot stack up.
  const MARK = ' · ';
  let lastTarget = '';

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
    const el = document.getElementById('status-text');
    if (!el) return;
    const label = labelFor(lastTarget);
    const base = el.textContent.split(MARK)[0];
    // Only annotate once the tool itself says it is connected — decorating
    // "Disconnected" or "Waiting for board…" would be actively misleading.
    const want = (label && /connect/i.test(base) && !/disconnect/i.test(base))
      ? base + MARK + label + ' ▾'
      : base;
    if (el.textContent !== want) el.textContent = want;

    // THE WAY BACK. The window has no browser chrome, so once you have picked a
    // transport there is otherwise no route to the chooser at all — you would have
    // to close and relaunch the app to switch from USB to WiFi. Make the label you
    // are already reading the control: it is the one thing on screen that names
    // the current connection, so it is where you look when you want to change it.
    if (!el.dataset.navilinkClick) {
      el.dataset.navilinkClick = '1';
      el.style.cursor = 'pointer';
      el.title = 'Change connection (USB / WiFi)';
      el.addEventListener('click', () => { location.href = '/_launcher'; });
    }
  }

  setInterval(() => { refreshTarget().then(annotateStatus); }, 2000);

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
    let b = document.getElementById('navilink-banner');
    if (!html) { if (b) b.remove(); return; }
    if (!b) {
      b = document.createElement('div');
      b.id = 'navilink-banner';
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
      console.info('[NaviLink] host is attached but the page is not — reconnecting');
      // KEEP RETRYING. The tool's own auto-reconnect is deliberately one-shot and
      // disarms after a single failure, which is the right call for real hardware
      // (it must never grab the wrong serial device). Here the target is not
      // ambiguous -- there is exactly one host and it has already told us it is
      // attached -- so retrying is safe, and necessary: the first attempt usually
      // lands while the droid is still rebooting and sees no PONG.
      autoConnect();
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

  // After load, so the tool has defined its functions and wired its UI. The delay
  // is for its own connect-modal setup, not the socket.
  window.addEventListener('load', () => setTimeout(() => {
    wireNativeFlash();
    autoConnect();
  }, 400), { once: true });
  // The tool rewrites these buttons whenever the transport changes, so re-assert
  // after any connect settles rather than only once at load.
  setInterval(wireNativeFlash, 3000);
})();
