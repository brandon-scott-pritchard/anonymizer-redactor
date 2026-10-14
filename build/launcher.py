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

    workers_ok, workers_note = _ocr_workers() if ok else (False, "no OCR engine")
    lines.append(f"OCR read-ahead:       {'OK  ' if workers_ok else 'FAIL'} {workers_note}")

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

    return 0 if ok and model_ok and photos_ok and workers_ok else 1


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


def _ocr_workers() -> tuple[bool, str]:
    """Read one scanned page in the worker processes, as a run would.

    Worker processes are started by relaunching the app's own binary, which
    is exactly what a frozen build can get wrong - and when it does, the app
    quietly reads one page at a time instead. Only a page read in a worker
    proves the bundle can do it.
    """
    import tempfile
    import time
    from pathlib import Path

    import pymupdf

    from redactor import ocr, ocr_pool, pdf_processor

    count = ocr_pool.worker_count()
    if count < 1:
        return True, "off (REDACTOR_OCR_WORKERS=0)"
    with tempfile.TemporaryDirectory() as tmp:
        text = pymupdf.open()
        text.new_page().insert_text((72, 100), "Self test 4417220193", fontsize=18)
        scan = pymupdf.open()
        scan.new_page().insert_image(pymupdf.Rect(0, 0, 595, 842),
                                     stream=text[0].get_pixmap(dpi=150).tobytes("png"))
        path = Path(tmp) / "scan.pdf"
        scan.save(path)
        started = time.time()
        read: list[int] = []
        ocr_pool.read_ahead([path], on_page=lambda _p, index: read.append(index))
        seconds = time.time() - started
        # what came back from the workers, straight out of the cache
        base = pdf_processor._ocr_key(path, 0)
        names = [engine.name for engine in ocr.usable_engines()]
        cached = base is not None and pdf_processor.ocr_cached(base, names)
        words = [w[4] for name in names
                 for w in (pdf_processor._ocr_cache.get(base + (name,)) or [])] if cached else []
        ocr_pool.shutdown()
    if read != [0] or not cached:
        return False, (f"{count} worker(s) started, but no page came back - "
                       "reading falls back to one page at a time")
    if not any("4417220193" in word for word in words):
        return False, f"a worker read the test page but not its text: {' '.join(words)[:80]}"
    return True, f"{count} worker(s), a test page read in {seconds:.1f}s"


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
