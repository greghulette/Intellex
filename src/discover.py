"""Find a droid, and say which adapter reached it.

A laptop at a bench routinely has several live interfaces at once -- house WiFi,
a second adapter on the droid's AP, Ethernet, Tailscale, Hyper-V switches. That
is the normal case, not an edge case, and it produces a specific and confusing
failure: the adapter reports "connected" with a valid DHCP lease while nothing
actually moves, because the association has gone stale or the OS is routing the
traffic out of a different interface.

Observed on this machine: two WiFi adapters up (house net + droid AP), a correct
on-link route for 192.168.4.0/24, and yet TCP to the droid timed out until the
adapter was disabled and re-enabled. From the app's side "cannot reach it" and
"reaching it via the wrong adapter" look identical, so this module answers the
question the user actually has: is it there, and which of my adapters is talking
to it?

WHY THE ADDRESS IS NOT THE HARD PART
An ESP32 SoftAP is always 192.168.4.1 unless softAPConfig() moves it, and the
firmware never does. So discovery is not a subnet sweep -- it is "probe the known
address, and report HOW it was reached".

WHY A PONG AND NOT A PORT CHECK
Plenty of things answer TCP 80. A home router at 192.168.4.1 would look like a
droid to a port scan. Only a NaviCore replies to a PING over /ws with a PONG
carrying a firmware version, so that is the test -- it identifies the device
rather than merely finding something alive.
"""

from __future__ import annotations

import contextlib
import json
import sys
import socket
from typing import Optional

# The ESP32 SoftAP gateway. Fixed unless the firmware calls softAPConfig(), which
# it does not.
DEFAULT_CANDIDATES = ["192.168.4.1"]

_PROBE_TIMEOUT_S = 1.5


def local_ip_for(target: str) -> Optional[str]:
    """Which of our addresses the OS would use to reach `target`.

    A connect() on a UDP socket sends nothing -- it just asks the routing table to
    pick a source address. That is exactly the question worth asking with several
    interfaces up, and it needs no dependency and no elevated rights.
    """
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.settimeout(0.5)
        s.connect((target, 80))
        return s.getsockname()[0]
    except OSError:
        return None          # no route at all
    finally:
        s.close()


def tcp_open(host: str, port: int = 80, timeout: float = _PROBE_TIMEOUT_S) -> bool:
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def identify(host: str, timeout: float = _PROBE_TIMEOUT_S) -> Optional[str]:
    """PING over /ws; return the firmware version if a NaviCore answers.

    None means "something may be there, but it is not a droid" -- which is a
    materially different message to show than "nothing found".
    """
    try:
        from websockets.sync.client import connect
    except ImportError:
        return None
    try:
        with connect(f"ws://{host}/ws", open_timeout=timeout, close_timeout=0.5) as ws:
            # TRAILING NEWLINE IS REQUIRED. The protocol is newline-delimited and
            # the firmware frames on it, so a bare message is buffered forever
            # waiting for a terminator that never arrives. Without it discovery
            # reported "answers on port 80 but did not PONG" against a perfectly
            # healthy droid — the PING was received and simply never dispatched.
            ws.send(json.dumps({"type": "PING"}) + "\n")
            deadline = timeout
            while deadline > 0:
                try:
                    msg = ws.recv(timeout=deadline)
                except Exception:
                    return None
                for line in str(msg).splitlines():
                    line = line.strip()
                    if not line.startswith("{"):
                        continue
                    try:
                        obj = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    if obj.get("type") == "PONG":
                        return str(obj.get("version", "unknown"))
                deadline -= 0.25
    except Exception:
        return None
    return None


