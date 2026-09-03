#!/usr/bin/env python3
"""Pre-download every firmware image, so flashing works with no internet.

    python tools/fetch_firmware.py           # NaviCore + both WCB targets
    python tools/fetch_firmware.py --check   # report what is cached, fetch nothing

THE WORKFLOW. Get everything current while you have a network, disconnect, join
the droid's AP, and update the whole fleet from there. On that AP there is no
route to GitHub, and both flashers fetch their images at flash time -- so without
this the update half of the app is unavailable exactly when it is wanted.

Flashing while online already caches what it downloads (src/fwcache.py). This
fetches the whole set deliberately, including the WCB targets you did not happen
to flash, so "I did the update step" covers every board you might meet later.

Called by the build scripts and by the app's Update Firmware button.
"""
from __future__ import annotations

import argparse
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

import flash                                                        # noqa: E402
import fwcache                                                      # noqa: E402
import wcb_flash                                                    # noqa: E402


def _log(msg: str) -> None:
    print(f"  {msg}")


def fetch_navicore() -> int:
    print("NaviCore")
    try:
        # fetch_images downloads the whole set and caches each file as it goes,
        # so there is nothing to store here -- reusing it means the cache holds
        # exactly what a real flash would use, not a second opinion about it.
        imgs = flash.fetch_images(log=_log)
        names = ", ".join(i["name"] for i in imgs)
        print(f"  cached: {names}")
        return 0
    except Exception as e:                    # noqa: BLE001
        print(f"  FAILED: {e}")
        return 1


def fetch_wcb() -> int:
    rc = 0
    # BOTH families and BOTH S3 flash sizes. Which one a board needs is only known
    # once it is plugged in and esptool has answered, and that will happen offline
    # -- so guessing here defeats the point. The whole set is a few MB.
    for binary_type, flash_mb in (("ESP32", None), ("ESP32S3", 16), ("ESP32S3", 8)):
        label = binary_type + (f" {flash_mb} MB" if flash_mb else "")
        print(f"WCB {label}")
        try:
            fw = wcb_flash.fetch_images(binary_type, flash_mb, log=_log)
            got = [i["name"] for i in (fw["app"], fw["part"], fw["boot"]) if i]
            print(f"  cached: {', '.join(got)}")
            if fw["bootBlocked"]:
                print(f"  note: {fw['bootBlocked']}")
        except Exception as e:                # noqa: BLE001
            print(f"  FAILED: {e}")
            rc = 1
    return rc


def report() -> int:
    s = fwcache.summary()
    for product, label in ((fwcache.NAVICORE, "NaviCore"), (fwcache.WCB, "WCB")):
        info = s.get(product, {})
        if info.get("count"):
            print(f"{label}: {info['count']} file(s) cached, newest {info['newest']}")
        else:
            print(f"{label}: nothing cached")
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--check", action="store_true", help="report only")
    a = ap.parse_args()
    if a.check:
        return report()

    rc = fetch_navicore() or 0
    rc = fetch_wcb() or rc
    print()
    report()
    # Not fatal overall if one product failed: a cached NaviCore set is still worth
    # having when the WCB fetch was rate-limited, and the caller decides what to do.
    return rc


if __name__ == "__main__":
    sys.exit(main())
