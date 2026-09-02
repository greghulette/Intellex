#!/usr/bin/env python3
"""Rebuild the app icon files from src/assets/navicore-icon.svg.

    python tools/make_icon.py

Writes src/assets/navicore-icon.ico (Windows, 7 sizes) and
src/assets/navicore-icon-512.png (the macOS Dock). Only needed when the art
changes; both outputs are committed, because Windows needs the .ico at startup
and a build step nobody remembers is a build step that rots.

WHY THE SVG AND NOT THE 512px PNG. The PNG is one fixed rasterisation of this
same drawing, so every size cut from it is a resample of a resample. From the
vector we rasterise once at 1024 with the crop already applied, and each icon
size is a single clean downsample. It also lets us drop elements, which is the
whole trick below.

WHY A .ico AT ALL. LoadImage cannot read a PNG, so the Windows taskbar path in
src/appicon.py needs a real icon file. macOS takes the PNG directly.

WHY PNG-COMPRESSED ENTRIES. An .ico may hold either BMP/DIB or PNG data per size.
PNG keeps the file small and is understood from Windows Vista on -- and this app
needs WebView2, which is Windows 10+, so there is no older Windows to be kind to.

Seven sizes rather than one, so Windows PICKS instead of smearing: a 256px icon
squeezed down to 16px for the title bar looks like mud.

NEEDS PILLOW AND A CHROMIUM BROWSER (Chrome or Edge, already on both machines) to
rasterise the SVG. Both are dev dependencies of this tool alone -- the app itself
imports nothing and shells out to nothing to show its icon, it just reads the
committed files.
"""
from __future__ import annotations

import io
import pathlib
import re
import shutil
import struct
import subprocess
import sys
import tempfile

try:
    from PIL import Image
except ImportError:
    print("this tool needs Pillow:  pip install pillow")
    raise SystemExit(1)

SIZES  = [16, 24, 32, 48, 64, 128, 256]
HERE   = pathlib.Path(__file__).resolve().parent.parent
ASSETS = HERE / "src" / "assets"
SRC    = ASSETS / "navicore-icon.svg"
ICO    = ASSETS / "navicore-icon.ico"
PNG    = ASSETS / "navicore-icon-512.png"

# Rasterise big, then downsample. Even the 256px entry is a 4x reduction, which
# is where the anti-aliasing on the thin steel strokes comes from.
RENDER = 1024

# CROP TO THE HEXAGON, AND DROP THE SIDE TICKS. This is the reason the script
# exists rather than a one-line convert.
#
# The drawing is 200x200, but the hexagon only spans x 22.4..177.6 -- 77% of the
# width. The rest is the little connector stubs and end dots on either side. At
# 24-32px, which is every size that matters on a taskbar, those ticks rasterise
# to faint specks: they contribute nothing legible while costing the hexagon a
# quarter of its width, so the logo reads as undersized next to its neighbours.
#
# Dropping them lets the frame close on the hexagon itself. Vertically it spans
# y 10.5..189.5 (the outer polygon's 11..189 plus its 1px stroke), so a square
# viewBox of side 179 centred on 100,100 fits it exactly: the hexagon then fills
# 100% of the height and 87% of the width, against 77% x 90% before.
#
# 179 is the floor, not a preference -- the hexagon is taller than it is wide, so
# a smaller box would clip the top and bottom points.
VIEWBOX = "10.5 10.5 179 179"

# Matched on the exact opening tags, so a change to the art fails here loudly
# rather than silently shipping an icon with half a tick hanging off the edge.
TICK_GROUPS = [
    '<g fill="none" stroke="url(#steelGrad)" stroke-width="2.2" stroke-linecap="round">',
    '<g fill="#0d1117" stroke="url(#steelGrad)" stroke-width="2">',
]

