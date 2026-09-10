"""Which branches each product actually has, so the launcher can offer a list.

The branch setting used to be a free-text box. That asked the user to remember a
git ref exactly, offered no way to discover what exists, and turned a typo into a
setting that validates fine and then fails much later at flash time -- by which
point nothing on screen connects the failure to the thing that was mistyped.

OFFLINE IS THE NORMAL CASE HERE, exactly as it is for firmware and the tools, so
this cannot simply call GitHub and give up:

  - `flash.reachable()` is asked ONCE, before any request. On a droid's AP there
    is a default route that goes nowhere, so a call does not fail, it times out
    at ~40 s; two of those to fill a dropdown is a minute of certain waiting.
  - Whatever was last fetched is written beside the other user state and read
    back when the network is gone, so the list survives into the field.
  - The answer ALWAYS contains the branch currently set and "main", whether or
    not either came back from GitHub. A dropdown that cannot show the value it is
    currently set to would silently offer to change it, and a cold cache must
    never make the released branch unselectable.

THE REPO COORDINATES ARE IMPORTED, NOT RESTATED. flash.py and wcb_flash.py each
already own their product's owner/repo, and a third copy here would be a fork of
that fact waiting to drift.
"""
from __future__ import annotations

import json
import time
import urllib.request

import certs
import flash
import paths
import settings
import wcb_flash

# GitHub pages at 30 by default; branch counts here are far below that, and one
# request per product is the point.
_PER_PAGE = 100

# Long, because this list changes rarely and the cost of being wrong is small --
# a branch created minutes ago is missing until the next refresh, and "Other..."
# still reaches it. Being aggressive here would mean a network round trip every
# time the panel is opened, on a tool whose whole premise is working offline.
_TTL_S = 6 * 3600

# REFETCHED ONCE PER RUN, whatever the TTL says. The TTL alone made the list
# unrefreshable by any means the user has: it lives in a file, so restarting the
# app re-reads the same stale copy, and deleted branches went on being offered for
# six hours with nothing anywhere to clear them. "Close it and open it again" is
# what anyone would try first, so that is what has to work.
#
# Within a run the TTL still applies -- the panel can be opened repeatedly without
# a round trip each time.
_fetched_this_run = False

REPOS = {
    settings.NAVICORE: (flash.GITHUB_OWNER, flash.GITHUB_REPO),
    settings.WCB: (wcb_flash.GITHUB_OWNER, wcb_flash.GITHUB_REPO),
}


def _cache_file():
    return paths.user_data_dir() / "branches_cache.json"


def _read_cache() -> dict:
    try:
        return json.loads(_cache_file().read_text(encoding="utf-8"))
    except Exception:                            # noqa: BLE001 - absent or corrupt
        return {}


def _write_cache(data: dict) -> None:
    try:
        f = _cache_file()
        f.parent.mkdir(parents=True, exist_ok=True)
        f.write_text(json.dumps(data), encoding="utf-8")
    except OSError:
        pass                                     # a cache that cannot be written is not fatal


def _fetch(owner: str, repo: str, timeout: float = 8.0) -> list[str]:
    url = f"https://api.github.com/repos/{owner}/{repo}/branches?per_page={_PER_PAGE}"
    req = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "Intellex",
    })
    # Same empty-CA-store trap as every other HTTPS caller here; see src/certs.py.
    with urllib.request.urlopen(req, timeout=timeout,
                                context=certs.context()) as r:
        return [b["name"] for b in json.loads(r.read().decode("utf-8"))]


def branches(force: bool = False) -> dict:
    """{"navicore": [...], "wcb": [...], "cached": bool, "error": str}

    `cached` means the lists did not come from GitHub this call -- either the TTL
    was still good or the network was not there. The launcher says so rather than
    presenting a stale list as current.
    """
    global _fetched_this_run
    cache = _read_cache()
    fresh = (time.time() - cache.get("at", 0)) < _TTL_S and _fetched_this_run
    out = {p: list(cache.get(p) or []) for p in REPOS}
    error = ""
    cached = True

    if force or not fresh or not any(out.values()):
        # ONE probe for both products. Two unreachable requests is two timeouts.
        if flash.reachable():
            got, failed = {}, []
            for product, (owner, repo) in REPOS.items():
                try:
                    got[product] = _fetch(owner, repo)
                except Exception as e:           # noqa: BLE001
                    failed.append(f"{repo}: {type(e).__name__}")
            # gh-pages is the PUBLISHED SITE, never a firmware source: CI puts
            # binaries in the repo tree per branch (NaviCore firmware/, WCB
            # Code/bin) and the browser tools on gh-pages. Offering it invites
            # someone to select a branch that cannot possibly flash.
            got = {p: [b for b in names if b != "gh-pages"] for p, names in got.items()}
            if got:
                # Merge rather than replace: one product failing must not wipe the
                # other's cached list, which is still perfectly good.
                out.update(got)
                _write_cache({**{p: out[p] for p in REPOS}, "at": time.time()})
                cached = False
                # Set on a PARTIAL failure: one product answering is enough to stop
                # this run asking again on every panel open. NOT on a total one --
                # that pinned a stale-but-in-TTL list for the rest of the run, and
                # the error explaining it was only ever returned once.
                _fetched_this_run = True
            error = "; ".join(failed)
        else:
            error = ("offline (" + (flash.unreachable_reason() or "unknown")
                     + ") — showing the last list fetched")

    current = settings.branches()
    # A branch that is SET but no longer listed is worth saying out loud: the
    # firmware fetch will fail at flash time, long after the branch was deleted,
    # and nothing on screen would otherwise connect the two. Computed BEFORE the
    # insertion below, which puts it back so the dropdown can render its own value.
    missing = [p for p in REPOS
               if (out.get(p) or []) and current.get(p) not in (out.get(p) or [])]

    for product in REPOS:
        names = out.get(product) or []
        # settings.DEFAULT_BRANCH and whatever is set right now are always
        # present, so the control can render its own value and can always be put
        # back to released firmware. See the header.
        for must in (current.get(product), settings.DEFAULT_BRANCH):
            if must and must not in names:
                names.insert(0, must)
        out[product] = names

    out["cached"] = cached
    out["error"] = error
    out["missing"] = missing
    return out
