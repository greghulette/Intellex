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
    if sys.platform != "win32":
        # macOS needs the interface name and a different tool; not worth guessing
        # at one here when the failure mode has only been observed on Windows.
        return False, "adapter bounce is implemented for Windows only"

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
        if m and m.group(1) == ssid:
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
