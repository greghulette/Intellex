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
    U5  host.py            /_api/webui-version calls a copy missing a file its page loads incomplete and stale at
                           the published stamp; the startup repair fetches that tool alone, whole, records it for the
                           launcher to tell open pages, and leaves a copy it could not fetch saying incomplete
    U7  host.py            the tools update themselves: a tool whose branch publishes another stamp is fetched
                           (that one alone, or --tool all for both), none when every stamp matches or nothing could be
                           read (offline), a fetch that does not land changes nothing; what landed is recorded for the
                           launcher; /_api/status names the copy each tool is served from (a token that moves when one
                           lands) and whether a flash runs; INTELLEX_AUTO_TOOLS=0 turns it off
    U6  no REST API        the release check and the branch list read github.com's own links (a local stand-in
                           here): the latest tag from where /releases/latest redirects, this computer's build from
                           the release's SHA256SUMS.txt, the branches from git's ref advertisement - and never ask
                           api.github.com, whose 60 calls an hour a phone hotspot shares with strangers

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

# ── U5 ─────────────────────────────────────────────────────────────────────
# 2026-10-21: an Update tools pressed on 0.2.1 left a Wizard without seq-builder.js at the published stamp; 0.2.2's
# launcher called it up to date and nothing fetched it again.
import asyncio  # noqa: E402

import host     # noqa: E402

tmp5 = pathlib.Path(tempfile.mkdtemp(prefix="intellex-smoke-host-"))
NC, WZ = tmp5 / "webui", tmp5 / "webui_wcb"
(NC / "cmdlib" / "droidnet").mkdir(parents=True)
(NC / "index.html").write_bytes(b'<script src="flasher.js"></script><span id="footer-dtg">NC-1</span>'
                                b"<script>const M='cmdlib/droidnet/manifest.json'</script>")
(NC / "flasher.js").write_bytes(b"//")
(NC / "cmdlib" / "droidnet" / "manifest.json").write_bytes(b"{}")
(WZ / "Wizard").mkdir(parents=True)
(WZ / "Wizard" / "index.html").write_bytes(b'<script src="app.js"></script><script src="seq-builder.js"></script>')
(WZ / "Wizard" / "app.js").write_bytes(b"const UI_VERSION = 'WZ-1';")

saved = {k: getattr(host, k) for k in ("webui_dir", "wcb_dir", "_published_dtg", "_wcb_published_ver")}
real_run = proc.run
fetched: list = []


def fake_fetch_whole(argv, **kw):
    """The fetcher, as far as the host can see: a new Wizard directory swapped in by rename, seq-builder.js in it."""
    fetched.append(argv)
    new = WZ.with_name("webui_wcb.new")
    shutil.copytree(WZ, new)
    (new / "Wizard" / "seq-builder.js").write_bytes(b"window.SeqBuilder = {};")
    WZ.rename(WZ.with_name("webui_wcb.prev"))
    new.rename(WZ)
    return subprocess.CompletedProcess(argv, 0, "[wcb] updated to WZ-1  (3 files)\n", "")


def offline_fetch(argv, **kw):
    fetched.append(argv)
    return subprocess.CompletedProcess(argv, 1, "github.io is not reachable - the bundled tools are unchanged.\n", "")


def status() -> dict:
    return json.loads(asyncio.run(host.api_webui_version(None)).body)


