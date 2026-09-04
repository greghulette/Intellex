"""Flash WCB firmware natively, for the same reason flash.py exists for NaviCore.

WHY THIS EXISTS. The Wizard flashes with esptool-js over Web Serial, which is the
right answer in a browser and unavailable to this app: navilink_shim.js presents the
host's byte pipe AS a Web Serial port, and esptool-js drives a real one far harder
than a pipe can fake -- timing-sensitive DTR/RTS to enter the bootloader, polling
readable.locked, cancelling readers mid-stream. Over a WebSocket there are no control
lines at all. So the host does it instead, with esptool as a Python package and the
real device in hand.

THIS IS A PORT OF Wizard/flasher.js, NOT A SECOND DESIGN. Same addresses, same
bootloader-selection rule, same NVS and otadata rules. Where the two must stay in
step the comments say so. Two places where this is deliberately STRICTER than
flasher.js are marked -- both concern pairing images from one build.

WHAT THE HOST DOES BETTER. flasher.js has to ask the chip for its family and flash
size over its own bootloader session; here esptool reports both from one `flash-id`
before anything is written, and the answer decides which images are even fetched.

THE PORT MUST BE OURS ALONE. esptool opens the serial device itself, so host.py has
to detach its transport first and reattach afterwards. This module just needs the
port free when it is called.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import tempfile
from typing import Optional

import certs
import fwcache
import settings
from flash import FlashError, reachable, _esptool_argv, _get, _PCT_RE

# ── Firmware source ─────────────────────────────────────────────────────────
# Mirrors flasher.js. If GITHUB_* there changes, these must too -- the whole point
# is that the app and the page flash the SAME bytes.
GITHUB_OWNER    = "greghulette"
GITHUB_REPO     = "Wireless_Communication_Board-WCB"
GITHUB_BIN_PATH = "Code/bin"
BRANCH_DEFAULT  = "main"        # the fallback; settings.py holds the choice

# ── Flash map ───────────────────────────────────────────────────────────────
# From flasher.js (its "Flash map" comment block and Step 3a):
#   ESP32:   boot -> 0x1000   part -> 0x8000   app -> 0x10000
#   ESP32S3: boot -> 0x0      part -> 0x8000   app -> 0x10000
#   nvs     @ 0x9000, 0x5000   saved config -- erased ONLY on a factory reset
#   otadata @ 0xE000, 0x2000   boot selector -- ALWAYS reset (see write_list)
ADDR_PART, ADDR_APP = 0x8000, 0x10000
ADDR_NVS,     SIZE_NVS     = 0x9000, 0x5000
ADDR_OTADATA, SIZE_OTADATA = 0xE000, 0x2000
BOOT_ADDR = {"ESP32": 0x1000, "ESP32S3": 0x0}

# esptool's chip name -> the Wizard's binary type. Only these two exist as WCB
# builds; anything else is a board this firmware was never compiled for, and
# guessing a binary for it is how you write an ESP32 image onto a C3.
CHIP_TO_TYPE = {"esp32": "ESP32", "esp32s3": "ESP32S3"}
TYPE_TO_CHIP = {v: k for k, v in CHIP_TO_TYPE.items()}


def _app_re(binary_type: str) -> re.Pattern:
    """Anchored, never a bare suffix match.

    STRICTER THAN flasher.js, which uses endsWith(). Anchoring costs nothing and
    means a file that merely ENDS the right way -- another product's image dropped
    into the same directory, a hand-renamed test build -- cannot be mistaken for a
    release. flash.py carries the same rule after a bare suffix match locked onto
    the wrong image in NaviCore's shared firmware/ directory.

    The captured group is the whole build tag (version + DTG + branch), e.g.
    "6.2.1_021242RSEP2026_main". Note _ESP32 cannot swallow an _ESP32S3 name: the
    pattern is anchored at both ends, so the two are mutually exclusive.
    """
    return re.compile(r"^WCB_(.+)_" + binary_type + r"\.bin$")


# ── GitHub ──────────────────────────────────────────────────────────────────
def list_firmware(branch: str = BRANCH_DEFAULT, log=lambda _m: None) -> list[dict]:
    url = (f"https://api.github.com/repos/{GITHUB_OWNER}/{GITHUB_REPO}"
           f"/contents/{GITHUB_BIN_PATH}?ref={branch}")
    # Offline, fall back to the listing we kept last time we could reach GitHub.
    # Its download_url values are dead, which is fine: download() below tries the
    # network, fails, and reads the bytes cached under the same filename.
    # Ask ONCE whether GitHub is even reachable. On a droid's AP a connection does
    # not fail, it times out at ~40 s -- so without this the offline path costs
    # minutes before reaching a cache that was ready all along.
    try:
        if not reachable():
            raise FlashError("GitHub is not reachable")
        raw = _get(url, log)
        fwcache.store_listing(fwcache.WCB, branch, raw)
    except FlashError:
        raw = fwcache.load_listing(fwcache.WCB, branch)
        if raw is None:
            raise
        log("  offline - using the cached firmware listing")
    try:
        files = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise FlashError(f"unexpected GitHub API response: {e}") from e
    if not isinstance(files, list):
        raise FlashError("unexpected GitHub API response (not a listing)")
    return [f for f in files if isinstance(f, dict) and f.get("type") == "file"]


def _pick_app(files: list[dict], binary_type: str) -> dict:
    """The one app image for this chip family, or an error naming the ambiguity.

    STRICTER THAN flasher.js, which takes the FIRST suffix match. Code/bin normally
    holds exactly one set because CI replaces the binaries on every release, so this
    only ever fires when something is genuinely wrong -- two builds left in the
    directory, say. Refusing then is right: the names carry a DTG that does NOT sort
    chronologically, so "first" and "newest" are unrelated, and picking wrong writes
    a stale firmware that reports a plausible version afterwards.
    """
    rx = _app_re(binary_type)
    hits = [f for f in files if rx.match(f.get("name", ""))]
    if not hits:
        raise FlashError(
            f"no {binary_type} app image (WCB_*_{binary_type}.bin) in "
            f"{GITHUB_BIN_PATH}/ -- has the firmware build published yet?")
    if len(hits) > 1:
        names = ", ".join(sorted(f["name"] for f in hits))
        raise FlashError(
            f"{len(hits)} {binary_type} app images in {GITHUB_BIN_PATH}/ and no way "
            f"to tell which is current -- the build tags do not sort by date. "
            f"Refusing to guess. Found: {names}")
    return hits[0]


def fetch_images(binary_type: str, flash_mb: Optional[int],
                 branch: str = BRANCH_DEFAULT, log=lambda _m: None) -> dict:
    """Fetch app + partition table + the RIGHT bootloader for this board.

    Returns {"version", "app", "part", "boot"} where each image is
    {"address", "data", "name"} and "boot" may be None.

    EVERY IMAGE COMES FROM ONE BUILD. The partition table and bootloader are looked
    up by the app's own build tag rather than searched for independently -- another
    place this is stricter than flasher.js, which fetches each by suffix. A table
    from a different build flashes silently and only shows up later as a partition
    that is the wrong size or missing entirely.
    """
    if branch != BRANCH_DEFAULT:
        log(f"WARNING firmware source branch is {branch!r}, not the released {BRANCH_DEFAULT!r}")
    log(f"Scanning {GITHUB_BIN_PATH}/ on GitHub ({branch})...")
    files = list_firmware(branch, log)
    by_name = {f["name"]: f for f in files}

    app_entry = _pick_app(files, binary_type)
    version = _app_re(binary_type).match(app_entry["name"]).group(1)
    log(f"Build {version} ({binary_type})")

    def download(entry) -> bytes:
        """Fetch an image, keeping a copy -- and using that copy when offline.

        Same reason as flash.py: on the droid's own AP there is no route to
        GitHub, which is exactly when a WCB most often needs updating.
        """
        name = entry["name"]
        log(f"Found: {name}")
        try:
            if not reachable():
                raise FlashError("GitHub is not reachable")
            data = _get(entry["download_url"], log)
        except FlashError:
            cached = fwcache.load(fwcache.WCB, branch, name)
            if cached is None:
                raise
            log(f"  offline - using the cached {branch} copy of {name}")
            return cached
        if not data:
            raise FlashError(f"{name} is empty")
        fwcache.store(fwcache.WCB, branch, name, data)
        return data

    app = {"address": ADDR_APP, "data": download(app_entry), "name": app_entry["name"]}

    part_name = f"WCB_{version}_{binary_type}_part.bin"
    part_entry = by_name.get(part_name)
    part = ({"address": ADDR_PART, "data": download(part_entry), "name": part_name}
            if part_entry else None)

    # ── The bootloader, and why the size must match ──────────────────────────
    # An ESP32-S3 bootloader's header DECLARES the flash size. Writing the 16 MB
    # build onto an 8 MB board (or the reverse) does not fail -- it boots and then
    # silently corrupts NVS, because the partition offsets the bootloader believes
    # in do not match the chip. flasher.js fetches both variants and picks after
    # detection for exactly this reason; here esptool has already told us the size,
    # so only the right one is ever downloaded.
    #
    # Classic ESP32 has a single compiled bootloader and no such hazard.
    boot = None
    boot_blocked = ""
    if binary_type == "ESP32S3":
        if flash_mb in (8, 16):
            # Prefer the sized name. The legacy unsized _boot.bin IS the 16 MB build
            # and is still published for back-compat, so it is the 16 MB fallback --
            # and ONLY the 16 MB fallback. There is no unsized 8 MB image to fall
            # back to, and treating one as interchangeable is the corruption above.
            cands = [f"WCB_{version}_{binary_type}_boot_{flash_mb}MB.bin"]
            if flash_mb == 16:
                cands.append(f"WCB_{version}_{binary_type}_boot.bin")
            entry = next((by_name[n] for n in cands if n in by_name), None)
            if entry:
                boot = {"address": BOOT_ADDR[binary_type], "data": download(entry),
                        "name": entry["name"]}
                log(f"Selected the {flash_mb} MB ESP32-S3 bootloader")
            else:
                boot_blocked = (f"no {flash_mb} MB ESP32-S3 bootloader in this build "
                                f"(looked for {', '.join(cands)})")
        else:
            boot_blocked = (f"could not determine the flash size"
                            if not flash_mb else
                            f"unsupported flash size {flash_mb} MB")
    else:
        name = f"WCB_{version}_{binary_type}_boot.bin"
        entry = by_name.get(name)
        if entry:
            boot = {"address": BOOT_ADDR[binary_type], "data": download(entry), "name": name}
        else:
            boot_blocked = f"no bootloader ({name}) in this build"

    kb = sum(len(i["data"]) for i in (app, part, boot) if i) // 1024
    log(f"Loaded {kb} KB ({'boot + partitions + app' if boot and part else 'app only'})")
    return {"version": version, "app": app, "part": part, "boot": boot,
            "bootBlocked": boot_blocked}


# ── Chip detection ──────────────────────────────────────────────────────────
# esptool prints these on any connect. Matched loosely on purpose: the exact
# wording has moved between esptool 4 and 5 ("Detecting chip type", "Chip is
# ESP32-S3", "Detected flash size: 16MB", "Flash size: 16MB"), and a detection that
# breaks on a phrasing change would fail CLOSED into "cannot determine flash size",
# which refuses to write a bootloader. Better to accept several spellings.
_CHIP_RE  = re.compile(r"\b(ESP32-S3|ESP32-S2|ESP32-C3|ESP32-C6|ESP32-H2|ESP32)\b")
_FLASH_RE = re.compile(r"[Ff]lash size:?\s*(\d+)\s*MB")


def detect(port: str, log=lambda _m: None) -> tuple[str, Optional[int]]:
    """Ask the board what it is. Returns (binary_type, flash_size_MB or None).

    DETECTION IS AUTHORITATIVE, exactly as flasher.js says of its own ("chip
    auto-detection is authoritative"). The Wizard's HW-version dropdown is a
    pre-fetch guess made before any board is open; this runs against the board
    itself, so it wins outright and the caller never needs the guess.
    """
    cmd = _esptool_argv() + ["--chip", "auto", "--port", port,
                             "--before", "default-reset", "--after", "no-reset",
                             "flash-id"]
    log(f"$ esptool --chip auto --port {port} flash-id")
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired) as e:
        raise FlashError(f"could not identify the board on {port}: {e}") from e

    out = (p.stdout or "") + (p.stderr or "")
    for line in out.splitlines():
        if line.strip():
            log(line.rstrip())
    if p.returncode != 0:
        raise FlashError(
            "esptool could not talk to the board on {}:\n{}".format(
                port, "\n".join(out.strip().splitlines()[-8:])))

    m = _CHIP_RE.search(out)
    if not m:
        raise FlashError(f"esptool did not report a chip family for {port}")
    chip = m.group(1).replace("-", "").lower()          # "ESP32-S3" -> "esp32s3"
    binary_type = CHIP_TO_TYPE.get(chip)
    if not binary_type:
        raise FlashError(
            f"{m.group(1)} is not a WCB target -- the firmware is built for ESP32 "
            f"and ESP32-S3 only, and there is no image to write to this chip.")

    fm = _FLASH_RE.search(out)
    flash_mb = int(fm.group(1)) if fm else None
    log(f"Chip: {m.group(1)} -> {binary_type} binary"
        + (f", {flash_mb} MB flash" if flash_mb else ", flash size unknown"))
    return binary_type, flash_mb


# ── The write list ──────────────────────────────────────────────────────────
def write_list(fw: dict, app_only: bool, erase_nvs: bool,
               log=lambda _m: None) -> list[dict]:
    """Which regions to write. Writing 0xFF makes esptool erase then rewrite.

    OTADATA IS ALWAYS RESET, update or not. The app is always written to ota_0, so
    if a previous OTA had flipped the boot selector to ota_1, leaving otadata alone
    makes the bootloader try to boot a slot that was just overwritten -- the
    rollback watchdog fires and the board reboots forever. Both 4 KB sectors go.
    flasher.js resets it on every path for the same reason.

    NVS is the ONLY difference between Update/Flash and Factory Reset.
    """
    if app_only:
        # Update FW: app only, exactly as flasher.js's appOnly path. The bootloader
        # and partition table are left alone, so no size hazard applies and a
        # missing bootloader is not a problem worth mentioning.
        images = [fw["app"]]
        log("Update: app only (bootloader and partition table left as they are).")
    else:
        if fw["bootBlocked"]:
            # REFUSE, do not quietly write app+part. A full flash that skips the
            # bootloader leaves a board whose bootloader and partition table
            # disagree -- the failure shows up later and looks like bad hardware.
            raise FlashError(
                "Cannot do a full flash: " + fw["bootBlocked"] + ".\n"
                "Writing the app and partition table without a matching bootloader "
                "would leave the board in an inconsistent state. Use Update FW "
                "(app only) instead, or wait for the firmware build to publish the "
                "missing file.")
        if not fw["part"]:
            raise FlashError(
                "Cannot do a full flash: this build has no partition table "
                f"(WCB_{fw['version']}_*_part.bin). Refusing to write a bootloader "
                "and app onto a table they were not built for.")
        images = [fw["boot"], fw["part"], fw["app"]]

    out: list[dict] = []
    if erase_nvs:
        out.append({"address": ADDR_NVS, "data": b"\xFF" * SIZE_NVS, "name": "nvs-erase"})
        log(f"Factory reset -- NVS (0x{ADDR_NVS:X}, {SIZE_NVS // 1024} KB) and the "
            "OTA boot selector will be erased.")
    else:
        log(f"OTA boot selector (0x{ADDR_OTADATA:X}) reset to ota_0; NVS/config preserved.")
    out.append({"address": ADDR_OTADATA, "data": b"\xFF" * SIZE_OTADATA,
                "name": "otadata-erase"})

    # Sorted by address, like flash.py and unlike flasher.js (which prepends the
    # blanks and hands esptool 0xE000 before 0x0). Harmless to esptool either way,
    # but an overlap is far easier to see on a sorted list.
    return sorted(out + images, key=lambda e: e["address"])


# ── esptool ─────────────────────────────────────────────────────────────────
def run_esptool(port: str, chip: str, entries: list[dict], log, progress=None,
                baud: int = 921600) -> None:
    """Write the images. Raises FlashError with esptool's own words on failure.

    Subprocess rather than esptool.main() in-process, for the reasons flash.py
    gives: esptool calls sys.exit on failure and writes to stdout, neither of which
    an aiohttp worker should inherit, and a crash in a flasher must not take the app
    down with it.

    Hyphenated option spellings ("write-flash", "--flash-mode", "default-reset").
    esptool 5 still accepts the underscore forms but warns they are deprecated, and
    those warnings land in the user's flash log looking like problems.
    """
    with tempfile.TemporaryDirectory(prefix="navilink-wcbflash-") as td:
        args = []
        for i, e in enumerate(entries):
            f = pathlib.Path(td) / f"{i}_{e['address']:#x}.bin"
            f.write_bytes(e["data"])
            args += [hex(e["address"]), str(f)]

        cmd = _esptool_argv() + [
            "--chip", chip, "--port", port, "--baud", str(baud),
            "--before", "default-reset", "--after", "hard-reset",
            "write-flash",
            # keep/keep/keep: the image header already carries what the board needs,
            # and overriding it is how you brick a variant you did not know about.
            "--flash-mode", "keep", "--flash-freq", "keep", "--flash-size", "keep",
            "--compress",
        ] + args

        log(f"$ esptool --chip {chip} --port {port} --baud {baud} write-flash ...")
        try:
            p = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                 stderr=subprocess.STDOUT, text=True, bufsize=1)
        except FileNotFoundError as e:
            raise FlashError(f"could not start esptool: {e}") from e

        tail = []
        assert p.stdout is not None
        for line in p.stdout:
            line = line.rstrip()
            if not line:
                continue
            tail.append(line)
            del tail[:-40]
            log(line)
            if progress:
                m = _PCT_RE.search(line)
                if m:
                    progress(int(m.group(1)))
        rc = p.wait()
        if rc != 0:
            raise FlashError("esptool failed (exit {}):\n{}".format(rc, "\n".join(tail[-12:])))


def flash(port: str, app_only: bool, erase_nvs: bool, log, progress=None,
          branch: str = "") -> str:
    """Detect, fetch and write. Returns the build flashed. Port must be free.

    Detection FIRST, before a single byte is downloaded. It decides the chip family
    (which images even exist) and the flash size (which S3 bootloader is safe), so
    fetching first would mean downloading a set that detection then invalidates.
    """
    # Read HERE, not as a default argument: a default is evaluated once at import
    # and would pin the branch for the life of the process.
    branch = branch or settings.branch(settings.WCB)
    binary_type, flash_mb = detect(port, log)
    fw = fetch_images(binary_type, flash_mb, branch, log)
    entries = write_list(fw, app_only, erase_nvs, log)
    total_kb = sum(len(e["data"]) for e in entries) // 1024
    log(f"Writing {total_kb} KB across {len(entries)} region(s) to {port}...")
    run_esptool(port, TYPE_TO_CHIP[binary_type], entries, log, progress)
    return fw["version"]
