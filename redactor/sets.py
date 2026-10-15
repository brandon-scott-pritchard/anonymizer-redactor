"""Several sets of documents in one window, each read, reviewed and run on its own.

Eleven large sets, one after another through the four steps, kept the operator
at the keyboard for days: every set's scans had to be read before its names
could be reviewed, and every run had to be started by hand when the last one
finished. Here each set is read in the background in the order it was made,
reviewed when its reading is done, and queued; the queue runs the sets one
after another, unattended, into a folder of results.

Each set is a separate case - its own documents, names, review decisions,
invented names and mapping-key password - exactly as if it had been run alone.

A queued run happens in a process of its own. The window stays free for
reviewing the next set while one runs, and two sets are never worked on by
the PDF library at the same time in one process, which it does not allow.
The run reads its pages from the copy of the readings kept on disk
(:func:`pdf_processor.keep_ocr_on_disk`), so no page is read twice.
"""

from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path

from .mapping import MappingStore

# A set's results folder is named after the set; these never belong in a name.
_UNSAFE = re.compile(r'[\\/:*?"<>|\x00-\x1f]+')


def folder_name(name: str) -> str:
    cleaned = _UNSAFE.sub(" ", name).strip(" .") or "Set"
    return re.sub(r"\s+", " ", cleaned)[:80]


@dataclass
class DocSet:
    """One set: what a whole session used to be."""

    id: str
    name: str
    rank: int                                  # its place in the reading line
    folder: Path
    files: list[Path] = field(default_factory=list)
    relative: dict = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    store: MappingStore = field(default_factory=MappingStore)
    caption_names: list = field(default_factory=list)
    officials: list = field(default_factory=list)
    protection: object = None
    suggestions: list = field(default_factory=list)
    overlaps: list = field(default_factory=list)
    run_result: object = None
    # reading in the background: pages to read per file, and pages read
    scans: dict = field(default_factory=dict)
    read: set = field(default_factory=set)
    unsurveyed: int = 0                        # files not yet looked over for scans
    # the name screen worked out ahead, once the set is read
    prepare_wanted: bool = False
    preparing: bool = False
    prepared: dict | None = None
    prepared_for: frozenset = frozenset()
    prepare_failed_for: frozenset = frozenset()
    last_added: float = 0.0
    # the queued run
    run_state: str = ""                        # queued | running | done | failed
    run_message: str = ""
    run_pages: set = field(default_factory=set)
    run_pages_total: int = 0
    run_error: str = ""
    audit: dict | None = None                  # the final check's summary
    results: Path | None = None
    queued_at: float = 0.0
    lock: threading.Lock = field(default_factory=threading.Lock)

    def __post_init__(self) -> None:
        from . import officials

        if self.protection is None:
            self.protection = officials.Protection([], [], [])
        self.uploads.mkdir(parents=True, exist_ok=True)
        self.output.mkdir(parents=True, exist_ok=True)

    @property
    def uploads(self) -> Path:
        return self.folder / "uploads"

    @property
    def output(self) -> Path:
        return self.folder / "output"

    # ---------------------------------------------------------- reading --
    def reading_started(self, files) -> None:
        with self.lock:
            self.unsurveyed += sum(1 for f in files if Path(f).suffix.lower() == ".pdf")

    def scans_found(self, path: Path, count: int) -> None:
        with self.lock:
            self.scans[str(Path(path).resolve())] = count
            self.unsurveyed = max(0, self.unsurveyed - 1)

    def page_read(self, path: Path, index: int) -> None:
        with self.lock:
            self.read.add((str(Path(path).resolve()), index))

    def reading(self) -> tuple[int, int]:
        """(pages read, pages to read) of this set's scans."""
        with self.lock:
            total = sum(self.scans.values())
            return min(len(self.read), total), total

    def read_through(self) -> bool:
        done, total = self.reading()
        return done >= total and not self.unsurveyed

    # -------------------------------------------------------- preparing --
    def _file_set(self) -> frozenset:
        """The set's files as they are now: a file removed and another added
        under its name is a different file."""
        def key(f) -> tuple:
            path = Path(f).resolve()
            try:
                stat = path.stat()
                return (str(path), stat.st_size, stat.st_mtime_ns)
            except OSError:
                return (str(path), -1, -1)
        return frozenset(key(f) for f in self.files)

    def ready_prepared(self) -> dict | None:
        """The name screen worked out ahead - if it is for these very files."""
        prepared = self.prepared
        if prepared is not None and self.prepared_for == self._file_set():
            return prepared
        return None

    def should_prepare(self, settle: float) -> bool:
        files = self._file_set()
        return (self.prepare_wanted and bool(files) and not self.run_state
                and not self.preparing and self.read_through()
                and files != self.prepared_for and files != self.prepare_failed_for
                and time.time() - self.last_added >= settle)

    # ---------------------------------------------------------- summary --
    def summary(self) -> dict:
        done, total = self.reading()
        if self.run_state:
            state = self.run_state
        elif not self.files:
            state = "empty"
        elif done < total or self.unsurveyed:
            state = "reading"
        elif (self.prepare_wanted and self.ready_prepared() is None
              and self._file_set() != self.prepare_failed_for):
            state = "preparing"
        else:
            state = "ready"
        return {
            "id": self.id, "name": self.name, "state": state,
            "files": len(self.files),
            "reading": {"done": done, "total": total},
            "run": {"message": self.run_message, "error": self.run_error,
                    "done": min(len(self.run_pages), self.run_pages_total),
                    "total": self.run_pages_total,
                    "results": str(self.results) if self.results else "",
                    "failed": len(self.run_result.failed) if self.run_result else 0,
                    "files": len(self.run_result.outcomes) if self.run_result else 0,
                    "audit": self.audit or {}},
        }


