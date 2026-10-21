#!/usr/bin/env python3
"""Do the two updates leave a whole, running app behind?

    python tools/smoke_updates.py

No network, no window. Checked against the REAL modules:

    U1  page_refs()        what a page loads from beside itself: relative <script src> / <link href>, not a font,
                           a CDN, a data: URI or ../Images
    U2  fetch_wcb()        against a fake Pages site: a Wizard that loads a file WCB_FILES does not list and a
                           cmdlib/ (a vendor CMDLIB_VENDORS does not list included) arrives whole; a second run is
                           "up to date"; a copy with the right stamp that lacks one of them is fetched again; an
                           unreadable manifest changes nothing; a missing vendor/ file does not stop the update;
                           a Wizard without cmdlib/ gets none
    U3  restart_now()      starts the new copy in proc.fresh_app_env(): PYINSTALLER_RESET_ENVIRONMENT=1, no _PYI_*
    U4  app.py at import   (Windows) a frozen --after-update start WITHOUT the fresh marker - what 0.2.1 and older
                           start - relaunches itself fresh before importing anything; with the marker, or without
                           --after-update, it goes straight on and the marker is gone from its environment

WHY THESE (2026-10-20)
U2: with WCB on seq-builder, Update tools bundled that Wizard from a fixed list, without seq-builder.js,
seq-builder.css or cmdlib/, and its sequence builder silently did nothing in the app. U3/U4: Update Intellex
started the new copy in the old one's environment, so it ran from the old copy's unpacked folder, which was
deleted under it - a browser instead of the window and a 404 on /_launcher.
"""
from __future__ import annotations

import json
import os
import pathlib
import shutil
import subprocess
import sys
import tempfile
import urllib.error

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tools"))

failures: list[str] = []


def check(ok: bool, what: str) -> None:
    print(("ok    " if ok else "FAIL  ") + what)
    if not ok:
        failures.append(what)


import fetch_webui as fw   # noqa: E402
import proc                # noqa: E402
import selfupdate          # noqa: E402

# ── U1 ─────────────────────────────────────────────────────────────────────
page = b"""<html><head>
<link rel="icon" type="image/png" href="favicon.png">
<link rel="icon" href="data:image/svg+xml;base64,AAAA">
<link rel="preconnect" href="https://fonts.googleapis.com">
<link href="https://fonts.googleapis.com/css2?family=Syne:wght@400&display=swap" rel="stylesheet">
<link rel="stylesheet" href="styles.css">
<LINK rel='stylesheet' href='extra.css?v=2'>
</head><body><img src="../Images/r2logo.png">
<script src="parser.js"></script><script src="app.js"></script>
<script defer src="extra.js"></script><script src="//cdn.example/x.js"></script>
<script src="../evil.js"></script><script src="/abs.js"></script>
</body></html>"""
check(fw.page_refs(page) == ["favicon.png", "styles.css", "extra.css", "parser.js", "app.js", "extra.js"],
      f"U1 page_refs takes the page's own files, in order, and nothing else: {fw.page_refs(page)}")
wiz_index = ROOT.parent / "Wireless_Communication_Board-WCB" / "Wizard" / "index.html"
if wiz_index.is_file():
    refs = fw.page_refs(wiz_index.read_bytes())
    check(all(n in refs for n in ("app.js", "parser.js", "styles.css")),
          f"U1 the WCB checkout's own Wizard index.html: {refs}")

# ── U2 ─────────────────────────────────────────────────────────────────────
tmp = pathlib.Path(tempfile.mkdtemp(prefix="intellex-smoke-updates-"))
SITE = "https://site.invalid/Wizard"
site: dict[str, bytes] = {}


