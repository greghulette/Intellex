#!/usr/bin/env python3
"""Refresh a bundled browser tool from GitHub Pages.

    python tools/fetch_webui.py                  # NaviCore config tool
    python tools/fetch_webui.py --tool wcb       # WCB Wizard
    python tools/fetch_webui.py --tool all       # both
    python tools/fetch_webui.py --check          # report only, change nothing
    python tools/fetch_webui.py --force          # re-fetch even if unchanged

This is what the app's Update button calls. The public NaviCore and WCB repos
stay the single source of truth for their own UI; this copies, never edits, so
each bundled file hashes identical to the published one and there is no fork.

NEITHER TOOL IS ONE FILE. The NaviCore tool's index.html loads flasher.js,
serial-hub.js and ~27 files under cmdlib/; the Wizard loads parser.js,
flasher.js, device-labels.js, serial-hub.js, app.js, a stylesheet and two
vendored flash libraries. All at RELATIVE paths at runtime, so fetching only
index.html leaves a stale library silently mismatched against a newer UI --
broken in a way that looks like a tool bug rather than a bad update. Each set
moves together, or not at all.

ATOMIC AND REVERTIBLE. Everything lands in a temp directory first and is only
swapped in once every file has arrived. A half-applied update would leave no way
to configure the droid, which is the one thing this app must never do.

THE TWO TOOLS UPDATE INDEPENDENTLY. They come from different repos with
different release cadences, so a Wizard fetch failing offline must not roll back
a NaviCore tool that updated fine seconds earlier -- and vice versa. --tool all
runs them as two separate atomic swaps for exactly that reason.
"""
from __future__ import annotations

import argparse
import pathlib
import re
import shutil
import socket
import sys
import tempfile
import time
import urllib.error
import urllib.request

# The app's own TLS handling rather than a second copy of it: this and the
# launcher's "is there an update" probe hit the same host and must fail the same
# way, with the same explanation. See src/certs.py for why a stock macOS Python
# cannot verify anything at all.
# append, not insert(0): this is a tool, and it has no business shadowing an
# installed package with a same-named file from src/.
sys.path.append(str(pathlib.Path(__file__).resolve().parent.parent / "src"))
import certs                                                        # noqa: E402
import paths                                                        # noqa: E402
import settings                                                     # noqa: E402
from flash import no_network, reachable                             # noqa: E402

# ── Per-branch tool previews ────────────────────────────────────────────────
# CI publishes the tools per branch to gh-pages under /dev/<branch>/, so a branch
# has its own UI as well as its own firmware. Those two must MATCH: a branch build
# that adds a config field is unreachable from a main-branch UI that has no widget
# for it, and the mismatch shows up as "the tool cannot see the new setting"
# rather than as a version problem.
#
# The layout under /dev/<branch>/ is the same as the site root -- Wizard/ and
# Images/ as siblings, config_tool/ and Images/ as siblings -- so the "../Images"
# resolution the bundling depends on carries over unchanged. Verified against
# gh-pages rather than assumed.
NAVICORE_SITE = "https://greghulette.github.io/NaviCore"
WCB_SITE      = "https://greghulette.github.io/Wireless_Communication_Board-WCB"


def base_for(site: str, leaf: str, branch: str) -> str:
    """Where a tool lives for `branch`. Delegates so there is ONE definition.

    settings.tool_base owns it, because the launcher's version check needs the
    same answer and a second copy here is how they came to disagree.
    """
    product = settings.WCB if leaf == "Wizard" else settings.NAVICORE
    return settings.tool_base(product, branch)


