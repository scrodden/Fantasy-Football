#!/usr/bin/env bash
# Fantasy Football Assistant launcher (macOS / Linux)
cd "$(dirname "$0")" || exit 1

if command -v python3 >/dev/null 2>&1; then
  exec python3 app.py
elif command -v python >/dev/null 2>&1; then
  exec python app.py
else
  echo "Python 3 is required. Install it from https://www.python.org/downloads/"
  exit 1
fi