try:
    host.webui_dir, host.wcb_dir = (lambda: NC), (lambda: WZ)
    host._published_dtg, host._wcb_published_ver = (lambda: "NC-1"), (lambda: "WZ-1")
    s = status()
    check(s["wcb"]["missing"] == ["seq-builder.js"] and s["wcb"]["stale"] and s["wcb"]["bundled"] == "WZ-1"
          and s["missing"] == [] and not s["stale"],
          f"U5 webui-version: the Wizard without seq-builder.js is incomplete and stale at the published stamp; the "
          f"whole config tool is neither: {s}")

    proc.run = offline_fetch
    host._tools_repaired.clear()
    asyncio.run(host._repair_incomplete_tools())
    check(len(fetched) == 1 and not host._tools_repaired and status()["wcb"]["missing"] == ["seq-builder.js"],
          "U5 offline, the repair changes nothing and the copy still says incomplete")

    fetched.clear()
    proc.run = fake_fetch_whole
    asyncio.run(host._repair_incomplete_tools())
    argv = fetched[0] if fetched else []
    check(len(fetched) == 1 and argv[argv.index("--tool") + 1] == "wcb" and "--force" not in argv,
          f"U5 the startup repair fetches the incomplete tool alone (not --tool all, not --force): {fetched}")
    rep = host._tools_repaired.get("wcb") or {}
    s = status()
    check(rep.get("missing") == ["seq-builder.js"] and rep.get("version") == "WZ-1" and not s["wcb"]["missing"]
          and not s["wcb"]["stale"] and s["wcb"]["repaired"] == rep and s["repaired"] is None,
          f"U5 ...records the repair for the launcher, and the copy is whole and current: {s['wcb']}")
    fetched.clear()
    asyncio.run(host._repair_incomplete_tools())
    check(not fetched, "U5 a whole copy is not fetched again")
finally:
    proc.run = real_run
    for k, v in saved.items():
        setattr(host, k, v)
    host._tools_repaired.clear()
    shutil.rmtree(tmp5, ignore_errors=True)

# ── U6 ─────────────────────────────────────────────────────────────────────
# 2026-10-21, on a Google Fi hotspot: "Intellex 0.2.3 - HTTPError: HTTP Error 403: rate limit exceeded" on every check.
import http.server  # noqa: E402
import threading    # noqa: E402

import branchlist   # noqa: E402
import version      # noqa: E402


def pkt(s: bytes) -> bytes:
    return b"%04x" % (len(s) + 4) + s


adv = (pkt(b"# service=git-upload-pack\n") + b"0000" + pkt(b"a" * 40 + b" HEAD\0multi_ack symref=HEAD:refs/heads/main\n")
       + pkt(b"b" * 40 + b" refs/heads/main\n") + pkt(b"c" * 40 + b" refs/heads/seq-builder\n")
       + pkt(b"d" * 40 + b" refs/pull/7/head\n") + pkt(b"e" * 40 + b" refs/tags/v7.0.0\n") + b"0000")
check(branchlist._heads(adv) == ["main", "seq-builder"], f"U6 branches from a ref advertisement: {branchlist._heads(adv)}")

asked: list = []
site = {"latest": None, "sums": b""}


class GH(http.server.BaseHTTPRequestHandler):
    def _go(self, body_too):
        asked.append(self.path)
        body, code, where = b"", 404, None
        if self.path == "/greghulette/Intellex/releases/latest":
            code = 302
            where = f"/greghulette/Intellex/releases/tag/{site['latest']}" if site["latest"] else "/greghulette/Intellex/releases"
        elif self.path.endswith("/SHA256SUMS.txt") and site["sums"]:
            body, code = site["sums"], 200
        self.send_response(code)
        if where:
            self.send_header("Location", where)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if body_too:
            self.wfile.write(body)

    def do_GET(self):
        self._go(True)

    def do_HEAD(self):
        self._go(False)

    def log_message(self, *a):
        pass