# --------------------------------------------------------------------------
# the run, in a process of its own
# --------------------------------------------------------------------------


def _run_set(events, files, store, settings, output_dir, password, relative, skipped,
             spill_dir, kev_url=None, models=None) -> None:
    """Child side: one set's run, then its final check, reporting as it goes.

    The models live in the parent, one at a time (see models.py): the child
    asks for Kev once the run is done - ``("need", "kev")`` on ``events`` -
    and for the local model only if a page was flagged, and is answered on
    ``models`` with (address or None, note). ``("done_models",)`` lets go.
    Without ``models`` it uses ``kev_url`` as given and no local model."""
    # everything was read before the set was queued; whatever was not is read
    # in-line here rather than by a second pool of readers
    readers = os.environ.get("REDACTOR_OCR_WORKERS")
    os.environ["REDACTOR_OCR_WORKERS"] = "0"
    try:
        _own_group()
        from . import audit, llm_grouping, pdf_processor, pipeline

        pdf_processor.keep_ocr_on_disk(spill_dir)
        if store.merged or store.named_after:
            llm_grouping.settle(store)             # the model's merges, in their shape

        def progress(message: str, fraction: float) -> None:
            events.put(("progress", message, fraction))

        events.put(("total", pipeline.count_pages(files)))
        with pdf_processor.page_listener(lambda path, index: events.put(("page", path, index))):
            result = pipeline.run_job(files, store, settings, output_dir, password, progress,
                                      relative=relative, skipped=skipped)
        # the delivered pages were never read: a redacted scan's are, a few
        # at once, for the final check
        if readers is None:
            os.environ.pop("REDACTOR_OCR_WORKERS", None)
        else:
            os.environ["REDACTOR_OCR_WORKERS"] = readers
        try:
            kev_note = ""
            if models is not None and result.archive is not None:
                kev_url, kev_note = _need(events, models, "kev", progress)
            check = audit.check_archive(result.archive, store, settings, output_dir, kev_url,
                                        progress)
            if kev_note and not kev_url and check.path is not None:
                check.kev_note += f" ({kev_note})"
                audit._write(check)
            if check.flags:
                _read_flagged(events, models, check, result, store, settings, output_dir,
                              password, progress)
            events.put(("audit", check.summary()))
        except Exception as exc:                   # the run is delivered either way
            events.put(("audit", {"error": f"{type(exc).__name__}: {exc}"}))
        finally:
            if models is not None:
                events.put(("done_models",))
        events.put(("done", result))
    except BaseException as exc:                   # the parent marks the set failed
        events.put(("error", f"{type(exc).__name__}: {exc}"))
    finally:
        # A process started by multiprocessing waits, as it exits, for every
        # process it started - and the readers for the final check never stop
        # on their own: the run hung finished but unable to exit.
        from . import ocr_pool
        ocr_pool.shutdown(wait=True)


