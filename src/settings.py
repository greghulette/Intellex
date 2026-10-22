"""Small persistent settings, kept beside the tool bundles and the firmware cache.

Two things: which GITHUB BRANCH each product's firmware comes from, and the ACCESS
TOKEN of each droid whose WebSocket asks for one (see "Access tokens" below and
src/wsauth.py).

WHY A BRANCH SETTING EXISTS
Both flashers already took a `branch` argument and defaulted to "main" -- the
plumbing was there, nothing exposed it. But branch builds are a first-class part
of how this ecosystem works: CI publishes binaries per branch (WCB's Code/bin,
NaviCore's firmware/), so a feature branch has a real, flashable build. Working on
one and being unable to flash it is the whole problem.

PER PRODUCT, not one global setting. Working on a WCB feature branch while the
droid stays on released NaviCore firmware is the normal case, not an edge case.

THE CACHE NEEDS NOTHING SPECIAL. Images are stored under their published filename
and a branch build carries the branch in its name --
WCB_6.2.1_022200RSEP2026_WIFI_ESP32S3.bin against
WCB_6.2.1_021242RSEP2026_main_ESP32S3.bin -- so branches cannot collide and
switching back and forth does not invalidate anything.
"""
from __future__ import annotations

import json
import re
from typing import Optional

import paths

NAVICORE = "navicore"
WCB = "wcb"
DEFAULT_BRANCH = "main"

# The same whitelist flasher.js applies, and for the same reason it gives: this
# value is interpolated into a GitHub API URL, so it must not be able to smuggle
# in URL operators (?, &, #, =). Alphanumerics, dot, underscore, hyphen and
# slash is the safe subset of real git ref names.
#
# THE CHARACTER SET ALONE DOES NOT KEEP /../ OUT. '.' and '/' are both in it, so
# '../x' passed, and tool_base() below puts the branch in a URL PATH: the tool
# update fetched from https://greghulette.github.io/<product>/dev/../x/..., which
# is another of the Pages sites. valid_branch() adds the segment rules git itself
# enforces (git check-ref-format), so no real branch name is refused.
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def _file():
    return paths.user_data_dir() / "settings.json"


def _load() -> dict:
    """The settings as a dict -- {} for a file that is missing, unreadable, not
    UTF-8, not JSON, or JSON of another shape. ValueError covers both JSONDecodeError
    and UnicodeDecodeError, and a list, a string or null parses fine but is not a
    settings object: each of those made branch() raise, so the shim, /_api/branches,
    /_api/firmware and both flashers failed -- and Set could not repair it (HIL
    intellex.settings_branch_fallback)."""
    try:
        d = json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    return d if isinstance(d, dict) else {}


def _save(d: dict) -> bool:
    try:
        p = _file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, indent=1), encoding="utf-8")
        return True
    except OSError:
        return False


def valid_branch(b: str) -> bool:
    """A usable branch name: the whitelist above, plus git's own segment rules.

    No '..' anywhere, and no '/'-separated segment that is empty or starts with
    '.', or ends with '.lock'; no trailing '.'. Git refuses every one of these as
    a ref name, so they can only be a URL path trick, never a branch.
    """
    if not b or len(b) > 100 or not _BRANCH_RE.match(b):
        return False
    if ".." in b or b.endswith("."):
        return False
    return all(seg and not seg.startswith(".") and not seg.endswith(".lock")
               for seg in b.split("/"))


def branch(product: str) -> str:
    """Which branch to flash `product` from. Always a usable value.

    Falls back to main on anything unset or malformed rather than raising: a
    corrupt settings file must not make the app unable to flash, and "main" is
    both the safe answer and the one that was in force before this existed.
    """
    stored = _load().get("branch")
    b = stored.get(product) if isinstance(stored, dict) else None
    if not isinstance(b, str) or not b:
        return DEFAULT_BRANCH
    return b if valid_branch(b) else DEFAULT_BRANCH


def set_branch(product: str, b: str) -> Optional[str]:
    """Store it. Returns an error string, or None on success."""
    if product not in (NAVICORE, WCB):
        return f"unknown product {product!r}"
    b = (b or "").strip() or DEFAULT_BRANCH
    if not valid_branch(b):
        return (f"{b!r} is not a usable branch name - letters, digits, "
                "'.', '_', '-' and '/' only, and no '..', empty or "
                "'.'-led path segment (git refuses those too)")
    d = _load()
    if not isinstance(d.get("branch"), dict):
        d["branch"] = {}               # a hand-edited or foreign shape: replace it, do not raise
    d["branch"][product] = b
    return None if _save(d) else "could not write the settings file"


