#!/usr/bin/env python3
"""Can the PAGE get firmware with no internet? That is what OTA depends on.

    python tools/smoke_ghproxy.py

Needs no network and no hardware: GitHub is forced unreachable and every request
made to fail, which is the con-floor state -- joined to a droid's AP, where there
is a default route that goes nowhere.

WHY THIS MATTERS MORE THAN THE NATIVE FLASH PATH
A cabled flash runs esptool in the host, which reads the cache directly, and that
worked already. OTA does not go near it: both OTA buttons fetch the image INSIDE
THE PAGE, from api.github.com, and OTA over a relay is the only way to reprogram a
board on a con floor without unplugging it. So the page's own fetch has to succeed
offline, and src/ghproxy.py is what makes it.

The failure this guards is silent in the worst way: everything looks right --
firmware cached, relay online, OTA button enabled -- and the transfer cannot start
because the page asked github.com for the bytes.
"""
from __future__ import annotations

import json
import pathlib
import sys

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import flash                                                        # noqa: E402
import fwcache                                                      # noqa: E402
import ghproxy                                                      # noqa: E402
import settings                                                     # noqa: E402


def go_offline() -> None:
    """The droid's AP: a route that goes nowhere, so nothing resolves or connects."""
    flash.reachable = lambda *a, **k: False

    def dead(*_a, **_k):
        raise OSError("Network is unreachable")

    flash._get = dead


def main() -> int:
    go_offline()
    bad = 0
    branches = settings.branches()

    targets = [
        ("NaviCore", "firmware", branches.get("navicore", "main"),
         fwcache.NAVICORE, "NaviCore_"),
        ("Wireless_Communication_Board-WCB", "Code/bin", branches.get("wcb", "main"),
         fwcache.WCB, "WCB_"),
    ]

    for repo, path, ref, product, prefix in targets:
        label = f"{product}@{ref}"
        if not fwcache.load_listing(product, ref):
            print(f"  SKIP  {label}: nothing cached — run tools/fetch_firmware.py")
            continue

        try:
            body, cached = ghproxy.contents("greghulette", repo, path, ref)
            files = json.loads(body)
        except Exception as e:                                      # noqa: BLE001
            print(f"  FAIL  {label}: listing raised {type(e).__name__}: {e}")
            bad += 1
            continue

        checks = [
            ("served from cache", cached),
            ("listing is not empty", bool(files)),
            # The whole point: a cached listing's GitHub download_url is dead, so
            # the page must be pointed back at the host instead.
            ("every download_url points at the host",
             all(str(f.get("download_url", "")).startswith("/_api/gh/raw")
                 for f in files)),
            # NOT "no github.com anywhere": the listing also carries url, html_url
            # and git_url, which are metadata the tools never fetch and which
            # SHOULD still point at github.com -- they are links to the web UI.
            # download_url is the only field anyone downloads, and it is checked
            # above. What matters beyond that is the shape the tools filter on:
            # flasher.js selects with f.type === 'file' and reads f.name, so a
            # listing missing either would come back empty with no error.
            ("every entry has name and type",
             all(f.get("name") and f.get("type") for f in files)),
            ("at least one entry is type=file",
             any(f.get("type") == "file" for f in files)),
        ]
        for name, ok in checks:
            bad += not ok
            print(f"  {'PASS' if ok else 'FAIL'}  {label}: {name}")

        app = next((f["name"] for f in files
                    if f.get("name", "").startswith(prefix)
                    and f["name"].endswith(".bin")), None)
        if not app:
            print(f"  FAIL  {label}: no {prefix}*.bin in the listing")
            bad += 1
            continue
        try:
            data, from_cache = ghproxy.raw("greghulette", repo, ref, app)
        except Exception as e:                                      # noqa: BLE001
            print(f"  FAIL  {label}: image raised {type(e).__name__}: {e}")
            bad += 1
            continue
        for name, ok in (("image served from cache", from_cache),
                         ("image is not empty", len(data) > 1024)):
            bad += not ok
            print(f"  {'PASS' if ok else 'FAIL'}  {label}: {name} ({len(data):,} bytes)")

    # Not an open proxy: this process can reach the internet and the page cannot,
    # so forwarding arbitrary repos would hand the page exactly that reach.
    for owner, repo in (("torvalds", "linux"), ("greghulette", "Intellex")):
        try:
            ghproxy.contents(owner, repo, "x", "main")
            print(f"  FAIL  {owner}/{repo} was proxied — should be refused")
            bad += 1
        except ghproxy.ProxyError:
            print(f"  PASS  {owner}/{repo} refused")

    # A filename is used as a URL segment and a cache key, so it must be bare.
    for name in ("../../secrets", "a/b.bin", ""):
        try:
            ghproxy.raw("greghulette", "NaviCore", "main", name)
            print(f"  FAIL  filename {name!r} accepted")
            bad += 1
        except ghproxy.ProxyError:
            print(f"  PASS  filename {name!r} refused")

    print()
    print("gh-proxy: " + ("OK" if not bad else f"{bad} PROBLEM(S)"))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
