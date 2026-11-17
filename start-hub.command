#!/bin/bash
# Start the MeshLearn teacher hub.
#   Mac:   double-click this file in Finder (the first time: right-click it, Open, Open).
#   Linux: run  bash start-hub.command
# The first start sets everything up (needs internet once, about a minute). After that it
# works offline. Anything you add is passed to the hub, e.g.  ./start-hub.command --radio-channel 3

cd "$(dirname "$0")" || exit 1

stop() {
  echo
  echo "$1"
  echo
  read -r -p "Press Return to close this window. " _
  exit 1
}

PY=""
for candidate in python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1; then
    PY="$candidate"
    break
  fi
done
if [ -z "$PY" ]; then
  if [ "$(uname)" = "Darwin" ]; then
    xcode-select --install >/dev/null 2>&1
    stop "MeshLearn needs Python 3.9 or newer. If a window offered to install the command line developer tools, click Install, wait for it to finish, then double-click start-hub again. (Or install Python from https://www.python.org/downloads/)"
  fi
  stop "MeshLearn needs Python 3.9 or newer. Install it (for example: sudo apt install python3 python3-venv) and start again."
fi

if [ ! -x .venv/bin/python ]; then
  echo "First start: setting MeshLearn up. This needs internet once and takes about a minute."
  "$PY" -m venv .venv || { rm -rf .venv; stop "Couldn't set up Python's private folder (.venv). On Linux, install python3-venv and start again."; }
  .venv/bin/python -m pip install --quiet --disable-pip-version-check --upgrade pip >/dev/null 2>&1
fi

if ! .venv/bin/python -c 'import meshtastic' >/dev/null 2>&1; then
  echo "Installing the radio software (meshtastic)..."
  # --no-compile and UTF-8 output avoid two install errors seen with the Mac's built-in Python 3.9.
  PYTHONIOENCODING=utf-8 .venv/bin/python -m pip install --quiet --disable-pip-version-check --no-compile \
    -r requirements-radio.txt ||
    stop "Couldn't install the radio software. Check the internet connection and start again."
fi

echo "Starting the MeshLearn hub..."
.venv/bin/python -m meshlearn hub --radio-serial auto --host 0.0.0.0 --open "$@" ||
  stop "The hub stopped. The lines above say why."