# Chrome renders the SVG's gradients and <use> exactly as authored -- it is a
# browser drawing, and the browser is the reference implementation. Order matters
# only in that the first hit wins.
BROWSERS = {
    "win32": [
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
    ],
    "darwin": [
        "/Applications/Google Chrome.app/Contents/MacOS/Google Chrome",
        "/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge",
        "/Applications/Chromium.app/Contents/MacOS/Chromium",
    ],
}


def find_browser() -> str | None:
    for p in BROWSERS.get(sys.platform, []):
        if pathlib.Path(p).is_file():
            return p
    for name in ("chrome", "chromium", "google-chrome", "msedge"):
        found = shutil.which(name)
        if found:
            return found
    return None


def cropped_svg() -> str:
    """The source drawing with the ticks removed and the frame closed in."""
    s = SRC.read_text(encoding="utf-8")
    for open_tag in TICK_GROUPS:
        if open_tag not in s:
            raise SystemExit(
                f"could not find this group in {SRC.name}, so the crop below is no\n"
                f"longer safe -- re-check which elements sit outside the hexagon:\n"
                f"  {open_tag}")
        i = s.index(open_tag)
        s = s[:i] + s[s.index("</g>", i) + 4:]
    s = re.sub(r'viewBox="[^"]*"', f'viewBox="{VIEWBOX}"', s, count=1)
    s = re.sub(r'width="\d+" height="\d+"',
               f'width="{RENDER}" height="{RENDER}"', s, count=1)
    return s


def render(browser: str) -> Image.Image:
    with tempfile.TemporaryDirectory(prefix="navilink-icon-") as td:
        tmp = pathlib.Path(td)
        page = tmp / "icon.html"
        # margin:0 or the screenshot inherits the body's default 8px inset, which
        # would silently shrink the art by 1.5% and off-centre it.
        page.write_text("<style>html,body{margin:0;background:transparent}</style>"
                        + cropped_svg(), encoding="utf-8")
        out = tmp / "icon.png"
        r = subprocess.run(
            [browser, "--headless=new", "--disable-gpu", "--hide-scrollbars",
             # Without this the page composites onto opaque white and the icon
             # ships with a white box behind it.
             "--default-background-color=00000000",
             "--virtual-time-budget=2000",
             f"--screenshot={out}", f"--window-size={RENDER},{RENDER}",
             page.as_uri()],
            capture_output=True, text=True, timeout=120)
        if not out.is_file():
            raise SystemExit(f"{pathlib.Path(browser).name} rendered nothing:\n"
                             f"{(r.stderr or r.stdout).strip()[:600]}")
        img = Image.open(out)
        img.load()                      # before the temp dir goes away
        return img.convert("RGBA")


def write_ico(art: Image.Image) -> None:
    entries, blobs = bytearray(), bytearray()
    offset = 6 + 16 * len(SIZES)
    for s in SIZES:
        buf = io.BytesIO()
        art.resize((s, s), Image.LANCZOS).save(buf, format="PNG", optimize=True)
        data = buf.getvalue()
        # A width/height byte of 0 means 256 -- the field is one byte wide.
        w = h = 0 if s >= 256 else s
        entries += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(data), offset)
        blobs   += data
        offset  += len(data)
    ICO.write_bytes(struct.pack("<HHH", 0, 1, len(SIZES)) + entries + blobs)


def main() -> int:
    if not SRC.is_file():
        print(f"no such file: {SRC}")
        return 1
    browser = find_browser()
    if not browser:
        print("need Chrome or Edge to rasterise the SVG; none found in the usual\n"
              "places or on PATH. Install one, or point BROWSERS at it.")
        return 1

    art = render(browser)
    write_ico(art)
    art.resize((512, 512), Image.LANCZOS).save(PNG, format="PNG", optimize=True)

    print(f"rendered with {pathlib.Path(browser).name} at {RENDER}px, viewBox {VIEWBOX}")
    print(f"wrote {ICO.relative_to(HERE)} ({ICO.stat().st_size:,} bytes, {len(SIZES)} sizes)")
    print(f"wrote {PNG.relative_to(HERE)} ({PNG.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
