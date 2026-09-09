#!/bin/bash
# =============================================================================
#  build-macos.command - build dist/Intellex.app and dist/Intellex.dmg
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

# ALWAYS, not only when the venv is created. requirements.txt gains entries --
# markdown-it-py arrived with the offline docs -- and a venv made before that
# would otherwise build an app that dies the first time someone opens the docs.
# pip is a no-op when everything is already satisfied.
.venv/bin/python -m pip install -q -r requirements.txt || \
    echo "  (dependency install failed - continuing with what is installed)"


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
echo "=== Downloading the wikis (so the docs work offline) ==="
# Not fatal: the app still runs, it just opens the docs viewer with nothing in it.
.venv/bin/python tools/fetch_wiki.py --wiki all || echo "  (docs fetch incomplete - see above)"

echo
echo "=== Caching firmware (so a fresh install can flash offline) ==="
# Not fatal: the app still works, it just has to download at flash time.
.venv/bin/python tools/fetch_firmware.py || echo "  (firmware fetch incomplete - see above)"

# ── The Dock icon ───────────────────────────────────────────────────────────
# iconutil is macOS-only, so the .icns is cut here rather than committed like the
# .ico. It is a build artefact and gitignored to match.
#
# FROM THE 1024 TILE, NOT THE 512. An .iconset wants icon_512x512@2x.png, which
# IS 1024 -- generated from a 512 source that is an upscale, and the Dock shows
# the 1024 entry on every Retina Mac, so the one size most people actually see
# was the softest in the file.
#
# THE SQUIRCLE TILE, NOT THE TRANSPARENT MARK. macOS draws app icons as rounded
# tiles and does NOT add the shape itself: a bare transparent mark renders as
# free-floating art among neighbours that all have a tile, reading as smaller and
# unfinished. intellex-dock-1024.png carries the tile; the transparent icon-512
# is right for the RUNNING process's Dock icon (src/appicon.py) and wrong here.
ICNS="src/assets/intellex.icns"
PNG="src/assets/intellex-dock-1024.png"
if [ ! -f "$ICNS" ] && [ -f "$PNG" ]; then
    echo "=== Generating $ICNS ==="
    ICONSET="$(mktemp -d)/Intellex.iconset"
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
.venv/bin/python -m PyInstaller --noconfirm --clean Intellex.spec

# ── The disk image ──────────────────────────────────────────────────────────
# A .dmg with an /Applications symlink beside the app is the drag-to-install
# convention every Mac user already knows; a bare .app in a zip is not.
if [ -d "dist/Intellex.app" ]; then
    echo
    echo "=== Building the disk image ==="
    STAGE="$(mktemp -d)/Intellex"
    mkdir -p "$STAGE"
    cp -R "dist/Intellex.app" "$STAGE/"
    ln -s /Applications "$STAGE/Applications"
    rm -f "dist/Intellex.dmg"
    hdiutil create -volname "Intellex" -srcfolder "$STAGE" \
                   -ov -format UDZO "dist/Intellex.dmg" >/dev/null
    echo "Built: dist/Intellex.dmg"
fi

echo
echo "Built: dist/Intellex.app"
echo "Install it by opening dist/Intellex.dmg and dragging Intellex to Applications."
echo "First launch: right-click the app -> Open (unsigned; see docs/MACOS_FIRST_RUN.md)."
