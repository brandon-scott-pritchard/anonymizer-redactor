"""Frozen-app entry point.

Run with ``--selftest`` to print what the bundle actually found at runtime -
which OCR engine, whether the language model loaded - and exit. That is the
only reliable way to check a frozen build, since packaging can quietly change
what ends up inside it.
"""

import multiprocessing
import sys


def selftest() -> int:
    from pathlib import Path

    from redactor import __version__, ner, ocr

    lines = [f"Anonymizer / Redactor {__version__}",
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

    report = "\n".join(lines)
    print(report)
    # the Windows build is windowed (console=False), so stdout goes nowhere;
    # a file beside the executable is the only visible result there
    try:
        target = Path(sys.executable).resolve().parent / "selftest.txt"
        target.write_text(report + "\n", encoding="utf-8")
        print(f"written to {target}")
    except OSError:
        pass

    return 0 if ok and model_ok else 1


def main() -> int:
    if "--selftest" in sys.argv:
        return selftest()
    from redactor.gui import main as gui_main
    gui_main()
    return 0


if __name__ == "__main__":
    multiprocessing.freeze_support()
    sys.exit(main())
