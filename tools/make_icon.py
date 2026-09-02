#!/usr/bin/env python3
"""Rebuild src/assets/navicore-badge.ico from the 512px badge.

    python tools/make_icon.py [path/to/navicore-badge-512.png]

Only needed when the badge art changes; the .ico is committed, because Windows
needs it at startup and a build step nobody remembers is a build step that rots.

WHY A .ico AT ALL. LoadImage cannot read a PNG, so the Windows taskbar path in
src/appicon.py needs a real icon file. macOS takes the PNG directly.

WHY PNG-COMPRESSED ENTRIES. An .ico may hold either BMP/DIB or PNG data per size.
PNG keeps the file small (73 KB for seven sizes rather than several hundred) and
is understood from Windows Vista on -- and this app needs WebView2, which is
Windows 10+, so there is no older Windows to be kind to.

Seven sizes rather than one, so Windows PICKS instead of smearing: a 256px icon
squeezed down to 16px for the title bar looks like mud.

NEEDS PILLOW, and deliberately not `sips`. sips ships with macOS and nothing else,
so the previous version of this script could not run on the one platform whose
file it generates -- a Windows box could not rebuild its own .ico. Pillow is a dev
dependency of this tool only; the app itself still imports nothing to show an icon.
"""
from __future__ import annotations

import io
import pathlib
import struct
import sys

try:
    from PIL import Image, ImageDraw
except ImportError:
    print("this tool needs Pillow:  pip install pillow")
    raise SystemExit(1)

SIZES = [16, 24, 32, 48, 64, 128, 256]
HERE  = pathlib.Path(__file__).resolve().parent.parent
DEST  = HERE / "src" / "assets" / "navicore-badge.ico"
SRC   = HERE / "src" / "assets" / "navicore-badge-512.png"

# THE .ico IS CROPPED TIGHTER THAN THE PNG, ON PURPOSE, and this is the whole
# reason this script does anything but resize.
#
# The badge art is drawn for the macOS Dock, where Apple's icon grid expects real
# padding inside the square and every neighbour has the same -- so the logo fills
# 70% of the width and looks correctly sized there. The Windows taskbar has the
# opposite convention: icons fill their square. Dropped in unchanged, the badge
# reads as noticeably smaller than everything beside it, and the dark navy plate
# vanishes into a dark taskbar, so only the small hexagon registers.
#
# So: same source art, two treatments. Zoom about the centre and crop back to the
# square, taking the logo to ~85% of the width. Measured, not guessed -- 1.22 is
# 0.85/0.70. Raising it much further starts clipping the plate's visible edge.
ZOOM = 1.22

# Re-rounded after the crop, because zooming pushes the original rounded corners
# outside the frame and leaves square ones. Measured off the source: the first
# opaque pixel on row 0 sits at x=101 of 512, so the radius is 101/512 of the side.
RADIUS_FRAC = 101 / 512


def prepare(src: pathlib.Path) -> Image.Image:
    """Zoom, centre-crop, and restore the rounded corners."""
    im = Image.open(src).convert("RGBA")
    w, h = im.size

    big = im.resize((round(w * ZOOM), round(h * ZOOM)), Image.LANCZOS)
    left, top = (big.width - w) // 2, (big.height - h) // 2
    im = big.crop((left, top, left + w, top + h))

    # Supersample the mask 4x and shrink it: drawn at final size, the rounded
    # corners come out with visibly stepped edges, and an icon is small enough
    # that a few hard pixels on the corner are the first thing you notice.
    ss = 4
    mask = Image.new("L", (w * ss, h * ss), 0)
    ImageDraw.Draw(mask).rounded_rectangle(
        (0, 0, w * ss - 1, h * ss - 1), radius=round(w * ss * RADIUS_FRAC), fill=255)
    mask = mask.resize((w, h), Image.LANCZOS)

    # Multiply into the existing alpha rather than replacing it, so any softness
    # the artwork already had at its own edge survives.
    im.putalpha(Image.composite(im.getchannel("A"), Image.new("L", (w, h), 0), mask))
    return im


def main() -> int:
    src = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else SRC
    if not src.is_file():
        print(f"no such file: {src}")
        return 1

    art = prepare(src)

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

    DEST.write_bytes(struct.pack("<HHH", 0, 1, len(SIZES)) + entries + blobs)
    print(f"wrote {DEST.relative_to(HERE)} "
          f"({DEST.stat().st_size:,} bytes, {len(SIZES)} sizes, zoom {ZOOM})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
