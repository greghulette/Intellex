// When the droid behind the Wizard reboots, does the shim bring the Wizard's link
// back WITHOUT trampling the mesh boards it was managing?
//
//   node tools/smoke_wizard_reconnect.js            # the shipped shim
//   node tools/smoke_wizard_reconnect.js <path>     # any other copy, e.g. a pre-fix one
//
// No hardware, no browser. Extracts autoConnectWcb() and routeMeshThroughRelay()
// from intellex_shim.js -- the section between their `let wcbRole` and the next
// banner -- and runs that real text against fakes. Three timing constants are
// shrunk so it runs in milliseconds; nothing else is restated.
//
// THE BUG THIS GUARDS (hardware, 2026-09-10)
// After an OTA rebooted NaviCore -- the Wizard's relay, filed at slot 20 -- the
// shim reconnected the Wizard by calling _modalDoConnect(1). Slot 1 was mesh board
// WCB1 by then, so that un-managed WCB1 and its pull wiped slot 1's config, while
// a second connection fought the Wizard's own reconnect of slot 20 for the same
// port. Every reconnect also started another relay-routing loop, and those called
// "Manage all" while the relay was still down: "Relay board not connected" over and
// over, "Manage all: 0/1 config(s) pulled via WCB20", nothing recovered until F5.
// And the guard meant to prevent a redundant connect read window.boardConnections,
// which does not exist -- boardConnections is a top-level `let` in app.js.
const fs = require('fs');
const path = require('path');

const shimPath = process.argv[2] || path.join(__dirname, '..', 'src', 'intellex_shim.js');
const src = fs.readFileSync(shimPath, 'utf8');
const start = src.indexOf("  let wcbRole = '', wcbRelayId = null;");
const banner = start < 0 ? -1 : src.indexOf('The same thing again, for a doorway that is an ORDINARY WCB', start);
const end = banner < 0 ? -1 : src.lastIndexOf('\n', banner);
if (start < 0 || end < 0) {
  console.error('FAIL: could not extract autoConnectWcb/routeMeshThroughRelay from ' + shimPath);
  process.exitCode = 1;
  return;
}
let body = src.slice(start, end);
// Shrink the timings. A constant that is missing is reported, not silently skipped:
// the pre-fix shim has no WCB_OWN_RECONNECT_MS, and that is part of what fails.
const shrink = [
  ['const RELAY_POLL_MS  = 2000;', 'const RELAY_POLL_MS  = 2;'],
  ['const RELAY_WATCH_MS = 300000;', 'const RELAY_WATCH_MS = 150;'],
  ['const WCB_OWN_RECONNECT_MS = 30000;', 'const WCB_OWN_RECONNECT_MS = 40;'],
];
for (const [from, to] of shrink) {
  if (body.includes(from)) body = body.replace(from, to);
  else console.log('  note: ' + from + ' not found in this shim');
}

const sleep = ms => new Promise(r => setTimeout(r, ms));
const thePort = { name: 'the shim port' };
const conn = up => ({ port: thePort, up, isConnected() { return this.up; } });

// The extracted text reads boardConnections as a FREE identifier, exactly as it
// does in the page; `new Function` bodies resolve those against the global scope.
// `window` is a separate fake object ON PURPOSE: in the page a top-level `let` is
// not a window property, and a test that put boardConnections on window would hide
// the very bug this guards.
function load(opts = {}) {
  const calls = { connect: [], routeAll: [], info: [] };
  const status = Object.assign(
    { attached: true, role: 'navicore', relayId: null, target: 'ws://192.168.4.1/ws' }, opts.status);
  const card = opts.card || null;          // { slot, unmanaged }
  const window = {
    relayRouteAll: slot => {
      calls.routeAll.push(slot);
      if (card) card.unmanaged = 0;       // what a successful Manage all leaves behind
    },
  };
  if (!opts.noModal) window._modalDoConnect = async (n, port) => { calls.connect.push([n, port === thePort]); };
  const document = {
    getElementById: id => (card && id === 'relay-card-' + card.slot ? { id } : null),
    querySelector: () => (card ? { id: 'relay-card-' + card.slot } : null),
    querySelectorAll: () => new Array(card ? card.unmanaged : 0).fill({}),
  };
  const navigator = { serial: { requestPort: async () => thePort } };
  const fetch = async () => ({ json: async () => status });
  const console_ = { info: (...a) => calls.info.push(a.join(' ')), warn() {} };
  // eslint-disable-next-line no-new-func
  const api = new Function('window', 'document', 'navigator', 'fetch', 'console', 'routeMeshThroughBoard',
    body + '\nreturn { autoConnectWcb, routeMeshThroughRelay,'
         + ' wcbLinkConn: typeof wcbLinkConn === "function" ? wcbLinkConn : null };')(
    window, document, navigator, fetch, console_, () => {});
  return { api, calls, card };
}