# How long a run waits for the parent to load a model: another step may be
# using the slot first (a review asking the local model), then the loading.
NEED_SECONDS = 3600


def _need(events, models, name: str, progress) -> tuple[str | None, str]:
    """Child side: the parent's address for model ``name``, or None and why."""
    import queue as queue_module

    progress("Starting Kev for the final check" if name == "kev"
             else "Starting the local model to read the flagged pages", 0.0)
    events.put(("need", name))
    try:
        url, note = models.get(timeout=NEED_SECONDS)
    except queue_module.Empty:
        return None, "it was not started in time"
    return url, note


def _read_flagged(events, models, check, result, store, settings, output_dir, password,
                  progress) -> None:
    """Child side: the local model reads the flagged pages, if there is one.
    Kev is stopped for it: the fixed pages are read back by the matcher and
    the local model."""
    from . import audit, mapping

    if models is None:
        return
    llm_url, note = _need(events, models, "llm", progress)
    if not llm_url:
        check.llm_note = note or "not running"
        audit._write(check)
        return
    fixed_store = audit.read_flagged(check, result, store, settings, output_dir, None,
                                     llm_url, progress)
    # the forms it found are in the mapping key too: they were replaced
    if fixed_store is not None and password and result.key_path:
        mapping.write_encrypted_key(fixed_store, result.key_path, password)


def _own_group() -> None:
    """Child side: a process group of its own, so the queue can stop it and
    everything it started at once - see :func:`_stop`. Windows has no
    process groups of this kind; there the child is stopped on its own."""
    if not hasattr(os, "setpgrp"):
        return
    try:
        os.setpgrp()
    except OSError:
        pass


def _next_event(events, child):
    """The child's next message, or None once it has exited with nothing more
    to say. A message put just before it exited is still read: one missed
    here marked a finished run failed."""
    import queue as queue_module

    while True:
        try:
            return events.get(timeout=1.0)
        except queue_module.Empty:
            if child.is_alive():
                continue
            try:
                return events.get(timeout=1.0)
            except queue_module.Empty:
                return None


def _stop(child, patience: float = 30) -> None:
    """Wait for a finished child to exit; then make sure nothing it started
    is left running. Stopping only the child orphaned its OCR readers."""
    import signal

    child.join(timeout=patience)
    if not hasattr(os, "killpg"):                  # Windows
        if child.is_alive():
            child.terminate()
            child.join(timeout=10)
        return
    for sig, grace in ((signal.SIGTERM, 10), (signal.SIGKILL, 5)):
        try:
            os.killpg(child.pid, sig)
        except (ProcessLookupError, PermissionError, OSError):
            break                                  # the group is gone: all clean
        child.join(timeout=grace)
        time.sleep(0.5)


