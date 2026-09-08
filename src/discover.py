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

# WHERE THE BOXES LIVE
#
# THREE kinds of firmware can host the AP now -- NaviCore, a MgmtRelay, and a WCB
# -- and two of them are at the same address, so ADDRESS IS NOT IDENTITY here.
# probe() decides what a thing is from what it answers; this list only decides
# where to knock.
#
# NaviCore never calls softAPConfig(), so its SoftAP is the ESP32 default .1.
#
# The WCB firmware deliberately does not call it either: pinning itself to
# 192.168.4.<board> was tried and BREAKS DHCP (WCB_WiFi.cpp:122) -- clients
# associate, get no lease, land on 169.254.x and cannot reach the board at all.
# So a WCB hosting its own AP is also at .1, sharing the address with NaviCore.
#
# The MgmtRelay DOES call it: with RELAY_STATIC_IP (on by default) it pins itself
# to 192.168.4.<DEVICE_ID> -- .19 out of the box -- and holds that address whether
# it is hosting the AP itself or has joined NaviCore's. So one fixed pair still
# covers every deployment:
#
#   NaviCore hosts the AP -> NaviCore at .1
#   WCB hosts the AP      -> WCB at .1 (looks identical from the address alone)
#   relay hosts the AP    -> nothing at .1 (the relay IS the gateway, at .19)
#   relay joined NaviCore -> NaviCore at .1 AND relay at .19, both reachable
#
# Probing both and reporting what answered is what lets the user pick.
NAVICORE_IP  = "192.168.4.1"           # ...or a WCB. Only probe() can say which.
RELAY_IP     = "192.168.4.19"          # 192.168.4.<relay DEVICE_ID>
DEFAULT_CANDIDATES = [NAVICORE_IP, RELAY_IP]

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


def _read_lines(ws, seconds: float):
    """Yield complete lines for `seconds`.

    Both endpoints are CONSOLE MIRRORS, not request/response: the answer arrives
    somewhere inside a stream that is also carrying telemetry and board chatter.
    So read for a window and sift, rather than expecting the reply first.
    """
    import time as _t
    buf, end = "", _t.monotonic() + seconds
    while _t.monotonic() < end:
        try:
            msg = ws.recv(timeout=max(0.05, end - _t.monotonic()))
        except Exception:
            return
        buf += (msg.decode("utf-8", "replace")
                if isinstance(msg, (bytes, bytearray)) else str(msg))
        while "\n" in buf:
            line, buf = buf.split("\n", 1)
            yield line.strip()


def _wdp_fields(line: str) -> dict:
    """Parse one [WDP:k=v,k=v,...] row. ALIAS may contain spaces."""
    body = line[line.index(":") + 1:].rstrip("]")
    out = {}
    for part in body.split(","):
        if "=" in part:
            k, _, v = part.partition("=")
            out[k.strip()] = v.strip()
    return out


