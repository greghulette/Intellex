#!/usr/bin/env python3
"""Moved to ANOTHER board's access point while attached: does the host still say
what it is attached to?

    python tools/smoke_reconnect_identity.py              # the shipped src/
    python tools/smoke_reconnect_identity.py <src dir>    # any other copy, e.g. pre-fix

No hardware, no network, no netsh. The three things the OS gets asked -- the
route, the SSID, and what answers -- are faked, and so is the Bridge. The REAL
reconnect_loop and _reidentify_if_moved run against them.

THE BUG THIS GUARDS
Every access point here is 192.168.4.1, NaviCore's and each WCB's alike. The
reconnect loop reattached by address and kept the role from the original attach,
so moving the laptop from WCB1's network to WCB2's, or to NaviCore's own,
silently attached to a different board under the old description. Since e7d5824
the shim trusts that role to force the NaviCore tool into Via WCB, so a hop from a
WCB to NaviCore pushed a direct link into bridge mode.

ORDER IS ASSERTED, not just the outcome. The page re-reads the role the moment
the host reports attached, so a correction applied after the attach races it.

Also covers wifi_bounce()'s failure message, which advised saving a profile that
was already saved while the adapter sat on a different board's network.

And a board that wants its ACCESS TOKEN (src/wsauth.py): moved onto one, the loop
holds and asks for that board's token rather than attaching or guessing; while it
holds it asks the OS nothing and opens nothing, until a token is saved or
AUTH_RECHECK_S has passed. The login itself is tools/smoke_ws_auth.py's.

And an attach on its way up is not a lost link: while one is inside open() the loop
asks nothing and attaches nothing, and its own reattach claims the link only if it
is still down (Bridge.attach if_down). The real Bridge and host against a fake board
are tools/smoke_ws_auth.py K.
"""
from __future__ import annotations

import asyncio
import contextlib
import io
import pathlib
import sys
import time
import types

SRC = (pathlib.Path(sys.argv[1]) if len(sys.argv) > 1
       else pathlib.Path(__file__).resolve().parent.parent / "src")
sys.path.insert(0, str(SRC))

# Failure details can carry the loop's arrows; a cp1252 console must not turn
# reporting a failure into a crash.
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")

import discover                                            # noqa: E402
import host as hostmod                                     # noqa: E402
import proc                                                # noqa: E402
import wsauth                                              # noqa: E402

REAL_WIFI_BOUNCE = discover.wifi_bounce
HOST = "192.168.4.1"
ON_SUBNET = "192.168.4.2"


class FakeOS:
    """The route, the SSID and the probe, answering whatever a case sets up."""

    def __init__(self, via=ON_SUBNET, ssid="WCB1", kind="unknown", relay_id=None,
                 alias=None, on_probe=None, ssid_raises=False, auth=None):
        self.via, self.ssid, self.kind = via, ssid, kind
        self.relay_id, self.alias, self.auth = relay_id, alias, auth
        self.on_probe, self.ssid_raises = on_probe, ssid_raises
        self.calls = []
        discover.local_ip_for = self._route
        discover.ssid_for_host = self._ssid
        discover.probe = self._probe
        discover.tcp_open = lambda *a, **k: True
        discover.wifi_bounce = self._bounce

    def _route(self, _host):
        self.calls.append("route")
        return self.via

    def _ssid(self, _host):
        self.calls.append("ssid")
        if self.ssid_raises:
            raise RuntimeError("netsh output this parser has never seen")
        return self.ssid

    def _probe(self, _host, timeout=1.5):
        self.calls.append("probe")
        if self.on_probe:
            self.on_probe()
        info = {"kind": self.kind, "version": None, "relayId": self.relay_id,
                "alias": self.alias, "peers": []}
        if self.auth is not None:
            info["auth"] = self.auth          # discover.probe() of a locked board
        return info

    def _bounce(self, _ssid="NaviCore"):
        self.calls.append("bounce")
        return False, "a test must never bounce an adapter"


