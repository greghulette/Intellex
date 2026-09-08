"""Give Intellex its own face in the Dock and on the taskbar.

Without this the app borrows the interpreter's identity: a generic Python rocket
in the macOS Dock, and on Windows a window grouped under "Python" wearing python's
icon. Neither says what the thing is, and on Windows the grouping is the worse
half -- two unrelated Python programs land in the same taskbar button.

NEITHER PLATFORM TAKES THE SAME PATH, and pywebview cannot do it for us: its
`icon=` argument to start() is GTK/Qt only, which is exactly the two backends this
app never uses. So:

  macOS   NSApplication.setApplicationIconImage_ with the 512px PNG. Applies to the
          running process, so it needs no .app bundle -- which matters, because
          packaging is not written yet and a Dock icon should not have to wait for
          it. Set BEFORE the GUI loop starts: sharedApplication() creates the app
          object early and the icon survives into the loop.

  Windows Two separate things, both required, in this order:
          1. SetCurrentProcessExplicitAppUserModelID BEFORE the window exists.
             This is what stops the taskbar filing Intellex under "Python", and it
             is ignored if the window is already up.
          2. WM_SETICON on the window once it exists, which needs a real .ico --
             LoadImage cannot read a PNG. src/assets/navicore-icon.ico carries
             seven sizes so Windows picks rather than smears.

NOTHING HERE IS ALLOWED TO BREAK THE APP. An icon is decoration; every failure is
caught and reported as a line of text, because losing the window over a missing
picture would be an absurd trade.
"""
from __future__ import annotations

import pathlib
import sys

# COMMITTED, unlike src/webui/. That is not a contradiction of "the UI is NOT
# forked": the config tool is NaviCore's and must never diverge, whereas an app
# icon is Intellex's own chrome and has to exist before any network does -- the
# Dock icon is wanted at startup, offline, on a first run. Copied from
# NaviCore/assets-navicore/navicore-icon.svg, and BOTH raster files are generated
# from that .svg by tools/make_icon.py (7 sizes, 16..256, PNG-compressed entries).
#
# They are NOT a plain rasterisation of the drawing: the generator drops the side
# ticks and closes the frame onto the hexagon, because at taskbar sizes those ticks
# cost the logo a quarter of its width while rendering as specks. Read that script's
# header before regenerating either file by any other means.
ASSETS = pathlib.Path(__file__).resolve().parent / "assets"
ICON_PNG = ASSETS / "navicore-icon-512.png"
ICON_ICO = ASSETS / "navicore-icon.ico"

# Reverse-DNS-ish, stable, and NOT the executable name: Windows keys taskbar
# pinning and grouping to this string, so changing it later orphans anyone's
# pinned shortcut.
APP_ID = "NaviCore.Intellex"


def prepare() -> str:
    """Call BEFORE the window is created. Returns a line worth printing."""
    try:
        if sys.platform == "darwin":
            return _mac_dock_icon()
        if sys.platform == "win32":
            return _win_app_id()
        return f"no icon support for {sys.platform}"
    except Exception as e:                      # noqa: BLE001 - never fatal
        return f"icon: {type(e).__name__}: {e}"


def attach(_window=None) -> str:
    """Call once the window exists.

    macOS re-applies rather than checking. Reading applicationIconImage() back
    cannot tell "ours" from the default rocket -- both are a valid NSImage -- so a
    check would be guesswork, while setting it again is idempotent, costs a file
    read, and wins outright if a future pywebview sets its own inside start().
    """
    try:
        if sys.platform == "win32":
            return _win_window_icon()
        if sys.platform == "darwin":
            _mac_dock_icon()
            return ""
        return ""
    except Exception as e:                      # noqa: BLE001 - never fatal
        return f"icon: {type(e).__name__}: {e}"


def _mac_dock_icon() -> str:
    if not ICON_PNG.is_file():
        return f"icon: missing {ICON_PNG}"
    from AppKit import NSApplication, NSImage    # pyobjc, already required on macOS
    img = NSImage.alloc().initWithContentsOfFile_(str(ICON_PNG))
    if img is None:
        return f"icon: could not decode {ICON_PNG.name}"
    NSApplication.sharedApplication().setApplicationIconImage_(img)
    return f"icon      Dock icon set from {ICON_PNG.name}"


