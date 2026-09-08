#!/bin/bash
# Run the Intellex host on macOS.
#
# Creates the venv and installs dependencies on first run. Double-clickable in
# Finder (that is what .command gets you), which is why it cd's to its own
# directory rather than trusting the caller's.
#
#   ./run-macos.command                    serve, attach nothing
#   ./run-macos.command --serial /dev/cu.usbmodem1101
#   ./run-macos.command --ws 192.168.4.1
set -e
cd "$(dirname "$0")/.."

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
    print("Note: this venv is Python %d.%d. Intellex is developed on 3.14 and only" % sys.version_info[:2])
    print("      3.11+ is exercised. It works today, but nothing tests it -- to move")
    print("      up, install a newer Python, delete .venv, and run this again.")
    print("")
' 2>/dev/null || true
}

if [ ! -x "$PY" ]; then
  echo "First run: creating the virtual environment..."
  if ! BOOTSTRAP=$(find_python); then
    echo
    echo "No usable python3 found (3.9+). Install Python 3.11+ (python.org, or: brew install python)"
    exit 1
  fi
  echo "Using $BOOTSTRAP ($("$BOOTSTRAP" -V 2>&1))"
  "$BOOTSTRAP" -m venv .venv
  echo "Installing dependencies..."
  "$PY" -m pip install --quiet --upgrade pip
  "$PY" -m pip install --quiet -r requirements.txt
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
  "$PY" tools/fetch_webui.py || echo "Could not fetch it. Intellex will start and explain."
fi

# On macOS a NaviCore enumerates as /dev/cu.usbmodem* (native USB CDC); a bridge
# WCB behind a CP210x/CH9102 shows as /dev/cu.usbserial* or /dev/cu.wchusbserial*.
# Use cu.* and not tty.* -- opening tty.* blocks waiting for carrier detect.
exec "$PY" src/app.py "$@"
