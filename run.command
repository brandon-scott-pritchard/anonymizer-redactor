#!/usr/bin/env bash
# Double-click this file to launch Anonymizer / Redactor from source on macOS.
set -euo pipefail
HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${ANONYMIZER_VENV:-$HOME/.venvs/anonymizer-redactor}"

if [ ! -x "$VENV/bin/python" ]; then
  echo "No environment found. Running setup first..."
  "$HERE/setup.sh"
fi

cd "$HERE"
exec "$VENV/bin/python" -m redactor
