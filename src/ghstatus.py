"""Why a firmware download from GitHub failed, in one sentence a user can act on.

WHY THIS EXISTS. Every image Intellex flashes comes from GitHub -- the file list from
api.github.com, the bytes from raw.githubusercontent.com -- and GitHub does go down,
does throttle, and is out of reach on a droid's AP. Each of those used to surface as
"cannot reach GitHub: <url> -- HTTP Error 503" or a page reading "HTTP 502", which
says nothing about whose problem it is. Users took it for a fault in the tools.

THE SAME RULES AS THE BROWSER TOOLS. The WCB Wizard and the NaviCore config tool
carry the same classifier in their flasher.js (the "gh-explain" block); this is its
port, and the wording matches:

  1. no internet  neither GitHub nor GitHub's status page answers
  2. rate limit   403/429 with x-ratelimit-remaining: 0, or GitHub's rate-limit message
  3. GitHub down  githubstatus.com reports Git Operations, API Requests or Pages as
                  anything but operational
  4. GitHub error a 5xx (or no answer) while the status page says all is well
  5. not found    a 404 is ours, not GitHub's (the branch has no such image):
                  explain() returns None and the caller keeps its own message

Plus one the browser has no counterpart for: INTELLEX_OFFLINE (a test host's leash,
CLAUDE.md "A test host needs a leash") says so instead of fetching the status page.

ONLY AFTER A FAILURE. Nothing here runs on a download that worked. The status page is
asked at most once a minute (STATUS_TTL_S), and never for longer than
STATUS_TIMEOUT_S of wall clock: on a droid's AP a request does not fail, it hangs
(flash.reachable's docstring), so the fetch runs on a daemon thread that is
abandoned when the budget is up. A status page that cannot be read falls back to
rule 1 or 4.

THE HOST DOES THIS, NOT THE PAGE. Under Intellex the pages' GitHub calls arrive at
the host (src/ghproxy.py), and the native flashers run here too. So the host
classifies -- with the TLS context and leash every other fetch here uses -- and the
page is handed the sentence (ghproxy's `explain`), which its flasher.js shows as is.
"""
from __future__ import annotations

import json
import os
import re
import socket
import threading
import time
import urllib.error
import urllib.request

import certs

STATUS_URL = "https://www.githubstatus.com/api/v2/components.json"
STATUS_TIMEOUT_S = 4.0
STATUS_TTL_S = 60.0
# The parts of GitHub a firmware download goes through, by the names githubstatus.com
# gives them (matched without regard to case). The page's overall indicator is NOT
# used: it goes amber for Copilot or Actions, which a download never touches, and
# would blame GitHub for a failure it had nothing to do with.
COMPONENTS = ("Git Operations", "API Requests", "Pages")

OFFLINE = "Can't reach the internet - check this computer's connection."
OFFLINE_MODE = ("Intellex is in offline mode (INTELLEX_OFFLINE is set), so it did not "
                "try GitHub.")
# What the Intellex callers add: they only explain after the firmware cache missed.
NO_COPY = "Intellex has no saved copy of this firmware to use instead."

_status_cache: "tuple[float, list[str]] | None" = None    # (monotonic at, trouble)
_status_lock = threading.Lock()


def rate_limited(reset_epoch: float = 0.0) -> str:
    when = (f"It resets at {time.strftime('%H:%M', time.localtime(reset_epoch))}."
            if reset_epoch and reset_epoch > 0 else "It resets within the hour.")
    return ("GitHub's download limit for this network is used up (60 requests an hour "
            f"without signing in). {when} This is GitHub's limit, not a fault in the WCB tools.")


def trouble(items: "list[str]") -> str:
    return (f"GitHub is having problems right now ({'; '.join(items)}). The firmware "
            "download depends on GitHub, so this isn't something on your side or in the "
            "WCB tools - try again once GitHub recovers. Live status: githubstatus.com")


def server_error(code) -> str:
    return (f"GitHub's servers answered with an error ({code}). That's usually a "
            "short-lived problem on GitHub's side - try again in a few minutes. If it "
            "keeps happening, check githubstatus.com.")


def offline_mode() -> bool:
    """The test-host leash flash.reachable() honours. Read here rather than imported
    from flash.py, which imports this module."""
    return os.environ.get("INTELLEX_OFFLINE", "") not in ("", "0")


def _chain(exc: "BaseException | None"):
    """exc, then what it was raised from, a bounded way down."""
    seen = 0
    while exc is not None and seen < 6:
        yield exc
        exc = exc.__cause__ or exc.__context__
        seen += 1