def resolve_base(site: str, leaf: str, branch: str) -> tuple[str, str]:
    """The branch's tool if it is published, else main's. Returns (base, note).

    NOT EVERY BRANCH PUBLISHES A TOOL. The two repos differ today: WCB deploys
    /dev/<branch>/Wizard for its branches, NaviCore's gh-pages has no /dev/ tree at
    all. A branch set for FIRMWARE reasons would then 404 the whole tool update --
    turning "I want the WIFI firmware" into "my config tool stopped updating",
    which is not a connection anyone would make.

    So probe, and fall back to main saying so. A branch whose tool is unchanged
    from main is the common case anyway; the fallback is right far more often than
    it is a compromise.
    """
    if not branch or branch == "main":
        return f"{site}/{leaf}", ""
    dev = base_for(site, leaf, branch)
    try:
        get(f"{dev}/index.html", timeout=15, attempts=1)
        return dev, f"branch {branch}"
    except FetchError:
        return (f"{site}/{leaf}",
                f"branch {branch} publishes no tool - using main's")


BASE = f"{NAVICORE_SITE}/config_tool"
SRC = pathlib.Path(__file__).resolve().parent.parent / "src"
# Writes go to the USER data dir in a frozen app: a one-file build unpacks
# itself to a temp directory that is deleted on exit, so an update written
# beside the app vanishes silently. See src/paths.py.
WEBUI = paths.write_dir("webui")

# Frozen snapshots nothing loads (docs/CONFIG_TOOL.md) -- deliberately excluded.
ROOT_FILES = ["index.html", "flasher.js", "serial-hub.js"]

# ── The WCB Wizard ──────────────────────────────────────────────────────────
WCB_BASE  = f"{WCB_SITE}/Wizard"
WEBUI_WCB = paths.write_dir("webui_wcb")

# STAGED UNDER Wizard/, NOT AT THE ROOT. index.html reaches its logos with
# "../Images/<name>", which on Pages resolves because /Wizard/ and /Images/ are
# siblings. Reproducing that pair here is what lets host.py mount the lot at
# /wcb/ and serve the published file untouched -- flattening it would mean
# rewriting paths inside index.html, i.e. forking it.
WCB_SUBDIR = "Wizard"
WCB_FILES = [
    "index.html",
    "app.js",              # carries UI_VERSION -- the Wizard's version stamp
    "parser.js",
    "flasher.js",
    "device-labels.js",
    "serial-hub.js",
    "styles.css",
    "favicon.png",
    "manifest.json",
]
# Vendored flash libraries. Bundled even though Intellex flashes natively and
# never loads them: the whole contract is that the bundled copy is the published
# copy, and a deliberately incomplete one is a fork with extra steps. They also
# cost 227 KB and make the bundle work unchanged if it is ever opened directly.
# esptool-js's bundle is self-contained (vendor/README.md) -- no chunk files to
# chase.
WCB_VENDOR = [
    "vendor/README.md",
    "vendor/crypto-js/crypto-js-4.2.0.min.js",
    "vendor/esptool-js/esptool-js-0.4.7.bundle.js",
]
# Siblings of Wizard/ on gh-pages, same as NaviCore's Images. Named explicitly
# because Pages serves no directory index and the repo's Images/ also holds
# large art the Wizard never references.
WCB_IMAGES = ["r2logo.png", "navicore-icon.png", "kyberLogo.png", "qr-code.png"]

# The Wizard has no footer-dtg. Its stamp is a JS constant in app.js, written by
# the WCB repo's pre-commit hook and by Wizard/watch-version.js -- the same role
# footer-dtg plays for the NaviCore tool, so it is the honest version handle here.
WCB_VER_RE = re.compile(r"""\bUI_VERSION\s*=\s*['"]([^'"]+)['"]""")

# Firmware binaries are not fetched HERE -- they are a different thing on a
# different cadence, so tools/fetch_firmware.py owns them. They ARE kept locally
# though: on a droid's SoftAP there is no route to GitHub, so without a cache the
# whole update path is unavailable exactly when it is wanted. See src/fwcache.py.

# Images live BESIDE config_tool/ in the NaviCore repo, and index.html reaches
# them with "../Images/<name>". On Pages that resolves because the site root holds
# both directories. Here index.html is served AT the root, so the browser clamps
# the ".." and asks for "/Images/<name>" -- which works, but only if the files are
# actually bundled. They were not, so both footer images were broken in the app
# while looking fine on the published site.
#
# Named explicitly rather than discovered: Pages serves no directory index, and
# the repo's Images/ folder also holds large logo art the tool never references.
IMAGES = ["qr-code.png", "r2logo.png"]

