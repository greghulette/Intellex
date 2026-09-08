#!/usr/bin/env python3
"""Download the project wikis so the documentation works with no internet.

    python tools/fetch_wiki.py --wiki all

Same premise as the firmware cache and the two tool bundles: get everything
current while there IS a network, then disconnect, join the droid's AP and work.
Documentation was the one piece still missing from that, and it is the piece you
reach for precisely when something is not going the way you expected -- on a con
floor, on a board's own access point, with no route to GitHub.

WHY THIS IS NOT A `git clone`
A frozen build cannot assume git exists on the machine, and shelling out to it
would be a console-window spawn on Windows for a program that has no console. So
this is plain HTTP, like every other fetcher here.

HOW A WIKI IS READ WITHOUT AN API
GitHub has no REST API for wikis, and codeload does not serve them (`.wiki`
tarballs 404 -- measured, not assumed). Two things DO work:

  1. `raw.githubusercontent.com/wiki/<owner>/<repo>/<Page>.md` returns the page
     source. That is the whole content problem solved -- but it needs a list.
  2. `github.com/<owner>/<repo>/wiki/_pages` is the wiki's own page index, and
     scraping it gives the COMPLETE list including orphans.

Scraping HTML is brittle, so it is the first source and not the only one: if the
index yields nothing, the pages are discovered by CRAWLING links out of Home and
_Sidebar instead. Measured against all three wikis, the crawl finds 24/24 of
NaviCore, 9/9 of Intellex and 40/42 of WCB -- the two it misses are orphans no
reader could navigate to either. So the fallback degrades to "everything you
could actually reach", which is the right shape for a fallback.

BOTH LINK STYLES, because the wikis do not agree. NaviCore and Intellex use
`[[Page Name]]`; the WCB wiki uses `[Text](Page-Name)` almost exclusively (285
wiki-links vs 0, against 4 vs 485). A crawler that knew only one style found two
pages of forty-two.
"""
from __future__ import annotations

import argparse
import html
import pathlib
import re
import shutil
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent.parent / "src"))

import paths                                                        # noqa: E402
from flash import no_network, reachable                             # noqa: E402

# owner/repo per product. The wiki lives at <repo>.wiki, but every URL below
# spells that out itself, because the three forms differ in an unobvious way.
WIKIS = {
    "intellex": ("greghulette", "Intellex"),
    "navicore": ("greghulette", "NaviCore"),
    "wcb": ("greghulette", "Wireless_Communication_Board-WCB"),
}

TITLES = {
    "intellex": "Intellex",
    "navicore": "NaviCore",
    "wcb": "WCB",
}

DEST = paths.write_dir("wiki")

# Wiki links, both dialects, plus image/asset references.
RE_WIKILINK = re.compile(r"\[\[([^\]]+)\]\]")
RE_MDLINK = re.compile(r"\]\(([^)\s]+?)(?:\s+\"[^\"]*\")?\)")
RE_IMG_TAG = re.compile(r"<img[^>]+src=[\"']([^\"']+)[\"']", re.I)

_UA = {"User-Agent": "Intellex"}

# PER-IMAGE CEILING. The WCB wiki carries 21 assembly photographs at 6-7 MB each
# and an 11.7 MB animation: fetched whole it is 123 MB, which is not a thing to
# put inside an application. At 1 MB the same three wikis come to 10.4 MB and
# keep every diagram, screenshot and schematic -- only the full-resolution
# soldering photos are left behind, and those are for building a board at a
# bench, not for the con floor this exists to serve.
#
# Checked with a HEAD first, so an oversized file is never transferred and then
# thrown away. A page whose image was skipped renders a link to it online rather
# than a broken picture (see wikidocs.render).
MAX_ASSET_BYTES = 1 << 20