def identify_serial(port: str, timeout: float = 2.5) -> dict:
    """PING a serial port; return the firmware version if a NaviCore answers.

    WHY NOT JUST READ THE USB DESCRIPTOR
    Because it cannot answer the question. A NaviCore and an SBUS controller are
    the same silicon -- ESP32-S3 with native USB CDC -- so they enumerate
    identically: VID:PID 303A:1001, description "USB Serial Device", nothing but
    the MAC in the serial number to tell them apart, and that is per-board rather
    than per-product. A descriptor match can honestly report the CHIP; naming the
    PRODUCT from it is a guess, and on this bench it was wrong half the time.

    ONLY A DIRECT PONG COUNTS. A USB-tethered mesh relay prints the telemetry it
    receives over ESP-NOW straight out of its own USB port -- including rc_hb
    heartbeats carrying a real NaviCore firmware version. Anything that matches
    "looks like NaviCore JSON" therefore labels the relay as a droid. Observed
    exactly that on COM16. A PONG is a reply to OUR ping; relayed traffic is not.

    Returns {"version": str|None, "busy": bool}. "busy" matters: a port another
    program is holding and a port that simply did not answer look identical from
    here, but only one of them is fixed by closing the other program -- and "no
    reply" sent people to check the board when the real problem was on this side.
    """
    import time
    try:
        import serial
    except ImportError:
        return {"version": None, "busy": False}

    s = serial.Serial()
    s.port = port
    s.baudrate = 115200
    s.timeout = 0.2
    # Deassert on the CLOSED port. Opening with either line asserted pulses the
    # ESP32 into reset, which is the whole reason SerialTransport.open() does the
    # same dance -- a probe that reboots the droid would be worse than no probe.
    with contextlib.suppress(Exception):
        s.dtr = False
        s.rts = False
    try:
        s.open()
    except Exception as e:
        # Distinguish "someone else has it" from "it said nothing". Windows raises
        # PermissionError / "Access is denied"; POSIX gives EBUSY or EACCES.
        txt = f"{type(e).__name__}: {e}".lower()
        busy = ("permission" in txt or "access is denied" in txt
                or "busy" in txt or "in use" in txt)
        return {"version": None, "busy": busy}
    try:
        with contextlib.suppress(Exception):
            s.dtr = False
            s.rts = False
        time.sleep(0.25)             # let the line settle before clearing
        with contextlib.suppress(Exception):
            s.reset_input_buffer()
        s.write((json.dumps({"type": "PING"}) + "\n").encode())
        s.flush()

        buf = ""
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = s.read(4096)
            if not chunk:
                continue
            buf += chunk.decode("utf-8", "replace")
            # A NaviCore is a firehose -- telemetry, MESH_STATS, CMDLIB_META all
            # arrive unbidden -- so scan every complete line rather than assuming
            # the reply is first or alone.
            for line in buf.splitlines():
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "PONG":
                    return {"version": str(obj.get("version", "unknown")), "busy": False}
        return {"version": None, "busy": False}
    except Exception:
        return {"version": None, "busy": False}
    finally:
        with contextlib.suppress(Exception):
            s.close()


def scan(candidates: Optional[list[str]] = None) -> list[dict]:
    """Probe each candidate and report what was found, and via which adapter."""
    out = []
    for host in (candidates or DEFAULT_CANDIDATES):
        via = local_ip_for(host)
        reachable = tcp_open(host) if via else False
        version = identify(host) if reachable else None
        out.append({
            "host": host,
            "via": via,                     # our source address = which adapter
            "routable": via is not None,
            "reachable": reachable,         # TCP 80 answered
            "isNaviCore": version is not None,
            "version": version,
            "hint": _hint(via, reachable, version),
        })
    return out


def _ssid_matches(current: str, want: str) -> bool:
    """Is `current` the droid's network?

    Exact first, then the "<want>-<suffix>" form. The firmware derives its AP name
    as NaviCore-<deviceId> whenever wifiSsid is left blank (NaviCore.ino), while
    every caller here defaults to the bare "NaviCore" -- so an exact-only test
    silently never matches a default-named droid, and the bounce reports "no
    interface is associated" against an adapter that is sitting on it.

    Deliberately NOT a substring test. `want in current` also matches a house
    network called NaviCore_Guest or a second droid's AP, and the whole reason this
    module scopes to one interface is that bouncing the wrong radio drops the
    user's real network (and any call on it).
    """
    current = (current or "").strip()
    return bool(current) and (current == want or current.startswith(want + "-"))