# The tool stamps this on every commit via NaviCore's pre-commit hook, so it is
# the honest version handle -- no guessing, no separate manifest to drift.
DTG_RE = re.compile(r'id="footer-dtg"[^>]*>([^<]+)<')


class FetchError(Exception):
    """A fetch that did not survive its retries, carrying the URL that failed.

    The bare traceback this used to raise named urlopen but not the file, and
    "HTTP Error 503" thirty files into a thirty-five file update is not actionable
    without knowing which one.
    """

    def __init__(self, url: str, cause: BaseException):
        super().__init__(f"{url} -- {type(cause).__name__}: {cause}")
        self.url = url
        self.cause = cause


# Pages throttles a burst, and this fetches ~35 files back to back. The first real
# Mac run died on a 503 partway through cmdlib and aborted the whole update -- yet
# every one of those files fetched fine individually seconds later. So the failure
# was pacing, not availability, and retrying is the honest response to it.
RETRY_STATUS = frozenset({408, 429, 500, 502, 503, 504})


def get(url: str, timeout: float = 30.0, attempts: int = 4) -> bytes:
    last: BaseException = RuntimeError("no attempt made")
    for i in range(attempts):
        try:
            with urllib.request.urlopen(url, timeout=timeout,
                                        context=certs.context()) as r:
                return r.read()
        except urllib.error.HTTPError as e:
            last = e
            if e.code not in RETRY_STATUS:
                break                     # a 404 will still be a 404 in two seconds
        except (urllib.error.URLError, socket.timeout, TimeoutError) as e:
            last = e
            # Offline, give up at once. The backoff below exists for Pages
            # throttling a burst of ~35 files; on a droid's AP there is no route
            # to github.io and never will be, so retrying every file is minutes
            # of waiting for a foregone conclusion. flash.py learned this the
            # same way -- one shared implementation so they cannot disagree.
            if no_network(e):
                break
            if certs.is_cert_error(e):
                break                     # no amount of retrying grows a CA store
        if i < attempts - 1:
            time.sleep(0.5 * 2 ** i)      # 0.5s, 1s, 2s -- ~3.5s of patience in all
    raise FetchError(url, last)


def dtg_of(html: str) -> str:
    m = DTG_RE.search(html)
    return m.group(1).strip() if m else "unknown"


def local_dtg() -> str:
    f = WEBUI / "index.html"
    if not f.is_file():
        return "(none bundled)"
    return dtg_of(f.read_text(encoding="utf-8", errors="replace"))


# cmdlib is per-VENDOR, each with its own manifest -- verified against the real
# tree, not assumed: cmdlib/droidnet/manifest.json + boards/*.json, and
# cmdlib/navicore/manifest.json + navicore.json. index.html hard-codes
# NC_DROIDNET_LOCAL_MANIFEST = 'cmdlib/droidnet/manifest.json', so the vendor
# directories are a real contract, not an implementation detail.
CMDLIB_VENDORS = ["droidnet", "navicore"]
# Provenance files the tool does not load but which must travel with the data.
CMDLIB_EXTRA = ["LICENSE", "NOTICE.md"]


