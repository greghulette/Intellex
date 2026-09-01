#!/bin/bash
# ============================================================================
#  NaviLink — double-click this.
# ============================================================================
#  The macOS counterpart to NaviLink.bat, and at the repo root for the same
#  reason: "how do I open it" should not require knowing that scripts/ exists.
#  scripts/run-macos.command still works and does the same thing; this is the
#  one to hand somebody.
#
#  Unlike Windows there is no pythonw, so double-clicking opens a Terminal
#  window and it stays open while the app runs. Closing that window quits
#  NaviLink. That is the same relationship the .bat has with its console — on
#  macOS it is simply visible.
#
#    NaviLink.command                                window + chooser
#    NaviLink.command --ws 192.168.4.1               skip the chooser
#    NaviLink.command --serial /dev/cu.usbmodem1101
#    NaviLink.command --browser                      no window, use the browser
#
#  If Finder refuses to open it ("unidentified developer"), this copy was
#  downloaded rather than cloned: a download carries com.apple.quarantine and a
#  clone does not. Right-click → Open once, or:
#      xattr -d com.apple.quarantine NaviLink.command
# ============================================================================
set -e

# Its own directory, not the caller's — a double-click starts in $HOME. Quoted
# because the path may contain spaces.
cd "$(dirname "$0")"

# Keep the setup failure readable. Double-clicked, this window may close the
# moment the script ends, taking the error message with it; the .bat pauses for
# the same reason. Dropped by exec below, so it only covers first-run setup.
on_error() {
  status=$?
  [ $status -eq 0 ] && exit 0
  echo
  echo "NaviLink could not start (exit $status)."
  if [ -t 0 ]; then
    read -n 1 -s -r -p "Press any key to close."
    echo
  fi
}
trap on_error EXIT

PY=".venv/bin/python3"

if [ ! -x "$PY" ]; then
  echo "First run: setting up. This takes a minute."
  if ! command -v python3 >/dev/null 2>&1; then
    echo
    echo "python3 was not found."
    echo "Install Python 3.11+ from https://www.python.org/downloads/"
    echo "  (or: brew install python), then try again."
    exit 1
  fi
  python3 -m venv .venv
  if [ ! -x "$PY" ]; then
    echo
    echo "Could not create a virtual environment in .venv"
    exit 1
  fi
  "$PY" -m pip install --quiet --upgrade pip
  # pywebview needs the pyobjc bridge here; requirements.txt gates that on
  # sys_platform == "darwin", so this is the same file Windows installs from.
  "$PY" -m pip install --quiet -r requirements.txt
  echo "Done."
fi

# The config tool is NOT in the repo: src/webui/ is gitignored because the public
# NaviCore repo is the single source of truth for the UI. So a fresh clone has no
# UI at all, and without this the first thing anyone sees is the app explaining why
# there is nothing to configure with -- accurate, and still the wrong first run.
# Fetch it once, here.
#
# Never fatal. Offline is a legitimate state and the app is useful without the
# bundle: the control API and the /_link byte pipe do not need it.
if [ ! -f src/webui/index.html ]; then
  echo "Fetching the config tool -- it is not in the repo, see README."
  "$PY" tools/fetch_webui.py || echo "Could not fetch it. NaviLink will start and explain."
fi

# exec: the app becomes this process, so closing the Terminal window closes the
# app and nothing is left holding a serial port or the droid's socket.
exec "$PY" src/app.py "$@"