class FakeBridge:
    """Only the surface reconnect_loop touches, recording what it is told, in order."""

    def __init__(self, spec):
        self._spec, self._want, self._open = spec, True, False
        self.last_rx, self.last_error, self.target_label = 0.0, "", ""
        self.reconnecting = False
        self.events = []
        # host.Bridge's reconnect counts and the session that owns them. Nothing here
        # attaches anew or detaches, so the session never moves.
        self.session, self.retry_fails, self.bounce_fails = 0, 0, 0
        # host.Bridge's access-token hold (Bridge.note_auth): None, or what the board
        # asked for, which the loop reads before anything else on every pass.
        self.auth_needed = None
        # host.Bridge.opening: an attach is inside open(), so the link is on its way up.
        self._opening = False

    @property
    def attached(self):
        return self._open

    @property
    def wants_link(self):
        return self._want and self._spec is not None

    @property
    def opening(self):
        return self._opening

    def rebuild(self):
        return object()

    def set_target(self, spec):
        self.events.append(("set_target", spec.get("role"), spec.get("ssid")))
        self._spec, self._want = spec, True

    def attach(self, _t, label, spec=None, if_down=False):
        # As host.Bridge.attach: the loop's reattach (if_down) claims the link only
        # while it is still down, nobody is opening one, and `spec` is still the target.
        if if_down and (self._open or self._opening or self._spec is not spec):
            self.events.append(("attach refused", (spec or {}).get("role")))
            return False
        self.events.append(("attach", (spec or {}).get("role"), label))
        self._open, self.target_label = True, label
        self.last_rx = time.monotonic()
        if spec is not None:
            self._spec = spec
        return True

    def _drop(self):
        self._open = False

    def note_auth(self, spec, need, why=""):
        """As host.Bridge.note_auth: recorded only for the target still wanted."""
        if self._spec is not spec:
            return False
        self.events.append(("auth", need.get("key")))
        self.auth_needed = dict(need, at=time.monotonic(), seq=1)
        self.last_error = why or need.get("message", "")
        return True

    def note_failure(self, spec, why, count=False):
        """As host.Bridge.note_failure: recorded (and counted) only for the target still wanted."""
        if self._spec is not spec:
            return False
        self.last_error = why
        if count:
            self.retry_fails += 1
        return True


def run_loop(bridge, passes):
    """The real reconnect_loop, for a fixed number of passes, its log swallowed."""
    hostmod.bridge = bridge
    hostmod.auto_bounce = False
    real_sleep = asyncio.sleep
    count = 0

    async def counted_sleep(_delay):
        nonlocal count
        count += 1
        if count > passes:
            raise asyncio.CancelledError
        await real_sleep(0)

    async def main():
        asyncio.sleep = counted_sleep
        try:
            await hostmod.reconnect_loop(None)
        except asyncio.CancelledError:
            pass
        finally:
            asyncio.sleep = real_sleep

    with contextlib.redirect_stdout(io.StringIO()):
        asyncio.run(main())


def wcb1(**extra):
    return dict({"kind": "ws", "host": HOST, "role": "wcb", "ssid": "WCB1",
                 "relayId": 1}, **extra)


CASES = []


def case(fn):
    CASES.append(fn)
    return fn


# -- _reidentify_if_moved, directly -------------------------------------------
@case
def same_network_is_not_a_move():
    os_ = FakeOS(ssid="WCB1")
    got = hostmod._reidentify_if_moved(wcb1())
    return got is None and "probe" not in os_.calls, f"got {got!r}, calls {os_.calls}"


@case
def wcb_to_navicore_takes_the_new_role():
    FakeOS(ssid="NaviCore", kind="navicore")
    old = wcb1()
    got = hostmod._reidentify_if_moved(old) or {}
    new = got.get("spec") or {}
    ok = (new.get("role") == "navicore" and new.get("ssid") == "NaviCore"
          and "relayId" not in new and old == wcb1())
    return ok, f"got {got!r}, original now {old!r}"


@case
def wcb1_to_wcb2_takes_the_new_board_id():
    FakeOS(ssid="WCB2", kind="wcb", relay_id="2", alias="Dome")
    got = hostmod._reidentify_if_moved(wcb1()) or {}
    new = got.get("spec") or {}
    ok = (new.get("role") == "wcb" and new.get("relayId") == 2
          and new.get("ssid") == "WCB2" and "Dome" in got.get("note", ""))
    return ok, f"got {got!r}"


