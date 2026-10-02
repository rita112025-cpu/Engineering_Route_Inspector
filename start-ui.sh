#!/usr/bin/env bash
# Engineering Route Inspector - Linux/macOS launcher (same steps as start-ui.bat).
# Runs only on this computer (127.0.0.1). Engineering files are never uploaded.
# First run: creates .venv and installs requirements.txt (internet needed once).
# Extra arguments are passed on, e.g.:  ./start-ui.sh --port 9000 --no-browser
set -u
cd "$(dirname "$0")"
ROOT="$(pwd)"
export PYTHONUTF8=1 PYTHONIOENCODING=utf-8
VENV="$ROOT/.venv"
VPY="$VENV/bin/python"

if [ ! -x "$VPY" ]; then
  SYSPY=""
  for c in python3 python; do
    if command -v "$c" >/dev/null 2>&1 && "$c" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)' >/dev/null 2>&1; then
      SYSPY="$c"; break
    fi
  done
  if [ -z "$SYSPY" ]; then
    echo "Python 3.10 or newer was not found. Install it, then run ./start-ui.sh again." >&2
    exit 1
  fi
  echo "[1/3] Creating the private Python environment in .venv (first run only)..."
  "$SYSPY" -m venv "$VENV" || { echo "Could not create .venv (is the folder writable?)." >&2; exit 1; }
fi

STAMP="$VENV/requirements.installed"
if ! cmp -s "$ROOT/requirements.txt" "$STAMP"; then
  echo "[2/3] Installing the required packages (internet needed once; no project data is sent)..."
  "$VPY" -m pip install --disable-pip-version-check -r "$ROOT/requirements.txt" \
    || { echo "Package installation failed. Check the internet connection and try again." >&2; exit 1; }
  cp "$ROOT/requirements.txt" "$STAMP"
fi

echo "[3/3] Starting. Press Ctrl+C to stop."
export PYTHONPATH="$ROOT/src"
"$VPY" -m app "$@"
rc=$?
diagnosing=0
for a in "$@"; do
  [ "$a" = "--diagnose" ] && diagnosing=1
done
if [ "$rc" -ne 0 ] && [ "$diagnosing" -eq 0 ]; then
  echo
  echo "The program stopped with error code $rc. Running the self-check:"
  echo
  "$VPY" -m app --diagnose
fi
exit "$rc"
