#!/usr/bin/env bash
# Put the built app in /Applications, replacing the copy already there.
#
# build_macos.sh runs this at the end of every build, so the copy in
# Applications is always the latest build. Run it by hand to finish an update
# the build could not make because the app was open.
#
#   INSTALL_DIR=/somewhere/else ./install_macos.sh    to install elsewhere
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_NAME="Document Redactions & Anonymization"
SOURCE="$HERE/dist/$APP_NAME.app"
TARGET_DIR="${INSTALL_DIR:-/Applications}"
TARGET="$TARGET_DIR/$APP_NAME.app"

if [ ! -d "$SOURCE" ]; then
  echo "No build at $SOURCE - run ./build_macos.sh first." >&2
  exit 1
fi

# Replacing an app while it runs pulls its files out from under it, and a run
# in progress would fail part-way. Leave it be and say how to finish.
if pgrep -f "$TARGET/Contents/MacOS/" >/dev/null 2>&1; then
  echo "The copy in $TARGET_DIR is open. Quit it, then run ./install_macos.sh" >&2
  echo "to put the new build in its place." >&2
  exit 2
fi

# Copied beside the old one and checked first, then swapped in: an interrupted
# copy never leaves Applications without a working app.
NEW="$TARGET_DIR/.$APP_NAME.installing.app"
rm -rf "$NEW"
ditto "$SOURCE" "$NEW"
codesign --verify --deep --strict "$NEW"
rm -rf "$TARGET"
mv "$NEW" "$TARGET"

# Finder and the Dock cache an app's icon; tell them this one changed.
touch "$TARGET"
LSREGISTER=/System/Library/Frameworks/CoreServices.framework/Frameworks/LaunchServices.framework/Support/lsregister
[ -x "$LSREGISTER" ] && "$LSREGISTER" -f "$TARGET" >/dev/null 2>&1 || true

echo "Installed: $TARGET"