def cmdlib_names(base: str) -> list[str]:
    """List every cmdlib file to fetch, discovered from each vendor's manifest.

    Pages serves no directory index, so the alternative is a hard-coded file list
    that rots the first time a board is added. Each manifest names its own boards;
    read them and take exactly the set the tool would load.
    """
    import json
    out: list[str] = []
    for vendor in CMDLIB_VENDORS:
        man = f"{vendor}/manifest.json"
        try:
            raw = get(f"{base}/cmdlib/{man}").decode("utf-8")
        except Exception:
            continue                      # a vendor may legitimately not exist
        out.append(man)
        names: set[str] = set()

        def walk(o):
            if isinstance(o, str) and o.endswith(".json"):
                names.add(o.lstrip("./"))
            elif isinstance(o, dict):
                for v in o.values():
                    walk(v)
            elif isinstance(o, list):
                for v in o:
                    walk(v)

        try:
            walk(json.loads(raw))
        except json.JSONDecodeError:
            continue
        for n in sorted(names):
            # Manifests may name a board bare or path-relative; normalise to the
            # vendor directory either way.
            if not _safe_relname(n):
                print(f"  skipping unsafe manifest entry: {n!r}")
                continue
            out.append(n if n.startswith(f"{vendor}/") else f"{vendor}/{n}")

        for extra in CMDLIB_EXTRA:
            try:
                get(f"{base}/cmdlib/{vendor}/{extra}")
            except Exception:
                continue
            out.append(f"{vendor}/{extra}")

    # De-dup, preserve order.
    seen, uniq = set(), []
    for n in out:
        if n not in seen:
            seen.add(n)
            uniq.append(n)
    return uniq


def _safe_relname(name: str) -> bool:
    """Is this manifest entry safe to use as a path under the staging directory?

    These names come from a manifest fetched over the network and are then joined
    onto a real directory and written. lstrip("./") only neutralises LEADING
    traversal, so "vendor/../../../evil.json" survived it intact.

    The source is greghulette.github.io, so this is defence in depth rather than a
    live hole -- but it is the one place in the app where remote data decides where
    bytes land on disk, which is worth being strict about.
    """
    import ntpath
    import posixpath
    if not name or name.startswith(("/", "\\")):
        return False
    if ntpath.splitdrive(name)[0]:            # C:, \\server\share
        return False
    parts = name.replace("\\", "/").split("/")
    if any(p in ("", ".", "..") for p in parts):
        return False
    return not posixpath.isabs(name)


def _swap_in(staged: pathlib.Path, dest: pathlib.Path) -> bool:
    """Move staged over dest, keeping the old one as <dest>.prev.

    Returns whether a previous bundle was actually kept -- on a fresh clone there
    is none, and pointing at a backup directory that was never created is exactly
    the wrong thing to believe if the new bundle turns out to be bad.
    """
    backup = dest.with_suffix(".prev")
    kept = False
    if dest.exists():
        if backup.exists():
            shutil.rmtree(backup)
        dest.rename(backup)
        kept = True
    shutil.move(str(staged), str(dest))
    return kept


def wcb_local_version() -> str:
    f = WEBUI_WCB / WCB_SUBDIR / "app.js"
    if not f.is_file():
        return "(none bundled)"
    m = WCB_VER_RE.search(f.read_text(encoding="utf-8", errors="replace"))
    return m.group(1).strip() if m else "unknown"


