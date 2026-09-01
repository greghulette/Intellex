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

# The NEWEST python3 available, not merely the first on PATH. On a Mac the first
# is very often the python.org 3.9 framework build even when a newer one sits
# right beside it, and the venv it builds is then silently a version the project
# does not develop on. That is exactly how asyncio.timeout (3.11+) ended up in a
# 3.9 venv, breaking smoke_host.py with an AttributeError nothing had warned
# about. Windows needs no equivalent: `py -3` already selects the newest.
find_python() {
  for c in python3.15 python3.14 python3.13 python3.12 python3.11 python3.10 python3.9 python3; do
    if command -v "$c" >/dev/null 2>&1 &&
       "$c" -c 'import sys; raise SystemExit(sys.version_info < (3, 9))' 2>/dev/null; then
      command -v "$c"
      return 0
    fi
  done
  return 1
}

# Every run, not just at setup: the venv outlives the decision that made it, and a
# 3.9 one built months ago is precisely the case worth surfacing. A note, not a
# refusal -- 3.9 does run everything today, and blocking a working setup would be
# worse than the silence it replaces.
note_if_old() {
  "$PY" -c 'import sys
if sys.version_info < (3, 11):
    print("")
    print("Note: this venv is Python %d.%d. NaviLink is developed on 3.14 and only" % sys.version_info[:2])
    print("      3.11+ is exercised. It works today, but nothing tests it -- to move")
    print("      up, install a newer Python, delete .venv, and run this again.")
    print("")
' 2>/dev/null || true
}

if [ ! -x "$PY" ]; then
  echo "First run: setting up. This takes a minute."
  if ! BOOTSTRAP=$(find_python); then
    echo
    echo "No usable python3 was found (3.9 or newer is required)."
    echo "Install Python 3.11+ from https://www.python.org/downloads/"
    echo "  (or: brew install python), then try again."
    exit 1
  fi
  echo "Using $BOOTSTRAP ($("$BOOTSTRAP" -V 2>&1))"
  "$BOOTSTRAP" -m venv .venv
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

note_if_old

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