def probe(host: str, timeout: float = _PROBE_TIMEOUT_S) -> dict:
    """Ask a host what it is. One socket, three questions.

    WHY MORE THAN ONE
    A NaviCore answers a JSON PING with a PONG carrying its firmware version. A
    MgmtRelay never will -- it is a bridge, not a droid -- so a PONG-only test
    reported it as "answers on port 80 but is neither" and the chooser had nothing
    to offer. And a WCB now hosts its own AP too, so "not a NaviCore" is no longer
    the same statement as "a relay": three kinds answer here, not two.

    IDENTIFIED BY WHAT IT ANSWERS, NEVER BY ITS ADDRESS. Every AP host is at
    192.168.4.1 -- moving one off .1 breaks DHCP (WCB_WiFi.cpp:122) -- so the
    address says only "the gateway", never which of the three it is.

    WHY ?WDP,DUMP AND NOT ?version OR ?backup
    ?version returns only "Software Version: 1.2", which identifies nothing.
    ?backup does carry the "?RELAY,1" marker, but it dumps the whole config
    INCLUDING ?EPASS, the mesh password in clear, and a discovery probe has no
    business pulling that across the wire on every scan -- ?RELAY,WIFI answers
    the same question without it. See the comment at the send.
    ?WDP,DUMP carries no secrets and says more: its SELF row (PEER=3) gives the
    device's id and alias, and the rows after it are the boards it can actually
    reach. Verified against the hardware:

        [WDP:N=19,CLIENT=0,ALIAS=Mgmt Relay,HW=32,...,PEER=3]
        [WDP:N=20,CLIENT=1,ALIAS=NaviCore,HWREV=NaviCore v2,FW=v0.2.0_...]
        [WDP:END,count=1]

    Asking the DOORWAY what it is, rather than bouncing a ping off a droid behind
    it, also means it is identified when no droid is powered at all.

    Returns {"kind", "version", "relayId", "alias", "peers"}, where kind is
    navicore | relay | wcb | unknown.
    """
    info = {"kind": "unknown", "version": None, "relayId": None,
            "alias": None, "peers": []}
    try:
        from websockets.sync.client import connect
    except ImportError:
        return info
    try:
        with connect(f"ws://{host}/ws", open_timeout=timeout, close_timeout=0.5) as ws:
            # Q1 - a NaviCore. TRAILING NEWLINE IS REQUIRED: both firmwares frame on
            # it, so a bare message is buffered forever waiting for a terminator.
            ws.send(json.dumps({"type": "PING"}) + "\n")
            for line in _read_lines(ws, timeout):
                if not line.startswith("{"):
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if obj.get("type") == "PONG":
                    info["kind"] = "navicore"
                    info["version"] = str(obj.get("version", "unknown"))
                    return info

            # Q2 and Q3, down one socket and one read.
            #
            # Q2 "where is it on the mesh?" is ?WDP,DUMP's SELF row. Q3 "what IS
            # it?" needs its own question, because a WCB HOSTING ITS OWN AP and a
            # MgmtRelay answer ?WDP,DUMP identically -- the relay's SELF row was
            # byte-matched to the firmware's on purpose (WCB_WDP.cpp:1127).
            #
            # ?RELAY,WIFI is the discriminator. It is a MgmtRelay command and the
            # WCB firmware has no handler for it at all, so the reply's presence
            # is the whole test. Its report names the mode and DELIBERATELY never
            # a password (MgmtRelay.ino:1094) -- which is what makes it usable
            # here where ?backup, the other place "?RELAY,1" appears, is not:
            # that dumps ?EPASS, the mesh password, in clear.
            #
            # Two things that look like they would work and do not:
            #   - The SELF row's HW field. MgmtRelay reports HW=32 to mean "not a
            #     real board", but 32 is ALSO a genuine hardware version, WCB 3.2
            #     (Wizard/parser.js:217 HW_VERSION_MAP). A v3.2 board is exactly
            #     the case being identified, so that test misreads every one.
            #   - The address. Sitting a WCB at 192.168.4.<id> was tried for this
            #     and breaks DHCP on the SoftAP (WCB_WiFi.cpp:122), so every AP
            #     host is .1 and the address carries no information at all.
            #
            # Sent FIRST and read in the SAME loop, so it costs no extra round
            # trip and needs no timeout of its own: the far end handles lines in
            # order, so the reply lands ahead of the dump and [WDP:END still ends
            # the read. A '?' command is handled locally and never re-broadcast to
            # the mesh (WCB_Help.cpp:858), so this asks nothing of other boards.
            ws.send("?RELAY,WIFI\n")
            ws.send("?WDP,DUMP\n")
            said_relay = False
            for line in _read_lines(ws, timeout * 2):
                if line.startswith("[WDP:END"):
                    break
                if line.startswith("[relay]"):
                    said_relay = True
                    continue
                if not line.startswith("[WDP:"):
                    continue
                f = _wdp_fields(line)
                if f.get("PEER") == "3":              # SELF - the device we are on
                    info["kind"] = "mesh"
                    info["relayId"] = f.get("N")
                    info["alias"] = f.get("ALIAS")
                else:
                    info["peers"].append({
                        "id": f.get("N"),
                        "alias": f.get("ALIAS"),
                        "fw": f.get("FW"),
                        "hwrev": f.get("HWREV"),
                    })
            # Settled after the loop rather than inside it, so the answer does not
            # depend on the two replies arriving in the order they were asked for.
            if info["kind"] == "mesh":
                info["kind"] = "relay" if said_relay else "wcb"
    except Exception:
        return info
    return info


