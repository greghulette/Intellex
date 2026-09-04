#!/bin/bash
# =============================================================================
#  build-macos.command - build dist/NaviLink.app and dist/NaviLink.dmg
#
#  Double-clickable in Finder, like ESP-Flasher-Companion's equivalent.
#
#  UNSIGNED, and that has consequences worth knowing before you hand the .dmg to
#  anyone: Gatekeeper quarantines anything downloaded, and an unsigned app gets
#  "cannot be opened because the developer cannot be verified". The way through
#  is right-click -> Open (once), which docs/MACOS_FIRST_RUN.md covers. Signing
#  and notarising needs a paid Developer ID; without one this is the situation.
# =============================================================================
set -e
cd "$(dirname "$0")/.."

if [ ! -x ".venv/bin/python" ]; then
    echo "Creating .venv ..."
    python3 -m venv .venv
    .venv/bin/python -m pip install -q --upgrade pip
    .venv/bin/python -m pip install -q -r requirements.txt
fi

echo
echo "=== Fetching the bundled tools ==="
# Not fatal: offline, build with whatever is already bundled.
.venv/bin/python tools/fetch_webui.py --tool all || \
    echo "  (fetch failed - building with the tools already present)"

if [ ! -f "src/webui/index.html" ]; then
    echo
    echo "WARNING: src/webui/ is empty. The app will start with no config tool"
    echo "         until its Update button is used."
    echo
fi

echo
echo "=== Caching firmware (so a fresh install can flash offline) ==="
# Not fatal: the app still works, it just has to download at flash time.
.venv/bin/python tools/fetch_firmware.py || echo "  (firmware fetch incomplete - see above)"

# ── The Dock icon ───────────────────────────────────────────────────────────
# iconutil is macOS-only, which is why this is not in tools/make_icon.py with the
# .ico and .png: those are generated on either platform, this one cannot be.
ICNS="src/assets/navicore-icon.icns"
PNG="src/assets/navicore-icon-512.png"
if [ ! -f "$ICNS" ] && [ -f "$PNG" ]; then
    echo "=== Generating $ICNS ==="
    ICONSET="$(mktemp -d)/NaviLink.iconset"
    mkdir -p "$ICONSET"
    for sz in 16 32 64 128 256 512; do
        sips -z $sz $sz "$PNG" --out "$ICONSET/icon_${sz}x${sz}.png" >/dev/null
        sips -z $((sz*2)) $((sz*2)) "$PNG" --out "$ICONSET/icon_${sz}x${sz}@2x.png" >/dev/null
    done
    iconutil -c icns "$ICONSET" -o "$ICNS" || echo "  (iconutil failed - building without a custom icon)"
fi

echo
echo "=== Stamping the build ==="
NLSHA="$(git rev-parse --short HEAD 2>/dev/null || echo unknown)"
.venv/bin/python -c "import sys; sys.path.insert(0,'src'); import pathlib, version; version.write_stamp(pathlib.Path('src/build_stamp.py'), '$NLSHA')"

echo
echo "=== Building ==="
.venv/bin/python -m PyInstaller --noconfirm --clean NaviLink.spec

# ── The disk image ──────────────────────────────────────────────────────────
# A .dmg with an /Applications symlink beside the app is the drag-to-install
# convention every Mac user already knows; a bare .app in a zip is not.
if [ -d "dist/NaviLink.app" ]; then
    echo
    echo "=== Building the disk image ==="
    STAGE="$(mktemp -d)/NaviLink"
    mkdir -p "$STAGE"
    cp -R "dist/NaviLink.app" "$STAGE/"
    ln -s /Applications "$STAGE/Applications"
    rm -f "dist/NaviLink.dmg"
    hdiutil create -volname "NaviLink" -srcfolder "$STAGE" \
                   -ov -format UDZO "dist/NaviLink.dmg" >/dev/null
    echo "Built: dist/NaviLink.dmg"
fi

echo
echo "Built: dist/NaviLink.app"
echo "Install it by opening dist/NaviLink.dmg and dragging NaviLink to Applications."
echo "First launch: right-click the app -> Open (unsigned; see docs/MACOS_FIRST_RUN.md)."
