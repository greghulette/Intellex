#!/bin/bash
# Run the NaviLink host on macOS.
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

if [ ! -x "$PY" ]; then
  echo "First run: creating the virtual environment..."
  if command -v python3 >/dev/null 2>&1; then
    python3 -m venv .venv
  else
    echo
    echo "python3 not found. Install Python 3.11+ (python.org, or: brew install python)"
    exit 1
  fi
  echo "Installing dependencies..."
  "$PY" -m pip install --quiet --upgrade pip
  "$PY" -m pip install --quiet -r requirements.txt
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

# On macOS a NaviCore enumerates as /dev/cu.usbmodem* (native USB CDC); a bridge
# WCB behind a CP210x/CH9102 shows as /dev/cu.usbserial* or /dev/cu.wchusbserial*.
# Use cu.* and not tty.* -- opening tty.* blocks waiting for carrier detect.
exec "$PY" src/app.py "$@"