def wifi_bounce(ssid: str = "NaviCore") -> tuple[bool, str]:
    """Disconnect and reconnect the WLAN profile for `ssid`.

    THE PROBLEM: an ESP32 SoftAP disappears every time the board reboots -- which
    is every flash, every config change that needs a restart, every OTA. Windows
    frequently keeps the association in a half-dead state afterwards: the adapter
    still reports "connected" with a valid DHCP lease and a correct on-link route,
    and no traffic passes. Toggling the adapter clears it, which is why that became
    a manual ritual after every reboot.

    This is the scripted version of that ritual. `netsh wlan connect` uses a
    profile Windows has already saved, so it needs no password and -- unlike
    disabling the adapter -- no elevation.

    NOT automatic. Bouncing someone's WiFi behind their back is the kind of thing
    that breaks a video call mid-sentence; it is exposed as an action the app can
    offer once discovery reports the specific "routable but no TCP" state.
    """
    import re
    import subprocess
    if sys.platform == "darwin":
        return _wifi_bounce_macos(ssid)
    if sys.platform != "win32":
        return False, f"adapter bounce is not implemented for {sys.platform}"

    # MUST be scoped to ONE interface. `netsh wlan disconnect` with no interface
    # disconnects EVERY wireless adapter -- on the two-adapter setup this exists to
    # serve, that drops the user's house WiFi (and any call on it) to fix the droid
    # link. Find the adapter actually associated with `ssid` and touch only that.
    try:
        show = subprocess.run(["netsh", "wlan", "show", "interfaces"],
                              capture_output=True, timeout=10, text=True, check=False)
    except (FileNotFoundError, subprocess.TimeoutExpired) as e:
        return False, f"netsh unavailable: {e.__class__.__name__}"

    iface = None
    current = None
    for line in (show.stdout or "").splitlines():
        m = re.match(r"\s*Name\s*:\s*(.+?)\s*$", line)
        if m:
            current = m.group(1)
            continue
        m = re.match(r"\s*SSID\s*:\s*(.+?)\s*$", line)
        if m and _ssid_matches(m.group(1), ssid):
            iface = current
            break

    if not iface:
        return False, (f'no wireless interface is associated with "{ssid}" — '
                       "join it once by hand so Windows saves the profile")

    try:
        subprocess.run(["netsh", "wlan", "disconnect", f"interface={iface}"],
                       capture_output=True, timeout=10, check=False)
        r = subprocess.run(["netsh", "wlan", "connect",
                            f"name={ssid}", f"interface={iface}"],
                           capture_output=True, timeout=15, text=True, check=False)
        out = (r.stdout or r.stderr or "").strip()
        if r.returncode != 0:
            return False, out or f"netsh exited {r.returncode}"
        return True, f'{iface}: {out or "reconnecting to " + ssid}'
    except FileNotFoundError:
        return False, "netsh not found"
    except subprocess.TimeoutExpired:
        return False, "netsh timed out"


def _wifi_bounce_macos(ssid: str) -> tuple[bool, str]:
    """Power-cycle only the Wi-Fi interface that is on `ssid`.

    UNTESTED — written without a Mac to run it on. The shape mirrors the Windows
    path, which was measured: find the interface actually associated with the
    droid's SSID and touch only that one, so a machine with a second adapter on
    the house network keeps it.

    A power cycle rather than `networksetup -setairportnetwork`, because that
    wants the passphrase on the command line; cycling lets macOS reconnect from
    its own keychain. Needs no sudo for -setairportpower on current macOS.
    """
    import subprocess

    def run(args, timeout=15):
        return subprocess.run(args, capture_output=True, text=True,
                              timeout=timeout, check=False)

    try:
        # Hardware ports -> the device names (en0, en1, ...) that are Wi-Fi.
        hw = run(["networksetup", "-listallhardwareports"])
        devices, want = [], False
        for line in (hw.stdout or "").splitlines():
            line = line.strip()
            if line.startswith("Hardware Port:"):
                want = "wi-fi" in line.lower() or "airport" in line.lower()
            elif line.startswith("Device:") and want:
                devices.append(line.split(":", 1)[1].strip())

        if not devices:
            return False, "no Wi-Fi hardware port found"

        # Only the one actually on the droid's network.
        target = None
        for dev in devices:
            cur = run(["networksetup", "-getairportnetwork", dev])
            # Parse the NAME out, do not substring the whole reply. The output is
            # "Current Wi-Fi Network: <name>" (or "You are not associated with an
            # AirPort network."), so a raw `ssid in stdout` also matched the literal
            # word appearing anywhere -- and would power-cycle the adapter carrying
            # the user's house network.
            name = ""
            for ln in (cur.stdout or "").splitlines():
                if ":" in ln and "network" in ln.split(":", 1)[0].lower():
                    name = ln.split(":", 1)[1]
                    break
            if _ssid_matches(name, ssid):
                target = dev
                break
        if not target:
            return False, (f'no Wi-Fi interface is on "{ssid}" — '
                           "join it once by hand so macOS remembers it")

        run(["networksetup", "-setairportpower", target, "off"])
        r = run(["networksetup", "-setairportpower", target, "on"])
        if r.returncode != 0:
            return False, (r.stderr or r.stdout or f"exited {r.returncode}").strip()
        return True, f"{target}: power-cycled, reconnecting to {ssid}"
    except FileNotFoundError:
        return False, "networksetup not found"
    except subprocess.TimeoutExpired:
        return False, "networksetup timed out"


def _hint(via: Optional[str], reachable: bool, version: Optional[str]) -> str:
    if version:
        return f"NaviCore {version}"
    if reachable:
        return "something answers on port 80, but it did not PONG — not a NaviCore"
    if via:
        # The exact state seen on this machine: a route exists and looks right, but
        # no traffic passes. Disabling and re-enabling the adapter cleared it.
        return (f"routed via {via} but no TCP — stale association or the AP is down. "
                "Toggle that adapter off/on, or check the board's serial log for "
                "'[WS] command endpoint ready'.")
    return "no route — this machine is not joined to the droid's WiFi"
