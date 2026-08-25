#!/usr/bin/env bash
# Package the sources a Windows machine needs into one ZIP.
#
# Deliberately excludes the macOS .app, the virtual environment and git
# history: the Windows machine builds its own from these sources.
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAME="Anonymizer-Redactor-Windows"
ZIP="$HERE/$NAME.zip"

python3 - "$HERE" "$ZIP" "$NAME" <<'PY'
import sys, zipfile
from pathlib import Path

root, zip_path, top = Path(sys.argv[1]), Path(sys.argv[2]), sys.argv[3]

INCLUDE_DIRS = ("redactor", "build", "tests", "samples", "webapp")
INCLUDE_FILES = (
    "START-HERE-WINDOWS.txt", "README.md", "LICENSE", "requirements.txt",
    "requirements-dev.txt", "build_windows.bat", "run.bat", "run_web.bat",
    # optional on Windows: RapidOCR already covers scanned PDFs
    "vendor_tesseract_windows.py",
)
SKIP_PARTS = {"__pycache__", ".pytest_cache", ".git", ".DS_Store", "vendor"}

if zip_path.exists():
    zip_path.unlink()

count = 0
with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
    for name in INCLUDE_FILES:
        path = root / name
        if path.exists():
            zf.write(path, f"{top}/{name}")
            count += 1
    for folder in INCLUDE_DIRS:
        base = root / folder
        if not base.is_dir():
            continue
        for path in sorted(base.rglob("*")):
            if not path.is_file():
                continue
            if SKIP_PARTS & set(path.parts) or path.name in SKIP_PARTS:
                continue
            zf.write(path, f"{top}/{path.relative_to(root)}")
            count += 1

size = zip_path.stat().st_size / 1024
print(f"{zip_path.name}: {count} files, {size:.0f} KB")
PY

echo
echo "Send $NAME.zip to the Windows machine."
echo "The instructions are inside it as START-HERE-WINDOWS.txt."
