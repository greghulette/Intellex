"""Small persistent settings, kept beside the tool bundles and the firmware cache.

Currently one thing: which GITHUB BRANCH each product's firmware comes from.

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
# in URL operators (?, &, #, =, /../). Alphanumerics, dot, underscore, hyphen and
# slash is the safe subset of real git ref names.
_BRANCH_RE = re.compile(r"^[A-Za-z0-9._/-]+$")


def _file():
    return paths.user_data_dir() / "settings.json"


def _load() -> dict:
    try:
        return json.loads(_file().read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _save(d: dict) -> bool:
    try:
        p = _file()
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(d, indent=1), encoding="utf-8")
        return True
    except OSError:
        return False


def valid_branch(b: str) -> bool:
    return bool(b) and len(b) <= 100 and bool(_BRANCH_RE.match(b))


def branch(product: str) -> str:
    """Which branch to flash `product` from. Always a usable value.

    Falls back to main on anything unset or malformed rather than raising: a
    corrupt settings file must not make the app unable to flash, and "main" is
    both the safe answer and the one that was in force before this existed.
    """
    b = (_load().get("branch") or {}).get(product) or DEFAULT_BRANCH
    return b if valid_branch(b) else DEFAULT_BRANCH


def set_branch(product: str, b: str) -> Optional[str]:
    """Store it. Returns an error string, or None on success."""
    if product not in (NAVICORE, WCB):
        return f"unknown product {product!r}"
    b = (b or "").strip() or DEFAULT_BRANCH
    if not valid_branch(b):
        return (f"{b!r} is not a usable branch name - letters, digits, "
                "'.', '_', '-' and '/' only")
    d = _load()
    d.setdefault("branch", {})[product] = b
    return None if _save(d) else "could not write the settings file"


def branches() -> dict:
    return {NAVICORE: branch(NAVICORE), WCB: branch(WCB)}
