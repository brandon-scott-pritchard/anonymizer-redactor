"""Frozen-app entry point.

Run with ``--selftest`` to print what the bundle actually found at runtime -
which OCR engine, whether the language model loaded - and exit. That is the
only reliable way to check a frozen build, since packaging can quietly change
what ends up inside it.
"""

import multiprocessing
import sys


def selftest() -> int:
    from redactor import __version__, ner, ocr

    lines = [f"Document Redactions & Anonymization {__version__}",
             f"frozen: {getattr(sys, 'frozen', False)}"]

    archive = ocr._bundled_archive()
    lines.append(f"vendored OCR archive: {archive or 'not bundled'}")

    bundled = ocr.bundled_tesseract()
    if bundled:
        binary, tessdata = bundled
        lines.append(f"tesseract binary:     {binary}")
        lines.append(f"tessdata:             {tessdata}")

    ok, note = ocr.describe()
    lines.append(f"OCR engine:           {'OK  ' if ok else 'NONE'} {note}")

    model_ok, model_note = ner.available()
    lines.append(f"language model:       {'OK  ' if model_ok else 'NONE'} {model_note}")

    photos_ok, photos_note = _photo_formats()
    lines.append(f"photo formats:        {'OK  ' if photos_ok else 'FAIL'} {photos_note}")

    report = "\n".join(lines)
    print(report)
    # the Windows build is windowed (console=False), so stdout goes nowhere;
    # a file is the only visible result there
    for target in _selftest_targets():
        try:
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(report + "\n", encoding="utf-8")
        except OSError:
            continue
        print(f"written to {target}")
        break

    return 0 if ok and model_ok and photos_ok else 1


def _photo_formats() -> tuple[bool, str]:
    """Write and read back each format a phone or a scanner hands over.

    The codecs are native libraries packaging can quietly swap or drop; a
    round trip through each, in the bundle as built, is the only proof.
    """
    import io

    from PIL import Image

    from redactor import photo

    photo._register_heif()
    worked, failed = [], []
    for fmt in ("JPEG", "PNG", "TIFF", "WEBP", "AVIF", "HEIF"):
        try:
            buffer = io.BytesIO()
            Image.new("RGB", (64, 32), (200, 30, 30)).save(buffer, format=fmt)
            buffer.seek(0)
            with Image.open(buffer) as back:
                back.load()
                assert back.size == (64, 32)
            worked.append(fmt)
        except Exception as exc:
            failed.append(f"{fmt} ({type(exc).__name__})")
    note = ", ".join(worked) + (f"; FAILED: {', '.join(failed)}" if failed else "")
    return not failed, note


def _selftest_targets():
    """Where the self-test report goes, most visible first.

    Never inside the macOS bundle: a file written into Contents/MacOS breaks
    the app's code seal, and codesign then reports it as modified. On Windows
    nothing is sealed, so beside the .exe is where anyone looks first.
    """
    import os
    from pathlib import Path

    home = Path.home()
    if sys.platform == "darwin":
        return [home / "Library" / "Logs" / "Anonymizer-Redactor" / "selftest.txt"]
    if sys.platform.startswith("win"):
        local = Path(os.environ.get("LOCALAPPDATA", home / "AppData" / "Local"))
        return [Path(sys.executable).resolve().parent / "selftest.txt",
                local / "Anonymizer-Redactor" / "selftest.txt"]
    return [home / ".cache" / "Anonymizer-Redactor" / "selftest.txt"]


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    if "--tk" in sys.argv:
        # the old Tkinter front end, kept for anyone that wants it
        from redactor.gui import main as gui_main
        gui_main()
        return 0
    if "--browser" in sys.argv:
        from redactor.webapp import main as web_main
        return web_main()
    # default: the React interface in its own application window
    from redactor.desktop import main as desktop_main
    return desktop_main()


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
