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
Windows 10+, so there is no older Windows to be kind to. Writing DIB entries by
hand would mean decoding the PNG, i.e. a Pillow dependency for one build step.

Seven sizes rather than one, so Windows PICKS instead of smearing: a 256px icon
squeezed down to 16px for the title bar looks like mud.
"""
from __future__ import annotations

import pathlib
import struct
import subprocess
import sys
import tempfile

SIZES = [16, 24, 32, 48, 64, 128, 256]
HERE  = pathlib.Path(__file__).resolve().parent.parent
DEST  = HERE / "src" / "assets" / "navicore-badge.ico"
SRC   = HERE / "src" / "assets" / "navicore-badge-512.png"


def main() -> int:
    src = pathlib.Path(sys.argv[1]) if len(sys.argv) > 1 else SRC
    if not src.is_file():
        print(f"no such file: {src}")
        return 1

    with tempfile.TemporaryDirectory(prefix="navilink-icon-") as td:
        tmp = pathlib.Path(td)
        entries, blobs = bytearray(), bytearray()
        offset = 6 + 16 * len(SIZES)
        for s in SIZES:
            out = tmp / f"{s}.png"
            # sips ships with macOS. On another platform, resize however you like
            # and drop the files in -- the container below is the only part that
            # has to be exact.
            r = subprocess.run(["sips", "-s", "format", "png", "-z", str(s), str(s),
                                str(src), "--out", str(out)],
                               capture_output=True, text=True)
            if r.returncode != 0 or not out.is_file():
                print(f"could not resize to {s}px: {(r.stderr or r.stdout).strip()}")
                return 1
            data = out.read_bytes()
            # A width/height byte of 0 means 256 -- the field is one byte wide.
            w = h = 0 if s >= 256 else s
            entries += struct.pack("<BBBBHHII", w, h, 0, 0, 1, 32, len(data), offset)
            blobs   += data
            offset  += len(data)

        DEST.write_bytes(struct.pack("<HHH", 0, 1, len(SIZES)) + entries + blobs)

    print(f"wrote {DEST.relative_to(HERE)} ({DEST.stat().st_size:,} bytes, {len(SIZES)} sizes)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