def fetch_wcb(base: str, check: bool, force: bool) -> int:
    """Refresh the bundled WCB Wizard. Same shape as the NaviCore path above."""
    have = wcb_local_version()
    print(f"[wcb] bundled  : {have}")

    try:
        # app.js first: it carries the version stamp, so fetching it up front
        # answers "is there anything to do" before pulling the other ~1 MB.
        app_bytes = get(f"{base}/app.js")
    except FetchError as e:
        print(f"[wcb] cannot reach {base}")
        print(f"  {e}")
        if certs.is_cert_error(e.cause):
            print(certs.ADVICE)
        else:
            print("(offline is fine -- the bundled copy keeps working)")
        return 1

    m = WCB_VER_RE.search(app_bytes.decode("utf-8", errors="replace"))
    remote = m.group(1).strip() if m else "unknown"
    print(f"[wcb] published: {remote}")

    # "unknown" on either side is not a match to act on: a stamp we could not read
    # says nothing about whether the copies differ, and treating two unknowns as
    # equal would wedge the bundle at whatever it happens to be. --force still works.
    if remote == have and remote != "unknown" and not force:
        print("[wcb] up to date")
        return 0
    if check:
        print("[wcb] update available (run without --check to apply)")
        return 0

    staged = pathlib.Path(tempfile.mkdtemp(prefix="intellex-wcbui-"))
    try:
        wiz = staged / WCB_SUBDIR
        wiz.mkdir(parents=True)
        # Bytes, never text. write_text() on Windows turns every \n into \r\n,
        # which silently breaks the byte-identical guarantee this depends on --
        # it added 20,558 stray CR bytes to the NaviCore index.html once already.
        (wiz / "app.js").write_bytes(app_bytes)
        got = 1
        for name in WCB_FILES:
            if name == "app.js":
                continue                    # already have it
            (wiz / name).write_bytes(get(f"{base}/{name}"))
            got += 1

        for rel in WCB_VENDOR:
            dest = wiz / rel
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(get(f"{base}/{rel}"))
            got += 1

        # ".../Wizard" -> ".../Images", the sibling the page reaches with "..".
        img_base = base.rsplit("/", 1)[0] + "/Images"
        (staged / "Images").mkdir()
        for name in WCB_IMAGES:
            try:
                (staged / "Images" / name).write_bytes(get(f"{img_base}/{name}"))
                got += 1
            except Exception as e:
                # Not fatal, but say so. A missing decoration must not block a tool
                # update -- and a silent skip is how the NaviCore images went
                # missing from the bundle in the first place.
                print(f"  could not fetch Images/{name}: {type(e).__name__}")

        kept = _swap_in(staged, WEBUI_WCB)
        staged = None
        print(f"[wcb] updated to {remote}  ({got} files)")
        if kept:
            print(f"[wcb] previous kept at {WEBUI_WCB.with_suffix('.prev')}")
        return 0
    except FetchError as e:
        print(f"[wcb] update failed: {e}")
        if certs.is_cert_error(e.cause):
            print(certs.ADVICE)
        print("[wcb] nothing was changed -- the bundled copy is still in place")
        return 1
    finally:
        if staged and staged.exists():
            shutil.rmtree(staged, ignore_errors=True)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=None,
                    help="override the Pages base URL for the selected tool")
    ap.add_argument("--tool", choices=["navicore", "wcb", "all"], default="navicore",
                    help="which bundled tool to refresh (default: navicore)")
    ap.add_argument("--check", action="store_true", help="report only")
    ap.add_argument("--force", action="store_true", help="fetch even if unchanged")
    a = ap.parse_args()

    # One probe, up front. Offline this turns tens of minutes of certain failure
    # into three seconds and a clear message -- on a droid's AP a connection times
    # out rather than failing, so every file would wait ~40 s to learn the same
    # thing. Nothing here has a cache to fall back to: unlike firmware, a bundled
    # tool is already on disk and simply stays as it is.
    if not reachable("greghulette.github.io"):
        print("github.io is not reachable - the bundled tools are unchanged.")
        print("  (on a droid's AP there is no route out; reconnect to update)")
        return 1

    if a.tool == "wcb":
        base = a.base
        if not base:
            base, note = resolve_base(WCB_SITE, "Wizard", settings.branch(settings.WCB))
            if note:
                print(f"[wcb] {note}")
        return fetch_wcb(base, a.check, a.force)
    if a.tool == "all":
        if a.base:
            # One --base cannot mean two different sites, and quietly applying it
            # to whichever tool ran first is worse than refusing.
            return ap.error("--base applies to one tool; use --tool navicore or --tool wcb") or 2
        # SEPARATE swaps, and the NaviCore result does not gate the WCB one. Each
        # tool is independently atomic; a Wizard fetch that fails offline must not
        # undo or skip a NaviCore update that already succeeded.
        nb, note = resolve_base(NAVICORE_SITE, "config_tool",
                                settings.branch(settings.NAVICORE))
        if note:
            print(f"[navicore] {note}")
        rc_nc = _fetch_navicore(nb, a.check, a.force)
        wb, note = resolve_base(WCB_SITE, "Wizard", settings.branch(settings.WCB))
        if note:
            print(f"[wcb] {note}")
        rc_wcb = fetch_wcb(wb, a.check, a.force)
        return rc_nc or rc_wcb
    base = a.base
    if not base:
        base, note = resolve_base(NAVICORE_SITE, "config_tool",
                                  settings.branch(settings.NAVICORE))
        if note:
            print(f"[navicore] {note}")
    return _fetch_navicore(base, a.check, a.force)