@case
def unidentified_host_is_held_not_guessed():
    FakeOS(ssid="NaviCore", kind="unknown")
    got = hostmod._reidentify_if_moved(wcb1()) or {}
    return "hold" in got and "spec" not in got, f"got {got!r}"


@case
def no_lease_on_the_subnet_asks_nothing_more():
    os_ = FakeOS(via="172.30.1.36", ssid="NaviCore", kind="navicore")
    got = hostmod._reidentify_if_moved(wcb1())
    return got is None and os_.calls == ["route"], f"got {got!r}, calls {os_.calls}"


@case
def no_route_at_all_asks_nothing_more():
    os_ = FakeOS(via=None, ssid="NaviCore", kind="navicore")
    got = hostmod._reidentify_if_moved(wcb1())
    return got is None and os_.calls == ["route"], f"got {got!r}, calls {os_.calls}"


# No recorded SSID (macOS, where ssid_for_host cannot answer): the board is asked
# instead (_reidentify_by_probe) - before 2026-10-07 nothing was asked, and a Mac
# hopping from a WCB's access point to NaviCore's kept the WCB's role.
@case
def no_recorded_ssid_asks_the_board():
    os_ = FakeOS(ssid="NaviCore", kind="navicore")
    spec = wcb1()
    del spec["ssid"]
    got = hostmod._reidentify_if_moved(spec)
    ok = (got and got.get("spec", {}).get("role") == "navicore" and "relayId" not in got["spec"]
          and "network changed" in got.get("note", "") and os_.calls == ["route", "probe"])
    return ok, f"got {got!r}, calls {os_.calls}"


@case
def no_recorded_ssid_same_board_is_not_a_move():
    os_ = FakeOS(ssid="NaviCore", kind="wcb", relay_id=1)
    spec = wcb1()
    del spec["ssid"]
    got = hostmod._reidentify_if_moved(spec)
    return got is None and os_.calls == ["route", "probe"], f"got {got!r}, calls {os_.calls}"


@case
def no_recorded_ssid_unidentified_reattaches():
    os_ = FakeOS(ssid="NaviCore", kind="unknown")
    spec = wcb1()
    del spec["ssid"]
    got = hostmod._reidentify_if_moved(spec)
    return got is None and os_.calls == ["route", "probe"], f"got {got!r}, calls {os_.calls}"


@case
def serial_asks_nothing():
    os_ = FakeOS(ssid="NaviCore", kind="navicore")
    got = hostmod._reidentify_if_moved({"kind": "serial", "port": "COM5"})
    return got is None and os_.calls == [], f"got {got!r}, calls {os_.calls}"


@case
def os_that_cannot_say_is_not_a_move():
    os_ = FakeOS(ssid=None, kind="navicore")
    got = hostmod._reidentify_if_moved(wcb1())
    return got is None and "probe" not in os_.calls, f"got {got!r}, calls {os_.calls}"


@case
def default_named_droid_is_not_a_move():
    os_ = FakeOS(ssid="NaviCore-20", kind="navicore")
    got = hostmod._reidentify_if_moved({"kind": "ws", "host": HOST, "ssid": "NaviCore"})
    return got is None and "probe" not in os_.calls, f"got {got!r}, calls {os_.calls}"


@case
def a_failing_check_fails_open():
    FakeOS(ssid_raises=True)
    got = hostmod._reidentify_if_moved(wcb1())
    return got is None, f"got {got!r}"


# -- The real reconnect_loop --------------------------------------------------
@case
def loop_hop_to_navicore_retargets_before_attaching():
    os_ = FakeOS(ssid="NaviCore", kind="navicore")
    b = FakeBridge(wcb1())
    run_loop(b, passes=2)
    want = [("set_target", "navicore", "NaviCore"),
            ("attach", "navicore", "ws://192.168.4.1/ws")]
    return b.events == want and "bounce" not in os_.calls, f"events {b.events}"


