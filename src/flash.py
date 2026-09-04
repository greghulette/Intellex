"""Flash NaviCore firmware natively, because the browser cannot get there from here.

WHY THIS EXISTS. The config tool flashes with esptool-js over Web Serial, and that
is the right answer in a browser. It is not available to this app: navilink_shim.js
presents the host's byte pipe AS a Web Serial port, and esptool-js drives a real one
far harder than a pipe can fake -- it toggles DTR/RTS in a timing-sensitive reset
dance to enter the bootloader, polls readable.locked / writable.locked, and cancels
readers mid-stream. Over a WebSocket there are no control lines at all, so the board
never enters download mode and esptool sits printing "... ___ ..." forever. The shim
therefore used to disable the flash buttons outright, which is honest but leaves the
app unable to do the one thing a native host is BETTER at than the page.

So do it the native way instead: esptool as a Python package, driving the real port
directly, exactly as ESP-Flasher-Companion does. Same binaries, same addresses, same
NVS rules as flasher.js -- this is a port of that logic, not a second design, and the
comments below record where the two must stay in step.

THE PORT MUST BE OURS ALONE. esptool opens the serial device itself, so the host has
to detach its transport first and reattach afterwards. host.py owns that dance; this
module just needs the port to be free when it is called.
"""
from __future__ import annotations

import json
import pathlib
import re
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request

import certs
import fwcache
import settings


# Cache the answer for a short while. A single firmware set is ~4 files plus a
# listing, and re-probing before each one turns one 3 s check into five.
_reach_at = 0.0
_reach_ok = False
REACH_TTL_S = 20.0


def reachable(host: str = "api.github.com", timeout: float = 3.0) -> bool:
    """Is GitHub actually reachable? One TCP connect, answered in ~3 s.

    WHY A PROBE AND NOT AN EXCEPTION CHECK. On a droid's SoftAP there IS a default
    route -- it just goes nowhere -- so a connection does not fail, it TIMES OUT.
    Measured at 40 s per attempt on that network, and urlopen's own timeout does
    not bound it because DNS and connect retry underneath. Four attempts across a
    ~35 file tool update is tens of minutes of certain failure.

    A timeout cannot be treated as "offline" in general (no_network() deliberately
    does not), because a real network does time out transiently. But asking ONCE
    whether the host is reachable at all separates the two cleanly: unreachable
    means go to cache now, reachable means a later timeout is worth retrying.
    """
    global _reach_at, _reach_ok
    now = time.monotonic()
    if now - _reach_at < REACH_TTL_S:
        return _reach_ok
    import socket
    try:
        with socket.create_connection((host, 443), timeout=timeout):
            _reach_ok = True
    except OSError:
        _reach_ok = False
    _reach_at = now
    return _reach_ok


def no_network(e: BaseException) -> bool:
    """Is this "there is no network" rather than "the network misbehaved"?

    Worth telling apart because the retry loop below is built for GitHub's
    throttles -- four attempts with a growing backoff, ~9 s in all. That is right
    for a 429 and completely wrong on a droid's SoftAP, where there is no route to
    GitHub and never will be: every one of the ~10 firmware files would burn its
    full backoff before falling back to cache, turning an offline flash into
    minutes of waiting for a foregone conclusion. Measured at over two minutes for
    one full set.

    A timeout is deliberately NOT in here: that really can be transient, and it is
    the case the backoff exists for.
    """
    import errno
    import socket
    reason = getattr(e, "reason", e)
    if isinstance(reason, socket.gaierror):
        return True                      # DNS did not resolve -- nothing is up
    if isinstance(reason, socket.timeout) or isinstance(reason, TimeoutError):
        return False
    if isinstance(reason, OSError) and reason.errno in (
            errno.ENETUNREACH, errno.EHOSTUNREACH, errno.ENETDOWN,
            errno.ECONNREFUSED, errno.ECONNRESET, errno.EHOSTDOWN):
        return True
    return False

