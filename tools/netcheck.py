#!/usr/bin/env python3
"""Why does this machine think GitHub is unreachable?

    python tools/netcheck.py

Prints what reachable() sees, one layer at a time: proxy settings, DNS, then a
connect to every address DNS returned. Something here will disagree with "GitHub
is not reachable", and whichever line does is the answer.

WHY THIS EXISTS
The app decides offline-versus-online from one TCP probe (src/flash.py
reachable()), and everything hangs off that answer: the tool update, the firmware
cache, the docs, the branch list, and the proxy that makes OTA work with no
internet. When the probe is wrong, all five fail at once with five different
messages, and the app has no way to say which layer actually broke.

That happened on a Mac whose git had just cloned from the very host being probed.
Two real bugs were found and fixed by reading the code -- a wall-clock budget
shorter than the DNS lookup it had to contain, and a cache that answered for the
wrong host -- and neither was the cause. Guessing from the far end of a chat is
what this replaces.

RUN IT THE SAME WAY THE APP RUNS. From a checkout that is `.venv/bin/python
tools/netcheck.py`; the frozen app has its own interpreter and its own bundled
modules, so a difference between the two is itself a finding.
"""
from __future__ import annotations

import os
import pathlib
import socket
import sys
import time

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

# The three hosts the app actually probes, and who asks about each.
HOSTS = [
    ("api.github.com", "flashing, the firmware proxy, the branch list"),
    ("greghulette.github.io", "the config tool / Wizard update"),
    ("github.com", "the wikis"),
]

PROXY_VARS = ("HTTPS_PROXY", "https_proxy", "HTTP_PROXY", "http_proxy",
              "ALL_PROXY", "all_proxy", "NO_PROXY", "no_proxy")


def main() -> int:
    print(f"python   {sys.version.split()[0]}")
    print(f"platform {sys.platform}")
    print(f"frozen   {getattr(sys, 'frozen', False)}")

    # A proxy is the classic reason a browser and git work while a raw socket
    # does not: both honour these, a bare connect() does not.
    proxies = {v: os.environ[v] for v in PROXY_VARS if os.environ.get(v)}
    print(f"proxy    {proxies or 'none set'}")
    print()

    worst = 0
    for host, who in HOSTS:
        print(f"-- {host}   ({who})")
        t = time.time()
        try:
            infos = socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)
        except OSError as e:
            print(f"   DNS FAILED after {(time.time() - t) * 1000:.0f} ms -> {e}")
            print("   => no resolver, or the name is blocked. Nothing else can work.")
            worst = 2
            print()
            continue
        dt = (time.time() - t) * 1000
        fams = {socket.AF_INET: "IPv4", socket.AF_INET6: "IPv6"}
        print(f"   DNS ok: {len(infos)} address(es) in {dt:.0f} ms")
        if dt > 3000:
            # The probe's own budget has to contain this; a slow resolver used to
            # be read as "offline" outright.
            print("   NOTE: the lookup alone took over 3 s -- slow resolver.")
            worst = max(worst, 1)

        any_ok = False
        for family, socktype, proto, _canon, addr in infos:
            t2 = time.time()
            try:
                with socket.socket(family, socktype, proto) as sk:
                    sk.settimeout(4.0)
                    sk.connect(addr)
                any_ok = True
                print(f"   connect OK   {fams.get(family, family)} {addr[0]}"
                      f"  {(time.time() - t2) * 1000:.0f} ms")
            except OSError as e:
                print(f"   connect FAIL {fams.get(family, family)} {addr[0]}"
                      f"  {(time.time() - t2) * 1000:.0f} ms  {e}")
        if not any_ok:
            print("   => DNS resolves but nothing accepts a connection on 443.")
            print("      A firewall, a proxy-only network, or a captive portal.")
            worst = 2
        print()

    # Finally the real thing, so its verdict can be compared with the layers above.
    import flash
    print("-- src/flash.py reachable(), the answer the app actually uses")
    for host, _who in HOSTS:
        flash._reach.clear()
        t = time.time()
        ok = flash.reachable(host)
        why = flash.unreachable_reason(host)
        print(f"   {host:24} {'OK' if ok else 'UNREACHABLE'}"
              f"  {(time.time() - t) * 1000:.0f} ms"
              + (f"  -> {why}" if why else ""))

    print()
    if worst == 0:
        print("Every layer works. If the app still says unreachable, the build is")
        print("older than the fix -- check the version stamp in its header against")
        print("`git log --oneline -1`.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
