// Does an open tool page reload itself onto a newer copy of its tool -- and only when it has nothing to lose?
//
//   node tools/smoke_tools_reload.js            # the shipped shim
//   node tools/smoke_tools_reload.js <path>     # any other copy
//
// No hardware, no browser. Extracts the shim's reload section (pageBusy, onToolsStatus) from intellex_shim.js rather
// than restating it, the way smoke_ota_label.js does, and runs it against a fake page: each tool's state the way the
// shim reads it (bare names in the tools' global scope), /_api/status answers the way host.py sends them.
//
// WHY IT EXISTS
// 2026-10-26, Greg: "It doesn't make sense to have to manually refresh the page if we update them." The host fetches a
// newer published tool by itself (host.py _auto_update_tools_loop) and /_api/status says which copy each tool is
// served from (`tools`). A page whose copy that no longer is reloads itself once nothing would be lost ("reload when
// safe"), and says what it waits for meanwhile. Asserted:
//   - the first answer is the copy the page loaded; the same token again does nothing;
//   - a new token on an idle page reloads it; a status from a host with no `tools` never does;
//   - each thing a reload would lose holds it, and says so: an open window, a key pressed in the last 15 s, a flash;
//     the config tool's unsaved changes and a save in flight; the Wizard's unpushed general settings or board, and a
//     pull, push or OTA in flight -- and it reloads as soon as the last of them is gone;
//   - a tool whose state cannot be read (a renamed variable) counts as busy, never as safe.
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const shimPath = process.argv[2] || path.join(__dirname, '..', 'src', 'intellex_shim.js');
const src = fs.readFileSync(shimPath, 'utf8');
const start = src.indexOf('  const TYPING_QUIET_MS = 15000;');
const end = start < 0 ? -1 : src.indexOf("    const tools = new BroadcastChannel('intellex');", start);
if (start < 0 || end < 0) {
  console.error('FAIL: could not extract the reload section from ' + shimPath);
  process.exitCode = 1;
  return;
}
// Up to the `try {` that opens the BroadcastChannel block.
const body = src.slice(start, src.lastIndexOf('  try {', end));

let failures = 0;
function check(ok, what) {
  console.log((ok ? 'ok    ' : 'FAIL  ') + what);
  if (!ok) failures++;
}

// A fake page: what the section reads (document, location, Date, the tool's globals) and what it calls (updateNotice).
function page(isWcb, globals = {}) {
  const listeners = {};
  const state = { reloads: 0, notices: [], modal: false, badge: false, now: 1_000_000 };
  const ctx = {
    IS_WCB: isWcb,
    TOOL_KEY: isWcb ? 'wcb' : 'navicore',
    TOOL_LABEL: isWcb ? 'WCB Wizard' : 'config tool',
    Date: { now: () => state.now },
    location: { reload: () => { state.reloads++; } },
    document: {
      addEventListener: (type, fn) => { listeners[type] = fn; },
      querySelector: (sel) => {
        if (sel === '.modal-overlay.open') return state.modal ? {} : null;
        if (sel.includes('-unsaved-badge')) return state.badge ? {} : null;
        return null;
      },
    },
    updateNotice: (text, tail) => { state.notices.push(text + tail); },
    ...globals,
  };
  vm.createContext(ctx);
  vm.runInContext(body + '\n;globalThis.__on = onToolsStatus; globalThis.__busy = pageBusy;', ctx);
  return { ctx, state, key: () => listeners.keydown && listeners.keydown(), on: (st) => ctx.__on(st) };
}
const st = (tok, extra = {}) => ({ tools: { navicore: tok, wcb: tok }, ...extra });
const last = (p) => p.state.notices[p.state.notices.length - 1] || '';

