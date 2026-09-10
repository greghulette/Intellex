"""Answer the tools' GitHub firmware calls from the local cache.

WHY THIS EXISTS
The native flash buttons are intercepted by the shim and run esptool in the host,
which reads the cache -- so a cabled flash already worked with no internet. **OTA
did not.** Both OTA paths fetch their image INSIDE THE PAGE:

    index.html:17441   Update over USB (OTA)      -> fetchFirmwareImages()
    index.html:17697   Update over WCB (OTA)      -> fetchFirmwareImages()
    flasher.js:117     fetchFirmwareImages()      -> api.github.com
    Wizard app.js:167 / flasher.js:117            -> api.github.com

and OTA over a relay is exactly the con-floor case: joined to a droid's AP, no
route to GitHub, cache full, relay sitting right there -- and the page could not
reach the one copy of the firmware on the machine.

WHY A PROXY AND NOT A FUNCTION SWAP
Replacing `window.fetchFirmwareImages` would fix the NaviCore tool and leave the
Wizard needing its own separate swap, against two functions that are free to be
renamed by their own repos at any time. Both tools instead agree on something far
more stable: the GitHub REST shape. Answering THAT covers flashing and OTA, both
tools, online and offline, with no change to either -- which is the same principle
the rest of the app runs on, the page asking for what it always asked for while
the host decides what is really on the other end.

NETWORK FIRST, CACHE SECOND, AND ALWAYS THROUGH HERE. Online the answer comes from
GitHub and is stored on the way past, so simply opening the Firmware tab fills the
cache. Offline the stored copy is served. Either way `download_url` is rewritten to
point back at this host, so the page's own follow-up fetch for the bytes lands here
too rather than at a raw.githubusercontent URL that cannot be reached.

NOT AN OPEN PROXY. Only the two firmware repos are served; anything else is
refused. This process can reach the internet and the page cannot, so forwarding
arbitrary URLs on the page's behalf would hand it exactly the reach it is denied.
"""
from __future__ import annotations

import json
import urllib.parse

import flash
import fwcache
import wcb_flash

# (owner, repo) -> the cache product it belongs to. Imported rather than
# restated, so a repo move cannot leave this file pointing at the old one.
REPOS = {
    (flash.GITHUB_OWNER, flash.GITHUB_REPO): fwcache.NAVICORE,
    (wcb_flash.GITHUB_OWNER, wcb_flash.GITHUB_REPO): fwcache.WCB,
}


class ProxyError(Exception):
    """Raised with a message meant for the page's own error display."""


def product_for(owner: str, repo: str) -> str:
    try:
        return REPOS[(owner, repo)]
    except KeyError:
        raise ProxyError(f"not a firmware repository: {owner}/{repo}") from None


def _raw_url(owner: str, repo: str, ref: str, name: str) -> str:
    q = urllib.parse.urlencode({"owner": owner, "repo": repo, "ref": ref, "name": name})
    return f"/_api/gh/raw?{q}"


def contents(owner: str, repo: str, path: str, ref: str) -> tuple[bytes, bool]:
    """The GitHub contents listing for a firmware directory.

    Returns (json bytes, served_from_cache). Shaped exactly as GitHub's own reply
    except that every `download_url` points back at this host -- see the header.
    """
    product = product_for(owner, repo)
    # THE FIRMWARE DIRECTORY AND NOTHING ELSE. What comes back is stored as THE
    # listing for (product, ref) -- the same file both native flashers fall back to
    # offline -- and every download_url below is pointed at _bin_path(product). Any
    # other path of the same repo would overwrite that cache with the wrong
    # directory, and the next offline flash would find no app image in it. This is
    # a GET, so no page of ours has to be the one asking.
    bin_path = _bin_path(product)
    if path.strip("/") != bin_path:
        raise ProxyError(f"not a firmware directory: {owner}/{repo}/{path}")
    url = (f"https://api.github.com/repos/{owner}/{repo}"
           f"/contents/{bin_path}?ref={urllib.parse.quote(ref)}")

    cached = False
    try:
        # ONE reachability probe, never inferred from an exception: on a droid's
        # AP a request does not fail, it times out at ~40 s, and the page would
        # sit on a spinner for the whole of it before anything said why.
        if not flash.reachable():
            raise flash.FlashError("GitHub is not reachable — "
                                   + (flash.unreachable_reason() or "unknown"))
        raw = flash._get(url, lambda _m: None)
    except Exception:                                # noqa: BLE001
        raw = fwcache.load_listing(product, ref)
        if raw is None:
            raise ProxyError(
                f"no cached firmware listing for {product} on '{ref}', and GitHub "
                f"is not reachable. Press Download firmware while you have a "
                f"network.") from None
        cached = True

    try:
        files = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise ProxyError(f"unreadable firmware listing: {e}") from e
    if not isinstance(files, list):
        raise ProxyError("unexpected GitHub response (not a listing)")
    if not cached:
        # Stored only once it has parsed AS a listing. Stored before the check, a
        # reply that was not one replaced a good cached listing with something
        # neither flasher can read.
        fwcache.store_listing(product, ref, raw)

    for f in files:
        if isinstance(f, dict) and f.get("name"):
            # A cached listing's download_url points at a GitHub URL that cannot
            # be reached from here; a live one points at a host the page cannot
            # reach either. Both become ours.
            f["download_url"] = _raw_url(owner, repo, ref, f["name"])
    return json.dumps(files).encode("utf-8"), cached


def raw(owner: str, repo: str, ref: str, name: str) -> tuple[bytes, bool]:
    """One firmware file's bytes, from GitHub or from the cache."""
    product = product_for(owner, repo)
    # The name is interpolated into a URL and used as a cache filename, so it must
    # be a bare filename -- never a path.
    if not name or "/" in name or "\\" in name or ".." in name:
        raise ProxyError(f"bad firmware filename: {name!r}")

    url = (f"https://raw.githubusercontent.com/{owner}/{repo}/"
           f"{urllib.parse.quote(ref)}/{_bin_path(product)}/{urllib.parse.quote(name)}")
    try:
        if not flash.reachable():
            raise flash.FlashError("GitHub is not reachable — "
                                   + (flash.unreachable_reason() or "unknown"))
        data = flash._get(url, lambda _m: None)
        fwcache.store(product, ref, name, data)
        return data, False
    except Exception:                                # noqa: BLE001
        data = fwcache.load(product, ref, name)
        if data is None:
            raise ProxyError(
                f"{name} is not cached for '{ref}' and GitHub is not reachable"
            ) from None
        return data, True


def _bin_path(product: str) -> str:
    """Where each product's binaries live inside its repo."""
    return flash.GITHUB_BIN_PATH if product == fwcache.NAVICORE \
        else wcb_flash.GITHUB_BIN_PATH
