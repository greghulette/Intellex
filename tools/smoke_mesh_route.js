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
let hangOn = null;   // board number whose pull never calls back
// On globalThis, not module scope: `new Function` bodies resolve free identifiers
// against the GLOBAL scope, which is also how they resolve in the page.

function makeScope() {
  const RELAY_POLL_MS = 1, RELAY_WATCH_MS = 60;
  const relayCardSlot = () => (relayCardPresent ? 19 : null);
  const window = {
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
  return new Function('RELAY_POLL_MS','RELAY_WATCH_MS','relayCardSlot','window','document','console',
    body + '; return routeMeshThroughBoard;')
    (RELAY_POLL_MS, RELAY_WATCH_MS, relayCardSlot, window, document,
     {info(){}, warn(){}});
}

// The extracted body reads boardConnections & co. as FREE identifiers, exactly as
// it does in the page. `new Function` bodies resolve free identifiers against the
// GLOBAL scope, so the globalThis assignments below stand in for app.js globals.

function reset() {
  pulls = []; armed = []; concurrent = 0; maxConcurrent = 0;
  globalThis.boardConnections = { 21: { isConnected: () => true } };  // the doorway WCB
  globalThis.boardBaselines = { 21: {} };                             // its own config is pulled
  globalThis.remoteRelayForBoard = {};
  globalThis._pullingBoards = new Set();
  globalThis._meshBoards = new Set([21, 1, 2]);                       // sweep heard two others
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

  console.log(failed ? `\nmesh-route: ${failed} FAILURE(S)` : '\nmesh-route: OK');
  process.exitCode = failed ? 1 : 0;
})();