def _get(url: str, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def _size_of(url: str, timeout: float = 15.0) -> int:
    """Content-Length via HEAD. 0 when the server will not say, which lets the
    download proceed -- an unknown size is not a reason to skip a small file."""
    req = urllib.request.Request(url, headers=_UA, method="HEAD")
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return int(r.headers.get("Content-Length") or 0)


def page_index(owner: str, repo: str) -> list[str]:
    """Every page name, from the wiki's own index. [] if it cannot be read."""
    try:
        body = _get(f"https://github.com/{owner}/{repo}/wiki/_pages").decode(
            "utf-8", "replace")
    except Exception:                                    # noqa: BLE001
        return []
    # Anchor hrefs into the wiki, minus the action pages GitHub puts there too.
    names = set(re.findall(rf'/{re.escape(owner)}/{re.escape(repo)}/wiki/'
                           r'([A-Za-z0-9._%()\'-]+)"', body))
    skip = {"_pages", "_new", "_history", "_edit", "_compare", "_access"}
    return sorted(html.unescape(urllib.parse.unquote(n)) for n in names
                  if n not in skip)


def _targets(text: str) -> tuple[set[str], set[str]]:
    """(page names, asset paths) referenced by one page's markdown."""
    pages, assets = set(), set()

    def sort_one(raw: str) -> None:
        raw = raw.strip()
        if not raw or raw.startswith(("http://", "https://", "mailto:", "#")):
            return
        # An extension means a file to copy; anything else is a page to visit.
        if pathlib.PurePosixPath(raw).suffix:
            assets.add(raw.lstrip("./"))
        else:
            pages.add(raw.strip("/").replace(" ", "-"))

    for m in RE_WIKILINK.findall(text):
        # [[Display|Target]] -- the target is the half after the pipe.
        sort_one(m.split("|")[-1].replace(" ", "-"))
    for m in RE_MDLINK.findall(text):
        sort_one(m.split("#", 1)[0])
    for m in RE_IMG_TAG.findall(text):
        sort_one(m.split("#", 1)[0])
    return pages, assets


def fetch_one(product: str, staged: pathlib.Path) -> tuple[int, int, list[str]]:
    """Download one wiki into `staged`. Returns (pages, assets, problems)."""
    owner, repo = WIKIS[product]
    raw = f"https://raw.githubusercontent.com/wiki/{owner}/{repo}"
    problems: list[str] = []

    want = page_index(owner, repo)
    if want:
        print(f"[{product}] index lists {len(want)} page(s)")
    else:
        # The index is the brittle half; say so rather than silently thinning out.
        problems.append("page index unreadable - crawled links instead")
        print(f"[{product}] page index unreadable — crawling links from Home")
        want = ["Home", "_Sidebar", "_Footer"]

    seen: set[str] = set()
    assets: set[str] = set()
    queue = list(dict.fromkeys(["Home", "_Sidebar", "_Footer", *want]))
    pages = 0

    while queue:
        name = queue.pop(0)
        if name in seen:
            continue
        seen.add(name)
        try:
            body = _get(f"{raw}/{urllib.parse.quote(name)}.md")
        except urllib.error.HTTPError as e:
            # A 404 IS NORMAL HERE, including for a name the index listed. Wikis
            # link to pages that were renamed or never written, and the index is
            # scraped from every anchor on the page -- so it reports the WCB
            # wiki's dead "Chapter-N" links as though they were pages. Verified
            # against a clone: no such file exists. Treating those as failures
            # printed twelve alarming lines about a wiki that was fine.
            if e.code == 404:
                continue
            problems.append(f"{name}: HTTP {e.code}")
            continue
        except Exception as e:                           # noqa: BLE001
            problems.append(f"{name}: {type(e).__name__}")
            continue

        (staged / f"{name}.md").write_bytes(body)
        pages += 1
        text = body.decode("utf-8", "replace")
        more, more_assets = _targets(text)
        assets |= more_assets
        for n in more:
            if n not in seen:
                queue.append(n)

    got_assets, skipped_big = 0, 0
    for rel in sorted(assets):
        # Never let a link walk out of the staging directory.
        dest = (staged / rel).resolve()
        if not str(dest).startswith(str(staged.resolve())):
            problems.append(f"{rel}: refused (path escapes the wiki directory)")
            continue
        url = f"{raw}/{urllib.parse.quote(rel)}"
        try:
            if _size_of(url) > MAX_ASSET_BYTES:
                skipped_big += 1
                continue
            blob = _get(url)
        except Exception:                                # noqa: BLE001
            continue                                     # a missing image is not a failure
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(blob)
        got_assets += 1

    if skipped_big:
        problems.append(f"{skipped_big} image(s) over "
                        f"{MAX_ASSET_BYTES // 1024} KB left online")
    return pages, got_assets, problems


def _swap_in(staged: pathlib.Path, dest: pathlib.Path) -> None:
    """Same rule as the tool bundles: swap only once everything has arrived."""
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists():
        backup = dest.with_name(dest.name + ".prev")
        if backup.exists():
            shutil.rmtree(backup, ignore_errors=True)
        dest.rename(backup)
        shutil.rmtree(backup, ignore_errors=True)
    shutil.move(str(staged), str(dest))


def summary() -> dict:
    """What is on disk, for the launcher to report."""
    out = {}
    for product in WIKIS:
        d = paths.data_dir("wiki") / product
        pages = sorted(d.glob("*.md")) if d.is_dir() else []
        out[product] = {
            "title": TITLES[product],
            "pages": len(pages),
            "have": bool(pages),
        }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description="Download the project wikis for offline use")
    ap.add_argument("--wiki", choices=[*WIKIS, "all"], default="all")
    args = ap.parse_args()

    products = list(WIKIS) if args.wiki == "all" else [args.wiki]

    # ONE probe for the whole run, never inferred from an exception. On a droid's
    # AP a request does not fail, it times out at ~40 s -- three wikis of that is
    # minutes of certain waiting before anything says why.
    if not reachable("github.com"):
        print("github.com is not reachable — the bundled docs are unchanged.")
        return 1

    rc = 0
    for product in products:
        with tempfile.TemporaryDirectory(prefix=f"intellex-wiki-{product}-") as td:
            staged = pathlib.Path(td) / product
            staged.mkdir(parents=True)
            try:
                pages, assets, problems = fetch_one(product, staged)
            except Exception as e:                       # noqa: BLE001
                print(f"[{product}] failed: {type(e).__name__}: {e}")
                if no_network(e):
                    print(f"[{product}] the network went away — keeping what is on disk")
                rc = 1
                continue

            if not pages:
                # SEPARATE PER WIKI, and a failure never swaps. Wiping a good
                # offline copy because a fetch went wrong is the one outcome this
                # whole feature exists to prevent.
                print(f"[{product}] nothing downloaded — keeping what is on disk")
                rc = 1
                continue

            _swap_in(staged, DEST / product)
            note = f", {len(problems)} problem(s)" if problems else ""
            print(f"[{product}] {pages} page(s), {assets} asset(s){note}")
            for p in problems[:5]:
                print(f"[{product}]   {p}")
            if len(problems) > 5:
                print(f"[{product}]   … and {len(problems) - 5} more")

    print(f"docs in {DEST}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