const cases = [];
const t = (name, fn) => cases.push({ name, fn });

t('first load, nothing connected: connects slot 1', async () => {
  globalThis.boardConnections = {};
  const { api, calls } = load();
  await api.autoConnectWcb();
  return JSON.stringify(calls.connect) === '[[1,true]]' || calls.connect;
});

t('link already up at its re-filed slot 20: does not connect again', async () => {
  globalThis.boardConnections = { 20: conn(true) };
  const { api, calls } = load();
  await api.autoConnectWcb();
  return calls.connect.length === 0 || calls.connect;
});

t('link down at 20 while the Wizard reconnects it: no connect at all, and never slot 1', async () => {
  // Slot 1 is mesh board WCB1 by now -- managed through the relay, no connection of its own.
  globalThis.boardConnections = { 20: conn(false) };
  const { api, calls } = load();
  await api.autoConnectWcb();
  await api.autoConnectWcb();              // a second watchLink tick inside the window
  return calls.connect.length === 0 || calls.connect;
});

t('still down after the Wizard had its chance: reconnects slot 20, not 1', async () => {
  globalThis.boardConnections = { 20: conn(false) };
  const { api, calls } = load();
  await api.autoConnectWcb();
  await sleep(70);                         // past WCB_OWN_RECONNECT_MS (shrunk to 40)
  await api.autoConnectWcb();
  return JSON.stringify(calls.connect) === '[[20,true]]' || calls.connect;
});

t('the Wizard gave up and deleted the relay connection: reconnects at the remembered slot', async () => {
  globalThis.boardConnections = { 20: conn(true) };
  const { api, calls } = load();
  if (api.wcbLinkConn) api.wcbLinkConn(thePort);   // what watchLink does every 2 s while up
  delete globalThis.boardConnections[20];          // app.js reconnect() give-up branch
  await api.autoConnectWcb();
  return JSON.stringify(calls.connect) === '[[20,true]]' || calls.connect;
});

t('a board cabled on some other port is not mistaken for our link', async () => {
  globalThis.boardConnections = { 3: { port: { name: 'another port' }, isConnected: () => true } };
  const { api, calls } = load();
  await api.autoConnectWcb();
  return JSON.stringify(calls.connect) === '[[1,true]]' || calls.connect;
});

t('host has nothing attached: does nothing', async () => {
  globalThis.boardConnections = {};
  const { api, calls } = load({ status: { attached: false } });
  await api.autoConnectWcb();
  return calls.connect.length === 0 || calls.connect;
});

t('not the Wizard (no _modalDoConnect): does not throw', async () => {
  globalThis.boardConnections = {};
  const { api, calls } = load({ noModal: true });
  await api.autoConnectWcb();
  return calls.connect.length === 0 || calls.connect;
});

t('relay routing waits while the relay itself is down, then routes once it is back', async () => {
  const relay = conn(false);
  globalThis.boardConnections = { 20: relay };
  const { api, calls } = load({ card: { slot: 20, unmanaged: 2 } });
  api.routeMeshThroughRelay();
  await sleep(40);
  const whileDown = calls.routeAll.length;
  relay.up = true;
  await sleep(40);
  return (whileDown === 0 && calls.routeAll.includes(20)) || { whileDown, after: calls.routeAll };
});

t('one relay watcher however many times the link comes back', async () => {
  globalThis.boardConnections = { 20: conn(true) };
  const { api, calls } = load({ card: { slot: 20, unmanaged: 0 } });
  api.routeMeshThroughRelay();
  api.routeMeshThroughRelay();
  api.routeMeshThroughRelay();
  await sleep(40);
  const loops = calls.info.filter(l => l.includes('relay card is up')).length;
  return loops === 1 || { loops };
});

(async () => {
  let bad = 0;
  for (const c of cases) {
    let res;
    try { res = await c.fn(); } catch (e) { res = 'threw ' + e.message; }
    const ok = res === true;
    if (!ok) bad++;
    console.log('  ' + (ok ? 'PASS' : 'FAIL') + '  ' + c.name + (ok ? '' : '  -- ' + JSON.stringify(res)));
  }
  await sleep(200);                        // let every background watcher run out
  console.log('\nwizard-reconnect: ' + (bad ? bad + ' FAILURE(S)' : 'OK') + '  (' + cases.length + ' cases)');
  process.exitCode = bad ? 1 : 0;
})();