@case
def loop_same_network_reattaches_unchanged():
    os_ = FakeOS(ssid="WCB1", kind="navicore")
    b = FakeBridge(wcb1())
    run_loop(b, passes=2)
    want = [("attach", "wcb", hostmod._label_for(wcb1()))]
    return (b.events == want and "probe" not in os_.calls,
            f"events {b.events}, calls {os_.calls}")


@case
def loop_never_attaches_to_an_unidentified_host():
    FakeOS(ssid="NaviCore", kind="unknown")
    b = FakeBridge(wcb1())
    run_loop(b, passes=3)
    return (b.events == [] and "not reattaching" in b.last_error,
            f"events {b.events}, last_error {b.last_error!r}")


@case
def loop_user_retarget_during_the_probe_wins():
    user_spec = {"kind": "ws", "host": HOST, "role": "relay", "ssid": "MgmtRelay",
                 "relayId": 19}
    b = FakeBridge(wcb1())
    FakeOS(ssid="NaviCore", kind="navicore",
           on_probe=lambda: setattr(b, "_spec", user_spec))
    run_loop(b, passes=1)
    return b.events == [] and b._spec is user_spec, f"events {b.events}, spec {b._spec!r}"


@case
def loop_leaves_an_attach_that_is_opening_alone():
    # /_api/attach inside open() (the access-token prompt wait: 1 s for a board with no
    # token) reads as wanted and down. The loop asked the OS and the board, then
    # reattached into it and dropped the link the attach published (HIL
    # intellex.wifi_link_loss, run 20261022-145043). It must ask nothing, attach nothing.
    os_ = FakeOS(ssid="NaviCore", kind="navicore")
    spec = wcb1()
    del spec["ssid"]                       # a Mac's attach: the move check probes
    b = FakeBridge(spec)
    b._opening = True
    run_loop(b, passes=3)
    return b.events == [] and os_.calls == [], f"events {b.events}, calls {os_.calls}"


@case
def loop_attach_made_during_the_check_is_not_reattached_over():
    # The loop found the link down and started its move check (a probe, on a Mac);
    # meanwhile an attach brought the link up. The loop's reattach is if_down, and
    # refused: nothing of the new link is dropped.
    spec = wcb1()
    del spec["ssid"]
    b = FakeBridge(spec)
    FakeOS(ssid="WCB1", kind="wcb", relay_id=1, on_probe=lambda: setattr(b, "_open", True))
    run_loop(b, passes=1)
    return b.events == [("attach refused", "wcb")], f"events {b.events}"


@case
def loop_a_failing_check_still_reconnects():
    FakeOS(ssid_raises=True)
    b = FakeBridge(wcb1())
    run_loop(b, passes=2)
    want = [("attach", "wcb", hostmod._label_for(wcb1()))]
    return b.events == want, f"events {b.events}"


# -- A board that wants its access token ---------------------------------------
LOCKED = {"key": "mac:24:0a:c4:00:00:02", "label": '"WCB2" (192.168.4.1)', "host": HOST,
          "why": "missing", "message": '"WCB2" (192.168.4.1) needs its access token'}


@case
def moved_to_a_locked_board_holds_and_asks_for_its_token():
    FakeOS(ssid="WCB2", kind="locked", auth=dict(LOCKED))
    got = hostmod._reidentify_if_moved(wcb1()) or {}
    ok = ("hold" in got and "spec" not in got and (got.get("auth") or {}).get("key") == LOCKED["key"]
          and "access token" in got["hold"])
    return ok, f"got {got!r}"


@case
def loop_locked_board_on_a_new_network_asks_never_attaches():
    FakeOS(ssid="WCB2", kind="locked", auth=dict(LOCKED))
    b = FakeBridge(wcb1())
    run_loop(b, passes=3)
    ok = (b.events == [("auth", LOCKED["key"])] and (b.auth_needed or {}).get("key") == LOCKED["key"]
          and "access token" in b.last_error)
    return ok, f"events {b.events}, auth_needed {b.auth_needed!r}, last_error {b.last_error!r}"


def _with_can_try(answer, fn):
    real = wsauth.can_try
    wsauth.can_try = lambda _key: answer
    try:
        return fn()
    finally:
        wsauth.can_try = real


