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

  navigator.serial = {
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

  console.info('[NaviLink] navigator.serial is backed by', LINK_URL);
})();
