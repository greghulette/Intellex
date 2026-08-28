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
//  Flashing. esptool-js drives a real port hard: it polls readable.locked /
//  writable.locked in waitForUnlock(), cancels readers mid-stream, and needs
//  setSignals() to toggle DTR/RTS in a timing-sensitive reset dance. Faking that
//  well enough to flash over a WebSocket is a bad trade when the native host can
//  already run esptool directly. setSignals() below is therefore a real call to
//  the host, not a lie -- but flashing is not wired up, and the tool's flash
//  buttons should stay disabled in the app.
// =============================================================================
(() => {
  'use strict';

  if (window.__navilinkShimInstalled) return;
  window.__navilinkShimInstalled = true;

  const LINK_URL = (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + '/_link';

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
            try { controller.close(); } catch (_) {}
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

      this.writable = new WritableStream({
        write(chunk) {
          if (ws.readyState !== WebSocket.OPEN) {
            const e = new Error('The device has been lost.');
            e.name = 'NetworkError';
            throw e;   // a failing write is the tool's ONLY liveness proof
          }
          ws.send(chunk instanceof Uint8Array ? chunk : new Uint8Array(chunk));
        },
        close() { try { ws.close(); } catch (_) {} },
        abort() { try { ws.close(); } catch (_) {} },
      });
    }

    async close() {
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

  // ── Disable esptool flashing ──────────────────────────────────────────────
  // Flashing genuinely CANNOT work through this shim, and letting it try wastes
  // minutes on a sync that can never succeed.
  //
  // esptool-js drives a real port: it toggles DTR/RTS in a timing-sensitive reset
  // dance to enter the bootloader, polls readable.locked / writable.locked, and
  // cancels readers mid-stream. Over a WebSocket there are no control lines at all
  // (set_signals is a documented no-op on that transport), so the board never
  // enters download mode and esptool sits printing "... ___ ..." forever.
  //
  // Not a gap to close later either: the native host runs esptool directly, the
  // same Python package ESP-Flasher-Companion already ships. Doing it through the
  // browser would be strictly worse. So disable the buttons and say why, rather
  // than leaving a trap that looks like a hardware fault.
  function disableFlashButtons() {
    const ids = ['btn-fw-flash', 'btn-fw-wipe'];
    const why = 'Flashing is not available through the NaviLink app — '
              + 'use the web tool over USB, or ESP-Flasher-Companion.';
    for (const id of ids) {
      const b = document.getElementById(id);
      if (!b) continue;
      b.disabled = true;
      b.title = why;
    }
    // OTA is different: it is just commands on the line, so it works over either
    // transport. Left enabled deliberately.
  }

  // After load, so the tool has defined its functions and wired its UI. The delay
  // is for its own connect-modal setup, not the socket.
  window.addEventListener('load', () => setTimeout(() => {
    disableFlashButtons();
    autoConnect();
  }, 400), { once: true });
  // The tool re-enables these whenever the transport changes, so re-assert after
  // any connect settles rather than only once at load.
  setInterval(disableFlashButtons, 3000);
})();
