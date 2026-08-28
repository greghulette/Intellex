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
import sys
import tempfile
import urllib.error
import urllib.request

BASE = "https://greghulette.github.io/NaviCore/config_tool"
WEBUI = pathlib.Path(__file__).resolve().parent.parent / "src" / "webui"

# Frozen snapshots nothing loads (docs/CONFIG_TOOL.md) -- deliberately excluded.
ROOT_FILES = ["index.html", "flasher.js", "serial-hub.js"]

# The tool stamps this on every commit via NaviCore's pre-commit hook, so it is
# the honest version handle -- no guessing, no separate manifest to drift.
DTG_RE = re.compile(r'id="footer-dtg"[^>]*>([^<]+)<')


def get(url: str, timeout: float = 30.0) -> bytes:
    with urllib.request.urlopen(url, timeout=timeout) as r:
        return r.read()


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
    except (urllib.error.URLError, TimeoutError) as e:
        print(f"cannot reach {a.base}: {e}")
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

        names = cmdlib_names(a.base)
        if not names:
            # Refuse rather than ship a UI with a stale library beside it.
            print("could not read the cmdlib manifest -- refusing a partial update")
            return 1
        (staged / "cmdlib").mkdir()
        for n in names:
            dest = staged / "cmdlib" / n
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(get(f"{a.base}/cmdlib/{n}"))
            got += 1

        # Swap only now that everything arrived.
        backup = WEBUI.with_suffix(".prev")
        if WEBUI.exists():
            if backup.exists():
                shutil.rmtree(backup)
            WEBUI.rename(backup)
        shutil.move(str(staged), str(WEBUI))
        staged = None
        print(f"updated to {remote}  ({got} files)")
        print(f"previous kept at {backup}")
        return 0
    finally:
        if staged and staged.exists():
            shutil.rmtree(staged, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main())
