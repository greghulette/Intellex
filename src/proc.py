"""Spawn a child process without flashing a console window at the user.

WHY THIS EXISTS
The app is windowed: Intellex.bat launches with pythonw and the frozen build is
built console=False. A GUI process on Windows has no console, so when it spawns a
console program -- netsh, esptool, git, or this executable re-invoking itself --
Windows CREATES ONE, and the user sees a black window flash open and shut.

That is not merely ugly. The reconnect loop bounces the WLAN adapter on a timer,
which is three netsh calls, so a droid that has gone away produces a console flash
every few seconds indefinitely. It looks exactly like malware, and it gives no clue
which program is doing it.

CREATE_NO_WINDOW suppresses that. It is Windows-only and must never be passed on
another platform, where it is not a valid flag at all -- hence one helper rather
than the flag scattered through five modules, each of which would have to remember
the guard.
"""
from __future__ import annotations

import os
import subprocess
import sys

# Set by fresh_app_env() in the copy it starts and by nothing else, so app.py can tell a clean
# restart from one an older Intellex started. Its value says which (app.py logs "healed").
FRESH_START = "INTELLEX_FRESH_START"

# 0x08000000. Named here rather than reaching for subprocess.CREATE_NO_WINDOW,
# which only exists on Windows -- referencing it on macOS is an AttributeError at
# import time, and this module is imported unconditionally.
_CREATE_NO_WINDOW = 0x08000000


def hidden() -> dict:
    """kwargs that keep a child process from opening a console. Empty off Windows."""
    if sys.platform != "win32":
        return {}
    return {"creationflags": _CREATE_NO_WINDOW}


def run(args, **kw):
    """subprocess.run, with no console window."""
    return subprocess.run(args, **{**hidden(), **kw})


def popen(args, **kw):
    """subprocess.Popen, with no console window."""
    return subprocess.Popen(args, **{**hidden(), **kw})


def fresh_app_env(why: str = "restart") -> dict:
    """The environment to start THIS frozen app in as a new copy of its own: the restart after an update.

    The one-file Windows build runs from a folder its bootloader unpacks into %TEMP%\\_MEIxxxxx, and the
    bootloader exports _PYI_* variables naming that folder. A process that inherits them is taken for a
    helper of the running copy and shares its folder - right for --run-esptool and the fetchers, which end
    before the app does, and wrong for a restart, which outlives it. If the old copy is still ending, the
    new one runs from the old one's folder, and the old one's bootloader deletes that folder as it exits:
    the window's WebView2 DLL and the launcher page went with it (0.2.0 -> 0.2.1 on 2026-10-20: a browser
    instead of a window, and a 404 on /_launcher). If the old copy has already gone, the bootloader
    refuses with "Security validation failure: failed to obtain executable path for parent proces!".

    PYINSTALLER_RESET_ENVIRONMENT=1 (PyInstaller 6.9 on) makes the new copy unpack a folder of its own.
    The bootloader removes the variable as it reads it, so the new copy's own helpers still share ITS
    folder. The _PYI_* variables are dropped as well, for an older bootloader."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("_PYI_") and k != "_MEIPASS2"}
    env["PYINSTALLER_RESET_ENVIRONMENT"] = "1"
    env[FRESH_START] = why
    return env