def _win_app_id() -> str:
    import ctypes
    # Fails on nothing modern, but it is a shell call and this must never be fatal.
    hr = ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(APP_ID)
    if hr != 0:                                 # S_OK
        return f"icon: SetCurrentProcessExplicitAppUserModelID returned {hr:#010x}"
    return f"icon      taskbar identity set to {APP_ID}"


def _win_window_icon(attempts: int = 12, gap: float = 0.25) -> str:
    """WM_SETICON on every top-level window this process owns.

    BY HANDLE, NOT BY TITLE: the config tool rewrites the window title, and
    FindWindow against a title that has already changed silently sets nothing.

    ARGTYPES ARE NOT OPTIONAL HERE. Left to its defaults ctypes gives a foreign
    function a c_int return, so on 64-bit Windows the HICON from LoadImage is
    TRUNCATED to 32 bits -- and the failure is a wrong-or-missing icon with no
    error anywhere, which is the worst way for this to go wrong. Same for the
    HICON travelling as an LPARAM into SendMessage.

    RETRIED, because pywebview hands control back on a worker thread as the GUI
    comes up and the window is not reliably visible at that instant.
    """
    import ctypes
    import time
    from ctypes import wintypes

    if not ICON_ICO.is_file():
        return f"icon: missing {ICON_ICO}"

    user32 = ctypes.windll.user32
    IMAGE_ICON, LR_LOADFROMFILE = 1, 0x0010
    WM_SETICON, ICON_SMALL, ICON_BIG = 0x0080, 0, 1
    SM_CXICON, SM_CXSMICON = 11, 49

    user32.LoadImageW.restype  = wintypes.HANDLE
    user32.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                  ctypes.c_int, ctypes.c_int, wintypes.UINT]
    user32.SendMessageW.restype  = ctypes.c_void_p
    user32.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT,
                                    wintypes.WPARAM, wintypes.LPARAM]
    user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                                ctypes.POINTER(wintypes.DWORD)]
    user32.IsWindowVisible.argtypes = [wintypes.HWND]
    user32.GetSystemMetrics.argtypes = [ctypes.c_int]

    def load(px: int):
        return user32.LoadImageW(None, str(ICON_ICO), IMAGE_ICON, px, px,
                                 LR_LOADFROMFILE)

    # Ask for the exact pixel sizes Windows wants, so it picks the right entry out
    # of the seven in the file rather than rescaling one of them. Small drives the
    # title bar and alt-tab; big drives the taskbar button.
    small = load(user32.GetSystemMetrics(SM_CXSMICON) or 16)
    big   = load(user32.GetSystemMetrics(SM_CXICON) or 32)
    if not small and not big:
        err = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
        return f"icon: LoadImage could not read {ICON_ICO.name} (GetLastError={err})"

    ENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    user32.EnumWindows.argtypes = [ENUMPROC, wintypes.LPARAM]
    user32.EnumWindows.restype  = wintypes.BOOL

    pid = ctypes.windll.kernel32.GetCurrentProcessId()

    for attempt in range(attempts):
        hit = 0

        def each(hwnd, _lparam):
            nonlocal hit
            owner = wintypes.DWORD()
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(owner))
            if owner.value == pid and user32.IsWindowVisible(hwnd):
                if small:
                    user32.SendMessageW(hwnd, WM_SETICON, ICON_SMALL, small)
                if big:
                    user32.SendMessageW(hwnd, WM_SETICON, ICON_BIG, big)
                hit += 1
            return True

        # The callback must stay referenced for the whole call, hence the local.
        cb = ENUMPROC(each)
        user32.EnumWindows(cb, 0)
        if hit:
            return f"icon      window icon set ({hit} window{'s' if hit != 1 else ''})"
        time.sleep(gap)

    return ("icon: no visible window of ours appeared within "
            f"{attempts * gap:.0f}s -- taskbar icon not set")