def _prepare_set(events, files, settings, spill_dir) -> None:
    """Child side: everything the name screen shows, worked out ahead."""
    os.environ["REDACTOR_OCR_WORKERS"] = "0"
    try:
        _own_group()
        from . import officials, pdf_processor, pipeline

        pdf_processor.keep_ocr_on_disk(spill_dir)
        caption_names = pipeline.collect_caption_names(files)
        bench = pipeline.collect_officials(files)
        protection = officials.protected_terms(bench, avoid=officials.parties_of(caption_names))
        settings.protected_names = list(protection.terms)
        found, notes = pipeline.collect_suggestions(files, MappingStore(), settings,
                                                    caption_names=caption_names)
        events.put(("done", {"caption_names": caption_names, "officials": bench,
                             "protection": protection, "suggestions": found,
                             "notes": notes, "use_ner": settings.use_ner}))
    except BaseException as exc:
        events.put(("error", f"{type(exc).__name__}: {exc}"))


class Preparer:
    """Works out each read set's name screen, one set at a time, in order.

    Eleven sets dropped in at night were read by morning, but every one still
    had its captions, children, bench and suggestions to work out when it was
    opened - minutes each, at the keyboard. Done here, as each set's reading
    finishes, the name screen opens at once. A set that gains files after it
    was prepared is prepared again; SETTLE seconds of quiet first, so a
    folder still uploading is not prepared once per batch of files.
    """

    SETTLE = 10.0          # seconds without a new file before a set is prepared

    def __init__(self, spill_dir: Path, candidates, settings_for) -> None:
        self.spill_dir = spill_dir
        self.candidates = candidates
        self.settings_for = settings_for
        self.child = None
        self.stopping = False
        threading.Thread(target=self._loop, daemon=True, name="name-preparer").start()

    def stop_now(self) -> None:
        """The app is quitting: stop working any set out."""
        self.stopping = True
        child = self.child
        if child is not None and child.is_alive():
            _stop(child, patience=0)

    def _loop(self) -> None:
        while not self.stopping:
            try:
                chosen = next((s for s in self.candidates() if s.should_prepare(self.SETTLE)),
                              None)
                if chosen is not None:
                    self._prepare(chosen)
                    continue
            except Exception:
                pass
            time.sleep(2)

    def _prepare(self, doc_set: DocSet) -> None:
        import multiprocessing

        files = list(doc_set.files)
        covered = doc_set._file_set()
        doc_set.preparing = True
        try:
            context = multiprocessing.get_context("spawn")
            events = context.Queue()
            child = context.Process(target=_prepare_set, name=f"prepare-{doc_set.id}",
                                    args=(events, files, self.settings_for(doc_set),
                                          self.spill_dir))
            child.start()
            self.child = child
            outcome = _next_event(events, child) or \
                ("error", f"stopped (exit code {child.exitcode})")
            _stop(child)
            self.child = None
            if outcome[0] == "done":
                doc_set.prepared, doc_set.prepared_for = outcome[1], covered
            else:
                # opened, the set works its names out at the keyboard as before
                doc_set.prepare_failed_for = covered
        finally:
            doc_set.preparing = False


