#!/usr/bin/env python3
"""Do the test-host hooks hold a host to what a test wants?

    python tools/smoke_test_hooks.py

No hardware, no network. Each hook is checked against the REAL module, with the variable
set in this process's environment the way a test bench sets it for a host
(CLAUDE.md "A test host needs a leash"):

    H1  INTELLEX_OFFLINE          flash.reachable() answers False at once, with the reason
    H2  INTELLEX_SERIAL_ALLOW     serial_allowed(), list_serial_ports(), SerialTransport.open(),
                                  discover.identify_serial() and the flash claim refuse a port
                                  outside the list -- BEFORE anything opens it
    H3  INTELLEX_DISCOVER_HOSTS   discover.scan() probes only the named hosts; set-but-empty
                                  probes nothing; an explicit list still wins
    H4  INTELLEX_DATA_DIR         paths.user_data_dir() is the named directory, on every platform

and, not a hook but the same fake makes it checkable: SerialTransport.open() opens quietly
and alone -- on Windows DTR and RTS low before the open; on macOS/Linux RTS low and the port
exclusive at the open, DTR low after it (the order that does not reset a board there).

WHY IT CANNOT PASS WITHOUT THE HOOKS
pyserial is replaced by a fake that records every open(), and discover.local_ip_for is
replaced by one that records every host scan() asks about. H1 is timed and its reason is
read back. So a missing hook shows up as a recorded open, a probed host, a slow answer or a
missing reason -- never as a pass.
"""
from __future__ import annotations

import os
import pathlib
import sys
import time
import types

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

failures: list[str] = []


def check(ok: bool, what: str) -> None:
    print(("ok    " if ok else "FAIL  ") + what)
    if not ok:
        failures.append(what)


# ── A pyserial that opens nothing and remembers what it was asked to open ─────────────────
OPENS: list[str] = []
LINES_AT_OPEN: list[tuple] = []      # (port, dtr, rts, exclusive) as each open() found them
PORTS = ["COM5", "COM6", "COM99"]


class _FakeSerial:
    def __init__(self, *a, **k):
        self.port, self.baudrate, self.timeout, self.write_timeout = None, 115200, None, None
        self.dtr = self.rts = True          # pyserial's defaults: both asserted unless told otherwise
        self.exclusive = None
        self.is_open = False

    def open(self):
        OPENS.append(str(self.port))
        LINES_AT_OPEN.append((str(self.port), self.dtr, self.rts, self.exclusive))
        self.is_open = True

    def close(self):
        self.is_open = False

    def read(self, n=1):
        return b""

    def write(self, data):
        return len(data)

    def reset_input_buffer(self):
        pass

    in_waiting = 0


class _Port:
    def __init__(self, device):
        self.device, self.description, self.vid, self.pid, self.serial_number = device, "fake", 0x303A, 0x1001, device


fake_serial = types.ModuleType("serial")
fake_serial.Serial = _FakeSerial
fake_serial.SerialException = type("SerialException", (OSError,), {})
fake_tools = types.ModuleType("serial.tools")
fake_list_ports = types.ModuleType("serial.tools.list_ports")
fake_list_ports.comports = lambda: [_Port(d) for d in PORTS]
fake_tools.list_ports = fake_list_ports
fake_serial.tools = fake_tools
sys.modules.update({"serial": fake_serial, "serial.tools": fake_tools, "serial.tools.list_ports": fake_list_ports})

for var in ("INTELLEX_OFFLINE", "INTELLEX_SERIAL_ALLOW", "INTELLEX_DISCOVER_HOSTS", "INTELLEX_DATA_DIR"):
    os.environ.pop(var, None)

import paths                                                   # noqa: E402
import flash                                                   # noqa: E402
import transport                                               # noqa: E402
import discover                                                # noqa: E402
from serial_transport import SerialTransport                   # noqa: E402
import host as hostmod                                         # noqa: E402

# ── H1 ────────────────────────────────────────────────────────────────────────────────────
print("-- H1 INTELLEX_OFFLINE")
os.environ["INTELLEX_OFFLINE"] = "1"
flash._reach.clear()
t0 = time.monotonic()
ok = flash.reachable("api.github.com")
dt = time.monotonic() - t0
check(ok is False, "reachable() says False with INTELLEX_OFFLINE=1")
check(dt < 0.2, f"...and answers at once ({dt * 1000:.0f} ms, a real probe takes up to 9 s)")
check("INTELLEX_OFFLINE" in flash.unreachable_reason("api.github.com"), "...and says why")
os.environ["INTELLEX_OFFLINE"] = "0"
flash._reach.clear()
check("INTELLEX_OFFLINE" not in flash.unreachable_reason("api.github.com"), "INTELLEX_OFFLINE=0 is not a leash")
os.environ.pop("INTELLEX_OFFLINE")
flash._reach.clear()