srv = http.server.ThreadingHTTPServer(("127.0.0.1", 0), GH)
threading.Thread(target=srv.serve_forever, daemon=True).start()
tmp6 = pathlib.Path(tempfile.mkdtemp(prefix="intellex-smoke-rel-"))
saved6 = (selfupdate.WEB, selfupdate.mode, selfupdate._cache_file, version.VERSION, os.environ.get("INTELLEX_OFFLINE"))
try:
    selfupdate.WEB = f"http://127.0.0.1:{srv.server_address[1]}"
    selfupdate.mode = lambda: "windows"
    selfupdate._cache_file = lambda: tmp6 / "app_update.json"
    os.environ["INTELLEX_OFFLINE"] = ""
    version.VERSION = "0.2.3"
    r = selfupdate._check_release(True)
    check(r.get("reason") == "no Intellex release has been published yet", f"U6 no release: {r}")
    site.update(latest="v0.2.4", sums=b"0" * 64 + b"  Intellex-0.2.4-windows.exe\n" + b"1" * 64 + b"  Intellex-0.2.4-macos-arm64.zip\n")
    r = selfupdate._check_release(True)
    check(r.get("available") and r.get("asset") == "Intellex-0.2.4-windows.exe" and r.get("latest") == "0.2.4",
          f"U6 the newer release found from the redirect, its build for this computer from SHA256SUMS.txt: {r}")
    check(selfupdate._release_files("0.2.4", r.get("asset", "")) == {
        "Intellex-0.2.4-windows.exe": f"{selfupdate.WEB}/greghulette/Intellex/releases/download/v0.2.4/Intellex-0.2.4-windows.exe",
        "SHA256SUMS.txt": f"{selfupdate.WEB}/greghulette/Intellex/releases/download/v0.2.4/SHA256SUMS.txt"},
          "U6 the install downloads from github.com's release links")
    site["sums"] = b"0" * 64 + b"  Intellex-0.2.4-macos-arm64.zip\n"
    r = selfupdate._check_release(True)
    check(not r.get("available") and "no build for this computer" in r.get("reason", ""), f"U6 no build for it: {r}")
    site["sums"] = b""
    r = selfupdate._check_release(True)
    check(not r.get("available") and "SHA256SUMS.txt" in r.get("reason", ""), f"U6 no checksums: {r}")
    version.VERSION = "0.2.4"
    site["sums"] = b"0" * 64 + b"  Intellex-0.2.4-windows.exe\n"
    r = selfupdate._check_release(True)
    check(r.get("reason") == "up to date" and not r.get("available"), f"U6 the same version: {r}")
    import inspect  # noqa: E402
    srcs = inspect.getsource(selfupdate) + inspect.getsource(branchlist)
    check("api.github.com" not in srcs.replace("not the REST API", ""),
          "U6 neither selfupdate.py nor branchlist.py names api.github.com")
finally:
    selfupdate.WEB, selfupdate.mode, selfupdate._cache_file, version.VERSION, off6 = saved6
    if off6 is None:
        os.environ.pop("INTELLEX_OFFLINE", None)
    else:
        os.environ["INTELLEX_OFFLINE"] = off6
    srv.shutdown()
    shutil.rmtree(tmp6, ignore_errors=True)

# ── U7 ─────────────────────────────────────────────────────────────────────
# 2026-10-26, Greg: "It doesn't make sense to have to manually refresh the page if we update them." The host fetches a
# newer published tool by itself (host._auto_update_tools_once, every AUTO_TOOLS_S); a page learns of it from
# /_api/status `tools` and reloads when it has nothing to lose (tools/smoke_tools_reload.js).
tmp7 = pathlib.Path(tempfile.mkdtemp(prefix="intellex-smoke-auto-"))
NC7, WZ7 = tmp7 / "webui", tmp7 / "webui_wcb"
NC7.mkdir()
(NC7 / "index.html").write_bytes(b'<span id="footer-dtg">NC-1</span>')
(WZ7 / "Wizard").mkdir(parents=True)
(WZ7 / "Wizard" / "app.js").write_bytes(b"const UI_VERSION = 'WZ-1';")
pub7 = {"navicore": "NC-1", "wcb": "WZ-1"}
saved7 = {k: getattr(host, k) for k in ("webui_dir", "wcb_dir", "_published_dtg", "_wcb_published_ver")}
fetched7: list = []
land7 = {"ok": True}


def fake_fetch_stamp(argv, **kw):
    """The fetcher: each tool named gets a new directory, at the published stamp, swapped in by rename."""
    fetched7.append(argv[argv.index("--tool") + 1])
    if not land7["ok"]:
        return subprocess.CompletedProcess(argv, 1, "github.io is not reachable - the bundled tools are unchanged.\n", "")
    tool = fetched7[-1]
    for p, d, f, old in (("navicore", NC7, "index.html", 'footer-dtg">'), ("wcb", WZ7, "Wizard/app.js", "UI_VERSION = '")):
        if tool not in (p, "all"):
            continue
        new = d.with_name(d.name + ".new")
        shutil.copytree(d, new)
        text = (new / f).read_text()
        cur = text.split(old, 1)[1][:4]
        (new / f).write_text(text.replace(old + cur, old + pub7[p]))
        prev = d.with_name(d.name + ".prev")
        shutil.rmtree(prev, ignore_errors=True)
        d.rename(prev)
        new.rename(d)
    return subprocess.CompletedProcess(argv, 0, f"[{tool}] updated\n", "")


