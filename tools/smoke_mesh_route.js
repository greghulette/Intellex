// Exercise routeMeshThroughBoard()'s REAL source text against fakes.
//
//   node tools/smoke_mesh_route.js       # no hardware, no browser, no droid
//
// The function auto-pulls the configs of mesh boards sitting behind a doorway
// that is an ORDINARY WCB -- the case where the Wizard renders no relay card, so
// the relay path cannot help. It runs unattended in the background, which is
// what makes it worth testing: every way it can go wrong is silent.
//
// It EXTRACTS the function from navilink_shim.js rather than restating it, so
// the test cannot quietly drift from what actually ships. Only one constant is
// rewritten -- the 45 s watchdog, shrunk to 40 ms so it is testable -- and the
// race, the cleanup and the reporting around it are the shipped text.
//
// The last case is the one that earns its keep: a pull that resolves but never
// calls onComplete. That is not hypothetical, it is the hang this test hit on
// its own first run, and without the watchdog it parks the loop forever on the
// first board and strands every board behind it. Set PULL_WATCHDOG_MS high and
// this file stops terminating at all -- which is precisely the bug.
const fs = require('fs');
const shimPath = require('path').join(__dirname, '..', 'src', 'navilink_shim.js');
const src = fs.readFileSync(shimPath, 'utf8');

const start = src.indexOf('  const _meshRoutedOnce = new Set();');
const endMark = '\n  }\n\n  // ── Native flashing for the Wizard';
const end = src.indexOf(endMark, start);
if (start < 0 || end < 0) { console.error('FAIL: could not extract the function'); process.exitCode = 1; return; }
let body = src.slice(start, end + 4);
// Shrink ONLY the watchdog constant so a 45 s guard is testable in milliseconds.
// The race, the clearTimeout and the reporting are the shipped text.
body = body.replace('const PULL_WATCHDOG_MS = 45000;', 'const PULL_WATCHDOG_MS = 40;');

let pulls = [], armed = [], concurrent = 0, maxConcurrent = 0;
let relayCardPresent = false;
let hangOn = null;    // board number whose pull never calls back
let wdpNodes = null;  // the sweep's parsed nodes, or null for "no sweep yet"
// On globalThis, not module scope: `new Function` bodies resolve free identifiers
// against the GLOBAL scope, which is also how they resolve in the page.

function makeScope() {
  const RELAY_POLL_MS = 1, RELAY_WATCH_MS = 60;
  const relayCardSlot = () => (relayCardPresent ? 19 : null);
  const window = {
    // The sweep calls this on every tick with its parsed nodes; the shim wraps it
    // to learn which of them are CLIENTS. Fired once here to stand in for a sweep.
    renderWdpMesh: () => {},
    _wdpMeshConn: () => (globalThis.boardConnections[21]?.isConnected() ? {slot:'21', conn:{}, fc:'?', wcbNum:21} : null),
    setRemoteConnected: (n, p) => { armed.push([n, p]); globalThis.remoteRelayForBoard[n] = p; },
    // NB the 5th arg: the Wizard signals completion through onComplete, NOT by
    // resolving. relayRouteAll relies on the same thing.
    remoteBoardPull: async (parent, n, attempt, max, onComplete) => {
      concurrent++; maxConcurrent = Math.max(maxConcurrent, concurrent);
      pulls.push([parent, n]);
      globalThis._pullingBoards.add(n);
      await new Promise(r => setTimeout(r, 5));
      globalThis._pullingBoards.delete(n); globalThis.boardBaselines[n] = {pulled:true};
      concurrent--;
      if (n !== hangOn) onComplete?.(true);   // hangOn: resolve, but never report
      return true;
    },
  };
  const document = { querySelectorAll: () => [] };
  // eslint-disable-next-line no-new-func
  const made = new Function('RELAY_POLL_MS','RELAY_WATCH_MS','relayCardSlot','window','document','console',
    body + '; return {run: routeMeshThroughBoard, watch: watchWdpSweeps};')
    (RELAY_POLL_MS, RELAY_WATCH_MS, relayCardSlot, window, document,
     {info(){}, warn(){}});
  // Deliver a sweep the way the tool does — through the function the shim wrapped.
  return async () => {
    made.watch();
    if (wdpNodes) window.renderWdpMesh(wdpNodes, 21, {});
    return made.run();
  };
}

// The extracted body reads boardConnections & co. as FREE identifiers, exactly as
// it does in the page. `new Function` bodies resolve free identifiers against the
// GLOBAL scope, so the globalThis assignments below stand in for app.js globals.

