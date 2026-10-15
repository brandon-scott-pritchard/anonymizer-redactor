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

Pages are read in order of need, not in the order they were asked for. A drop
of a few hundred files queued every page of every file, and the caption step -
which reads the first six pages of each - waited behind the lot with its page
counter at nought. So the pages a step is waiting for go first, then the
first pages of every file (the caption step's), then the rest. Which pages
of a file are scans is found by a process of its own, so that never queues
behind the reading either.

Everything is shared, so a drop read in the background and the step that
later needs the same pages never read a page twice: a page already queued is
moved up, and one already being read is waited for.

``REDACTOR_OCR_WORKERS`` sets the number of workers; 0 turns reading ahead
off (the corpus harness runs its own pool of processes and sets it so).
"""

from __future__ import annotations

import atexit
import heapq
import itertools
import os
import queue
import threading
from concurrent.futures import Future, ProcessPoolExecutor
from pathlib import Path
from typing import Callable, Iterable

# The most pages read at once. Each in flight holds a 300-dpi picture and both
# engines' working memory - about 1.2 GB at the peak - and past four the
# engines contend for cores more than they gain.
MAX_WORKERS = 4

# Lower is sooner: a page a step is waiting for, then the sets read ahead in
# the order they were made - each set's caption pages before its others.
NEEDED_NOW = (1,)


def _ahead(rank: int, index: int) -> tuple:
    from . import pdf_processor

    return (2, rank, 0 if index < pdf_processor.CAPTION_PAGES else 1)

_lock = threading.Lock()
_scheduler: "_Scheduler | None" = None
_broken = False
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


class _Scheduler:
    """Pages waiting to be read, most needed first, fed to the workers a few
    at a time so a page asked for later can still go ahead of them."""

    def __init__(self, workers: int) -> None:
        import multiprocessing

        spawn = multiprocessing.get_context("spawn")
        threads = max(1, (os.cpu_count() or 2) // workers)
        self.readers = ProcessPoolExecutor(workers, mp_context=spawn,
                                           initializer=_start_worker, initargs=(threads,))
        self.surveyor = ProcessPoolExecutor(1, mp_context=spawn)
        self.slots = workers + 1                  # one ready as each finishes
        self.heap: list[tuple[tuple, int, tuple]] = []
        self.order = itertools.count()
        self.pages: dict[tuple, dict] = {}        # page key -> its place in line
        self.running = 0
        self.wake = threading.Condition(_lock)
        self.closed = False
        # submitted from a thread of its own rather than from the workers'
        # completion callbacks, which run on the executor's own thread
        threading.Thread(target=self._feed, daemon=True, name="ocr-feeder").start()

    def want(self, path: Path, index: int, names: list[str], priority: tuple) -> Future | None:
        """The reading of one page: None if it is already cached, else a
        future that completes once it is (or once it has failed - the step
        then reads it in-line)."""
        from . import pdf_processor

        base = pdf_processor._ocr_key(path, index)
        if base is None or pdf_processor.ocr_cached(base, names):
            return None
        with self.wake:
            entry = self.pages.get(base)
            if entry is None:
                entry = self.pages[base] = {"future": Future(), "priority": priority,
                                            "running": False, "path": str(path),
                                            "index": index}
                heapq.heappush(self.heap, (priority, next(self.order), base))
                self.wake.notify()
            elif not entry["running"] and priority < entry["priority"]:
                entry["priority"] = priority
                heapq.heappush(self.heap, (priority, next(self.order), base))
                self.wake.notify()
            return entry["future"]

    def _feed(self) -> None:
        while not self.closed:
            try:
                self._feed_one()
            except Exception:                     # never leave a step waiting on a
                self.shutdown()                   # line nothing will move
                return

    def _feed_one(self) -> None:
        """Hand the most needed waiting page to a worker once one is free."""
        with self.wake:
            while not self.closed and (self.running >= self.slots or not self.heap):
                self.wake.wait()
            if self.closed:
                return
            priority, _order, base = heapq.heappop(self.heap)
            entry = self.pages.get(base)
            if entry is None or entry["running"] or priority != entry["priority"]:
                return                        # read already, or moved up the line
            entry["running"] = True
            self.running += 1
        try:
            task = self.readers.submit(_read, entry["path"], entry["index"])
        except Exception:                     # shut down: the steps read in-line
            self._finished(base, None)
            return
        task.add_done_callback(lambda done, base=base: self._finished(base, done))

    def _finished(self, base: tuple, task) -> None:
        from . import pdf_processor

        if task is not None:
            try:
                pdf_processor.store_ocr(base, task.result())
            except Exception:                     # the step reads it in-line instead
                pass
        with self.wake:
            self.running -= 1
            entry = self.pages.pop(base, None)
            self.wake.notify()
        if entry is not None and not entry["future"].done():
            entry["future"].set_result(None)

    def shutdown(self, wait: bool = False) -> None:
        with self.wake:
            self.closed = True
            self.wake.notify_all()
            waiting = list(self.pages.values())
            self.pages.clear()
            self.heap.clear()
        for entry in waiting:
            if not entry["future"].done():
                entry["future"].set_result(None)
        self.readers.shutdown(wait=wait, cancel_futures=True)
        self.surveyor.shutdown(wait=wait, cancel_futures=True)


def _get_scheduler() -> _Scheduler | None:
    global _scheduler, _broken
    with _lock:
        if _scheduler is not None or _broken:
            return _scheduler
        workers = worker_count()
        if workers < 1:
            return None
    try:
        scheduler = _Scheduler(workers)
    except Exception:                              # reading falls back to in-line
        with _lock:
            _broken = True
        return None
    with _lock:
        if _scheduler is None:
            _scheduler = scheduler
            atexit.register(scheduler.shutdown)
        else:
            scheduler.shutdown()
        return _scheduler


def shutdown(wait: bool = False) -> None:
    """Stop the workers and forget everything. ``wait`` until they have exited -
    which a process started by multiprocessing must, before it can exit itself."""
    global _scheduler, _broken
    with _lock:
        scheduler, _scheduler, _broken = _scheduler, None, False
        _surveys.clear()
    if scheduler is not None:
        scheduler.shutdown(wait=wait)


def _file_key(path: Path) -> tuple | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (str(path.resolve()), stat.st_size, stat.st_mtime_ns)


def _survey_of(scheduler: _Scheduler, path: Path) -> Future | None:
    key = _file_key(path)
    if key is None:
        return None
    with _lock:
        found = _surveys.get(key)
        if found is None:
            try:
                found = _surveys[key] = scheduler.surveyor.submit(_survey, str(path))
            except Exception:
                return None
        return found


def read_ahead(paths: Iterable[Path], per_file: int | None = None, wait: bool = True,
               on_page: Callable[[Path, int], None] | None = None, rank: int = 0,
               on_scans: Callable[[Path, int], None] | None = None) -> None:
    """Read the scanned pages of ``paths`` into the OCR cache, a few at once.

    ``per_file`` stops at that many pages of each file. With ``wait`` those
    pages go to the front of the line and the call returns once every one is
    read; without, they join the line behind every set of a lower ``rank`` -
    a file's first pages ahead of the rest - and the call returns at once.
    Either way ``on_scans`` is told how many of a file's pages are to be read,
    and ``on_page`` of each the moment it is (a page found already read
    counts at once). Anything that fails here is simply read again, in-line,
    by the step that needs it.
    """
    scheduler = _get_scheduler()
    if scheduler is None:
        return
    from . import ocr, pdf_processor

    try:
        names = [engine.name for engine in ocr.usable_engines()]
    except Exception:
        return
    if not names:
        return
    pdfs = [Path(p) for p in paths if Path(p).suffix.lower() == ".pdf"]
    events: "queue.Queue[tuple | None]" = queue.Queue()
    expected = [0]
    counting = threading.Lock()

    def surveyed(path: Path, survey: Future | None) -> None:
        try:
            scanned = survey.result() if survey is not None else []
        except Exception:
            scanned = []
        readings, already = [], []
        for index in scanned:
            if per_file is not None and index >= per_file:
                break
            priority = NEEDED_NOW if wait else _ahead(rank, index)
            future = scheduler.want(path, index, names, priority)
            if future is not None:
                readings.append((future, index))
            else:
                already.append(index)
        if on_scans is not None:
            on_scans(path, len(readings) + len(already))
        if wait:
            with counting:
                expected[0] += len(readings)
            for future, index in readings:
                future.add_done_callback(
                    lambda _done, path=path, index=index: events.put((path, index)))
        elif on_page is not None:
            for future, index in readings:
                future.add_done_callback(
                    lambda _done, path=path, index=index: on_page(path, index))
        if on_page is not None:
            for index in already:
                on_page(path, index)
        events.put(None)                           # this file's pages are all in line

    for path in pdfs:
        survey = _survey_of(scheduler, path)
        if survey is None:
            surveyed(path, None)
        else:
            survey.add_done_callback(lambda done, path=path: surveyed(path, done))
    if not wait:
        return
    surveys_done = pages_done = 0
    while True:
        with counting:
            finished = surveys_done == len(pdfs) and pages_done >= expected[0]
        if finished:
            return
        event = events.get()
        if event is None:
            surveys_done += 1
            continue
        pages_done += 1
        if on_page is not None:
            on_page(*event)


def read_ahead_in_background(paths: Iterable[Path], rank: int = 0,
                             on_page: Callable[[Path, int], None] | None = None,
                             on_scans: Callable[[Path, int], None] | None = None) -> bool:
    """Start reading ``paths`` and return at once - for files just added.
    False when there is no reading ahead to do it (turned off, or no engine)."""
    files = list(paths)
    if not files or worker_count() < 1 or _get_scheduler() is None:
        return False
    read_ahead(files, wait=False, rank=rank, on_page=on_page, on_scans=on_scans)
    return True