def branches() -> dict:
    return {NAVICORE: branch(NAVICORE), WCB: branch(WCB)}


# ── Access tokens, per droid ────────────────────────────────────────────────
# A droid whose WebSocket has a token set will run nothing a session sends until
# that session logs in with ?AUTH,<token> (src/wsauth.py has the protocol). The
# user types the token once; it is kept here, under the key wsauth.droid_key()
# names for the board that asked, so the next attach logs in by itself.
#
# IN THE CLEAR, beside the branch setting, on purpose: the file is the user's own
# (paths.user_data_dir), a WCB's own config pull and ?backup carry the same
# token in clear (and the mesh password beside it), and a keyring would be a
# dependency per platform for a secret that already travels that way. What this
# module does guarantee is that the value goes nowhere else: no log line, no
# /_api reply and no error message carries it.
#
# THE SAME RULES THE FIRMWARE APPLIES (WCB ?WIFI,TOKEN, NaviCore wifiToken): 8-32
# printable ASCII characters, none of space , ^ ; ? " \. Those break the config
# chain (^ ; ,) or the line grammar, so a token holding one can never have been
# set on a board. Refusing it here also keeps a newline -- a second command --
# out of the ?AUTH line this value is pasted into.
_TOKEN_FORBIDDEN = set(' ,^;?"\\')
# Prefixes wsauth.droid_key() hands out. A key arrives back from the page with the
# token (POST /_api/token), so it is checked rather than trusted. Any printable
# text after the prefix: a network name may hold spaces and non-ASCII.
_KEY_RE = re.compile(r"^(mac|ssid|host):[^\x00-\x1f\x7f]{1,110}$")


def valid_token(t) -> bool:
    return (isinstance(t, str) and 8 <= len(t) <= 32
            and all(0x21 <= ord(c) <= 0x7E and c not in _TOKEN_FORBIDDEN for c in t))


def valid_token_key(k) -> bool:
    return isinstance(k, str) and bool(_KEY_RE.match(k))


def token(key: str) -> Optional[str]:
    """The token saved for the droid `key` names, or None. A stored value that is
    not a usable token (a hand edit) is None too: sent, it would only fail."""
    stored = _load().get("tokens")
    entry = stored.get(key) if isinstance(stored, dict) else None
    t = entry.get("token") if isinstance(entry, dict) else None
    return t if valid_token(t) else None


def set_token(key: str, t: str, label: str = "") -> Optional[str]:
    """Store it. Returns an error string, or None on success. The error never
    repeats the value: it is shown to the user and may be logged."""
    if not valid_token_key(key):
        return "unknown droid - scan again and enter the token when it asks"
    if not valid_token(t):
        return ("an access token is 8-32 characters: letters, digits and punctuation, "
                'with no spaces and none of , ^ ; ? " \\')
    d = _load()
    if not isinstance(d.get("tokens"), dict):
        d["tokens"] = {}               # a hand-edited or foreign shape: replace it, do not raise
    # The label is only what the launcher showed (the network and the address), kept
    # so a person reading the file can tell the entries apart.
    clean = "".join(c for c in str(label or "") if c.isprintable())[:80]
    import datetime
    d["tokens"][key] = {"token": t, "label": clean,
                        "saved": datetime.datetime.now().isoformat(timespec="seconds")}
    return None if _save(d) else "could not write the settings file"


# ── Where a tool is published ───────────────────────────────────────────────
# Here, not in the fetcher, because the VERSION CHECK needs the same answer. It
# had its own hard-coded main URL, so a WIFI-branch Wizard was compared against
# main's published copy and reported "update available" pointing at an OLDER
# build -- a warning that could never be satisfied, because updating fetches the
# branch again. Two places deciding one thing, disagreeing.
NAVICORE_SITE = "https://greghulette.github.io/NaviCore"
WCB_SITE      = "https://greghulette.github.io/Wireless_Communication_Board-WCB"
_LEAF = {NAVICORE: (NAVICORE_SITE, "config_tool"), WCB: (WCB_SITE, "Wizard")}


def tool_base(product: str, br: Optional[str] = None) -> str:
    """Published location of `product`'s browser tool for its configured branch."""
    site, leaf = _LEAF[product]
    b = br if br is not None else branch(product)
    if not b or b == DEFAULT_BRANCH:
        return f"{site}/{leaf}"
    return f"{site}/dev/{b}/{leaf}"