# ── Firmware source ─────────────────────────────────────────────────────────
# Mirrors flasher.js exactly. If that file's constants change, these must too --
# the whole point is that the app and the page flash the SAME bytes.
GITHUB_OWNER    = "greghulette"
GITHUB_REPO     = "NaviCore"
GITHUB_BIN_PATH = "firmware"
BRANCH_DEFAULT  = "main"        # the fallback; settings.py holds the choice

# Anchored, never a bare suffix match. firmware/ is a SHARED directory: it also
# holds another product's images (RC-Controller_*_ESP32S3*.bin) and may hold older
# NaviCore builds. An endsWith() match locked onto the wrong (oldest) image once
# already -- and DTG tags do not sort chronologically, so alphabetical order is no
# safety net.
APP_RE          = re.compile(r"^NaviCore_(.+)_ESP32S3\.bin$")
# FIXED name, not a per-build one: CI also emits a stock NaviCore_<v>_ESP32S3_boot.bin
# which is NOT this. This is the custom short-WDT 16MB bootloader, the matched pair of
# the firmware's in-app boot guard.
BOOTLOADER_NAME = "WCB_S3_custom_bootloader_16MB_wdt3s.bin"

# ── Flash map (ESP32-S3, 16 MB, the custom table in partitions.csv) ─────────
#   boot     @ 0x0
#   part     @ 0x8000
#   nvs      @ 0x9000,   0x5000   saved config -- erased ONLY on a full wipe
#   otadata  @ 0xE000,   0x2000   boot selector -- ALWAYS reset (see below)
#   app0     @ 0x10000
ADDR_BOOT, ADDR_PART, ADDR_APP = 0x0, 0x8000, 0x10000
ADDR_NVS,     SIZE_NVS     = 0x9000, 0x5000
ADDR_OTADATA, SIZE_OTADATA = 0xE000, 0x2000

CHIP = "esp32s3"


class FlashError(Exception):
    """Anything that stopped the flash, phrased for someone holding a board."""


# ── GitHub ──────────────────────────────────────────────────────────────────
def _get(url: str, log, attempts: int = 4, timeout: float = 60.0) -> bytes:
    """Fetch, retrying the throttles GitHub actually uses.

    429 and 5xx are the obvious ones; 403 is the one that surprises people, because
    GitHub returns it for abuse throttling as well as for auth. Honour Retry-After
    when it is offered -- guessing shorter than GitHub asked for is how a throttle
    becomes a ban.
    """
    last: BaseException = FlashError("no attempt made")
    for i in range(attempts):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "NaviLink"})
            with urllib.request.urlopen(req, timeout=timeout,
                                        context=certs.context()) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in (403, 429) and e.code < 500:
                break
            wait = 0.0
            try:
                wait = float(e.headers.get("Retry-After") or 0)
            except (TypeError, ValueError):
                wait = 0.0
            wait = wait or 1.5 * (i + 1)
            if i < attempts - 1:
                log(f"GitHub said {e.code}; waiting {wait:.0f}s and retrying")
                time.sleep(wait)
                continue
        except (urllib.error.URLError, OSError) as e:
            last = e
            if certs.is_cert_error(e):
                break                      # retrying will not grow a CA store
            if no_network(e):
                break                      # offline: fail fast so the cache is reached
            if i < attempts - 1:
                time.sleep(1.5 * (i + 1))
                continue
    if certs.is_cert_error(last):
        raise FlashError(f"cannot reach GitHub -- {last}\n{certs.ADVICE}")
    raise FlashError(f"cannot reach GitHub: {url} -- {last}")


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
        fwcache.store_listing(fwcache.NAVICORE, branch, raw)
    except FlashError:
        raw = fwcache.load_listing(fwcache.NAVICORE, branch)
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


