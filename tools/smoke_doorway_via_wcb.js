// Does the shim put the NaviCore tool into Via WCB when Intellex is attached to
// a doorway -- and ONLY then?
//
//   node tools/smoke_doorway_via_wcb.js            # the shipped shim
//   node tools/smoke_doorway_via_wcb.js <path>     # any other copy, e.g. a pre-fix one
//
// No hardware, no browser, no droid.
//
// THE BUG THIS GUARDS
// The tool decides direct-versus-bridged from whether a PING draws a PONG, and
// it accepts ANY PONG. Through a WCB doorway the NaviCore's PONG is mirrored
// straight back, so the tool believed it was wired to the droid and left
// "Update over USB (OTA)" enabled. That button sends ?OTALOCAL,BEGIN -- a '?'
// command the WCB runs itself -- so it aimed a NaviCore firmware image at the
// WCB. Seen on hardware as [OTA:BEGIN,ERR,0]: the WCB's chip-family brick guard
// refused it. An ESP32-S3 WCB shares the image's family and would not have.
// There is no manual escape any more (the tool's Via-WCB checkbox was removed),
// so the shim has to make the switch.
//
// It EXTRACTS autoConnect() from intellex_shim.js rather than restating it, the
// same way smoke_mesh_route.js does, so the test cannot drift from what ships.
//
// ORDER IS ASSERTED, not merely "was it called". openPortAndStart() forces
// viaWcbActive = false at its very start, so a switch made BEFORE connectDirect()
// finishes is silently undone by the handshake it was meant to correct.
const fs = require('fs');
const path = require('path');

const shimPath = process.argv[2] || path.join(__dirname, '..', 'src', 'intellex_shim.js');
const src = fs.readFileSync(shimPath, 'utf8');

const start = src.indexOf('  async function autoConnect() {');
// The next section's heading, found by its text rather than its box-drawing rule
// so this file stays ASCII -- then back to the start of that comment's line.
const next = src.indexOf("GitHub firmware calls, answered locally", start);
const end = next < 0 ? -1 : src.lastIndexOf('\n', next);
if (start < 0 || end < 0) {
  console.error('FAIL: could not extract autoConnect() from ' + shimPath);
  process.exitCode = 1;
  return;
}
const body = src.slice(start, end);

function run(st, win) {
  const seq = [];
  const fakeWindow = {};
  if (win.connectDirect) {
    fakeWindow.connectDirect = async () => {
      seq.push('connect:start');
      await new Promise(r => setTimeout(r, 5));   // the handshake takes real time
      seq.push('connect:end');
    };
  }
  if (win.onViaWcbToggle) fakeWindow.onViaWcbToggle = v => seq.push('toggle:' + v);
  const fakeFetch = async () => ({ json: async () => st });
  const quiet = { info() {}, warn() {} };
  // eslint-disable-next-line no-new-func
  const autoConnect = new Function('window', 'console', 'fetch',
    body + '\nreturn autoConnect;')(fakeWindow, quiet, fakeFetch);
  return autoConnect().then(() => seq);
}

const BOTH = { connectDirect: 1, onViaWcbToggle: 1 };
const cases = [
  { name: 'WCB doorway switches the tool to Via WCB, after the handshake',
    st: { attached: true, role: 'wcb', target: 'WCB 192.168.4.1 -> mesh' },
    win: BOTH, want: ['connect:start', 'connect:end', 'toggle:true'] },
  { name: 'relay doorway switches too',
    st: { attached: true, role: 'relay' },
    win: BOTH, want: ['connect:start', 'connect:end', 'toggle:true'] },
  { name: "NaviCore's own access point stays direct",
    st: { attached: true, role: 'navicore' },
    win: BOTH, want: ['connect:start', 'connect:end'] },
  { name: 'a serial attach (no role) stays direct',
    st: { attached: true, role: '' },
    win: BOTH, want: ['connect:start', 'connect:end'] },
  { name: 'nothing attached: no connect and no switch',
    st: { attached: false, role: 'wcb' },
    win: BOTH, want: [] },
  { name: 'the Wizard page (neither function) does not throw',
    st: { attached: true, role: 'wcb' },
    win: {}, want: [] },
  { name: 'a tool without onViaWcbToggle still connects without throwing',
    st: { attached: true, role: 'wcb' },
    win: { connectDirect: 1 }, want: ['connect:start', 'connect:end'] },
];

(async () => {
  let bad = 0;
  for (const c of cases) {
    let got = null, err = null;
    try { got = await run(c.st, c.win); } catch (e) { err = e; }
    const ok = !err && JSON.stringify(got) === JSON.stringify(c.want);
    if (!ok) bad++;
    console.log('  ' + (ok ? 'PASS' : 'FAIL') + '  ' + c.name +
      (ok ? '' : err ? '  threw: ' + err.message
                     : '  got ' + JSON.stringify(got) + '  want ' + JSON.stringify(c.want)));
  }
  console.log('\ndoorway-via-wcb: ' + (bad ? bad + ' FAILURE(S)' : 'OK'));
  process.exitCode = bad ? 1 : 0;
})();
