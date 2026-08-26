#!/usr/bin/env bash
# Launch the app in its own window. Everything runs and stays on this computer.
cd "$(dirname "$0")"
VENV="$HOME/.venvs/anonymizer-redactor"
if [ ! -x "$VENV/bin/python" ]; then
  echo "Run ./setup.sh first."
  exit 1
fi
exec "$VENV/bin/python" -m redactor.desktop