// The mesh this test models, and the one that produced the bug: a doorway WCB at
// 21, two real boards (1 Body, 2 Dome), and TWO CLIENTS — a MgmtRelay at 19 and a
// NaviCore at 20. Clients are WCB_Client hosts with no WCB config to pull, and the
// Wizard gives them a lightweight client card, never a board config.
function reset() {
  pulls = []; armed = []; concurrent = 0; maxConcurrent = 0;
  globalThis.boardConnections = { 21: { isConnected: () => true } };  // the doorway WCB
  globalThis.boardBaselines = { 21: {} };                             // its own config is pulled
  globalThis.remoteRelayForBoard = {};
  globalThis._pullingBoards = new Set();
  // _meshBoards is "has a numbered section", NOT "is a board": upsertClientCard
  // calls addDiscoveredBoards for clients too, so 19 and 20 sit in here as well.
  // That conflation is exactly what made this pull a relay and a NaviCore.
  globalThis._meshBoards = new Set([21, 1, 2, 19, 20]);
  globalThis._meshClients = new Map([[19, {}], [20, {}]]);
  globalThis.boardConfigs = { 19: {type:'client'}, 20: {type:'client'} };
  wdpNodes = [
    { n: 21, client: false, alias: 'Doorway' },   // the board we are attached to
    { n: 1,  client: false, alias: 'Body'    },
    { n: 2,  client: false, alias: 'Dome'    },
    { n: 19, client: true,  alias: 'Mgmt Relay' },
    { n: 20, client: true,  alias: 'NaviCore'   },
  ];
  relayCardPresent = false;
}

let failed = 0;
const check = (name, cond, extra='') => {
  console.log(`  ${cond ? 'PASS' : 'FAIL'}  ${name}${cond ? '' : '  ' + extra}`);
  if (!cond) failed++;
};

(async () => {
  console.log('routeMeshThroughBoard');

  // 1 — plain WCB doorway: both mesh boards armed + pulled, one at a time.
  reset();
  await makeScope()();
  check('armed both mesh boards through the doorway',
        JSON.stringify(armed) === JSON.stringify([[1,'21'],[2,'21']]), JSON.stringify(armed));
  check('pulled both, through slot 21',
        JSON.stringify(pulls) === JSON.stringify([['21',1],['21',2]]), JSON.stringify(pulls));
  check('pulls were SEQUENTIAL (never two in flight)', maxConcurrent === 1, `max=${maxConcurrent}`);
  check('did not pull the doorway itself', !pulls.some(p => p[1] === 21));

  // 2 — a relay card exists: stand down entirely, the relay path owns it.
  reset(); relayCardPresent = true;
  await makeScope()();
  check('stood down when a relay card is present', pulls.length === 0, JSON.stringify(pulls));

  // 3 — a board already pulled is not pulled again.
  reset(); globalThis.boardBaselines[1] = {pulled:true};
  await makeScope()();
  check('skipped the board that already had a baseline',
        JSON.stringify(pulls) === JSON.stringify([['21',2]]), JSON.stringify(pulls));

  // 4 — a directly-cabled board is left alone.
  reset(); globalThis.boardConnections[2] = { isConnected: () => true };
  await makeScope()();
  check('left the directly-connected board alone',
        JSON.stringify(pulls) === JSON.stringify([['21',1]]), JSON.stringify(pulls));

  // 5 — nothing connected yet: no crash, no pulls.
  reset(); globalThis.boardConnections = {};
  await makeScope()();
  check('no doorway connected → did nothing', pulls.length === 0);

  // 6 — a pull that NEVER calls onComplete must not park the loop forever.
  //     Not hypothetical: this is the exact hang this test hit on its first run,
  //     and without the watchdog it strands every board behind the silent one.
  reset();
  hangOn = 1;                        // WCB1's pull resolves but never reports
  await makeScope()();
  check('a silent pull did not strand the board behind it',
        pulls.some(p => p[1] === 2), JSON.stringify(pulls));
  hangOn = null;

  // 7 — CLIENTS ARE NOT BOARDS. Reported from hardware: a MgmtRelay at 19 and a
  //     NaviCore at 20 were both pulled, because _meshBoards holds clients too.
  //     They have no WCB config; the pull can only fail.
  reset();
  await makeScope()();
  check('did not pull the MgmtRelay client at 19', !pulls.some(p => p[1] === 19),
        JSON.stringify(pulls));
  check('did not pull the NaviCore client at 20', !pulls.some(p => p[1] === 20),
        JSON.stringify(pulls));
  check('still pulled both REAL boards',
        JSON.stringify(pulls) === JSON.stringify([['21',1],['21',2]]), JSON.stringify(pulls));

  // 8 — same, before any sweep has been captured: the fallback must subtract
  //     clients too, or the first poll pulls them in the window before a sweep.
  reset(); wdpNodes = null;
  await makeScope()();
  check('no sweep yet → fallback still excluded both clients',
        !pulls.some(p => p[1] === 19 || p[1] === 20), JSON.stringify(pulls));
  check('no sweep yet → still pulled the real boards',
        JSON.stringify(pulls) === JSON.stringify([['21',1],['21',2]]), JSON.stringify(pulls));

  console.log(failed ? `\nmesh-route: ${failed} FAILURE(S)` : '\nmesh-route: OK');
  process.exitCode = failed ? 1 : 0;
})();
