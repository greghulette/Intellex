#!/usr/bin/env python3
"""Does discover.probe() say what a host IS, or merely what answered?

    python tools/smoke_probe_identity.py

No hardware. Replays recorded console streams through the REAL probe() by
swapping in a fake websockets client, so the decision logic under test is the one
that ships.

THE BUG THIS GUARDS
A WCB hosting the AP alternated between "Body, a WCB" and "NaviCore" on
successive scans of the same address. Both endpoints are CONSOLE MIRRORS, and a
WCB mirrors the whole mesh -- so a PING sent to the WCB reached the NaviCore
behind it over ESP-NOW and the NaviCore's PONG came back through the mirror.
probe() returned on the first PONG it saw, so whether that PONG landed inside the
first read window decided the answer. A race, on hardware that had not changed.

WHAT MAKES THE TWO CASES TELLABLE APART
NaviCore has two PONG paths and only one of them is the host speaking for itself:

    direct   NaviCore.ino ~3828    {"type":"PONG","version":...}         no id
    mesh     rc_telemetry.h ~2189  {"sys":1,"type":"PONG","id":N,...}    has id

So an id-bearing PONG counts only when its id IS the host's own WDP SELF row.
CASE_WCB_PONG below is a verbatim transcript from the bench -- the PONG says
id=20 while the SELF row says N=1, which is the whole proof.
"""
from __future__ import annotations

import pathlib
import sys
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))


# ── Recorded streams ────────────────────────────────────────────────────────
# The mesh chatter is kept in: a real probe sifts the answer out of telemetry
# that never stops, and dropping it would test a quiet line that does not exist.
_RC_CH = ('{"sys":1,"type":"rc_ch","id":20,"ch":[992,992,992,992,992,992,992,'
          '173,173,173,173,173,173,173,173,173,173,992,992,992,992,992,992,992]}')
_RC_HB = ('{"sys":1,"type":"rc_hb","id":20,"fw":"v0.2.0_030003QSEP26","up":14332,'
          '"mode":1,"model":2,"sbusFps":112,"sbusAge":8}')

_WCB_DUMP = [
    "[WDP:N=1,CLIENT=0,ALIAS=Body,HW=1,HWREV=,FW=6.2.1_071610RSEP2026,CAP=00D0,"
    "CTRL=20,CAPTAGS=,MAESTRO=1,AGE=0,SEEN=1,PEER=3]",
    "[WDPIF:N=1,S=1,DEV=Maestro 1]",
    "[WDP:N=2,CLIENT=0,ALIAS=Dome,HW=24,HWREV=,FW=6.2.1_071610RSEP2026,CAP=00D4,"
    "CTRL=20,CAPTAGS=,MAESTRO=2,AGE=58,SEEN=1,PEER=1]",
    "[WDP:N=19,CLIENT=1,ALIAS=Mgmt Relay,HW=0,HWREV=,FW=1.2,CAP=0000,CTRL=0,"
    "CAPTAGS=,MAESTRO=-,AGE=12,SEEN=1,PEER=4]",
    "[WDP:N=20,CLIENT=1,ALIAS=NaviCore,HW=0,HWREV=NaviCore v2,FW=v0.2.0_030003QSEP26,"
    "CAP=0000,CTRL=0,CAPTAGS=rc sbus maestro hcr,MAESTRO=-,AGE=56,SEEN=1,PEER=4]",
    "[WDPCFG:EN=1,AUTOJOIN=1,PEERS=4]",
    "[WDP:END,count=3]",
]

# Verbatim from the bench, attempt 4: the mesh PONG (id=20) arrives BEFORE the
# SELF row (N=1) that names the actual host. This is the case that used to
# return "navicore" and stop reading.
CASE_WCB_PONG = [
    '{"sys":1,"type":"PONG","id":20,"version":"v0.2.0_030003QSEP26","model":2,"mode":1}',
    _RC_HB, _RC_CH,
    '{"sys":1,"type":"WCB_STATUS","quantity":1,"self":20,"relay":1,"relayName":"Body"}',
    *_WCB_DUMP,
    _RC_CH,
]

# Same board, same scan, no PONG inside the window -- the other half of the coin
# flip. This one already answered correctly, and must keep doing so.
CASE_WCB_NO_PONG = [_RC_CH, _RC_CH, _RC_HB, *_WCB_DUMP, _RC_CH]

# NaviCore hosting the AP itself, answering a direct host PING. Bare PONG, no id,
# no "sys" marker, and no WDP at all.
CASE_NAVICORE_BARE = ['{"type":"PONG","version":"v0.2.0_030003QSEP26"}']

