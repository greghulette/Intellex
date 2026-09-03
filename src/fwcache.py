"""A local copy of every firmware image, so flashing works with no internet.

THE WORKFLOW THIS EXISTS FOR
Get everything current while you have a network, disconnect, join the droid's AP,
and configure and update the whole fleet from there. That is the normal way this
app is used at an event, and it has a hard consequence: **the moment you are on
the droid's SoftAP you have no route to GitHub**. Both flashers fetch their images
from the GitHub Contents API at flash time, so without a cache the entire update
half of the app is unavailable exactly when it is wanted.

So every image that is ever downloaded is also written here, and both flashers
fall back to here when the network is gone. Nothing has to be done to populate it
beyond flashing once online -- and tools/fetch_firmware.py fills it deliberately,
which is what the "Update firmware" button and the build scripts call.

WHERE IT LIVES
Same two-location rule as the bundled browser tools, and for the same reason: a
one-file frozen build unpacks to a temp directory that is deleted on exit, so a
cache written beside the app would evaporate. Writes go to the user data dir;
reads prefer it and fall back to whatever shipped inside the app. See paths.py.

WHAT IS AND IS NOT CHECKED
Images are stored under their published filename, which carries the build tag
(NaviCore_v0.2.0_022126QSEP26_ESP32S3.bin). A name is therefore a version, and a
cached file either is the build you asked for or is not there. There is no
staleness problem to solve and nothing to invalidate -- a newer build is simply a
different filename, and the manifest records what arrived and when so the UI can
say what you are carrying.
"""
from __future__ import annotations

import json
import pathlib
import time
from typing import Optional

import paths

# Subdirectory per product, because the two repos can and do publish files with
# overlapping shapes (both have *_ESP32S3_boot.bin) and one flat directory would
# have them collide silently.
NAVICORE = "navicore"
WCB = "wcb"

MANIFEST = "manifest.json"


def _read_dir() -> pathlib.Path:
    return paths.data_dir("firmware")      # user copy if present, else bundled


def _write_dir() -> pathlib.Path:
    return paths.write_dir("firmware")


def load(product: str, name: str) -> Optional[bytes]:
    """The cached bytes for one published filename, or None.

    Checks the writable copy first and the shipped copy second, so a build
    downloaded since install wins over the one that came with the app.
    """
    for base in (_write_dir(), _read_dir()):
        f = base / product / name
        if f.is_file():
            try:
                return f.read_bytes()
            except OSError:
                continue                    # unreadable is the same as absent here
    return None


def store(product: str, name: str, data: bytes) -> None:
    """Keep a downloaded image. Best effort -- never break a flash over a cache.

    Written to a temp name and renamed, so an interrupted write cannot leave a
    truncated file under a name that means "this exact build". A short image that
    looked cached would be flashed later and brick the board.
    """
    if not data:
        return
    try:
        d = _write_dir() / product
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / (name + ".part")
        tmp.write_bytes(data)
        tmp.replace(d / name)
        _touch_manifest(product, name)
    except OSError:
        pass


def _touch_manifest(product: str, name: str) -> None:
    d = _write_dir()
    f = d / MANIFEST
    try:
        m = json.loads(f.read_text(encoding="utf-8")) if f.is_file() else {}
    except (OSError, json.JSONDecodeError):
        m = {}
    m.setdefault(product, {})[name] = int(time.time())
    try:
        d.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(m, indent=1), encoding="utf-8")
    except OSError:
        pass


def summary() -> dict:
    """What is cached, for the UI. {product: {"count": n, "newest": name}}."""
    out: dict = {}
    for product in (NAVICORE, WCB):
        names = set()
        for base in (_write_dir(), _read_dir()):
            d = base / product
            if d.is_dir():
                names.update(p.name for p in d.iterdir()
                             if p.is_file() and p.suffix == ".bin")
        if not names:
            out[product] = {"count": 0, "newest": ""}
            continue
        # The app image is the interesting one -- boot/part are its companions and
        # carry the same build tag, so naming them adds nothing.
        apps = sorted(n for n in names
                      if "_boot" not in n and "_part" not in n and "bootloader" not in n)
        out[product] = {"count": len(names), "newest": apps[-1] if apps else ""}
    return out


# ── The file listing ────────────────────────────────────────────────────────
# Caching the images alone is not enough. Both flashers call the GitHub Contents
# API FIRST to discover what exists, and that call fails offline before any
# download is attempted -- so without this the cache could never be reached.
#
# The cached listing's download_url values are useless offline, and that is fine:
# download() tries the network, fails, and falls back to the bytes stored under
# the same filename. The listing's job here is only to say which files make up
# the set and what the build tag is.
LISTING = "listing.json"


def store_listing(product: str, raw: bytes) -> None:
    if not raw:
        return
    try:
        d = _write_dir() / product
        d.mkdir(parents=True, exist_ok=True)
        tmp = d / (LISTING + ".part")
        tmp.write_bytes(raw)
        tmp.replace(d / LISTING)
    except OSError:
        pass


def load_listing(product: str) -> Optional[bytes]:
    for base in (_write_dir(), _read_dir()):
        f = base / product / LISTING
        if f.is_file():
            try:
                return f.read_bytes()
            except OSError:
                continue
    return None
