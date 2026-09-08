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

import subprocess
import sys

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