class RunQueue:
    """Queued sets, run one after another in the order they were queued."""

    def __init__(self, spill_dir: Path, on_change=None) -> None:
        self.spill_dir = spill_dir
        self.waiting: list[tuple[DocSet, object, str, Path]] = []
        self.running: DocSet | None = None
        self.wake = threading.Condition()
        self.on_change = on_change or (lambda: None)
        self.child = None
        self.stopping = False
        threading.Thread(target=self._loop, daemon=True, name="run-queue").start()

    # A run whose app quits under it must not carry on: the set's documents,
    # in the app's temporary folder, are deleted as it quits, and the run went
    # on for hours - invisible, every file left failing - into an archive that
    # looked finished. It is stopped instead, and its folder says so.
    INTERRUPTED = "RUN INTERRUPTED - NOT COMPLETE.txt"

    def stop_now(self) -> None:
        """The app is quitting: stop the running set, start no other."""
        with self.wake:
            self.stopping = True
            self.wake.notify_all()
            doc_set, child = self.running, self.child
        if child is None:
            return
        if child.is_alive():
            _stop(child, patience=0)
        if doc_set is not None and doc_set.results is not None and doc_set.run_state != "done":
            _mark_interrupted(doc_set.results, self.INTERRUPTED)

    def add(self, doc_set: DocSet, settings, password: str, results_root: Path) -> None:
        with self.wake:
            if doc_set.run_state in ("queued", "running"):
                return
            doc_set.run_state, doc_set.run_message, doc_set.run_error = "queued", "Waiting to run", ""
            doc_set.queued_at = time.time()
            self.waiting.append((doc_set, settings, password, results_root))
            self.wake.notify()
        self.on_change()

    def cancel(self, doc_set: DocSet) -> bool:
        with self.wake:
            for item in list(self.waiting):
                if item[0] is doc_set:
                    self.waiting.remove(item)
                    doc_set.run_state, doc_set.run_message = "", ""
                    return True
        return False

    def busy(self) -> bool:
        with self.wake:
            return bool(self.waiting) or self.running is not None

    def _loop(self) -> None:
        while True:
            with self.wake:
                while not self.waiting and not self.stopping:
                    self.wake.wait()
                if self.stopping:
                    return
                doc_set, settings, password, root = self.waiting.pop(0)
                self.running = doc_set
            try:
                self._run(doc_set, settings, password, root)
            except Exception as exc:               # never stop the queue for one set
                doc_set.run_state, doc_set.run_error = "failed", f"{type(exc).__name__}: {exc}"
            finally:
                with self.wake:
                    self.running = None
                self.on_change()

    READ_STALL = 600.0     # seconds a set's reading may stand still before a run goes on

    def _run(self, doc_set: DocSet, settings, password: str, root: Path) -> None:
        import multiprocessing

        results = _unique_folder(root, folder_name(doc_set.name))
        results.mkdir(parents=True)
        doc_set.results = results
        doc_set.run_state, doc_set.run_message = "running", "Starting"
        doc_set.run_pages = set()
        doc_set.run_pages_total = 0

        # A set queued before its reading finished waits for it: the run would
        # read the same pages again, beside the readers still at them. A
        # reading that stops moving for READ_STALL seconds is not waited for.
        last, moved = None, time.time()
        while not doc_set.read_through() and not self.stopping \
                and time.time() - moved < self.READ_STALL:
            now = doc_set.reading()
            if now != last:
                last, moved = now, time.time()
            doc_set.run_message = (f"Waiting for its reading to finish - page {now[0]:,} "
                                   f"of {now[1]:,}")
            time.sleep(1)
        doc_set.run_message = "Starting"
        if self.stopping:
            return
        context = multiprocessing.get_context("spawn")
        events = context.Queue()
        models = context.Queue()
        child = self._start_child(context, doc_set, settings, password, results, events,
                                  models)
        self.child = child
        turn = _Turn()
        try:
            result, error = self._follow(doc_set, events, child, models, turn)
        finally:
            _stop(child)
            turn.release()                         # the model may stop when idle
        self.child = None
        self._after_run(doc_set, result, error)

    def _start_child(self, context, doc_set: DocSet, settings, password: str, results: Path,
                     events, models):
        child = context.Process(
            target=_run_set, name=f"run-{doc_set.id}",
            args=(events, list(doc_set.files), doc_set.store, settings, results, password,
                  dict(doc_set.relative), list(doc_set.skipped), self.spill_dir, None,
                  models))
        child.start()
        return child

    def _follow(self, doc_set: DocSet, events, child, models, turn: "_Turn"):
        """The child's messages until it is done: (result, error)."""
        result = error = None
        while result is None and error is None:
            event = _next_event(events, child)
            if event is None:
                error = f"the run stopped without finishing (exit code {child.exitcode})"
                break
            kind = event[0]
            if kind == "need":
                models.put(turn.load(doc_set, event[1]))
            elif kind == "done_models":
                turn.release()                     # another step may have the slot
            elif kind == "total":
                doc_set.run_pages_total = event[1]
            elif kind == "progress":
                doc_set.run_message = event[1]
            elif kind == "page":
                doc_set.run_pages.add((event[1], event[2]))
            elif kind == "audit":
                doc_set.audit = event[1]
            elif kind == "done":
                result = event[1]
            elif kind == "error":
                error = event[1]
        return result, error

    def _after_run(self, doc_set: DocSet, result, error) -> None:
        from . import pdf_processor

        if error is not None:
            if self.stopping:
                error = "the app was closed while it ran - run it again"
            doc_set.run_state, doc_set.run_error = "failed", error
            doc_set.run_message = "Failed"
            return
        doc_set.run_result = result
        doc_set.run_state = "done"
        doc_set.run_message = (f"Finished - {len(result.failed)} file(s) refused"
                               if result.failed else "Finished")
        # the set's copies of the client's documents, and what was read off
        # them, are not kept a moment longer than its results need them
        pdf_processor.forget_ocr(doc_set.files)
        shutil.rmtree(doc_set.uploads, ignore_errors=True)