def _fetch_navicore(base: str, check: bool, force: bool) -> int:
    have = local_dtg()
    print(f"bundled : {have}")

    try:
        # Keep the BYTES. Decoding to str and writing it back re-encodes newlines:
        # on Windows write_text() turns every \n into \r\n, which added 20,558 stray
        # CR bytes to index.html and silently broke the byte-identical guarantee this
        # whole approach depends on. Decode only to read the version stamp.
        index_bytes = get(f"{base}/index.html")
        index_html = index_bytes.decode("utf-8", errors="replace")
    except FetchError as e:
        print(f"cannot reach {base}")
        print(f"  {e}")
        if certs.is_cert_error(e.cause):
            print(certs.ADVICE)
        else:
            print("(offline is fine -- the bundled copy keeps working)")
        return 1

    remote = dtg_of(index_html)
    print(f"published: {remote}")

    if remote == have and not force:
        print("up to date")
        return 0
    if check:
        print("update available (run without --check to apply)")
        return 0

    staged = pathlib.Path(tempfile.mkdtemp(prefix="intellex-webui-"))
    try:
        (staged / "index.html").write_bytes(index_bytes)   # bytes, never text
        got = 1
        for name in ROOT_FILES[1:]:
            (staged / name).write_bytes(get(f"{base}/{name}"))
            got += 1

        # ".../config_tool" -> ".../Images"
        img_base = base.rsplit("/", 1)[0] + "/Images"
        (staged / "Images").mkdir()
        for name in IMAGES:
            try:
                (staged / "Images" / name).write_bytes(get(f"{img_base}/{name}"))
                got += 1
            except Exception as e:
                # Not fatal. A missing decoration must not block a tool update --
                # but say so, because a silent skip is how they went missing in the
                # first place.
                print(f"  could not fetch Images/{name}: {type(e).__name__}")

        names = cmdlib_names(base)
        if not names:
            # Refuse rather than ship a UI with a stale library beside it.
            print("could not read the cmdlib manifest -- refusing a partial update")
            return 1
        (staged / "cmdlib").mkdir()
        for n in names:
            dest = staged / "cmdlib" / n
            # Belt and braces: even with _safe_relname above, assert the resolved
            # path really is inside the staging tree before creating anything.
            root = (staged / "cmdlib").resolve()
            if not str(dest.resolve()).startswith(str(root)):
                print(f"  refusing to write outside the bundle: {n!r}")
                continue
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(get(f"{base}/cmdlib/{n}"))
            got += 1

        # Swap only now that everything arrived.
        backup = WEBUI.with_suffix(".prev")
        kept = False
        if WEBUI.exists():
            if backup.exists():
                shutil.rmtree(backup)
            WEBUI.rename(backup)
            kept = True
        shutil.move(str(staged), str(WEBUI))
        staged = None
        print(f"updated to {remote}  ({got} files)")
        # Only claim a backup that actually exists. On a fresh clone there is no
        # previous bundle to keep, and pointing at a directory that was never
        # created is exactly the wrong thing to have believed if the new one is bad.
        if kept:
            print(f"previous kept at {backup}")
        return 0
    except FetchError as e:
        # Atomic by design: the swap happens only once every file has arrived, so
        # failing here leaves src/webui/ exactly as it was. Say so -- "update
        # failed" on its own reads like the bundle might now be half-written, which
        # is the one outcome this function is built to make impossible.
        print(f"update failed: {e}")
        if certs.is_cert_error(e.cause):
            print(certs.ADVICE)
        print("nothing was changed -- the bundled copy is still in place")
        return 1
    finally:
        if staged and staged.exists():
            shutil.rmtree(staged, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
