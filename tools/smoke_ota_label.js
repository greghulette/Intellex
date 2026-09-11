// Does the NaviCore tool's direct-OTA button say WiFi when the link IS WiFi --
// and leave everything else on that button alone?
//
//   node tools/smoke_ota_label.js            # the shipped shim
//   node tools/smoke_ota_label.js <path>     # any other copy
//
// No hardware, no browser. Extracts the relabel section from intellex_shim.js
// rather than restating it, the way smoke_doorway_via_wcb.js does, so the test
// cannot drift from what ships.
//
// WHY IT EXISTS
// index.html names the button "Update over USB (OTA)" because in a browser the
// link can only be Web Serial. Through Intellex the same button streams the image
// over a droid's WiFi -- on hardware 2026-09-10, straight to a NaviCore on its own
// access point -- so the shim swaps the wording. Three ways that goes wrong, and
// each is asserted:
//   - the tool writes "Updating over USB..." when a run starts and the USB label
//     back when it ends, so a one-shot swap is undone by the first OTA;
//   - a swap that matched loosely would overwrite text the tool put there itself;
//   - an observer that reacts to its own write never stops.
const fs = require('fs');
const path = require('path');

const shimPath = process.argv[2] || path.join(__dirname, '..', 'src', 'intellex_shim.js');
const src = fs.readFileSync(shimPath, 'utf8');
const start = src.indexOf('  const OTA_WORDS = [');
const end = start < 0 ? -1 : src.indexOf('  const FLASH_POLL_MS = 700;', start);
if (start < 0 || end < 0) {
  console.error('FAIL: could not extract the OTA relabel section from ' + shimPath);
  process.exitCode = 1;
  return;
}
const body = src.slice(start, end);

// The tool's own strings, spelled with escapes so this file stays ASCII.
const BOLT = '\u26A1 ';
const USB_IDLE = BOLT + 'Update over USB (OTA)';
const WIFI_IDLE = BOLT + 'Update over WiFi (OTA)';
const USB_BUSY = BOLT + 'Updating over USB\u2026';
const WIFI_BUSY = BOLT + 'Updating over WiFi\u2026';
const HTML_TITLE = 'OTA update over the existing USB connection (no bootloader mode). '
                 + 'Direct-USB only.';

function load() {
  const observers = [];
  let btn = null;
  const doc = { getElementById: id => (id === 'btn-fw-ota' ? btn : null) };
  class FakeObserver {
    constructor(cb) { this.cb = cb; observers.push(this); }
    observe() {}
  }
  // eslint-disable-next-line no-new-func
  const api = new Function('document', 'MutationObserver',
    body + '\nreturn { otaLabel, relabelOtaButton };')(doc, FakeObserver);
  return {
    api,
    observers,
    // A button that counts writes to its text, so a self-feeding observer shows.
    setButton(text) {
      let t = text;
      let writes = 0;
      btn = {
        title: HTML_TITLE,
        get textContent() { return t; },
        set textContent(v) { t = v; writes++; },
        get writes() { return writes; },
      };
      return btn;
    },
    // What the browser does after any change to the button, the shim's own included.
    fire() { observers.forEach(o => o.cb([])); },
  };
}

const cases = [];
const t = (name, fn) => cases.push({ name, fn });

t('the idle USB label becomes WiFi',
  () => load().api.otaLabel(USB_IDLE, true) === WIFI_IDLE);

t('the in-progress wording becomes WiFi too',
  () => load().api.otaLabel(USB_BUSY, true) === WIFI_BUSY);

t('back on serial, both WiFi wordings return to USB', () => {
  const { api } = load();
  return api.otaLabel(WIFI_IDLE, false) === USB_IDLE && api.otaLabel(WIFI_BUSY, false) === USB_BUSY;
});

t('the whitespace around the label in the HTML is tolerated',
  () => load().api.otaLabel('\n        ' + USB_IDLE + '\n      ', true) === WIFI_IDLE);

t('text the tool did not write is left alone', () => {
  const { api } = load();
  return api.otaLabel('Uploading 42%', true) === null
      && api.otaLabel('Retry: Update over USB (OTA)', true) === null
      && api.otaLabel(USB_IDLE, false) === null
      && api.otaLabel(WIFI_IDLE, true) === null;
});

t('attached over WiFi: the label and the tooltip say WiFi', () => {
  const e = load();
  const b = e.setButton('\n   ' + USB_IDLE + '\n  ');
  e.api.relabelOtaButton('ws');
  return b.textContent === WIFI_IDLE && /WiFi/.test(b.title) && !/USB/.test(b.title)
      && e.observers.length === 1;
});

t('the tool writing USB at the start and end of a run is undone at once', () => {
  const e = load();
  const b = e.setButton(USB_IDLE);
  e.api.relabelOtaButton('ws');
  b.textContent = USB_BUSY;              // otaUpdateOverUsb() starting
  e.fire();
  const busy = b.textContent === WIFI_BUSY;
  b.textContent = USB_IDLE;              // ...and finishing
  e.fire();
  return busy && b.textContent === WIFI_IDLE;
});

t('the observer does not react to its own write', () => {
  const e = load();
  const b = e.setButton(USB_IDLE);
  e.api.relabelOtaButton('ws');
  const n = b.writes;
  e.fire();
  e.fire();
  return b.writes === n;
});

t('switching to serial restores the wording and the original tooltip', () => {
  const e = load();
  const b = e.setButton(USB_IDLE);
  e.api.relabelOtaButton('ws');
  e.api.relabelOtaButton('serial');
  return b.textContent === USB_IDLE && b.title === HTML_TITLE;
});

t('re-asserting on the 3 s poll installs one observer, not one per poll', () => {
  const e = load();
  e.setButton(USB_IDLE);
  for (let i = 0; i < 5; i++) e.api.relabelOtaButton('ws');
  return e.observers.length === 1;
});

t('nothing attached yet: the tool keeps its own wording', () => {
  const e = load();
  const b = e.setButton(USB_IDLE);
  e.api.relabelOtaButton('');
  return b.textContent === USB_IDLE && b.title === HTML_TITLE && b.writes === 0;
});

t('the Wizard page (no such button) does not throw', () => {
  const e = load();
  e.api.relabelOtaButton('ws');
  return e.observers.length === 0;
});

let bad = 0;
for (const c of cases) {
  let ok = false;
  let err = null;
  try { ok = c.fn() === true; } catch (e) { err = e; }
  if (!ok) bad++;
  console.log('  ' + (ok ? 'PASS' : 'FAIL') + '  ' + c.name + (err ? '  threw: ' + err.message : ''));
}
console.log('\nota-label: ' + (bad ? bad + ' FAILURE(S)' : 'OK') + '  (' + cases.length + ' cases)');
process.exitCode = bad ? 1 : 0;
