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

    INTELLEX_DATA_DIR names the directory outright, on every platform (H4, CLAUDE.md "A
    test host needs a leash"): a test bench's staged host keeps its settings, logs, bundles
    and firmware cache there. LOCALAPPDATA did that on Windows alone; on macOS and Linux
    the directory hangs off the home folder, so a staged host wrote the real one.
    """
    override = os.environ.get("INTELLEX_DATA_DIR")
    if override:
        return pathlib.Path(override)
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

    `name` is "webui" (NaviCore config tool), "webui_wcb" (WCB Wizard),
    "firmware" (the offline flash cache -- see src/fwcache.py) or "wiki" (the
    downloaded project wikis -- see tools/fetch_wiki.py).

    Non-empty is the test, not merely present. An update that is interrupted
    between creating the directory and filling it would otherwise shadow a
    perfectly good bundled copy with nothing at all, and the app would serve a
    blank tool while the real one sat unused inside it.
    """
    if FROZEN:
        user = user_data_dir() / name
        if user.is_dir() and any(user.iterdir()) and _user_copy_wins(name, user, BUNDLE_DIR / name):
            return user
    return BUNDLE_DIR / name


# ── Which copy of a TOOL is served: the app's own, or one downloaded since ──
# "The user copy wins" was right until the app itself updated. An app built today
# carries today's tools, and a copy fetched by Update tools a month ago - still in
# the user data dir - kept winning over them: a new Intellex opened with old tools
# (Greg, 2026-10-15: "Config tools should be up to date as of the time of app
# creation/build"). So for the two tools the choice is made on what each copy IS:
# every fetch writes <dir>.source.json beside it (record_tool_source: the branch it
# came from and its version stamp), the build's fetch included, and
#   - a copy of the branch now chosen beats one of another branch, either way round
#     (a feature branch's tool is wanted even when main's in the app is newer);
#   - otherwise the newer version stamp wins, the downloaded copy on a tie;
#   - a copy with no record (fetched before this existed) is judged by its stamp.
# Firmware and the wikis are not chosen here: fwcache looks in both places per image,
# and data_subdir below decides per wiki.
_TOOLS = {"webui": ("navicore", "index.html", r'id="footer-dtg"[^>]*>([^<]+)<'),
          "webui_wcb": ("wcb", "Wizard/app.js", r"""\bUI_VERSION\s*=\s*['"]([^'"]+)['"]""")}
_MONTHS = {m: i for i, m in enumerate(("JAN", "FEB", "MAR", "APR", "MAY", "JUN", "JUL", "AUG", "SEP", "OCT", "NOV",
                                       "DEC"), 1)}
_choice_cache: dict = {}


def tool_stamp(d: pathlib.Path, name: str) -> str:
    """The version stamp inside a tool copy: the config tool's footer-dtg, the Wizard's UI_VERSION. '' if none."""
    import re
    _, rel, rx = _TOOLS[name]
    try:
        m = re.search(rx, (d / rel).read_text(encoding="utf-8", errors="replace"))
    except OSError:
        return ""
    return m.group(1).strip() if m else ""


def stamp_key(stamp: str):
    """'14.21:48.Q.OCT.2026' (DD.HH:MM.<zone>.MON.YYYY, both tools' pre-commit hooks) -> a sortable tuple, or None."""
    import re
    m = re.match(r"^(\d{1,2})\.(\d{1,2}):(\d{2})\.[A-Z]\.([A-Z]{3})\.(\d{4})$", (stamp or "").strip())
    if not m or m.group(4) not in _MONTHS:
        return None
    return (int(m.group(5)), _MONTHS[m.group(4)], int(m.group(1)), int(m.group(2)), int(m.group(3)))


def tool_source(d: pathlib.Path) -> dict:
    import json
    try:
        got = json.loads(d.with_name(d.name + ".source.json").read_text(encoding="utf-8"))
        return got if isinstance(got, dict) else {}
    except (OSError, ValueError):
        return {}


def record_tool_source(d: pathlib.Path, branch: str, stamp: str) -> None:
    """Called by tools/fetch_webui.py after every fetch that left a tool current: <d>.source.json, beside the copy
    and never inside it (the copy stays the published files byte for byte)."""
    import json
    import time
    try:
        d.with_name(d.name + ".source.json").write_text(
            json.dumps({"branch": branch, "stamp": stamp, "at": time.time()}), encoding="utf-8")
    except OSError:
        pass


def _user_copy_wins(name: str, user: pathlib.Path, bundled: pathlib.Path) -> bool:
    if name not in _TOOLS:
        return True
    if not (bundled.is_dir() and any(bundled.iterdir())):
        return True
    import time
    try:
        import settings
        want = settings.branch(_TOOLS[name][0])
    except Exception:                                   # noqa: BLE001 - no setting is main
        want = "main"
    try:
        key = (name, want, user.stat().st_mtime_ns, user.with_name(user.name + ".source.json").stat().st_mtime_ns
               if user.with_name(user.name + ".source.json").exists() else 0)
    except OSError:
        key = None
    hit = _choice_cache.get(name)
    if key and hit and hit[0] == key and time.monotonic() - hit[2] < 5.0:
        return hit[1]                                   # per request otherwise: a 700 KB app.js read every time
    ub, bb = tool_source(user).get("branch"), tool_source(bundled).get("branch")
    if ub == want and bb and bb != want:
        wins = True
    elif bb == want and ub and ub != want:
        wins = False
    else:
        ku, kb = stamp_key(tool_stamp(user, name)), stamp_key(tool_stamp(bundled, name))
        wins = kb is None or (ku is not None and ku >= kb)
    _choice_cache[name] = (key, wins, time.monotonic())
    return wins


def data_subdir(name: str, sub: str) -> pathlib.Path:
    """data_dir(), but the user-or-bundled choice is made PER SUBDIRECTORY.

    THE WIKIS NEEDED THIS AND THE TOOL BUNDLES DID NOT. data_dir() decides once
    for a whole directory, which is right when its contents are one indivisible
    thing -- src/webui/ is a config tool, and half of one is not useful. But
    src/wiki/ holds THREE independent wikis that are downloaded and swapped
    separately, so deciding for the parent means the first one written to the user
    directory hides every sibling that only exists in the bundle.

    Observed on the frozen build: fetching just the Intellex wiki left NaviCore
    and WCB reporting `have=False` while both sat inside the app, perfectly good.
    A partial download is a normal outcome -- one wiki can fail while the others
    succeed -- so this is not an edge case.
    """
    if FROZEN:
        user = user_data_dir() / name / sub
        if user.is_dir() and any(user.iterdir()) and _user_wiki_wins(user, BUNDLE_DIR / name / sub):
            return user
    return BUNDLE_DIR / name / sub


def record_wiki_source(d: pathlib.Path) -> None:
    """tools/fetch_wiki.py, after a wiki was swapped in: <d>.source.json with when it was fetched."""
    import json
    import time
    try:
        d.with_name(d.name + ".source.json").write_text(json.dumps({"at": time.time()}), encoding="utf-8")
    except OSError:
        pass


def _user_wiki_wins(user: pathlib.Path, bundled: pathlib.Path) -> bool:
    """The wikis' version of _user_copy_wins: a wiki has no version stamp, so the later fetch wins. A downloaded copy
    with no record (fetched before records existed) loses to a bundled one that has one - the app's own build."""
    if not (bundled.is_dir() and any(bundled.iterdir())):
        return True
    u, b = tool_source(user).get("at"), tool_source(bundled).get("at")
    if b is None:
        return True
    return u is not None and u >= b


def write_dir(name: str) -> pathlib.Path:
    """Where an UPDATE should land. Never inside the app when frozen."""
    return (user_data_dir() / name) if FROZEN else (BUNDLE_DIR / name)