# NaviCore hosting the AP with WCB_Client aboard, so it ALSO emits a SELF row
# (WCB_Mgmt.h ~205 hardcodes CLIENT=0/PEER=3 for self, byte-compatible with a
# real board). Identity must still come from the direct PONG, not the SELF row.
CASE_NAVICORE_WITH_WDP = [
    '{"type":"PONG","version":"v0.2.0_030003QSEP26"}',
    "[WDP:N=20,CLIENT=0,ALIAS=NaviCore,HW=0,HWREV=,FW=v0.2.0_030003QSEP26,CAP=0000,"
    "CTRL=0,CAPTAGS=,MAESTRO=-,AGE=0,SEEN=1,PEER=3]",
    "[WDP:N=1,CLIENT=0,ALIAS=Body,HW=1,HWREV=,FW=6.2.1_071610RSEP2026,CAP=00D0,"
    "CTRL=20,CAPTAGS=,MAESTRO=1,AGE=0,SEEN=1,PEER=1]",
    "[WDP:END,count=1]",
]

# A MgmtRelay hosting the AP, with a NaviCore behind it. Same trap as the WCB:
# the relayed PONG must not turn the relay into a droid.
CASE_RELAY_PONG = [
    '{"sys":1,"type":"PONG","id":20,"version":"v0.2.0_030003QSEP26","model":2,"mode":1}',
    "[relay] mode=AP ssid=NaviCore",
    "[WDP:N=19,CLIENT=0,ALIAS=Mgmt Relay,HW=32,HWREV=,FW=1.2,CAP=0000,CTRL=0,"
    "CAPTAGS=,MAESTRO=-,AGE=0,SEEN=1,PEER=3]",
    "[WDP:N=20,CLIENT=1,ALIAS=NaviCore,HW=0,HWREV=NaviCore v2,FW=v0.2.0_030003QSEP26,"
    "CAP=0000,CTRL=0,CAPTAGS=,MAESTRO=-,AGE=1,SEEN=1,PEER=4]",
    "[WDP:END,count=1]",
]

# Something on port 80 that is none of the three.
CASE_SILENT: list[str] = []


class _FakeWS:
    """Enough of websockets.sync.client's socket for _read_lines to work.

    Lines are handed over in CHUNKS THAT DO NOT RESPECT LINE BOUNDARIES -- two
    per recv, with the split falling mid-line -- because _read_lines buffers and
    splits on newlines, and a fake that yields one tidy line per recv would never
    exercise that.
    """

    def __init__(self, lines):
        blob = "".join(l + "\n" for l in lines)
        cut = len(blob) // 2
        self._chunks = [blob[:cut], blob[cut:]] if blob else []
        self._i = 0

    def __enter__(self):
        return self

    def __exit__(self, *_exc):
        return False

    def send(self, _data):
        pass

    def recv(self, timeout=None):
        if self._i >= len(self._chunks):
            # A real socket blocks here until the read window expires; raising is
            # how _read_lines is told the stream has gone quiet.
            raise TimeoutError("no more data")
        c = self._chunks[self._i]
        self._i += 1
        return c


def _install_fake(lines):
    """Swap in a websockets client that replays `lines`. probe() imports it at
    call time, so this has to be a module in sys.modules, not an attribute."""
    mod = types.ModuleType("websockets.sync.client")
    mod.connect = lambda *_a, **_k: _FakeWS(lines)
    sys.modules["websockets.sync.client"] = mod


CASES = [
    ("WCB doorway, relayed PONG arrives", CASE_WCB_PONG,
     {"kind": "wcb", "alias": "Body", "relayId": "1"}),
    ("WCB doorway, PONG misses the window", CASE_WCB_NO_PONG,
     {"kind": "wcb", "alias": "Body", "relayId": "1"}),
    ("NaviCore hosting, bare PONG", CASE_NAVICORE_BARE,
     {"kind": "navicore", "version": "v0.2.0_030003QSEP26"}),
    ("NaviCore hosting, own SELF row too", CASE_NAVICORE_WITH_WDP,
     {"kind": "navicore", "version": "v0.2.0_030003QSEP26"}),
    ("relay doorway, relayed PONG arrives", CASE_RELAY_PONG,
     {"kind": "relay", "alias": "Mgmt Relay", "relayId": "19"}),
    ("nothing answers", CASE_SILENT, {"kind": "unknown"}),
]


def main() -> int:
    import discover

    bad = 0
    for name, lines, expect in CASES:
        _install_fake(lines)
        got = discover.probe("192.168.4.1", timeout=0.05)
        for key, want in expect.items():
            ok = got.get(key) == want
            bad += not ok
            print(f"  {'PASS' if ok else 'FAIL'}  {name}: {key}="
                  f"{got.get(key)!r}" + ("" if ok else f"  (expected {want!r})"))

    # The peer list is what the chooser prints as "sees ...", and the SELF row
    # must never appear in it -- a doorway listing itself among its own peers
    # reads as a duplicate board.
    _install_fake(CASE_WCB_PONG)
    peers = discover.probe("192.168.4.1", timeout=0.05)["peers"]
    ids = [p["id"] for p in peers]
    for check, ok in (
        ("SELF (N=1) is not in the peer list", "1" not in ids),
        ("the NaviCore behind it IS a peer", "20" in ids),
        ("peers found", sorted(ids) == ["19", "2", "20"]),
    ):
        bad += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {check}" + ("" if ok else f"  got {ids}"))

    print()
    print("probe-identity: " + ("OK" if not bad else f"{bad} FAILURE(S)"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
