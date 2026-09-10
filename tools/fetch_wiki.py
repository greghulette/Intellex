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

import certs                                                        # noqa: E402
import paths                                                        # noqa: E402
from flash import no_network, reachable, unreachable_reason        # noqa: E402

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


# EVERY REQUEST BELOW GOES THROUGH certs.context(). The stock python.org macOS
# build ships an EMPTY CA store, so the default context cannot verify anything and
# every fetch here dies with CERTIFICATE_VERIFY_FAILED on a machine whose network
# is fine -- which is precisely how this fetcher failed: a TCP reachability probe
# that passed, then "page index unreadable" and "nothing downloaded" for all three
# wikis. flash.py, host.py and fetch_webui.py already did this; this file was
# written later and did not. See src/certs.py.
def _get(url: str, timeout: float = 20.0) -> bytes:
    req = urllib.request.Request(url, headers=_UA)
    with urllib.request.urlopen(req, timeout=timeout,
                                context=certs.context()) as r:
        return r.read()


def _size_of(url: str, timeout: float = 15.0) -> int:
    """Content-Length via HEAD. 0 when the server will not say, which lets the
    download proceed -- an unknown size is not a reason to skip a small file."""
    req = urllib.request.Request(url, headers=_UA, method="HEAD")
    with urllib.request.urlopen(req, timeout=timeout,
                                context=certs.context()) as r:
        return int(r.headers.get("Content-Length") or 0)


# Set by any fetch that died of certificate verification, so main() can print the
# fix ONCE at the end. Without this the only symptoms are "page index unreadable"
# and "nothing downloaded", which both point at GitHub and at the wiki's markup --
# everywhere except the interpreter, which is where the fault actually is.
_cert_error = False


def _note_cert_error(exc: BaseException) -> bool:
    """Record a verification failure for main()'s advice; answer about THIS one."""
    global _cert_error
    if not certs.is_cert_error(exc):
        return False
    _cert_error = True
    return True


def page_index(owner: str, repo: str) -> list[str]:
    """Every page name, from the wiki's own index. [] if it cannot be read."""
    try:
        body = _get(f"https://github.com/{owner}/{repo}/wiki/_pages").decode(
            "utf-8", "replace")
    except Exception as e:                               # noqa: BLE001
        _note_cert_error(e)
        return []
    # Anchor hrefs into the wiki, minus the action pages GitHub puts there too.
    names = set(re.findall(rf'/{re.escape(owner)}/{re.escape(repo)}/wiki/'
                           r'([A-Za-z0-9._%()\'-]+)"', body))
    skip = {"_pages", "_new", "_history", "_edit", "_compare", "_access"}
    return sorted(html.unescape(urllib.parse.unquote(n)) for n in names
                  if n not in skip)


# What makes a link a FILE rather than a page. NOT "it has a suffix": wiki page
# names carry dots, and the WCB wiki's version3.2_build has an apparent suffix of
# ".2_build". Filed as an asset, a page like that was fetched without its .md,
# 404'd silently, and its own links were never followed -- the crawl fallback lost
# it outright. host.py's wiki_page hit the same trap and decides by what is on
# disk; nothing is on disk yet here, so this asks for a real extension instead: a
# short run of letters and digits with at least one letter, which ".png" and
# ".mp4" are and ".2_build" and ".2" are not.
_EXT = re.compile(r"\.(?=[A-Za-z0-9]*[A-Za-z])[A-Za-z0-9]{1,5}")


def _is_asset(link: str) -> bool:
    suffix = pathlib.PurePosixPath(link).suffix
    return bool(_EXT.fullmatch(suffix)) and suffix.lower() != ".md"


def _targets(text: str) -> tuple[set[str], set[str]]:
    """(page names, asset paths) referenced by one page's markdown."""
    pages, assets = set(), set()

    def sort_one(raw: str) -> None:
        raw = raw.strip()
        if not raw or raw.startswith(("http://", "https://", "mailto:", "#")):
            return
        # A real extension means a file to copy; anything else is a page to visit.
        if _is_asset(raw):
            assets.add(raw.lstrip("./"))
        else:
            page = raw.strip("/").replace(" ", "-")
            pages.add(page[:-3] if page.lower().endswith(".md") else page)

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
            if _note_cert_error(e):
                # Every remaining page would fail identically. Stop rather than
                # spend a queue of several hundred names proving it.
                problems.append(f"{name}: certificate verification failed")
                break
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
        # is_relative_to, not a string prefix: a prefix test also accepted a
        # sibling whose name merely starts the same ("wcb" -> "wcb-x").
        if not dest.is_relative_to(staged.resolve()):
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
    """Same rule as the tool bundles: swap only once everything has arrived.

    THE NEW COPY GOES BESIDE `dest` FIRST, and the old one is removed LAST.
    `staged` is in the system temp directory, which is routinely another volume,
    so moving it is a file-by-file copy that can fail half way. This used to
    delete the old copy before that copy began, so a failure left no docs at all
    -- and run from source there is no bundled copy behind it to fall back to.
    Once the old copy is out of the way only renames happen, and a failed one
    puts it back.
    """
    dest.parent.mkdir(parents=True, exist_ok=True)
    incoming = dest.with_name(dest.name + ".new")
    backup = dest.with_name(dest.name + ".prev")
    if backup.exists():
        if dest.exists():
            shutil.rmtree(backup, ignore_errors=True)
        else:
            backup.rename(dest)     # a previous run died mid-swap: this is the good copy
    if incoming.exists():
        shutil.rmtree(incoming, ignore_errors=True)
    try:
        shutil.move(str(staged), str(incoming))
        had_old = dest.exists()
        if had_old:
            dest.rename(backup)
    except OSError:
        shutil.rmtree(incoming, ignore_errors=True)
        raise
    try:
        incoming.rename(dest)
    except OSError:
        if had_old and not dest.exists():
            backup.rename(dest)
        raise
    shutil.rmtree(backup, ignore_errors=True)


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
        print("  reason:", unreachable_reason("github.com") or "unknown")
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

            try:
                _swap_in(staged, DEST / product)
            except OSError as e:
                # PER WIKI, like the fetch above. One directory that cannot be
                # replaced -- a file held open, antivirus -- must not stop the other
                # wikis updating, and must not end the run in a traceback.
                print(f"[{product}] could not swap in the new copy "
                      f"({type(e).__name__}: {e}) — keeping what is on disk")
                rc = 1
                continue
            note = f", {len(problems)} problem(s)" if problems else ""
            print(f"[{product}] {pages} page(s), {assets} asset(s){note}")
            for p in problems[:5]:
                print(f"[{product}]   {p}")
            if len(problems) > 5:
                print(f"[{product}]   … and {len(problems) - 5} more")

    if _cert_error:
        print(certs.ADVICE)
    print(f"docs in {DEST}")
    return rc


if __name__ == "__main__":
    sys.exit(main())