# ── H2 ────────────────────────────────────────────────────────────────────────────────────
print("-- H2 INTELLEX_SERIAL_ALLOW")
check(transport.serial_allowed("COM5") and transport.serial_allowed("anything"), "unset: every port is allowed")
check([p["path"] for p in transport.list_serial_ports()] == PORTS, "unset: every port is listed")

os.environ["INTELLEX_SERIAL_ALLOW"] = " com6 , COM99"
check(transport.serial_allowed("COM6") and transport.serial_allowed("com99"), "the named ports are allowed (any case)")
check(not transport.serial_allowed("COM5"), "an unnamed port is not")
check([p["path"] for p in transport.list_serial_ports()] == ["COM6", "COM99"], "only the named ports are listed")

OPENS.clear()
try:
    SerialTransport("COM5", on_data=lambda b: None).open()
    check(False, "SerialTransport.open() refuses COM5")
except transport.TransportError as e:
    check("INTELLEX_SERIAL_ALLOW" in str(e), "SerialTransport.open() refuses COM5, naming the variable")
check(OPENS == [], f"...without opening it (opens: {OPENS})")

OPENS.clear()
r = discover.identify_serial("COM5")
check(r == {"version": None, "busy": False} and OPENS == [], f"identify_serial(COM5) answers without opening it ({r}, opens {OPENS})")

saved_spec = hostmod.bridge._spec
try:
    hostmod.bridge._spec = {"kind": "serial", "port": "COM5"}
    spec, port, refused = hostmod._claim_port_for_flash()
    body = refused.text if refused is not None else ""
    check(refused is not None and refused.status == 409 and "INTELLEX_SERIAL_ALLOW" in body,
          "the flash claim refuses COM5 with a 409 naming the variable")
    check(not hostmod._flash_state["running"], "...and leaves no flash marked running")
finally:
    hostmod.bridge._spec = saved_spec

os.environ["INTELLEX_SERIAL_ALLOW"] = ""
check(not transport.serial_allowed("COM6") and transport.list_serial_ports() == [], "set but empty: no port at all")
os.environ.pop("INTELLEX_SERIAL_ALLOW")

# ── H3 ────────────────────────────────────────────────────────────────────────────────────
print("-- H3 INTELLEX_DISCOVER_HOSTS")
asked: list[str] = []
real_local_ip_for = discover.local_ip_for


def fake_local_ip_for(host):
    asked.append(host)
    return None                     # no route: scan() then probes nothing on the wire


discover.local_ip_for = fake_local_ip_for
try:
    discover.scan()
    check(asked == list(discover.DEFAULT_CANDIDATES), f"unset: the default candidates ({asked})")

    asked.clear()
    os.environ["INTELLEX_DISCOVER_HOSTS"] = "10.255.255.1, 10.255.255.2"
    discover.scan()
    check(asked == ["10.255.255.1", "10.255.255.2"], f"the named hosts only ({asked})")

    asked.clear()
    discover.scan(["10.255.255.9"])
    check(asked == ["10.255.255.9"], f"an explicit list still wins ({asked})")

    asked.clear()
    os.environ["INTELLEX_DISCOVER_HOSTS"] = ""
    found = discover.scan()
    check(asked == [] and found == [], f"set but empty: nothing probed ({asked})")
finally:
    discover.local_ip_for = real_local_ip_for
    os.environ.pop("INTELLEX_DISCOVER_HOSTS", None)

# ── H4 ────────────────────────────────────────────────────────────────────────────────────
print("-- H4 INTELLEX_DATA_DIR")
stage = pathlib.Path(__file__).resolve().parent / "_smoke_stage" / "appdata" / "Intellex"   # never created
os.environ["INTELLEX_DATA_DIR"] = str(stage)
check(paths.user_data_dir() == stage, f"user_data_dir() is the named directory ({paths.user_data_dir()})")
check(not stage.exists(), "...and naming it creates nothing")
os.environ.pop("INTELLEX_DATA_DIR")

# ── The open itself ───────────────────────────────────────────────────────────────────────
print("-- SerialTransport.open(): quiet and alone")
LINES_AT_OPEN.clear()
t = SerialTransport("COM6", on_data=lambda b: None)
t.open()
try:
    port, dtr, rts, exclusive = LINES_AT_OPEN[-1]
    if os.name == "nt":
        check((dtr, rts) == (False, False), f"Windows: DTR and RTS low before the open (dtr={dtr}, rts={rts})")
    else:
        check(rts is False, f"macOS/Linux: RTS low at the open (rts={rts})")
        check(exclusive is True, f"...the port exclusive (exclusive={exclusive})")
        check(t._ser.dtr is False, f"...and DTR low after it (dtr={t._ser.dtr})")
finally:
    t.close()

print()
print("PASSED" if not failures else f"FAILED: {len(failures)} check(s)")
sys.exit(1 if failures else 0)