def publish(stamp: str, cmdlib: bool = True, extra: bool = True, vendor: bool = True) -> None:
    """A fake Pages copy of a Wizard. extra.js is the file WCB_FILES does not list; it is what loads cmdlib/,
    including 'acme', a vendor CMDLIB_VENDORS does not list."""
    site.clear()
    tags = '<script src="extra.js"></script><link rel="stylesheet" href="extra.css">' if extra else ""
    for n in fw.WCB_FILES:
        site[f"{SITE}/{n}"] = f"/* {n} */".encode()
    site[f"{SITE}/index.html"] = f'<html><script src="app.js"></script>{tags}</html>'.encode()
    site[f"{SITE}/app.js"] = f"const UI_VERSION = '{stamp}';".encode()
    if vendor:
        for n in fw.WCB_VENDOR:
            site[f"{SITE}/{n}"] = f"/* {n} */".encode()
    if extra:
        site[f"{SITE}/extra.js"] = (b"const M = ['cmdlib/droidnet/manifest.json', 'cmdlib/acme/manifest.json'];"
                                    if cmdlib else b"// no library")
        site[f"{SITE}/extra.css"] = b"/* extra */"
    if cmdlib:
        site[f"{SITE}/cmdlib/droidnet/manifest.json"] = json.dumps({"boards": [{"file": "boards/a.json"}]}).encode()
        site[f"{SITE}/cmdlib/droidnet/boards/a.json"] = b'{"name": "a"}'
        site[f"{SITE}/cmdlib/droidnet/LICENSE"] = b"MIT"
        site[f"{SITE}/cmdlib/acme/manifest.json"] = json.dumps({"boards": ["b.json"]}).encode()
        site[f"{SITE}/cmdlib/acme/b.json"] = b'{"name": "b"}'


def fake_get(url: str, timeout: float = 30.0, attempts: int = 4) -> bytes:
    if url in site:
        return site[url]
    raise fw.FetchError(url, urllib.error.HTTPError(url, 404, "Not Found", None, None))


fw.get = fake_get
fw.WEBUI_WCB = tmp / "webui_wcb"
WIZ = fw.WEBUI_WCB / fw.WCB_SUBDIR


def run_wcb(force: bool = False) -> int:
    return fw.fetch_wcb(SITE, check=False, force=force)


publish("01.00:00.R.OCT.2026")
rc = run_wcb()
want = ["extra.js", "extra.css", "cmdlib/droidnet/manifest.json", "cmdlib/droidnet/boards/a.json",
        "cmdlib/droidnet/LICENSE", "cmdlib/acme/manifest.json", "cmdlib/acme/b.json"]
check(rc == 0 and all((WIZ / n).read_bytes() == site[f"{SITE}/{n}"] for n in want if (WIZ / n).is_file())
      and all((WIZ / n).is_file() for n in want),
      "U2 a Wizard loading a file WCB_FILES lacks, and a cmdlib/ with an unlisted vendor, arrives whole and "
      "byte-identical")
check(rc == 0 and not fw.WEBUI_WCB.with_suffix(".prev").exists() and run_wcb() == 0
      and not fw.WEBUI_WCB.with_suffix(".prev").exists(), "U2 a second run is up to date and swaps nothing")
(WIZ / "extra.js").unlink()
shutil.rmtree(WIZ / "cmdlib")
check(fw._missing_from(WIZ) == ["extra.js"], f"U2 the same stamp without extra.js is incomplete: "
                                              f"{fw._missing_from(WIZ)}")
check(run_wcb() == 0 and (WIZ / "extra.js").is_file() and (WIZ / "cmdlib" / "acme" / "b.json").is_file(),
      "U2 ...and is fetched whole again")
(WIZ / "cmdlib" / "acme" / "b.json").unlink()
shutil.rmtree(WIZ / "cmdlib")
check(fw._missing_from(WIZ) == ["cmdlib/"], "U2 the same stamp without cmdlib/ is incomplete")
run_wcb()

publish("02.00:00.R.OCT.2026")
for k in [k for k in site if "/cmdlib/" in k and k.endswith("manifest.json")]:
    del site[k]
before = (WIZ / "app.js").read_bytes()
check(run_wcb() == 1 and (WIZ / "app.js").read_bytes() == before,
      "U2 a Wizard that loads cmdlib/ whose manifests cannot be read changes nothing")

publish("03.00:00.R.OCT.2026", vendor=False)
check(run_wcb() == 0 and b"03.00" in (WIZ / "app.js").read_bytes(),
      "U2 a vendor/ file the site lacks (a renamed flasher library) does not stop the update")

