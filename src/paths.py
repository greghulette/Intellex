"""Where things live, run from source AND run as a frozen app.

THE PROBLEM THIS EXISTS FOR
The bundled config tool and Wizard are *updatable* — the Update button rewrites
them from GitHub Pages. Run from a checkout that is trivially true: they sit in
src/webui/ and src/webui_wcb/ and stay there.

Frozen, it is not. PyInstaller's one-file mode unpacks the whole app into a fresh
temp directory on every launch and deletes it on exit, so an update written
"beside the app" is gone the moment the app closes -- silently, with the button
reporting success. One-dir mode is barely better: on Windows the app lands under
Program Files or %LOCALAPPDATA%\\Programs, and writing into an installed
application directory is the sort of thing that works for the installing user and
fails for everyone else, or trips antivirus.

So a frozen build has TWO locations and a precedence rule:

    bundled   read-only, inside the app       - what shipped, always present
    user      writable, in the user data dir  - what the Update button wrote

The user copy WINS when it exists, and the bundled copy is the permanent
fallback. That is exactly the "atomic and revertible" requirement in CLAUDE.md:
a bad update can be undone by deleting one directory, and the app still starts
because what shipped was never touched.

Run from source there is only one location and it is writable, so both resolve to
the same path and nothing changes.
"""
from __future__ import annotations

import os
import pathlib
import sys

# True when running from a PyInstaller build (one-file or one-dir).
FROZEN = getattr(sys, "frozen", False)

# Where the app's own files are. Under one-file PyInstaller this is the temp
# extraction directory (sys._MEIPASS); from source it is src/.
BUNDLE_DIR = pathlib.Path(getattr(sys, "_MEIPASS", "")) if FROZEN \
    else pathlib.Path(__file__).resolve().parent


# The app was called NaviLink until 2026-09-08, and this directory carried that
# name. Renaming without moving the old one would silently orphan every piece of
# user state at once: the per-product branch settings, both updated tool bundles,
# and -- the one that actually bites -- the offline firmware cache. That cache is
# what makes flashing work on a con floor with no network, so its loss would not
# surface until exactly the moment there is no way to refill it, and it would
# look like "flashing broke", nowhere near a rename.
_LEGACY_DIR_NAME = "NaviLink"

_migrated = False


def _migrate_legacy(new: pathlib.Path) -> None:
    """Move a pre-rename user directory to `new`, at most once per process.

    Only ever when `new` does not exist. An existing new directory is the newer
    truth and is never merged into or overwritten -- a half-merge of two tool
    bundles is worse than either one alone.

    Failure is deliberately silent. If the move cannot happen the app falls back
    to the bundled copies and shipped defaults, which is precisely what it would
    have done had there been no legacy directory at all.
    """
    global _migrated
    if _migrated:
        return
    _migrated = True
    try:
        if new.exists():
            return
        old = new.parent / _LEGACY_DIR_NAME
        if old.is_dir():
            old.rename(new)
    except OSError:
        pass


def user_data_dir() -> pathlib.Path:
    """Per-user, writable, and survives an app update.

    Deliberately the platform's own location rather than somewhere next to the
    executable: an installed app directory is not writable by a normal user on
    either platform, and putting user state there is what makes an app need
    admin rights to do something as ordinary as refreshing its UI.
    """
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~\\AppData\\Local")
        d = pathlib.Path(base) / "Intellex"
    elif sys.platform == "darwin":
        d = pathlib.Path.home() / "Library" / "Application Support" / "Intellex"
    else:
        d = pathlib.Path(os.environ.get("XDG_DATA_HOME")
                         or (pathlib.Path.home() / ".local" / "share")) / "Intellex"
    _migrate_legacy(d)
    return d


def data_dir(name: str) -> pathlib.Path:
    """Where to READ a bundled directory from: the updated copy if there is one.

    `name` is "webui" (NaviCore config tool), "webui_wcb" (WCB Wizard) or
    "firmware" (the offline flash cache -- see src/fwcache.py).

    Non-empty is the test, not merely present. An update that is interrupted
    between creating the directory and filling it would otherwise shadow a
    perfectly good bundled copy with nothing at all, and the app would serve a
    blank tool while the real one sat unused inside it.
    """
    if FROZEN:
        user = user_data_dir() / name
        if user.is_dir() and any(user.iterdir()):
            return user
    return BUNDLE_DIR / name


def write_dir(name: str) -> pathlib.Path:
    """Where an UPDATE should land. Never inside the app when frozen."""
    return (user_data_dir() / name) if FROZEN else (BUNDLE_DIR / name)
