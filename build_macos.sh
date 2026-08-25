#!/usr/bin/env bash
# Build Anonymizer-Redactor.app for macOS.
#
# The project folder's name contains a colon, which PyInstaller cannot build
# from, so everything is staged into a temporary colon-free directory first and
# the finished app is copied back.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
STAGE="$(mktemp -d /tmp/anonymizer-build-XXXXXX)"
BUILD_VENV="$STAGE/venv"
cleanup() { rm -rf "$STAGE"; }
trap cleanup EXIT

echo "==> Staging sources in $STAGE"
mkdir -p "$STAGE/src"
cp -R "$HERE/redactor" "$STAGE/src/"
cp "$HERE/requirements.txt" "$STAGE/src/"
cp "$HERE/build/launcher.py" "$STAGE/src/"
cp "$HERE/build/redactor.spec" "$STAGE/src/"
if [ -d "$HERE/vendor" ]; then
  echo "    including vendored Tesseract"
  cp -R "$HERE/vendor" "$STAGE/src/"
else
  echo "    NOTE: no vendor/ directory - run vendor_tesseract_macos.py first if"
  echo "          you want Tesseract bundled. RapidOCR still ships as a fallback."
fi

echo "==> Creating build environment"
python3 -m venv "$BUILD_VENV"
"$BUILD_VENV/bin/python" -m pip install --upgrade pip wheel
"$BUILD_VENV/bin/python" -m pip install -r "$STAGE/src/requirements.txt" pyinstaller
"$BUILD_VENV/bin/python" -m spacy download en_core_web_sm

echo "==> Building"
cd "$STAGE/src"
"$BUILD_VENV/bin/pyinstaller" --noconfirm --clean redactor.spec

echo "==> Copying the app back"
mkdir -p "$HERE/dist"
if [ -e "$HERE/dist/Anonymizer-Redactor.app" ]; then
  rm -rf "$HERE/dist/Anonymizer-Redactor.app"
fi
cp -R "$STAGE/src/dist/Anonymizer-Redactor.app" "$HERE/dist/"

cat <<'NOTE'

Built: dist/Anonymizer-Redactor.app

The app is not code-signed, so the first launch needs a right-click > Open,
then "Open" again in the dialog. After that it opens normally.

OCR travels with the app: the vendored Tesseract is unpacked on first use,
and RapidOCR is bundled as a fallback. Nothing needs installing on the
machine that runs it.
NOTE