@case
def loop_holds_while_there_is_no_token_to_send():
    # Ahead of the move check: not even the route or the SSID is asked, so a Mac
    # (which probes on every reattach) opens no session on the board either.
    def run():
        os_ = FakeOS(ssid="WCB1", kind="wcb", relay_id=1)
        b = FakeBridge(wcb1())
        b.auth_needed = dict(LOCKED, at=time.monotonic(), seq=1)
        run_loop(b, passes=3)
        return b.events == [] and os_.calls == [], f"events {b.events}, calls {os_.calls}"
    return _with_can_try(False, run)


@case
def loop_a_saved_token_ends_the_hold_at_once():
    def run():
        FakeOS(ssid="WCB1", kind="wcb", relay_id=1)
        b = FakeBridge(wcb1())
        b.auth_needed = dict(LOCKED, at=time.monotonic(), seq=1)
        run_loop(b, passes=2)
        return b.events == [("attach", "wcb", hostmod._label_for(wcb1()))], f"events {b.events}"
    return _with_can_try(True, run)


@case
def loop_the_hold_is_rechecked_after_a_while():
    def run():
        FakeOS(ssid="WCB1", kind="wcb", relay_id=1)
        b = FakeBridge(wcb1())
        b.auth_needed = dict(LOCKED, at=time.monotonic() - hostmod.AUTH_RECHECK_S - 1, seq=1)
        run_loop(b, passes=2)
        return b.events == [("attach", "wcb", hostmod._label_for(wcb1()))], f"events {b.events}"
    return _with_can_try(False, run)


# -- wifi_bounce's failure message --------------------------------------------
def _bounce_with(netsh_out, ssid):
    ran = []
    real = proc.run

    def fake_run(args, **_kw):
        ran.append(list(args))
        return types.SimpleNamespace(stdout=netsh_out, stderr="", returncode=0)

    proc.run = fake_run
    try:
        return REAL_WIFI_BOUNCE(ssid), ran
    finally:
        proc.run = real


@case
def bounce_failure_names_where_each_adapter_is():
    if sys.platform != "win32":
        return "SKIP"
    out = ("There are 2 interfaces on the system:\n\n"
           "    Name                   : Wi-Fi\n"
           "    State                  : connected\n"
           "    SSID                   : RHN-COMM\n"
           "    AP BSSID               : 00:11:22:33:44:55\n\n"
           "    Name                   : Wi-Fi 2\n"
           "    State                  : connected\n"
           "    SSID                   : WCB2\n"
           "    AP BSSID               : 66:77:88:99:aa:bb\n")
    (ok, msg), ran = _bounce_with(out, "WCB1")
    good = (not ok and 'associated with "WCB1"' in msg and 'Wi-Fi 2 is on "WCB2"' in msg
            and 'Wi-Fi is on "RHN-COMM"' in msg and "saves the profile" not in msg
            and len(ran) == 1)
    return good, f"ok={ok} msg={msg!r} netsh calls={ran}"


@case
def bounce_failure_with_nothing_connected_says_so():
    if sys.platform != "win32":
        return "SKIP"
    out = ("There is 1 interface on the system:\n\n"
           "    Name                   : Wi-Fi 2\n"
           "    State                  : disconnected\n")
    (ok, msg), ran = _bounce_with(out, "WCB1")
    good = not ok and "connected to anything" in msg and len(ran) == 1
    return good, f"ok={ok} msg={msg!r}"


def main() -> int:
    bad = 0
    for fn in CASES:
        try:
            result = fn()
        except Exception as e:                  # a crash is a failure, and says where
            result = (False, f"raised {e.__class__.__name__}: {e}")
        if result == "SKIP":
            print(f"  SKIP  {fn.__name__}")
            continue
        ok, detail = result
        bad += not ok
        print(f"  {'PASS' if ok else 'FAIL'}  {fn.__name__}" + ("" if ok else f"  -- {detail}"))
    print(f"\nreconnect-identity: {bad} FAILURE(S)" if bad else "\nreconnect-identity: OK")
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
