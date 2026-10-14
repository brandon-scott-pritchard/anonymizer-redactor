"""Scanned pages read ahead, several at a time, in worker processes.

A scanned page is the one slow thing the app does: both OCR engines read it,
about two seconds each, and nothing else comes close. Read one page at a time
on a sixteen-core Mac, a forty-page scan took minutes while most of the
machine sat idle. This reads a batch's scanned pages a few at once, ahead of
whichever step needs them, into the same per-page cache every step reads -
so the readings, and everything after them, are the ones the step would have
made itself.

Processes, not threads. PyMuPDF must not be used from two threads at once, and
the page has to be drawn before it can be read; each worker opens the file
itself, draws the page and hands the picture to both engines side by side.

The pool is started on first use and shared by everything, so a drop read in
the background and the step that later needs the same pages never read a page
twice: a page already on its way is waited for, not read again.

``REDACTOR_OCR_WORKERS`` sets the number of workers; 0 turns reading ahead
off (the corpus harness runs its own pool of processes and sets it so).
"""

from __future__ import annotations

import atexit
import os
import threading
from concurrent.futures import Future, ProcessPoolExecutor, as_completed
from pathlib import Path
from typing import Callable, Iterable

# The most pages read at once. Each in flight holds a 300-dpi picture and both
# engines' working memory - about 1.2 GB at the peak - and past four the
# engines contend for cores more than they gain.
MAX_WORKERS = 4

_lock = threading.Lock()
_pool: ProcessPoolExecutor | None = None
_broken = False
_pending: dict[tuple, Future] = {}        # page key -> its reading, in flight
_surveys: dict[tuple, Future] = {}        # file key -> which pages are scans


def worker_count() -> int:
    configured = os.environ.get("REDACTOR_OCR_WORKERS")
    if configured is not None:
        try:
            return max(0, int(configured))
        except ValueError:
            pass
    return max(1, min(MAX_WORKERS, (os.cpu_count() or 2) // 4))


def _start_worker(threads: int) -> None:
    # every worker sharing the cores rather than each taking all of them
    os.environ["REDACTOR_OCR_THREADS"] = str(threads)
    os.environ["OMP_THREAD_LIMIT"] = "1"


def _survey(path: str) -> list[int]:
    """Worker side: the pages of ``path`` that have no text layer to read."""
    import pymupdf

    from . import pdf_processor

    with pymupdf.open(path) as doc:
        return [index for index, page in enumerate(doc)
                if len(page.get_text("text").strip()) < pdf_processor.MIN_TEXT_CHARS]


def _read(path: str, index: int) -> dict[str, list | None]:
    """Worker side: every engine's words on one scanned page."""
    import pymupdf

    from . import ocr, pdf_processor

    with pymupdf.open(path) as doc:
        image = pdf_processor.render_for_ocr(doc[index])
    engines = ocr.usable_engines()
    readings = pdf_processor._read_side_by_side(image, engines)
    return {engine.name: words for engine, words in zip(engines, readings)}


def _get_pool() -> ProcessPoolExecutor | None:
    global _pool, _broken
    with _lock:
        if _pool is not None or _broken:
            return _pool
        workers = worker_count()
        if workers < 1:
            return None
        try:
            import multiprocessing

            threads = max(1, (os.cpu_count() or 2) // workers)
            _pool = ProcessPoolExecutor(
                workers, mp_context=multiprocessing.get_context("spawn"),
                initializer=_start_worker, initargs=(threads,))
            atexit.register(_pool.shutdown, wait=False, cancel_futures=True)
        except Exception:                          # reading falls back to in-line
            _broken = True
            _pool = None
        return _pool


def _file_key(path: Path) -> tuple | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (str(path.resolve()), stat.st_size, stat.st_mtime_ns)


def _survey_of(pool: ProcessPoolExecutor, path: Path) -> Future | None:
    key = _file_key(path)
    if key is None:
        return None
    with _lock:
        found = _surveys.get(key)
        if found is None:
            found = _surveys[key] = pool.submit(_survey, str(path))
        return found


def _reading_of(pool: ProcessPoolExecutor, path: Path, index: int,
                names: list[str]) -> Future | None:
    """The page's reading in flight, started if it is neither cached nor on its way."""
    from . import pdf_processor

    base = pdf_processor._ocr_key(path, index)
    if base is None or pdf_processor.ocr_cached(base, names):
        return None
    with _lock:
        found = _pending.get(base)
        if found is not None:
            return found
        found = _pending[base] = pool.submit(_read, str(path), index)

    def keep(future: Future, base=base) -> None:
        try:
            pdf_processor.store_ocr(base, future.result())
        except Exception:                          # the step reads it in-line instead
            pass
        finally:
            with _lock:
                _pending.pop(base, None)

    found.add_done_callback(keep)
    return found


def read_ahead(paths: Iterable[Path], per_file: int | None = None, wait: bool = True,
               on_page: Callable[[Path, int], None] | None = None) -> None:
    """Read the scanned pages of ``paths`` into the OCR cache, a few at once.

    ``per_file`` stops at that many pages of each file. With ``wait`` the call
    returns once every page is read, telling ``on_page`` of each as it is;
    without, it starts the reading and returns. Anything that fails here is
    simply read again, in-line, by the step that needs it.
    """
    pool = _get_pool()
    if pool is None:
        return
    from . import ocr

    try:
        names = [engine.name for engine in ocr.usable_engines()]
    except Exception:
        return
    if not names:
        return
    pdfs = [Path(p) for p in paths if Path(p).suffix.lower() == ".pdf"]
    surveys = [(path, _survey_of(pool, path)) for path in pdfs]
    readings: list[tuple[Future, Path, int]] = []
    for path, survey in surveys:
        if survey is None:
            continue
        try:
            scanned = survey.result()
        except Exception:
            continue
        for index in scanned:
            if per_file is not None and index >= per_file:
                break
            future = _reading_of(pool, path, index, names)
            if future is not None:
                readings.append((future, path, index))
    if not wait:
        return
    where = {id(future): (path, index) for future, path, index in readings}
    for future in as_completed([future for future, _p, _i in readings]):
        if on_page is not None:
            path, index = where[id(future)]
            on_page(path, index)


def read_ahead_in_background(paths: Iterable[Path]) -> None:
    """Start reading ``paths`` and return at once - for files just added."""
    files = list(paths)
    if not files or worker_count() < 1:
        return
    threading.Thread(target=read_ahead, args=(files,), kwargs={"wait": False},
                     daemon=True, name="ocr-read-ahead").start()