def failure(exc: "BaseException | None") -> "dict | None":
    """What kind of GitHub failure this was, or None when it is not one.

    {"kind": "unreachable"}                    flash.reachable() said no; nothing was sent
    {"kind": "http", "code", "headers", "body"} GitHub (or something in the way) answered
    {"kind": "timeout"} / {"kind": "network"}  no answer, or no connection

    flash._get raises FlashError FROM the urllib error, so the HTTP code and headers
    are on the cause. A FlashError with no cause that still names "HTTP Error <code>"
    (a stand-in, an older raise) is read by its text. A certificate failure is not a
    GitHub problem (certs.ADVICE says what it is), so it is None.
    """
    for e in _chain(exc):
        if getattr(e, "unreachable", False):
            return {"kind": "unreachable"}
        if certs.is_cert_error(e):
            return None
        if isinstance(e, urllib.error.HTTPError):
            return {"kind": "http", "code": e.code, "headers": e.headers or {},
                    "body": getattr(e, "ix_body", b"") or b""}
        reason = getattr(e, "reason", e) if isinstance(e, urllib.error.URLError) else e
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return {"kind": "timeout"}
        if isinstance(e, OSError):          # URLError, a refused or reset connection, DNS
            return {"kind": "network"}
    m = re.search(r"HTTP Error (\d{3})", str(exc or ""))
    if m:
        return {"kind": "http", "code": int(m.group(1)), "headers": {}, "body": b""}
    return None


def _header(headers, name: str) -> str:
    try:
        v = headers.get(name)
    except AttributeError:
        v = None
    return str(v).strip() if v is not None else ""


def _fetch_status() -> "list[str] | None":
    """The download-related components that are not operational, worded; [] when all
    of them are fine; None when the status page could not be read in time."""
    box: dict = {}

    def run() -> None:
        try:
            req = urllib.request.Request(STATUS_URL, headers={
                "User-Agent": "Intellex", "Accept": "application/json"})
            with urllib.request.urlopen(req, timeout=STATUS_TIMEOUT_S,
                                        context=certs.context()) as r:
                box["json"] = json.loads(r.read(2_000_000).decode("utf-8"))
        except Exception as e:                               # noqa: BLE001
            box["err"] = e

    t = threading.Thread(target=run, daemon=True, name="ghstatus")
    t.start()
    t.join(STATUS_TIMEOUT_S + 0.5)
    j = box.get("json")
    comps = j.get("components") if isinstance(j, dict) else None
    if not isinstance(comps, list):
        return None
    want = {c.lower() for c in COMPONENTS}
    out = []
    for c in comps:
        if not isinstance(c, dict):
            continue
        name, status = c.get("name"), c.get("status")
        if isinstance(name, str) and name.strip().lower() in want \
                and isinstance(status, str) and status != "operational":
            out.append(f"{name.strip()}: {status.replace('_', ' ')}")
    return out


def status_trouble() -> "list[str] | None":
    """_fetch_status, remembered for STATUS_TTL_S when it answered."""
    global _status_cache
    with _status_lock:
        if _status_cache and time.monotonic() - _status_cache[0] < STATUS_TTL_S:
            return list(_status_cache[1])
    got = _fetch_status()
    if got is not None:
        with _status_lock:
            _status_cache = (time.monotonic(), got)
    return got


def explain(exc: "BaseException | None") -> "str | None":
    """One sentence about a failed GitHub fetch, or None when it is not GitHub's to
    explain (a 404, a certificate store, anything not from a GitHub request)."""
    f = failure(exc)
    if f is None:
        return None
    if f["kind"] in ("unreachable", "network", "timeout") and offline_mode():
        return OFFLINE_MODE
    code = f.get("code")
    if f["kind"] == "http":
        if code == 404:
            return None
        if code in (403, 429):
            body = f.get("body") or b""
            if isinstance(body, bytes):
                body = body.decode("utf-8", "replace")
            if _header(f["headers"], "x-ratelimit-remaining") == "0" or \
                    re.search(r"rate limit", body, re.I):
                reset = 0.0
                try:
                    reset = float(_header(f["headers"], "x-ratelimit-reset") or 0)
                except ValueError:
                    reset = 0.0
                if not reset:
                    try:
                        reset = time.time() + float(_header(f["headers"], "retry-after"))
                    except ValueError:
                        reset = 0.0
                return rate_limited(reset)
    # Respect the leash: an offline-mode host fetches nothing, the status page included.
    found = None if offline_mode() else status_trouble()
    if found:
        return trouble(found)
    if f["kind"] == "http":
        return server_error(code)
    if found is None:
        return OFFLINE                       # neither GitHub nor its status page answered
    return server_error("timeout" if f["kind"] == "timeout" else "no response")
