"""What a bundled browser tool loads, and whether a copy of it has all of it.

Shared by the two places that must agree on it: tools/fetch_webui.py, which bundles each tool from GitHub Pages, and
host.py's /_api/webui-version, which tells the launcher whether the copy being served is current.

WHY THE STAMP IS NOT ENOUGH (2026-10-20/21)
Intellex 0.2.1 and older bundled the Wizard from a fixed file list, so the seq-builder branch's Wizard arrived without
seq-builder.js, seq-builder.css and cmdlib/ - twice, the second time from an Update tools pressed on 0.2.1 just before
updating Intellex. That copy carried the published version stamp, so the launcher said "up to date" and nothing ever
fetched it again: the sequence builder stayed dead with no sign why. missing_from() is the second question every
copy now has to answer.

The WCB repo's tests/hil/hil/intellex.py reads PAGE_REF_RE and CMDLIB_MANIFEST_RE out of this file's source to stage
the same bundle a test runs against, so keep each a single re.compile(<bytes literal>, ...) assignment.
"""
from __future__ import annotations

import pathlib
import re

# A file the page loads from beside itself: a <script src> or a <link href> (stylesheet, icon, manifest). Absolute
# URLs (the fonts, a CDN) and data: URIs are not ours to bundle, and ../Images is fetched on its own.
PAGE_REF_RE = re.compile(rb"""<(?:script|link)\b[^>]*?\b(?:src|href)\s*=\s*["']([^"'#?]+)""", re.I)

# A command-library vendor the page's code names by its manifest ('cmdlib/<vendor>/manifest.json', as both tools
# write it), so a vendor a tool starts loading is fetched instead of skipped as one that does not exist.
CMDLIB_MANIFEST_RE = re.compile(rb"""cmdlib/([A-Za-z0-9_-]+)/manifest\.json""")


def safe_relname(name: str) -> bool:
    """Is this name safe to use as a path under a bundle directory?

    The names come from pages and manifests fetched over the network and are then joined onto a real directory and
    written. lstrip("./") only neutralises LEADING traversal, so "vendor/../../../evil.json" survived it intact.

    The source is greghulette.github.io, so this is defence in depth rather than a live hole -- but it is the one
    place in the app where remote data decides where bytes land on disk, which is worth being strict about.
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


def page_refs(index_bytes: bytes) -> list[str]:
    """Every file index.html loads from beside itself, in page order.

    fetch_webui's WCB_FILES and ROOT_FILES are what each page loaded when they were written. A branch that adds a
    file was bundled without it; read the page's own tags, and the next file a page starts loading comes with it."""
    out: list[str] = []
    for m in PAGE_REF_RE.findall(index_bytes):
        name = m.decode("utf-8", "replace").strip()
        if ":" in name or not safe_relname(name) or name in out:
            continue
        out.append(name)
    return out


def cmdlib_needed(*sources: bytes) -> bool:
    """Does the page's code load a command library from cmdlib/ beside it? The NaviCore tool always does; the
    Wizard does from the sequence builder on. The file names come from the manifests (fetch_webui.cmdlib_names)."""
    return any(b"cmdlib/" in s for s in sources)


def named_vendors(*sources: bytes) -> list[str]:
    """The cmdlib vendors the page's code names by their manifest, sorted."""
    return sorted({m.decode("ascii") for s in sources for m in CMDLIB_MANIFEST_RE.findall(s)})


def page_sources(page_dir: pathlib.Path, names) -> list[bytes]:
    """The page's own html/js/css among `names`, as bytes: what image and cmdlib references are read from."""
    return [(page_dir / n).read_bytes() for n in names
            if n.endswith((".html", ".js", ".css")) and (page_dir / n).is_file()]


def missing_from(page_dir: pathlib.Path) -> list[str]:
    """What the page in page_dir loads and the directory does not have: each file its index.html loads, and
    "cmdlib/" when its code loads a command library and there is none. [] for a complete copy, and for no copy at
    all - that is a different answer, told elsewhere.

    cmdlib/ as a whole, not per vendor: a page may name a vendor its site does not publish yet, which the fetcher
    skips (cmdlib_names), and a copy missing that vendor would then read as incomplete - and be refetched - forever."""
    index = page_dir / "index.html"
    if not index.is_file():
        return []
    refs = page_refs(index.read_bytes())
    missing = [n for n in refs if not (page_dir / n).is_file()]
    if cmdlib_needed(*page_sources(page_dir, ["index.html"] + refs)) and not (page_dir / "cmdlib").is_dir():
        missing.append("cmdlib/")
    return missing