class _Turn:
    """Parent side: a run's turn at the model slot, taken when its child
    first needs a model and kept until it is done with them - Kev for the
    final check, then the local model for the flagged pages in its place."""

    def __init__(self) -> None:
        self.lease = None

    def load(self, doc_set: DocSet, name: str) -> tuple[str | None, str]:
        from . import models, ocr_pool

        if self.lease is None:
            def waiting() -> None:
                doc_set.run_message = ("Waiting for the models - another set's review is "
                                       "using the local model")
            self.lease = models.SLOT.acquire(on_wait=waiting)
        doc_set.run_message = ("Starting Kev for the final check" if name == "kev"
                               else "Starting the local model to read the flagged pages")
        # the readers ahead of the run are twelve gigabytes beside the model;
        # they start again when next wanted. Not while they read another set:
        # its reading would stop where it stood.
        if not ocr_pool.busy():
            ocr_pool.shutdown(wait=True)
        url = self.lease.load(name)
        return url, "" if url else models.SLOT.note(name)

    def release(self) -> None:
        lease, self.lease = self.lease, None
        if lease is not None:
            lease.release()


def _mark_interrupted(results: Path, marker: str) -> None:
    """Say, in the set's results folder, that its run did not finish - and
    take away the half-made documents, so nothing there passes for done."""
    try:
        for work in results.glob("_work-*"):
            shutil.rmtree(work, ignore_errors=True)
        (results / marker).write_text(
            "This set's run was stopped when the app was closed, before it finished.\n"
            "Nothing in this folder is complete. Run the set again.\n", encoding="utf-8")
    except OSError:
        pass


def _unique_folder(root: Path, name: str) -> Path:
    candidate, counter = root / name, 2
    while candidate.exists():
        candidate = root / f"{name} ({counter})"
        counter += 1
    return candidate


# --------------------------------------------------------------------------
# keeping the Mac awake
# --------------------------------------------------------------------------


class KeepAwake:
    """Hold off idle sleep while anything is being read, queued or run.

    An overnight queue is no use if the Mac sleeps at midnight. ``caffeinate
    -i`` stops idle sleep and nothing else - the screen still sleeps - and
    ``-w`` ties it to this process, so it can never outlive the app.
    """

    def __init__(self, busy) -> None:
        self.busy = busy
        self.process: subprocess.Popen | None = None
        if sys.platform == "darwin" and shutil.which("caffeinate"):
            threading.Thread(target=self._loop, daemon=True, name="keep-awake").start()

    def _loop(self) -> None:
        while True:
            try:
                wanted = bool(self.busy())
            except Exception:
                wanted = False
            if wanted and self.process is None:
                self.process = subprocess.Popen(["caffeinate", "-i", "-w", str(os.getpid())])
            elif not wanted and self.process is not None:
                self.process.terminate()
                self.process = None
            time.sleep(10)


def new_id() -> str:
    return uuid.uuid4().hex[:12]