def latest_version(branch: str = BRANCH_DEFAULT) -> str:
    """The version the Update button would write, from the app image's filename.

    A flashed board reports the SAME string in its PONG, so the two compare directly.
    """
    for f in list_firmware(branch):
        m = APP_RE.match(f.get("name", ""))
        if m:
            return m.group(1)
    raise FlashError("no NaviCore app image (NaviCore_*_ESP32S3.bin) in firmware/")


def fetch_images(branch: str = BRANCH_DEFAULT, log=lambda _m: None) -> list[dict]:
    """The images to write, ascending by address. Port of flasher.js fetchFirmwareImages.

    BOOTLOADER AND PARTITION TABLE ARE A PAIR -- both or neither. Exactly one present
    means a corrupted or half-finished firmware upload, and falling back to app-only
    there would write an app onto a board with no matching table: unbootable, and the
    damage only shows up later. So refuse loudly instead.
    """
    if branch != BRANCH_DEFAULT:
        log(f"WARNING firmware source branch is {branch!r}, not the released {BRANCH_DEFAULT!r}")
    log(f"Scanning {GITHUB_BIN_PATH}/ on GitHub ({branch})...")
    files = list_firmware(branch, log)

    app_entry = next((f for f in files if APP_RE.match(f["name"])), None)
    if not app_entry:
        raise FlashError("no NaviCore app image (NaviCore_*_ESP32S3.bin) in firmware/")
    version = APP_RE.match(app_entry["name"]).group(1)

    def download(entry):
        """Fetch an image, keeping a copy -- and using that copy when offline.

        The cache is what makes the whole update path work on a droid's AP, where
        there is no route to GitHub by definition. See src/fwcache.py.
        """
        name = entry["name"]
        log(f"Found: {name}")
        try:
            if not reachable():
                raise FlashError("GitHub is not reachable")
            data = _get(entry["download_url"], log)
        except FlashError:
            cached = fwcache.load(fwcache.NAVICORE, branch, name)
            if cached is None:
                raise
            log(f"  offline - using the cached {branch} copy of {name}")
            return cached
        if not data:
            raise FlashError(f"{name} is empty")
        fwcache.store(fwcache.NAVICORE, branch, name, data)
        return data

    images = [{"address": ADDR_APP, "data": download(app_entry), "name": app_entry["name"]}]

    # The table must carry the app's EXACT version, so an app and a table from two
    # different builds can never be paired. A mismatched table flashes silently and
    # then, for instance, leaves clipsFS unmounted because its row is missing.
    part_name = f"NaviCore_{version}_ESP32S3_part.bin"
    by_name   = {f["name"]: f for f in files}
    boot_e, part_e = by_name.get(BOOTLOADER_NAME), by_name.get(part_name)

    if boot_e and part_e:
        images.insert(0, {"address": ADDR_PART, "data": download(part_e), "name": part_name})
        images.insert(0, {"address": ADDR_BOOT, "data": download(boot_e), "name": BOOTLOADER_NAME})
    elif boot_e or part_e:
        missing = f"partition table ({part_name})" if boot_e else f"custom bootloader ({BOOTLOADER_NAME})"
        raise FlashError(
            f"Incomplete firmware on GitHub: the {missing} is missing while its pair "
            "is present. Refusing to flash a partial set -- app-only onto a blank "
            "board would leave it unbootable. Re-run the firmware build/upload.")
    else:
        log("Note: bootloader/partition files not on GitHub -- flashing app only.")
        log("      A blank board will need a one-time full flash via Arduino IDE.")

    kb = sum(len(i["data"]) for i in images) // 1024
    log(f"Loaded {kb} KB ({'boot + partitions + app' if boot_e and part_e else 'app only'})")
    return images


