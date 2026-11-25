#!/bin/bash
# Double-click to open Job Collector. The first time it installs what it needs (a few minutes).
cd "$(dirname "$0")" || exit 1

pause() { read -r -p "Press Enter to close this window." _; }

# Python 3.10 or newer
PY=""
for c in python3 /usr/local/bin/python3 /opt/homebrew/bin/python3; do
  if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(sys.version_info < (3, 10))' 2>/dev/null; then
    PY="$c"; break
  fi
done
if [ -z "$PY" ]; then
  echo "Job Collector needs Python 3.10 or newer. Opening the download page..."
  echo "Install it, then double-click this file again."
  open "https://www.python.org/downloads/"
  pause; exit 1
fi

if [ ! -x .venv/bin/python ]; then
  echo "Setting up for the first time..."
  "$PY" -m venv .venv || { echo "Couldn't create the .venv folder."; pause; exit 1; }
fi

# Install (again) only when requirements.txt changed
want=$(shasum -a 256 requirements.txt | cut -d' ' -f1)
if [ "$(cat .venv/requirements.sha 2>/dev/null)" != "$want" ]; then
  echo "Installing (the first time takes a few minutes)..."
  if .venv/bin/python -m pip install --disable-pip-version-check -q -r requirements.txt; then
    echo "$want" > .venv/requirements.sha
  else
    echo "Install failed. Check your internet connection and double-click again."
    pause; exit 1
  fi
fi

echo
echo "Keep this window open while you use Job Collector. Close it to stop the page"
echo "(scheduled job checks keep running)."
echo
.venv/bin/python -m collector ui
