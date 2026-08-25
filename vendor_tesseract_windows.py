#!/usr/bin/env python3
"""Copy an installed Tesseract into ``vendor/`` on Windows.

Much simpler than the macOS equivalent: Windows keeps a program's DLLs beside
its executable and resolves them relative to it, so there are no absolute
library paths to rewrite and nothing to re-sign. It is a folder copy with a
filter.

    python vendor_tesseract_windows.py

Run it on a Windows machine that has Tesseract installed, then build with
build_windows.bat.

This step is **optional**. RapidOCR ships as an ordinary pip package with its
models inside, so scanned PDFs already work on Windows without it. Vendor
Tesseract only if you want that engine specifically.
"""

from __future__ import annotations

import platform
import shutil
import tarfile
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent


def _folder() -> str:
    """Same platform-and-architecture identity ocr.py and the spec use.

    The names must agree exactly - the spec bundles, and ocr.py unpacks,
    ``tesseract-{folder}.tar.gz`` and nothing else.
    """
    machine = platform.machine().lower()
    machine = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(machine, machine)
    return f"windows-{machine}"


TARGET = HERE / "vendor" / "tesseract" / _folder()

LIKELY_INSTALLS = (
    Path(r"C:\Program Files\Tesseract-OCR"),
    Path(r"C:\Program Files (x86)\Tesseract-OCR"),
    Path.home() / "AppData" / "Local" / "Tesseract-OCR",
)

TESSDATA_WANTED = ("eng.traineddata", "osd.traineddata", "pdf.ttf", "configs")


def find_install() -> Path | None:
    found = shutil.which("tesseract")
    if found:
        return Path(found).resolve().parent
    for candidate in LIKELY_INSTALLS:
        if (candidate / "tesseract.exe").exists():
            return candidate
    return None


def main() -> int:
    if not sys.platform.startswith("win"):
        raise SystemExit(
            "this script vendors the Windows build and must be run on Windows.\n"
            "On macOS use vendor_tesseract_macos.py instead."
        )

    install = find_install()
    if install is None:
        raise SystemExit(
            "Tesseract was not found.\n\n"
            "Install it from https://github.com/UB-Mannheim/tesseract/wiki\n"
            "(the 64-bit installer), then run this script again.\n\n"
            "This step is optional - RapidOCR already handles scanned PDFs."
        )

    print(f"==> Vendoring from {install}")
    bin_dir, data_dir = TARGET / "bin", TARGET / "tessdata"
    for directory in (bin_dir, data_dir):
        directory.mkdir(parents=True, exist_ok=True)

    # the executable and every DLL sitting beside it
    copied = 0
    for item in install.iterdir():
        if item.is_file() and item.suffix.lower() in {".exe", ".dll"}:
            shutil.copy2(item, bin_dir / item.name)
            copied += 1
    print(f"    {copied} files (tesseract.exe and its DLLs)")

    tessdata = install / "tessdata"
    if not tessdata.is_dir():
        raise SystemExit(f"no tessdata directory under {install}")
    print(f"==> Language data from {tessdata}")
    for name in TESSDATA_WANTED:
        item = tessdata / name
        if not item.exists():
            continue
        destination = data_dir / name
        if item.is_dir():
            shutil.copytree(item, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(item, destination)
        print(f"    {name}")

    archive = TARGET.parent.parent / f"tesseract-{_folder()}.tar.gz"
    print(f"==> Packing {archive.name}")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(TARGET, arcname="tesseract")

    size = sum(f.stat().st_size for f in TARGET.rglob("*") if f.is_file()) / 1e6
    print(f"==> Done: {TARGET.relative_to(HERE)} ({size:.0f} MB)")
    print("    Now run build_windows.bat to fold it into the .exe.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
