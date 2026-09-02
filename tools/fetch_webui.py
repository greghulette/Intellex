#!/usr/bin/env python3
"""Refresh the bundled config tool from GitHub Pages.

    python tools/fetch_webui.py            # check + update if newer
    python tools/fetch_webui.py --check    # report only, change nothing
    python tools/fetch_webui.py --force    # re-fetch even if unchanged

This is what the app's Update button will call. The public NaviCore repo stays
the single source of truth for the UI; this copies it, never edits it, so the
bundled file hashes identical to the published one and there is no fork.

THE TOOL IS NOT ONE FILE. index.html loads flasher.js, serial-hub.js and ~27
files under cmdlib/ at RELATIVE paths at runtime. Fetching only index.html
leaves a stale command library silently mismatched against a newer UI -- broken
in a way that looks like a tool bug rather than a bad update. So the whole set
moves together, or not at all.

ATOMIC AND REVERTIBLE. Everything lands in a temp directory first and is only
swapped in once every file has arrived. A half-applied update would leave no way
to configure the droid, which is the one thing this app must never do.
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

BASE = "https://greghulette.github.io/NaviCore/config_tool"
WEBUI = pathlib.Path(__file__).resolve().parent.parent / "src" / "webui"

# Frozen snapshots nothing loads (docs/CONFIG_TOOL.md) -- deliberately excluded.
ROOT_FILES = ["index.html", "flasher.js", "serial-hub.js"]

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


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default=BASE)
    ap.add_argument("--check", action="store_true", help="report only")
    ap.add_argument("--force", action="store_true", help="fetch even if unchanged")
    a = ap.parse_args()

    have = local_dtg()
    print(f"bundled : {have}")

    try:
        # Keep the BYTES. Decoding to str and writing it back re-encodes newlines:
        # on Windows write_text() turns every \n into \r\n, which added 20,558 stray
        # CR bytes to index.html and silently broke the byte-identical guarantee this
        # whole approach depends on. Decode only to read the version stamp.
        index_bytes = get(f"{a.base}/index.html")
        index_html = index_bytes.decode("utf-8", errors="replace")
    except FetchError as e:
        print(f"cannot reach {a.base}")
        print(f"  {e}")
        if certs.is_cert_error(e.cause):
            print(certs.ADVICE)
        else:
            print("(offline is fine -- the bundled copy keeps working)")
        return 1

    remote = dtg_of(index_html)
    print(f"published: {remote}")

    if remote == have and not a.force:
        print("up to date")
        return 0
    if a.check:
        print("update available (run without --check to apply)")
        return 0

    staged = pathlib.Path(tempfile.mkdtemp(prefix="navilink-webui-"))
    try:
        (staged / "index.html").write_bytes(index_bytes)   # bytes, never text
        got = 1
        for name in ROOT_FILES[1:]:
            (staged / name).write_bytes(get(f"{a.base}/{name}"))
            got += 1

        # ".../config_tool" -> ".../Images"
        img_base = a.base.rsplit("/", 1)[0] + "/Images"
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

        names = cmdlib_names(a.base)
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
            dest.write_bytes(get(f"{a.base}/cmdlib/{n}"))
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