def write_list(images: list[dict], erase_nvs: bool, log=lambda _m: None) -> list[dict]:
    """Prepend the blank regions. Writing 0xFF makes esptool erase then rewrite.

    OTADATA IS ALWAYS RESET, wipe or not. The app is always written to ota_0, so if a
    previous OTA had flipped the boot selector to ota_1, leaving otadata alone makes
    the bootloader try to boot a slot that was just overwritten -- the rollback
    watchdog fires and the board reboots forever. Both of its 4 KB sectors must go.

    NVS is the ONLY difference between Update and Full Wipe.
    """
    out = []
    if erase_nvs:
        out.append({"address": ADDR_NVS, "data": b"\xFF" * SIZE_NVS, "name": "nvs-erase"})
        log(f"NVS (0x{ADDR_NVS:X}, {SIZE_NVS // 1024} KB) and OTA data will be erased.")
    else:
        log(f"OTA boot selector (0x{ADDR_OTADATA:X}) reset to ota_0 (config preserved).")
    out.append({"address": ADDR_OTADATA, "data": b"\xFF" * SIZE_OTADATA, "name": "otadata-erase"})

    # Sorted, which flasher.js is NOT: it prepends the blanks, leaving otadata
    # (0xE000) ahead of the bootloader (0x0) in the list it hands esptool. That is
    # harmless today because esptool takes the pairs in any order, but it makes the
    # write list disagree with the "ascending address order" the code says it keeps,
    # and an overlap check is far easier to reason about on a sorted list.
    return sorted(out + images, key=lambda e: e["address"])


# ── esptool ─────────────────────────────────────────────────────────────────
def _esptool_argv() -> list[str]:
    """How to invoke esptool from wherever this is running.

    A frozen build cannot spawn `python -m esptool`: there is no python, and the
    interpreter IS the app. ESP-Flasher-Companion's answer is to re-invoke ITSELF
    with a sentinel argument that app.py intercepts before anything else starts --
    see --run-esptool there and in app.py here.
    """
    if getattr(sys, "frozen", False):
        return [sys.executable, "--run-esptool"]
    return [sys.executable, "-m", "esptool"]


_PCT_RE = re.compile(r"\((\d+)\s*%\)")


def run_esptool(port: str, entries: list[dict], log, progress=None,
                baud: int = 921600) -> None:
    """Write the images. Raises FlashError with esptool's own words on failure.

    Subprocess rather than esptool.main() in-process: esptool calls sys.exit on
    failure and writes to stdout, neither of which an aiohttp worker should inherit,
    and a crash in a flasher must not take the app down with it.
    """
    with tempfile.TemporaryDirectory(prefix="navilink-flash-") as td:
        args = []
        for i, e in enumerate(entries):
            f = pathlib.Path(td) / f"{i}_{e['address']:#x}.bin"
            f.write_bytes(e["data"])
            args += [hex(e["address"]), str(f)]

        cmd = _esptool_argv() + [
            "--chip", CHIP, "--port", port, "--baud", str(baud),
            "--before", "default_reset", "--after", "hard_reset",
            "write_flash",
            # keep/keep/keep: the header already carries what the board needs, and
            # overriding it is how you brick a variant you did not know about.
            "--flash_mode", "keep", "--flash_freq", "keep", "--flash_size", "keep",
            "--compress",
        ] + args

        log(f"$ esptool --chip {CHIP} --port {port} --baud {baud} write_flash ...")
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


def flash(port: str, erase_nvs: bool, log, progress=None,
          branch: str = "") -> str:
    """Fetch and write. Returns the version flashed. The port must already be free.

    An empty branch means "whatever is configured", read HERE rather than bound as
    a default argument -- a default is evaluated once at import and would pin the
    branch for the life of the process, so changing it would need a restart.
    """
    branch = branch or settings.branch(settings.NAVICORE)
    images = fetch_images(branch, log)
    version = APP_RE.match(images[-1]["name"]).group(1)
    entries = write_list(images, erase_nvs, log)
    total_kb = sum(len(e["data"]) for e in entries) // 1024
    log(f"Writing {total_kb} KB across {len(entries)} region(s) to {port}...")
    run_esptool(port, entries, log, progress)
    return version