// ── The config tool ─────────────────────────────────────────────────────────
{
  const g = { _configUnsaved: () => g.unsaved, _bridgeUploadInFlight: false, _bulkActive: null, unsaved: false };
  const p = page(false, g);
  p.on(st('A'));
  p.on(st('A'));
  check(p.state.reloads === 0 && !p.state.notices.length, 'NC the copy it loaded, twice: nothing');
  p.on({ attached: true });
  check(p.state.reloads === 0, 'NC a host that sends no tools never reloads it');
  // Each thing a reload would lose holds it, and the notice says what it waits for.
  p.state.modal = true;
  p.on(st('B'));
  check(p.state.reloads === 0 && /once its open window is closed/.test(last(p)), `NC an open window holds it: ${last(p)}`);
  p.state.modal = false;
  g.unsaved = true;
  p.ctx._configUnsaved = () => true;
  p.on(st('B'));
  check(p.state.reloads === 0 && /once its changes are saved to the board/.test(last(p)), `NC unsaved changes hold it: ${last(p)}`);
  p.ctx._configUnsaved = () => false;
  p.ctx._bulkActive = { sid: 1 };
  p.on(st('B'));
  check(p.state.reloads === 0 && /once the save finishes/.test(last(p)), `NC a library upload holds it: ${last(p)}`);
  p.ctx._bulkActive = null;
  p.ctx._bridgeUploadInFlight = true;
  p.on(st('B'));
  check(p.state.reloads === 0 && /once the save finishes/.test(last(p)), 'NC a bridged save holds it');
  p.ctx._bridgeUploadInFlight = false;
  p.key();
  p.on(st('B'));
  check(p.state.reloads === 0 && /once you stop typing/.test(last(p)), `NC a key just pressed holds it: ${last(p)}`);
  p.state.now += 16_000;
  p.on(st('B', { flashing: true }));
  check(p.state.reloads === 0 && /once the flash finishes/.test(last(p)), `NC a flash holds it: ${last(p)}`);
  check(/A new version of the config tool has been downloaded/.test(last(p)) && /or Reload now/.test(last(p)),
        `NC the notice names the tool and offers Reload: ${last(p)}`);
  p.on(st('B'));
  check(p.state.reloads === 1, 'NC nothing left to lose: it reloads');
}
{
  // A tool whose state cannot be read: busy, never safe.
  const p = page(false, { _configUnsaved: () => false });   // no _bridgeUploadInFlight / _bulkActive at all
  p.on(st('A'));
  p.on(st('B'));
  check(p.state.reloads === 0 && /once it can tell nothing is unsaved/.test(last(p)), `NC unreadable state holds it: ${last(p)}`);
}

// ── The Wizard ──────────────────────────────────────────────────────────────
{
  const sets = () => ({ _pullingBoards: new Set(), _pushingBoards: new Set(), _otaInProgress: new Set(),
                        _boardPullInFlight: new Set(), _activePulls: new Map() });
  const p = page(true, { generalSettingsDirty: false, ...sets() });
  p.on(st('A'));
  p.ctx.generalSettingsDirty = true;
  p.on(st('B'));
  check(p.state.reloads === 0 && /once its changes are pushed/.test(last(p)) && /WCB Wizard/.test(last(p)),
        `WZ unpushed general settings hold it: ${last(p)}`);
  p.ctx.generalSettingsDirty = false;
  p.state.badge = true;
  p.on(st('B'));
  check(p.state.reloads === 0 && /once its changes are pushed/.test(last(p)), "WZ a board's unsaved badge holds it");
  p.state.badge = false;
  for (const name of ['_pullingBoards', '_pushingBoards', '_otaInProgress', '_boardPullInFlight']) {
    p.ctx[name].add(3);
    p.on(st('B'));
    check(p.state.reloads === 0 && /once its pull, push or update finishes/.test(last(p)), `WZ ${name} holds it`);
    p.ctx[name].clear();
  }
  p.ctx._activePulls.set(3, {});
  p.on(st('B'));
  check(p.state.reloads === 0, 'WZ an active pull holds it');
  p.ctx._activePulls.clear();
  p.on(st('B'));
  check(p.state.reloads === 1, 'WZ nothing unpushed and nothing in flight: it reloads');
}
{
  const p = page(true, { generalSettingsDirty: false });     // the in-flight sets renamed away
  p.on(st('A'));
  p.on(st('B'));
  check(p.state.reloads === 0 && /once it can tell nothing is unpushed/.test(last(p)), `WZ unreadable state holds it: ${last(p)}`);
}

console.log();
console.log(failures ? `FAILED: ${failures} check(s)` : 'PASSED');
process.exitCode = failures ? 1 : 0;
