#!/usr/bin/env bash
# Set up a virtual environment for running Anonymizer / Redactor from source.
#
# The venv is deliberately created OUTSIDE the project folder, because Python
# refuses to build a venv under a path containing a colon and this project's
# folder has one.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV="${ANONYMIZER_VENV:-$HOME/.venvs/anonymizer-redactor}"

echo "==> Creating virtual environment at $VENV"
python3 -m venv "$VENV"

echo "==> Installing dependencies"
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install -r "$HERE/requirements.txt"

echo "==> Downloading the offline language model"
"$VENV/bin/python" -m spacy download en_core_web_sm

echo
if command -v tesseract >/dev/null 2>&1; then
  echo "==> Tesseract found: $(tesseract --version 2>&1 | head -1)"
else
  echo "==> Tesseract is NOT installed."
  echo "    Scanned PDFs still work - RapidOCR is installed with the app and"
  echo "    covers them. Install Tesseract only if you want that engine:"
  echo "        brew install tesseract"
fi

echo
echo "Done. Launch with:  ./run.command      (or)   $VENV/bin/python -m redactor"
