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
cp -R "$HERE/webapp" "$STAGE/src/"
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

APP_NAME="Document Redactions & Anonymization"

echo "==> Copying the app back"
mkdir -p "$HERE/dist"
rm -rf "$HERE/dist/$APP_NAME.app" "$HERE/dist/Anonymizer-Redactor.app"
cp -R "$STAGE/src/dist/$APP_NAME.app" "$HERE/dist/"

if [ -n "${CODESIGN_IDENTITY:-}" ]; then
  echo "==> Signing the bundle with: $CODESIGN_IDENTITY"
  codesign --force --deep --options runtime --timestamp \
    -s "$CODESIGN_IDENTITY" "$HERE/dist/$APP_NAME.app"
  codesign --verify --strict "$HERE/dist/$APP_NAME.app" && echo "    signature verifies"
  echo "    To notarize (needed so other Macs open it without the right-click dance):"
  echo "      ditto -c -k --keepParent \"dist/$APP_NAME.app\" /tmp/app.zip"
  echo "      xcrun notarytool submit /tmp/app.zip --keychain-profile <profile> --wait"
  echo "      xcrun stapler staple \"dist/$APP_NAME.app\""
fi

cat <<'NOTE'

Built: dist/Document Redactions & Anonymization.app

Unless CODESIGN_IDENTITY was set, the app is ad-hoc signed only, so the
first launch needs a right-click > Open, then "Open" again in the dialog.
After that it opens normally.

OCR travels with the app: the vendored Tesseract is unpacked on first use,
and RapidOCR is bundled as a fallback. Nothing needs installing on the
machine that runs it.

This .app is built for the architecture of THIS Mac. To produce an Intel
build, run this script on an Intel Mac - the Intel Tesseract is already
vendored and waiting.
NOTE
