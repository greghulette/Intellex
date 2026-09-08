"""Fit the app window to the screen it is actually opening on.

THE BUG THIS EXISTS FOR
The window asked for a fixed 1280x880 and pywebview was given no position, so on
a 1920x1080 screen at 125% scaling the bottom of the app sat under the taskbar
and could not be reached. Two separate causes, and fixing either one alone leaves
the window still cut off:

  1. SIZE. pywebview's width/height are LOGICAL pixels and its Windows backend
     multiplies them by the monitor's scale (winforms.py: `Size(initial_width *
     scale, ...)`). 880 logical at 125% is 1100 physical against a work area of
     1020 -- so the request was 80px taller than the screen had room for before
     the title bar was even counted.

  2. POSITION. With no x/y pywebview falls back to WinForms
     FormStartPosition.CenterScreen, which centres on the monitor's FULL BOUNDS
     and knows nothing about the taskbar. Even a window clamped to exactly the
     work area's height comes out centred in 1080 rather than in 1020, putting
     its bottom 30 physical px behind the taskbar.

MEASURING DPI BEFORE THE WINDOW EXISTS
This runs before webview.start(), and pywebview does not call SetProcessDPIAware
until inside it -- so the process is normally DPI-UNAWARE here, and every metric
Windows reports is pre-divided by the scale factor. A frozen build could also
arrive here already aware, via a manifest.

Rather than guess which, the work area is normalised by GetDpiForSystem(), which
returns a flat 96 for an unaware process and the true DPI for an aware one. That
makes `physical * 96 / GetDpiForSystem()` land on the same LOGICAL number in
both states -- measured on a 1920x1080 at 125%: unaware reports 1536x816 with DPI
96, aware reports 1920x1020 with DPI 120, and both normalise to 816. Logical is
also exactly the unit pywebview wants back, so nothing has to be converted twice.

NOTHING HERE IS ALLOWED TO BREAK THE APP, and it must NOT change process state.
Calling SetProcessDpiAwareness to make measuring easier would silently alter how
WinForms renders every window for the rest of the run. Every failure falls back
to the caller's preferred size, which is exactly the old behaviour.
"""
from __future__ import annotations

import sys

# Leave the work area's last few pixels alone rather than filling it exactly. A
# window flush to the taskbar reads as a broken maximise, and on Windows the
# resize grip on the bottom edge becomes impossible to grab.
MARGIN = 24


def fit(want_w: int, want_h: int, min_size: tuple[int, int]) -> dict:
    """Keyword arguments for webview.create_window, in LOGICAL pixels.

    Returns `width`/`height` always, plus `x`/`y` when the screen could be
    measured. Never raises: an unmeasurable screen yields the requested size and
    no position, which is what the app did before this existed.
    """
    try:
        if sys.platform == "win32":
            area = _win_work_area()
        elif sys.platform == "darwin":
            area = _mac_visible_frame()
        else:
            area = None
    except Exception:                           # noqa: BLE001 - never fatal
        area = None

    if area is None:
        return {"width": want_w, "height": want_h, "min_size": min_size}

    x0, y0, aw, ah = area
    w = max(320, min(want_w, aw - MARGIN))
    h = max(240, min(want_h, ah - MARGIN))

    # MinimumSize is applied to the form too, and WinForms grows a window back up
    # to it -- so a min_size taller than the screen would undo the clamp above and
    # put the bottom right back under the taskbar.
    mw, mh = min(min_size[0], w), min(min_size[1], h)

    return {
        "width": w,
        "height": h,
        "x": x0 + (aw - w) // 2,
        "y": y0 + (ah - h) // 2,
        "min_size": (mw, mh),
    }


def _win_work_area() -> tuple[int, int, int, int] | None:
    """The primary monitor's work area (taskbar excluded), in logical pixels."""
    import ctypes

    user32 = ctypes.windll.user32

    class RECT(ctypes.Structure):
        _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                    ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

    r = RECT()
    SPI_GETWORKAREA = 0x0030
    if not user32.SystemParametersInfoW(SPI_GETWORKAREA, 0, ctypes.byref(r), 0):
        return None

    # 96 on an unaware process, the real DPI on an aware one -- see the header.
    # Missing before Windows 10 1607, where 96 is the only answer anyway.
    try:
        dpi = user32.GetDpiForSystem() or 96
    except AttributeError:
        dpi = 96

    scale = dpi / 96.0
    return (int(r.left / scale), int(r.top / scale),
            int((r.right - r.left) / scale), int((r.bottom - r.top) / scale))


def _mac_visible_frame() -> tuple[int, int, int, int] | None:
    """The main screen minus the menu bar and Dock, in points.

    Cocoa points ARE pywebview's logical pixels, so no scaling is needed. The
    origin is flipped: AppKit measures y from the BOTTOM of the screen and
    pywebview positions from the top, so visibleFrame's y is re-expressed
    against the full frame's height.
    """
    from AppKit import NSScreen                 # pyobjc, already required on macOS

    screen = NSScreen.mainScreen()
    if screen is None:
        return None
    vis, full = screen.visibleFrame(), screen.frame()
    top = full.size.height - (vis.origin.y + vis.size.height)
    return (int(vis.origin.x), int(top), int(vis.size.width), int(vis.size.height))