def status7() -> dict:
    return json.loads(asyncio.run(host.api_status(None)).body)


try:
    host.webui_dir, host.wcb_dir = (lambda: NC7), (lambda: WZ7)
    host._published_dtg, host._wcb_published_ver = (lambda: pub7["navicore"]), (lambda: pub7["wcb"])
    proc.run = fake_fetch_stamp
    host._tools_auto.clear()
    st0 = status7()
    tok = st0.get("tools") or {}
    check(set(tok) == {"navicore", "wcb"} and all(len(v) == 12 for v in tok.values()) and st0.get("flashing") is False,
          f"U7 /_api/status names each tool's copy (a 12-character token) and that no flash runs: {tok}")
    check(asyncio.run(host._auto_update_tools_once()) == [] and not fetched7,
          "U7 every stamp the published one: nothing is fetched")
    pub7.update(navicore="", wcb="")
    check(asyncio.run(host._auto_update_tools_once()) == [] and not fetched7,
          "U7 nothing published could be read (offline): nothing is fetched")
    pub7.update(navicore="NC-2", wcb="WZ-1")
    got = asyncio.run(host._auto_update_tools_once())
    st1 = status7()
    check(got == ["navicore"] and fetched7 == ["navicore"] and host._bundled_dtg() == "NC-2",
          f"U7 a newer config tool alone is fetched alone: {got} {fetched7} {host._bundled_dtg()}")
    check(st1["tools"]["navicore"] != tok["navicore"] and st1["tools"]["wcb"] == tok["wcb"],
          f"U7 ...its token moves and the Wizard's does not: {tok} -> {st1['tools']}")
    wv = json.loads(asyncio.run(host.api_webui_version(None)).body)
    check((wv.get("autoUpdated") or {}).get("version") == "NC-2" and (wv.get("wcb") or {}).get("autoUpdated") is None,
          f"U7 ...and the launcher is told what updated itself: {wv.get('autoUpdated')}")
    fetched7.clear()
    pub7.update(navicore="NC-3", wcb="WZ-2")
    got = asyncio.run(host._auto_update_tools_once())
    check(sorted(got) == ["navicore", "wcb"] and fetched7 == ["all"] and host._wcb_bundled_ver() == "WZ-2",
          f"U7 both newer: one fetch of --tool all: {got} {fetched7}")
    fetched7.clear()
    pub7.update(navicore="NC-4")
    land7["ok"] = False
    before7 = dict(host._tools_auto)
    got = asyncio.run(host._auto_update_tools_once())
    check(got == [] and fetched7 == ["navicore"] and host._bundled_dtg() == "NC-3" and host._tools_auto == before7,
          f"U7 a fetch that does not land changes nothing: {got} {host._bundled_dtg()}")
    with host._flash_lock:
        host._flash_state["running"] = True
    check(status7().get("flashing") is True, "U7 /_api/status says a flash runs")
    with host._flash_lock:
        host._flash_state["running"] = False
    old7 = os.environ.get("INTELLEX_AUTO_TOOLS")
    os.environ["INTELLEX_AUTO_TOOLS"] = "0"
    off = host._auto_tools_enabled()
    os.environ.pop("INTELLEX_AUTO_TOOLS", None)
    on = host._auto_tools_enabled()
    if old7 is not None:
        os.environ["INTELLEX_AUTO_TOOLS"] = old7
    check(off is False and on is True, "U7 INTELLEX_AUTO_TOOLS=0 turns it off; unset it is on")
finally:
    proc.run = real_run
    for k, v in saved7.items():
        setattr(host, k, v)
    host._tools_auto.clear()
    shutil.rmtree(tmp7, ignore_errors=True)

print()
print("PASSED" if not failures else f"FAILED: {len(failures)} check(s)")
sys.exit(1 if failures else 0)