publish("04.00:00.R.OCT.2026", cmdlib=False, extra=False)
check(run_wcb() == 0 and not (WIZ / "cmdlib").exists() and not (WIZ / "extra.js").exists(),
      "U2 a Wizard that loads no cmdlib/ gets none, and no extra file")
shutil.rmtree(tmp, ignore_errors=True)

# ── U3 ─────────────────────────────────────────────────────────────────────
class _Exited(BaseException):
    pass


calls: list = []
real_popen, real_exit, real_mode = proc.popen, os._exit, selfupdate.mode
os.environ["_PYI_ARCHIVE_FILE"] = "C:\\stale\\Intellex.exe"
os.environ["_PYI_APPLICATION_HOME_DIR"] = "C:\\stale\\_MEI1"
try:
    proc.popen = lambda args, **kw: calls.append((args, kw))
    os._exit = lambda code: (_ for _ in ()).throw(_Exited(code))
    selfupdate.mode = lambda: "windows"
    try:
        selfupdate.restart_now()
    except _Exited:
        pass
finally:
    proc.popen, os._exit, selfupdate.mode = real_popen, real_exit, real_mode
    os.environ.pop("_PYI_ARCHIVE_FILE", None)
    os.environ.pop("_PYI_APPLICATION_HOME_DIR", None)
env = calls[0][1].get("env") if calls else None
check(bool(env) and env.get("PYINSTALLER_RESET_ENVIRONMENT") == "1" and env.get(proc.FRESH_START) == "restart"
      and not any(k.startswith("_PYI_") for k in env),
      "U3 restart_now starts the new copy in a fresh environment (its own unpacked folder)")
check(bool(calls) and selfupdate.AFTER_UPDATE in calls[0][0], "U3 ...with --after-update")

# ── U4 ─────────────────────────────────────────────────────────────────────
# app.py's check runs at import, so each case is a separate interpreter: proc.popen and os._exit recorded, and
# `import applog` - the line after the check - stopped, so reaching it means the check let the start go on.
PROBE = r"""
import json, os, sys
sys.path.insert(0, sys.argv[1])
sys.frozen = True
sys.argv = ["Intellex.exe"] + json.loads(sys.argv[2])
import proc
out = {"popen": None, "exit": None, "reached": False}
proc.popen = lambda args, **kw: out.__setitem__("popen", {"args": args[1:], "env": {
    k: kw["env"].get(k) for k in ("PYINSTALLER_RESET_ENVIRONMENT", proc.FRESH_START)}})
class Exited(BaseException): pass
def _exit(code):
    out["exit"] = code
    raise Exited()
os._exit = _exit
sys.modules["applog"] = None
try:
    import app
except Exited:
    pass
except ImportError as e:
    out["reached"] = "applog" in str(e)
out["marker_left"] = os.environ.get(proc.FRESH_START)
print(json.dumps(out))
"""
if sys.platform == "win32":
    def probe(args: list, marker: str | None) -> dict:
        e = {k: v for k, v in os.environ.items() if k != proc.FRESH_START}
        if marker:
            e[proc.FRESH_START] = marker
        r = subprocess.run([sys.executable, "-c", PROBE, str(ROOT / "src"), json.dumps(args)],
                           capture_output=True, text=True, env=e, timeout=60)
        try:
            return json.loads(r.stdout.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return {"error": r.stderr[-400:]}

    old = probe(["--after-update", "--port", "8765"], None)
    check(old.get("exit") == 0 and old.get("popen") == {
        "args": ["--after-update", "--port", "8765"],
        "env": {"PYINSTALLER_RESET_ENVIRONMENT": "1", proc.FRESH_START: "healed"}} and not old.get("reached"),
          f"U4 an --after-update start from an older Intellex relaunches itself fresh, same arguments: {old}")
    new = probe(["--after-update"], "restart")
    check(new.get("reached") and new.get("popen") is None and new.get("marker_left") is None,
          f"U4 one this version restarted goes straight on, and the marker is not passed on: {new}")
    plain = probe([], None)
    check(plain.get("reached") and plain.get("popen") is None, f"U4 an ordinary start goes straight on: {plain}")
else:
    print("skip  U4 (the one-file restart is Windows-only)")

print()
print("PASSED" if not failures else f"FAILED: {len(failures)} check(s)")
sys.exit(1 if failures else 0)
