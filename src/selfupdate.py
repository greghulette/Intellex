"""Update Intellex itself - the third kind of update, beside the tools and the firmware.

WHERE A NEW VERSION COMES FROM
GitHub Releases on this repo. Pushing a tag vX.Y.Z that matches version.VERSION makes
.github/workflows/release.yml build every platform on GitHub's own machines and attach
the results, plus a SHA256SUMS.txt, to a release:

    Intellex-<v>-windows.exe          the one-file Windows app
    Intellex-<v>-macos-x86_64.zip     the .app for an Intel Mac  (ditto -c -k --keepParent)
    Intellex-<v>-macos-arm64.zip      the .app for an Apple-silicon Mac
    Intellex-<v>-macos-<arch>.dmg     the same app for a first install, drag-to-Applications

THREE WAYS IT RUNS, THREE WAYS IT UPDATES (mode())
- windows: a frozen .exe. Windows will not overwrite a running .exe but WILL rename one,
  so the running file becomes Intellex.old.exe, the verified download takes its name, the
  new one is started and this process ends. The next start deletes the .old. The new one is
  started in proc.fresh_app_env(), or it would run from this one's unpacked folder, which is
  deleted as this one exits (proc.fresh_app_env says how that failed).
- macos: a frozen Intellex.app. The verified zip is unpacked with ditto (an .app holds
  symlinks a plain unzip flattens), the running bundle is moved into the user data dir as
  the rollback copy, the new one takes its place, and `open -n` starts it.
- source: a git checkout, run through Intellex.command / Intellex.bat - how Greg runs it.
  It follows its branch's upstream, not the releases: a fast-forward merge (never a merge
  commit, never over local changes), the Python packages refreshed, and the process
  replaced in place (os.execv), so the Terminal that launched it stays its parent.

THE CHECK runs once at launch, in the background (host.py _start_bg), and only when GitHub
is reachable: offline it records why and says nothing more. It never installs anything; the
launcher's Update Intellex button does that, with its own confirm. A result is reused for
CHECK_TTL_S so launching three times in a row does not spend three of the hourly 60
unauthenticated API calls.

NOTHING IS REPLACED UNTIL THE NEW COPY IS WHOLE AND VERIFIED. The download goes to a
.part file, its SHA-256 must match the release's SHA256SUMS.txt (a release without one is
refused), and only then is anything renamed. A failed rename puts the old one back.

THE RESTART. The new process cannot bind the port while this one holds it, so it is started
with --after-update, which makes app.py retry the bind for a while (and say "updated" once
up). The host answers the launcher first and restarts a moment later (restart_soon), so the
page sees "restarting" rather than a dropped request.
"""
from __future__ import annotations

import hashlib
import json
import os
import pathlib
import platform
import shutil
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
from typing import Callable, Optional

import certs
import paths
import proc
import version

REPO = "greghulette/Intellex"
API = "https://api.github.com"
CHECK_TTL_S = 3600
AFTER_UPDATE = "--after-update"
SUMS = "SHA256SUMS.txt"

_lock = threading.Lock()
_state: dict = {
    "checked": False, "checking": False, "at": 0.0, "error": "", "reason": "",
    "latest": "", "available": False, "notes_url": "", "asset": "", "behind": 0, "branch": "",
    "install": {"running": False, "step": "", "percent": 0, "error": "", "done": False, "restarting": False},
}
# Test seams: the network, the git/pip runner and the restart. Plain module attributes, so a
# test swaps one for a stand-in and puts it back.
urlopen = urllib.request.urlopen


def root() -> pathlib.Path:
    """The checkout this runs from (source mode)."""
    return pathlib.Path(__file__).resolve().parent.parent


def mode() -> str:
    if paths.FROZEN:
        return {"win32": "windows", "darwin": "macos"}.get(sys.platform, "unknown")
    return "source" if (root() / ".git").exists() else "unknown"


def mac_arch() -> str:
    return "arm64" if platform.machine().lower() in ("arm64", "aarch64") else "x86_64"