def scan(candidates: Optional[list[str]] = None) -> list[dict]:
    """Probe each candidate and report what was found, and via which adapter."""
    out = []
    for host in (candidates or DEFAULT_CANDIDATES):
        via = local_ip_for(host)
        reachable = tcp_open(host) if via else False
        info = probe(host) if reachable else {"kind": "unknown", "version": None,
                                              "relayId": None, "alias": None,
                                              "peers": []}
        out.append({
            "host": host,
            "via": via,                     # our source address = which adapter
            "routable": via is not None,
            "reachable": reachable,         # TCP 80 answered
            "kind": info["kind"],           # navicore | relay | wcb | unknown
            "isNaviCore": info["kind"] == "navicore",
            "isRelay": info["kind"] == "relay",
            "isWcb": info["kind"] == "wcb",
            # Anything that fronts the mesh, whichever of the two it is. The
            # launcher cares about "can I manage boards through this" far more
            # often than about which box is doing it.
            "isMesh": info["kind"] in ("relay", "wcb"),
            "version": info["version"],
            "relayId": info["relayId"],
            "alias": info["alias"],
            "peers": info.get("peers", []),
            "hint": _hint(via, reachable, info),
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
    import proc
    if sys.platform == "darwin":
        return _wifi_bounce_macos(ssid)
    if sys.platform != "win32":
        return False, f"adapter bounce is not implemented for {sys.platform}"

    # MUST be scoped to ONE interface. `netsh wlan disconnect` with no interface
    # disconnects EVERY wireless adapter -- on the two-adapter setup this exists to
    # serve, that drops the user's house WiFi (and any call on it) to fix the droid
    # link. Find the adapter actually associated with `ssid` and touch only that.
    try:
        show = proc.run(["netsh", "wlan", "show", "interfaces"],
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
        proc.run(["netsh", "wlan", "disconnect", f"interface={iface}"],
                       capture_output=True, timeout=10, check=False)
        r = proc.run(["netsh", "wlan", "connect",
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


def _mac_iface_for_ip(ip: str, run) -> Optional[str]:
    """Which interface actually carries traffic to `ip`, if any specifically does.

    The routing table is the decision the packets themselves follow. It needs no
    entitlement and does not care what macOS has decided to call the adapter --
    both of which the SSID route gets wrong on a modern Mac (see below).

    REJECTS THE DEFAULT ROUTE, which is the whole safety of this. `route -n get`
    always answers: with no specific route to the droid it hands back the default
    gateway, i.e. the user's real network. Bouncing THAT to fix the droid link is
    precisely the failure this module scopes to one interface to avoid, and it is
    the normal state whenever the droid is simply switched off.

    Belt and braces: also require the adapter to hold an address in the droid's
    own /24, so an unrelated host route cannot volunteer the wrong radio either.
    """
    r = run(["route", "-n", "get", ip])
    dest = iface = ""
    for ln in (r.stdout or "").splitlines():
        k, _, v = ln.partition(":")
        k, v = k.strip(), v.strip()
        if k == "interface":
            iface = v
        elif k == "destination":
            dest = v
    if not iface or dest == "default":
        return None

    want = ip.rsplit(".", 1)[0] + "."
    cfg = run(["ifconfig", iface])
    for ln in (cfg.stdout or "").splitlines():
        parts = ln.split()
        if len(parts) >= 2 and parts[0] == "inet" and parts[1].startswith(want):
            return iface
    return None


def _mac_service_for_device(dev: str, run) -> Optional[str]:
    """The network SERVICE name for a device — en6 -> "802.11ac NIC".

    Needed because `-setairportpower` only works on ports macOS itself considers
    Wi-Fi, and a USB 802.11 adapter is very often not one of them: it answers
    "en6 is not a Wi-Fi interface" to every airport verb. Toggling its service is
    then the only way to cycle it that still needs no sudo.

    The listing pairs a "(N) Name" line with the "(Hardware Port: ..., Device: enX)"
    line that follows it.
    """
    import re
    r = run(["networksetup", "-listnetworkserviceorder"])
    name = None
    for ln in (r.stdout or "").splitlines():
        ln = ln.strip()
        m = re.match(r"^\(\d+\)\s+(.*\S)\s*$", ln)
        if m:
            name = m.group(1)
            continue
        if ln.startswith("(Hardware Port:") and ln.rstrip(")").endswith(f"Device: {dev}"):
            return name
    return None


def _wifi_bounce_macos(ssid: str, ip: str = "") -> tuple[bool, str]:
    """Power-cycle only the interface that is actually carrying the droid.

    VERIFIED BROKEN, then rewritten — the original was written without a Mac, and
    the first real run found two independent reasons it could never have worked.

    1. NOT EVERY 802.11 ADAPTER IS A "Wi-Fi" HARDWARE PORT. The droid was on en6,
       which `-listallhardwareports` names "802.11ac NIC". The old name filter
       ("wi-fi"/"airport") never considered it, and it would not have helped:
       macOS answers "en6 is not a Wi-Fi interface" to -getairportnetwork AND to
       -setairportpower, so both the detect and the bounce were unavailable there.

    2. SSID LOOKUP IS GATED ON LOCATION SERVICES from macOS 14. Even the genuine
       built-in Wi-Fi port answers "You are not associated with an AirPort
       network" to an unprivileged process while plainly associated. So ANY design
       keyed on reading the SSID is unreliable on a current Mac, whatever the
       hardware.

    Hence: ask the routing table first, and fall back to the SSID scan only when
    it declines to name an interface (droid off, so nothing to bounce anyway).
    """
    import proc

    def run(args, timeout=15):
        return proc.run(args, capture_output=True, text=True,
                              timeout=timeout, check=False)

    ip = ip or DEFAULT_CANDIDATES[0]

    try:
        target = _mac_iface_for_ip(ip, run)
        how = f"carrying {ip}" if target else ""
        # Fallback only. Kept because it is still correct on the machines it was
        # written for -- a built-in Wi-Fi port, on a macOS that will answer -- and
        # costs nothing when the routing table has already answered.
        hw = run(["networksetup", "-listallhardwareports"]) if not target else None
        devices, want = [], False
        for line in ((hw.stdout if hw else "") or "").splitlines():
            line = line.strip()
            if line.startswith("Hardware Port:"):
                want = "wi-fi" in line.lower() or "airport" in line.lower()
            elif line.startswith("Device:") and want:
                devices.append(line.split(":", 1)[1].strip())

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
                how = f'associated with "{ssid}"'
                break
        if not target:
            return False, (f'nothing is carrying {ip}, and no Wi-Fi interface '
                           f'reports being on "{ssid}" — join it once by hand, and '
                           "note that macOS 14+ hides the SSID unless Location "
                           "Services is granted, so the routing check is the one "
                           "that normally answers")

        # Cycle it the only way this particular adapter allows. -setairportpower is
        # preferred where it works (it is what the Wi-Fi menu does), but it is
        # refused outright on a port macOS does not classify as Wi-Fi -- which is
        # exactly the USB adapter this rewrite exists for. Probe, do not assume:
        # a failed "off" followed by a failed "on" would otherwise report success
        # while having done nothing at all.
        if run(["networksetup", "-getairportpower", target]).returncode == 0:
            run(["networksetup", "-setairportpower", target, "off"])
            r = run(["networksetup", "-setairportpower", target, "on"])
            did = "power-cycled"
        else:
            svc = _mac_service_for_device(target, run)
            if not svc:
                return False, (f"{target} ({how}) is not a Wi-Fi interface and has "
                               "no network service to cycle")
            run(["networksetup", "-setnetworkserviceenabled", svc, "off"])
            r = run(["networksetup", "-setnetworkserviceenabled", svc, "on"])
            did = f'service "{svc}" cycled'
        if r.returncode != 0:
            return False, (r.stderr or r.stdout or f"exited {r.returncode}").strip()
        return True, f"{target} ({how}): {did}, reconnecting"
    except FileNotFoundError:
        return False, "networksetup not found"
    except subprocess.TimeoutExpired:
        return False, "networksetup timed out"


def _hint(via: Optional[str], reachable: bool, info: dict) -> str:
    kind = info.get("kind")
    if kind == "navicore":
        return f"NaviCore {info.get('version')}"
    if kind in ("relay", "wcb"):
        # Same shape for both, because what the user needs to know is the same:
        # who it is, and what it can reach. Only the noun differs.
        noun = "mgmt relay" if kind == "relay" else "WCB"
        who = info.get("alias") or noun
        rid = info.get("relayId")
        seen = [p for p in info.get("peers", []) if p.get("alias")]
        behind = (" — sees " + ", ".join(
            f"{p['alias']}" + (f" {p['fw']}" if p.get("fw") else "") for p in seen[:3])
        ) if seen else " — no boards seen yet"
        tag = f" (WCB #{rid})" if rid else ""
        # Don't print "Vader WCB (WCB #21)" when the alias already says WCB.
        if kind == "wcb" and "wcb" in who.lower():
            tag = f" (#{rid})" if rid else ""
        return f"{who}{tag}{behind}"
    if reachable:
        return ("something answers on port 80, but it is not a NaviCore, "
                "a WCB or a management relay")
    if via:
        # The exact state seen on this machine: a route exists and looks right, but
        # no traffic passes. Disabling and re-enabling the adapter cleared it.
        return (f"routed via {via} but no TCP — stale association or the AP is down. "
                "Toggle that adapter off/on, or check the board's serial log for "
                "'[WS] command endpoint ready'.")
    return "no route — this machine is not joined to the droid's WiFi"
