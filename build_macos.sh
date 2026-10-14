#!/usr/bin/env bash
# Build Anonymizer-Redactor.app for macOS.
#
# The project folder's name contains a colon, which PyInstaller cannot build
# from, so everything is staged into a temporary colon-free directory first and
# the finished app is copied back.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# The app is built and tested on Python 3.11, and PyInstaller freezes for the
# architecture of the interpreter it runs under - so the build needs a 3.11
# built for THIS Mac. "python3" alone is not that: Homebrew's moved on to 3.14,
# and the first "python3.11" on this machine's PATH is an Intel copy, which
# would have produced an Intel app running under Rosetta. Set PYTHON to choose
# one explicitly.
WANT="3.11 $(uname -m)"
describe() { "$1" -c 'import platform, sys; print("%d.%d %s" % (*sys.version_info[:2], platform.machine()))' 2>/dev/null; }
pick_python() {
  if [ -n "${PYTHON:-}" ]; then
    [ "$(describe "$PYTHON")" = "$WANT" ] && { echo "$PYTHON"; return 0; }
    echo "PYTHON=$PYTHON is $(describe "$PYTHON" || echo 'not runnable'), not Python $WANT." >&2
    return 1
  fi
  local candidate found
  for candidate in python3.11 python3 /opt/homebrew/bin/python3.11 \
      /Library/Frameworks/Python.framework/Versions/3.11/bin/python3.11 \
      /opt/anaconda3/bin/python3.11 /usr/local/bin/python3.11; do
    found="$(command -v "$candidate" 2>/dev/null)" || continue
    [ "$(describe "$found")" = "$WANT" ] && { echo "$found"; return 0; }
  done
  echo "No Python $WANT found. Install one (brew install python@3.11) or set" >&2
  echo "PYTHON=/path/to/python3.11 and run this again." >&2
  return 1
}
APP_NAME="Document Redactions & Anonymization"

# A build replaces dist/ - pulling the files out from under a copy that is
# running from there. Stop before spending ten minutes on it.
if pgrep -f "$HERE/dist/$APP_NAME.app/Contents/MacOS/" >/dev/null 2>&1; then
  echo "The app in dist/ is open. Quit it first: a build replaces it." >&2
  exit 1
fi

BUILD_PYTHON="$(pick_python)"
echo "==> Building with $BUILD_PYTHON ($(describe "$BUILD_PYTHON"))"

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
[ -f "$HERE/build/icon.icns" ] && cp "$HERE/build/icon.icns" "$STAGE/src/"
if [ -d "$HERE/vendor" ]; then
  echo "    including vendored Tesseract"
  cp -R "$HERE/vendor" "$STAGE/src/"
else
  echo "    NOTE: no vendor/ directory - run vendor_tesseract_macos.py first if"
  echo "          you want Tesseract bundled. RapidOCR still ships as a fallback."
fi

echo "==> Creating build environment"
"$BUILD_PYTHON" -m venv "$BUILD_VENV"
"$BUILD_VENV/bin/python" -m pip install --upgrade pip wheel
"$BUILD_VENV/bin/python" -m pip install -r "$STAGE/src/requirements.txt" pyinstaller
"$BUILD_VENV/bin/python" -m spacy download en_core_web_sm

echo "==> Building"
cd "$STAGE/src"
"$BUILD_VENV/bin/pyinstaller" --noconfirm --clean redactor.spec

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

# Every build goes straight into Applications unless INSTALL=0. If the copy
# there is open, the install waits for ./install_macos.sh to be run again.
if [ "${INSTALL:-1}" != "0" ]; then
  echo "==> Installing in ${INSTALL_DIR:-/Applications}"
  "$HERE/install_macos.sh" || echo "    not installed this time - see above"
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