def asset_name(ver: str, m: Optional[str] = None) -> str:
    """The release file this computer needs for version `ver` ('0.2.0'), or ''."""
    m = m or mode()
    if m == "windows":
        return f"Intellex-{ver}-windows.exe"
    if m == "macos":
        return f"Intellex-{ver}-macos-{mac_arch()}.zip"
    return ""


def offline() -> bool:
    return os.environ.get("INTELLEX_OFFLINE", "") not in ("", "0")


def _cache_file() -> pathlib.Path:
    return paths.user_data_dir() / "app_update.json"


def _updated_file() -> pathlib.Path:
    return paths.user_data_dir() / "app_updated.json"


def _updates_dir() -> pathlib.Path:
    d = paths.user_data_dir() / "updates"
    d.mkdir(parents=True, exist_ok=True)
    return d


# ── What is running ────────────────────────────────────────────────────────
def status() -> dict:
    """Everything the launcher shows: this build, the last check, an install in progress."""
    with _lock:
        s = json.loads(json.dumps(_state))
    i = version.info()
    s.update({"current": i["version"], "commit": i["commit"], "built": i["built"], "mode": mode(),
              "updatedFrom": _just_updated()})
    return s


def _just_updated() -> str:
    """'0.1.0' when this run is the first since an update from it (the launcher says so once)."""
    try:
        d = json.loads(_updated_file().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    return d.get("from", "") if d.get("to") == version.VERSION and not d.get("shown") else ""


def mark_update_shown() -> None:
    try:
        f = _updated_file()
        d = json.loads(f.read_text(encoding="utf-8"))
        d["shown"] = True
        f.write_text(json.dumps(d), encoding="utf-8")
    except (OSError, ValueError):
        pass


# ── The check ──────────────────────────────────────────────────────────────
def check(force: bool = False, report: bool = True) -> Optional[dict]:
    """Ask whether a newer Intellex exists; never installs. Returns status(), or None with report=False (the launch
    check, which nobody reads: status() asks git for the commit)."""
    with _lock:
        busy = _state["checking"] or (not force and _state["checked"] and time.time() - _state["at"] < CHECK_TTL_S)
        if not busy:
            _state["checking"] = True
    if busy:
        return status() if report else None
    try:
        got = _check_source() if mode() == "source" else _check_release(force)
    except Exception as e:  # noqa: BLE001 - a check never breaks the app
        got = {"error": f"{type(e).__name__}: {e}", "available": False}
    with _lock:
        for k in ("error", "reason", "latest", "available", "notes_url", "asset", "behind", "branch"):
            _state[k] = got.get(k, "" if k not in ("available", "behind") else (False if k == "available" else 0))
        _state["checked"], _state["checking"], _state["at"] = True, False, time.time()
    return status() if report else None


def _check_release(force: bool) -> dict:
    m = mode()
    if m not in ("windows", "macos"):
        return {"reason": "this copy of Intellex is not one a release can replace"}
    if offline():
        return {"reason": "offline mode (INTELLEX_OFFLINE)"}
    if not force:
        try:
            c = json.loads(_cache_file().read_text(encoding="utf-8"))
            if c.get("for") == version.VERSION and time.time() - c.get("at", 0) < CHECK_TTL_S:
                return c["result"]
        except (OSError, ValueError, KeyError):
            pass
    try:
        rel = _get_json(f"{API}/repos/{REPO}/releases/latest")
    except urllib.error.HTTPError as e:
        if e.code == 404:
            return {"reason": "no Intellex release has been published yet"}
        raise
    except (urllib.error.URLError, OSError) as e:
        return {"reason": f"GitHub is not reachable ({getattr(e, 'reason', e)})"}
    tag = str(rel.get("tag_name") or "")
    latest = tag[1:] if tag.startswith("v") else tag
    cur, new = version.parse(version.VERSION), version.parse(latest)
    out = {"latest": latest, "notes_url": rel.get("html_url", ""), "available": False}
    if not new:
        out["reason"] = f"the latest release's tag {tag!r} is not a version"
    elif not cur or new <= cur:
        out["reason"] = "up to date"
    else:
        names = {a.get("name"): a for a in rel.get("assets") or []}
        want = asset_name(latest, m)
        if want not in names:
            out["reason"] = f"release {latest} has no build for this computer ({want})"
        elif SUMS not in names:
            out["reason"] = f"release {latest} has no {SUMS} to check a download against"
        else:
            out.update(available=True, asset=want)
    try:
        _cache_file().write_text(json.dumps({"for": version.VERSION, "at": time.time(), "result": out}),
                                 encoding="utf-8")
    except OSError:
        pass
    return out


def _get_json(url: str):
    req = urllib.request.Request(url, headers={"User-Agent": "Intellex", "Accept": "application/vnd.github+json"})
    with urlopen(req, timeout=20, context=certs.context()) as r:
        return json.loads(r.read().decode("utf-8"))


def _git(*args, timeout: float = 60.0):
    return proc.run(["git", "-C", str(root()), *args], capture_output=True, text=True, timeout=timeout)


def _check_source() -> dict:
    if offline():
        return {"reason": "offline mode (INTELLEX_OFFLINE)"}
    br = _git("rev-parse", "--abbrev-ref", "HEAD", timeout=10).stdout.strip()
    if not br or br == "HEAD":
        return {"reason": "the checkout is not on a branch"}
    up = _git("rev-parse", "--abbrev-ref", "--symbolic-full-name", "@{u}", timeout=10)
    if up.returncode != 0:
        return {"branch": br, "reason": f"branch {br} follows no remote branch"}
    f = _git("fetch", "--quiet", timeout=60)
    if f.returncode != 0:
        return {"branch": br, "reason": "could not fetch from GitHub (" +
                ((f.stderr or f.stdout).strip().splitlines() or ["no reason given"])[-1][:160] + ")"}
    behind = int(_git("rev-list", "--count", "HEAD..@{u}", timeout=10).stdout.strip() or 0)
    head = _git("rev-parse", "--short", "@{u}", timeout=10).stdout.strip()
    out = {"branch": br, "behind": behind, "latest": head, "available": behind > 0}
    if not behind:
        out["reason"] = "up to date"
    return out


# ── The install ────────────────────────────────────────────────────────────
def _step(step: str, percent: Optional[int] = None) -> None:
    with _lock:
        _state["install"]["step"] = step
        if percent is not None:
            _state["install"]["percent"] = percent
    print(f"self-update  {step}")


def start_install(busy: Callable[[], str] = lambda: "") -> tuple[bool, str]:
    """Begin installing what check() found, on a thread. (False, why) when it cannot start. busy() names
    something that must not be cut off (a flash running), or ''."""
    why = busy()
    if why:
        return False, why
    with _lock:
        if _state["install"]["running"]:
            return False, "an update is already running"
        if not _state["available"]:
            return False, "there is no update to install - check again first"
        _state["install"] = {"running": True, "step": "starting", "percent": 0, "error": "", "done": False,
                             "restarting": False}
    threading.Thread(target=_install_job, name="intellex-self-update", daemon=True).start()
    return True, ""


def _install_job() -> None:
    try:
        m = mode()
        if m == "source":
            _install_source()
        elif m == "windows":
            _install_windows(_download_verified())
        elif m == "macos":
            _install_macos(_download_verified())
        else:
            raise UpdateError("this copy of Intellex cannot update itself")
        _record_update()
        with _lock:
            _state["install"].update(running=False, done=True, step="restarting", percent=100, restarting=True)
        restart_soon()
    except Exception as e:  # noqa: BLE001 - reported to the launcher, nothing half-done stays behind
        with _lock:
            _state["install"].update(running=False, done=False, error=str(e) or type(e).__name__)
        print(f"self-update  FAILED: {e}")


class UpdateError(Exception):
    pass


def _record_update() -> None:
    latest = _state.get("latest") or ""
    try:
        _updated_file().write_text(json.dumps({"from": version.VERSION, "to": latest, "at": time.time()}),
                                   encoding="utf-8")
    except OSError:
        pass


def _release_files(ver: str) -> dict:
    rel = _get_json(f"{API}/repos/{REPO}/releases/tags/v{ver}")
    return {a.get("name"): a.get("browser_download_url") for a in rel.get("assets") or []}


def _download(url: str, dest: pathlib.Path, label: str) -> None:
    req = urllib.request.Request(url, headers={"User-Agent": "Intellex", "Accept": "application/octet-stream"})
    part = dest.with_name(dest.name + ".part")
    with urlopen(req, timeout=60, context=certs.context()) as r, open(part, "wb") as f:
        total = int(r.headers.get("Content-Length") or 0)
        got = 0
        while True:
            chunk = r.read(256 * 1024)
            if not chunk:
                break
            f.write(chunk)
            got += len(chunk)
            if total:
                _step(f"downloading {label}", min(95, int(got * 95 / total)))
    os.replace(part, dest)


def _sha256(p: pathlib.Path) -> str:
    h = hashlib.sha256()
    with open(p, "rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _download_verified() -> pathlib.Path:
    ver, name = _state["latest"], _state["asset"]
    if offline():
        raise UpdateError("offline mode (INTELLEX_OFFLINE): nothing downloaded")
    _step(f"asking GitHub for release {ver}", 0)
    files = _release_files(ver)
    if name not in files or SUMS not in files:
        raise UpdateError(f"release {ver} has no {name if name not in files else SUMS}")
    d = _updates_dir()
    sums = d / SUMS
    _download(files[SUMS], sums, SUMS)
    want = ""
    for line in sums.read_text(encoding="utf-8", errors="replace").splitlines():
        parts = line.split()
        if len(parts) == 2 and parts[1].lstrip("*") == name:
            want = parts[0].lower()
    if not want:
        raise UpdateError(f"{SUMS} of release {ver} does not list {name}")
    dest = d / name
    _download(files[name], dest, name)
    _step("checking the download", 96)
    got = _sha256(dest)
    if got != want:
        dest.unlink(missing_ok=True)
        raise UpdateError(f"the download of {name} does not match {SUMS} (got {got[:12]}, expected {want[:12]}) - "
                          "nothing was changed")
    return dest


def _install_windows(new: pathlib.Path) -> None:
    exe = pathlib.Path(sys.executable)
    old = exe.with_name(exe.stem + ".old.exe")
    _step("replacing Intellex.exe", 97)
    if old.exists():
        old.unlink()                      # left by the previous update, whose process has ended since
    exe.rename(old)                       # Windows renames a running .exe; it will not overwrite one
    try:
        shutil.copy2(new, exe)
    except Exception:
        old.rename(exe)                   # put it back: the app must still start
        raise
    new.unlink(missing_ok=True)


def _bundle() -> pathlib.Path:
    """The running Intellex.app (sys.executable is <bundle>/Contents/MacOS/Intellex)."""
    return pathlib.Path(sys.executable).resolve().parents[2]


def _install_macos(new_zip: pathlib.Path) -> None:
    bundle = _bundle()
    if bundle.suffix != ".app":
        raise UpdateError(f"cannot find the running Intellex.app (it runs from {sys.executable})")
    if "/AppTranslocation/" in str(bundle):
        raise UpdateError("macOS is running Intellex from a temporary copy (App Translocation): move Intellex.app "
                          "into Applications, start it from there, and update again")
    if not os.access(bundle.parent, os.W_OK):
        raise UpdateError(f"this account cannot write to {bundle.parent}: download the .dmg from the release page "
                          "and drag the new Intellex to Applications instead")
    _step("unpacking", 97)
    tmp = pathlib.Path(tempfile.mkdtemp(prefix="intellex-update-", dir=_updates_dir()))
    try:
        r = proc.run(["ditto", "-x", "-k", str(new_zip), str(tmp)], capture_output=True, text=True, timeout=300)
        app = tmp / "Intellex.app"
        if r.returncode != 0 or not (app / "Contents" / "MacOS" / "Intellex").is_file():
            raise UpdateError(f"the download did not unpack into an Intellex.app ({(r.stderr or '').strip()[:160]})")
        proc.run(["xattr", "-dr", "com.apple.quarantine", str(app)], capture_output=True, timeout=60)
        prev = _updates_dir() / "Intellex.prev.app"
        if prev.exists():
            shutil.rmtree(prev)
        _step("replacing Intellex.app", 98)
        shutil.move(str(bundle), str(prev))           # the rollback copy, out of Applications
        try:
            shutil.move(str(app), str(bundle))
        except Exception:
            shutil.move(str(prev), str(bundle))       # put it back: the app must still start
            raise
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        new_zip.unlink(missing_ok=True)


def _install_source() -> None:
    if offline():
        raise UpdateError("offline mode (INTELLEX_OFFLINE): nothing fetched")
    _step("checking the checkout", 5)
    dirty = _git("status", "--porcelain", "--untracked-files=no", timeout=20).stdout.strip()
    if dirty:
        raise UpdateError("the checkout has uncommitted changes - commit or stash them, then update "
                          f"({len(dirty.splitlines())} file(s))")
    _step("fetching", 20)
    f = _git("fetch", "--quiet", timeout=120)
    if f.returncode != 0:
        raise UpdateError("git fetch failed: " + ((f.stderr or f.stdout).strip() or "no reason given")[-200:])
    _step("fast-forwarding", 50)
    m = _git("merge", "--ff-only", "@{u}", timeout=60)
    if m.returncode != 0:
        raise UpdateError("the checkout cannot fast-forward to its upstream (it has commits of its own): "
                          + ((m.stderr or m.stdout).strip() or "")[-200:])
    _step("updating the Python packages", 70)
    req = root() / "requirements.txt"
    if req.is_file():
        p = proc.run([sys.executable, "-m", "pip", "install", "-q", "-r", str(req)], capture_output=True, text=True,
                     timeout=900, cwd=str(root()))
        if p.returncode != 0:
            raise UpdateError("the code was updated, but installing its Python packages failed - restart "
                              "Intellex from its launcher to retry: " + (p.stderr or p.stdout).strip()[-200:])


# ── The restart ────────────────────────────────────────────────────────────
def _relaunch_args() -> list[str]:
    args = [a for a in sys.argv[1:] if a != AFTER_UPDATE]
    return args + [AFTER_UPDATE]


def restart_now() -> None:
    """Start the updated Intellex and end this one."""
    m = mode()
    sys.stdout.flush()
    if m == "source":
        # In place: same PID, same Terminal. The listening socket is not inherited (Python makes sockets
        # non-inheritable), so the new image binds the port the moment this one is gone.
        os.execv(sys.executable, [sys.executable, sys.argv[0]] + _relaunch_args())
    if m == "windows":
        # A copy of its own, not a helper sharing this one's folder - which is deleted as this one exits.
        proc.popen([sys.executable] + _relaunch_args(), close_fds=True, env=proc.fresh_app_env(),
                   creationflags=0x00000008 | 0x00000200)          # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP
    elif m == "macos":
        proc.popen(["open", "-n", str(_bundle()), "--args"] + _relaunch_args(), close_fds=True)
    os._exit(0)


restart = restart_now   # a test seam: the host calls restart() a moment after it answered


def restart_soon(delay: float = 1.5) -> None:
    threading.Timer(delay, lambda: restart()).start()


def cleanup_after_update() -> None:
    """At start: delete what the previous update could not (the old .exe was still running then)."""
    if mode() == "windows":
        old = pathlib.Path(sys.executable).with_name(pathlib.Path(sys.executable).stem + ".old.exe")
        try:
            old.unlink(missing_ok=True)
        except OSError:
            pass
