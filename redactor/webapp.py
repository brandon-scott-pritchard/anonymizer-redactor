"""The web front end's backend.

One local process serves the React app and wraps the same pipeline the Tk
GUI uses. Everything stays on this machine:

- the server binds 127.0.0.1 only, on a random port;
- every request must carry a token minted at startup (the launcher opens
  the browser with it once; after that it lives in a same-site cookie);
- uploaded documents and results live in a per-session temp directory.

Run with ``python -m redactor.webapp``.
"""

from __future__ import annotations

import atexit
import contextvars
import os
import re
import secrets
import shutil
import sys
import tempfile
import threading
import time
import uuid
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import (__version__, caption, categories, feedback, grouping, names as _names, ner,
               ocr_pool, officials, pdf_processor, pipeline, review, sets)
from .engine import Settings
from .mapping import MappingStore

def _static_dir() -> Path:
    """The React assets: inside the frozen bundle, or beside the source tree."""
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen and (Path(frozen) / "webapp" / "static").is_dir():
        return Path(frozen) / "webapp" / "static"
    return Path(__file__).resolve().parent.parent / "webapp" / "static"


STATIC_DIR = _static_dir()

TOKEN = secrets.token_urlsafe(32)
COOKIE = "anonymizer_token"


# --------------------------------------------------------------------------
# session and jobs
# --------------------------------------------------------------------------


@dataclass
class Job:
    status: str = "running"            # running | done | error
    message: str = "Starting"
    fraction: float = 0.0
    result: dict | None = None
    error: str = ""
    set_id: str = ""                   # the set the step works on
    # pages this step has read, of the pages it will read: the time is in
    # the pages, not the files, and a scanned page is the slow one
    pages_done: int = 0
    pages_total: int = 0


# Which set a request is about: the page names it on every call (the X-Set
# header, or ?set= on a download), and a job carries it onto its thread.
_current_set: contextvars.ContextVar[str | None] = contextvars.ContextVar("current_set",
                                                                          default=None)


class _PerSet:
    """A Session attribute that is really the current set's."""

    def __set_name__(self, owner, name: str) -> None:
        self.name = name

    def __get__(self, session, owner=None):
        if session is None:
            return self
        return getattr(session.current, self.name)

    def __set__(self, session, value) -> None:
        setattr(session.current, self.name, value)


# What the run does with each document, set once for every set in the
# window: a night's cases are run the same way, and a setting changed in one
# set and not the next was a difference nobody would see until a set came
# back redacted when it should have been anonymized.
DEFAULT_OPTIONS = {
    "docx_mode": "anonymize", "pdf_mode": "redact", "metadata": True, "comments": True,
    "embedded": True, "filenames": True, "images": True, "ocr": True, "ner": True,
    "labels": False, "allowlist": "",
}
_MODES = {"docx_mode": ("anonymize", "redact"), "pdf_mode": ("redact", "anonymize")}


class Session:
    """One operator, any number of sets - see :mod:`redactor.sets`.

    Everything a set owns (its files, names, review list and results) is
    reached through the session as before - ``SESSION.files``,
    ``SESSION.store`` - and means the set the request is about.
    """

    uploads = _PerSet()
    output = _PerSet()
    files = _PerSet()
    relative = _PerSet()
    skipped = _PerSet()
    store = _PerSet()
    caption_names = _PerSet()
    officials = _PerSet()
    protection = _PerSet()
    suggestions = _PerSet()
    overlaps = _PerSet()
    run_result = _PerSet()

    def __init__(self) -> None:
        self.workdir = Path(tempfile.mkdtemp(prefix="anonymizer-web-"))
        # the uploads are the client's documents, unredacted, and the readings
        # kept beside them are their text: gone when the app is, not left in
        # the temp folder
        atexit.register(shutil.rmtree, self.workdir, True)
        self.ocr_dir = self.workdir / "ocr"
        pdf_processor.keep_ocr_on_disk(self.ocr_dir)
        self.sets: dict[str, sets.DocSet] = {}
        self.selected = self.new_set().id
        self.options: dict = dict(DEFAULT_OPTIONS)        # every set's, see above
        self.results_root: Path | None = None
        self.jobs: dict[str, Job] = {}
        self.busy = threading.Lock()
        self.running_job: Job | None = None
        self._starting = threading.Lock()
        self.preparer: sets.Preparer | None = None
        self.queue: sets.RunQueue | None = None
        self.awake: sets.KeepAwake | None = None

    def new_set(self, name: str = "") -> sets.DocSet:
        rank = max((s.rank for s in self.sets.values()), default=0) + 1
        set_id = sets.new_id()
        doc_set = sets.DocSet(id=set_id, name=name.strip() or f"Set {rank}", rank=rank,
                              folder=self.workdir / "sets" / set_id)
        self.sets[set_id] = doc_set
        return doc_set

    @property
    def current(self) -> sets.DocSet:
        """The set the request or job is about. One that names a set since
        removed is refused - never handed another set's names to write over."""
        chosen = _current_set.get()
        if chosen is not None:
            found = self.sets.get(chosen)
            if found is None:
                raise HTTPException(404, "That set has been removed.")
            return found
        return self.sets.get(self.selected) or next(iter(self.sets.values()))

    def run_queue(self) -> sets.RunQueue:
        """Started on first use: a session that never queues never runs a thread."""
        with self._starting:
            if self.queue is None:
                self.queue = sets.RunQueue(self.ocr_dir)
                self.preparer = sets.Preparer(
                    self.ocr_dir, lambda: sorted(self.sets.values(), key=lambda s: s.rank),
                    # with the options the run will use: worked out with the
                    # model on and opened with it off, the screen is scanned again
                    lambda doc_set: _settings(self.options))
                self.awake = sets.KeepAwake(self.anything_going)
                # registered after the temporary folder's removal, so run
                # first: the run and the preparing are stopped before their
                # documents are deleted under them
                atexit.register(self.preparer.stop_now)
                atexit.register(self.queue.stop_now)
        return self.queue

    def anything_going(self) -> bool:
        if self.queue is not None and self.queue.busy():
            return True
        return any(s.summary()["state"] in ("reading", "preparing") for s in self.sets.values())


SESSION = Session()


def _start_job(work, pages=None) -> str:
    """Run ``work(progress)`` on a thread; the browser polls the job.

    ``pages`` says how many pages the step will read - counted on the job's
    thread, where the PDFs are opened; each page read is counted against it as
    it happens. The job works on the set the request was about, whichever set
    the page shows by the time it finishes.
    """
    if not SESSION.busy.acquire(blocking=False):
        other = SESSION.running_job
        owner = SESSION.sets.get(other.set_id) if other is not None else None
        here = _current_set.get() or SESSION.selected
        if owner is not None and owner.id != here:
            raise HTTPException(409, f'"{owner.name}" is still working on a step - '
                                     "try again when it finishes.")
        raise HTTPException(409, "Still working on the previous step.")
    try:
        set_id = SESSION.current.id
    except HTTPException:
        SESSION.busy.release()
        raise
    job_id = uuid.uuid4().hex
    job = SESSION.jobs[job_id] = Job(set_id=set_id)
    SESSION.running_job = job
    context = contextvars.copy_context()
    seen: set[tuple[str, int]] = set()

    def progress(message: str, fraction: float) -> None:
        job.message, job.fraction = message, fraction

    def page_read(path: str, index: int) -> None:
        seen.add((path, index))
        job.pages_done = min(len(seen), job.pages_total) if job.pages_total else len(seen)

    def runner() -> None:
        try:
            if pages is not None:
                job.pages_total = pages()
            with pdf_processor.page_listener(page_read):
                job.result = work(progress)
            job.status = "done"
        except Exception as exc:                   # surfaced to the operator
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            SESSION.running_job = None
            SESSION.busy.release()

    threading.Thread(target=lambda: context.run(runner), daemon=True).start()
    return job_id


def _options(body: dict) -> dict:
    """The options a request runs with: the ones it sends, or else the
    window's shared ones (``PUT /api/options``)."""
    given = body.get("options")
    return given if isinstance(given, dict) else SESSION.options


def _settings(options: dict) -> Settings:
    extra = [line.strip() for line in options.get("allowlist", "").splitlines()
             if line.strip()]
    return Settings(
        docx_mode=options.get("docx_mode", "anonymize"),
        # anything but an explicit "anonymize" redacts: the safe default
        pdf_mode="anonymize" if options.get("pdf_mode") == "anonymize" else "redact",
        use_ner=bool(options.get("ner", True)),
        extra_allowlist=extra,
        protected_names=list(SESSION.protection.terms),
        scrub_metadata=bool(options.get("metadata", True)),
        scrub_comments=bool(options.get("comments", True)),
        scrub_embedded=bool(options.get("embedded", True)),
        anonymize_filenames=bool(options.get("filenames", True)),
        label_redaction_boxes=bool(options.get("labels", False)),
        ocr_scanned_pdfs=bool(options.get("ocr", True)),
        redact_images=bool(options.get("images", True)),
    )


def _name_entries(lines: list[dict]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for line in lines:
        name = (line.get("name") or "").strip()
        if name:
            out.append((name, line.get("category", "person")))
    return out


def _reprotect(entries: list[tuple[str, str]]) -> list[str]:
    """Rebuild the do-not-change list against the current party names."""
    SESSION.protection = officials.protected_terms(
        SESSION.officials, avoid=[name for name, _category in entries]
        + officials.parties_of(SESSION.caption_names))
    return list(SESSION.protection.terms)


def _prepare(body: dict) -> tuple[Settings, MappingStore, list[tuple[str, str]]]:
    """Settings and a store for one request, do-not-change list refreshed.

    The judicial protection list depends on the party names - a judge sharing a
    surname with a client gets the full name shielded and nothing more - so it
    is rebuilt whenever the name list changes, not once when the files landed.
    """
    entries = _name_entries(body.get("names", []))
    _reprotect(entries)
    settings = _settings(_options(body))
    roles = {c.name.casefold(): c.role for c in SESSION.caption_names}
    store, overlaps = review.build_store(entries, roles)
    SESSION.overlaps = overlaps
    return settings, store, entries


def _guard_payload() -> dict:
    """What the run will leave alone, and which names were folded together."""
    protection = SESSION.protection
    return {
        "officials": [{"name": o.name, "title": o.title,
                       "confidence": o.confidence, "source": o.source}
                      for o in SESSION.officials],
        "protected": list(protection.terms),
        "guard_notes": protection.notes(),
        "overlaps": [{"inner": o.inner, "outer": o.outer,
                      "merge": o.merge, "note": o.note}
                     for o in SESSION.overlaps],
    }


def _entity_row(entity, store=None) -> dict:
    row = {
        "key": entity.key,
        "category": entity.category,
        "label": entity.label,
        "canonical": entity.canonical,
        "replacement": entity.replacement,
        "occurrences": entity.occurrences,
        "source": entity.source,
        "enabled": entity.enabled,
        "is_person": entity.is_person,
        "parent": entity.parent,
        "note": review.review_note(entity),
    }
    if store is not None and entity.parent is None:
        # a head row: its forms ride along, and Times counts all of them
        row["forms"] = [_entity_row(e) for e in grouping.members(store, entity)]
        row["occurrences"] = grouping.group_total(store, entity)
    return row


def _entities_payload() -> dict:
    store = SESSION.store
    heads = sorted((e for e in store.entities.values() if e.parent is None),
                   key=lambda e: (0 if e.is_person else 1, e.category,
                                  -grouping.group_total(store, e), e.canonical.casefold()))
    risky = sorted({v.text for e in store.persons() for v in e.variants if v.risky})
    unused = sum(1 for e in store.entities.values() if e.occurrences == 0)
    return {"entities": [_entity_row(e, store) for e in heads],
            "risky": risky[:12], "unused": unused,
            "active": sum(1 for e in store.entities.values() if e.enabled),
            "instructions": review.REVIEW_INSTRUCTIONS}


# --------------------------------------------------------------------------
# app and auth
# --------------------------------------------------------------------------


app = FastAPI(title="Document Redactions & Anonymization", version=__version__)


@app.middleware("http")
async def _require_token(request: Request, call_next):
    if request.url.path == "/" or request.url.path.startswith("/assets/"):
        return await call_next(request)
    supplied = request.cookies.get(COOKIE) or request.headers.get("x-auth-token")
    # A download is a plain navigation, so it carries neither our header nor,
    # in a web view, reliably even the cookie: WKWebView hands the transfer to
    # a separate download task and the same-site cookie does not always come
    # with it. What got saved was this function's 401 body under the name
    # "anonymized-….zip", which opens as "unsupported format" - the download
    # appeared to work and the file was four hundred bytes of JSON.
    #
    # So the download routes accept the token on the query string, exactly as
    # "/" already does. The server is bound to 127.0.0.1, the token is random
    # per process, and the alternative is a button that silently saves an
    # error page.
    if not secrets.compare_digest(supplied or "", TOKEN) \
            and request.url.path.startswith("/api/download/"):
        supplied = request.query_params.get("token", "")
    if not secrets.compare_digest(supplied or "", TOKEN):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    chosen = request.headers.get("x-set") or request.query_params.get("set")
    if chosen and chosen not in SESSION.sets:
        return JSONResponse({"detail": "That set has been removed."}, status_code=404)
    marker = _current_set.set(chosen)
    try:
        return await call_next(request)
    finally:
        _current_set.reset(marker)


@app.get("/")
def index(request: Request):
    token = request.query_params.get("token", "")
    cookie_ok = secrets.compare_digest(request.cookies.get(COOKIE, ""), TOKEN)
    if not cookie_ok and not secrets.compare_digest(token, TOKEN):
        raise HTTPException(401, "Open the app from its launcher.")
    response = FileResponse(STATIC_DIR / "index.html")
    if not cookie_ok:
        response.set_cookie(COOKIE, TOKEN, httponly=True, samesite="strict")
    return response


# --------------------------------------------------------------------------
# files
# --------------------------------------------------------------------------


@app.get("/api/state")
def state():
    ocr_ok, ocr_note = pipeline.pdf_processor.ocr_available()
    return {
        "version": __version__,
        **_files_payload(),
        "categories": [{"key": c.key, "label": c.label, "group": c.group}
                       for c in categories.CATEGORIES],
        "name_categories": ["person", "minor", "organization", "location"],
        "error_types": list(feedback.ERROR_TYPES),
        "ocr": {"ok": ocr_ok, "note": ocr_note},
        # so the results screen can put it on the download links; see the
        # middleware above for why a cookie is not enough
        "download_token": TOKEN,
        "kev": _kev_note(),
        "options": dict(SESSION.options),
        **_sets_payload(),
    }


@app.get("/api/options")
def get_options():
    return dict(SESSION.options)


@app.put("/api/options")
async def put_options(request: Request):
    """The one set of options every set is prepared and run with. Unknown
    keys are dropped and a mode is one of its two values, so a typo cannot
    reach a run as an option nobody chose."""
    body = await request.json()
    if not isinstance(body, dict):
        raise HTTPException(422, "Options are a JSON object.")
    merged = dict(SESSION.options)
    for key, value in body.items():
        if key not in DEFAULT_OPTIONS:
            continue
        if key in _MODES:
            if value not in _MODES[key]:
                raise HTTPException(422, f"{key} is one of: {', '.join(_MODES[key])}.")
            merged[key] = value
        elif key == "allowlist":
            merged[key] = str(value or "")
        else:
            merged[key] = bool(value)
    SESSION.options = merged
    return dict(merged)


def _files_payload() -> dict:
    def kind(path: Path) -> str | None:
        shown = SESSION.relative.get(path, path.name)
        return "photo" if pipeline.classify(Path(shown)) == "image" else pipeline.classify(path)
    return {"files": [{"name": SESSION.relative.get(p, p.name), "kind": kind(p)}
                      for p in SESSION.files],
            "skipped": list(SESSION.skipped)}


def _safe_relative(raw: str) -> str | None:
    """A browser-supplied path inside a dropped folder, or None if it is not one.

    The path names where the file goes in the upload area, so nothing in it may
    climb out: no "..", no absolute root, no drive letter.
    """
    parts = [part for part in re.split(r"[\\/]+", raw or "") if part not in ("", ".")]
    if not parts or any(part == ".." or "\0" in part for part in parts):
        return None
    if os.name == "nt" and any(":" in part for part in parts):
        return None
    return "/".join(parts)


def _keep(relative: str, write) -> Path | None:
    """Store one document under its relative path; ``write(target)`` fills it.

    A photo is made into a PDF here, once, so every later step reads it as a
    scan; the name shown and reported stays the one it came in with.
    """
    target = SESSION.uploads.joinpath(*relative.split("/"))
    counter = 1
    while target.exists():
        stem, suffix = Path(relative).stem, Path(relative).suffix
        target = target.with_name(f"{stem}-{counter}{suffix}")
        counter += 1
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        write(target)
    except OSError as exc:
        # "Case/Report.docx/b.docx" after a file "Case/Report.docx": the
        # one document is reported, the rest of the batch still goes in
        SESSION.skipped.append(f"{relative} (could not be stored: {exc.strerror or exc})")
        return None
    if pipeline.classify(target) == "image":
        try:
            converted = pipeline.as_document(target, target.parent)
        except Exception:
            SESSION.skipped.append(f"{relative} (could not be read as an image)")
            return None
        finally:
            target.unlink(missing_ok=True)
        target = converted
    SESSION.files.append(target)
    SESSION.relative[target] = relative
    return target


@app.get("/api/files")
def list_files():
    """The set's documents - for a page opened again over sets already filled."""
    return _files_payload()


@app.post("/api/files")
async def upload(files: list[UploadFile] = File(default=[]),
                 paths: list[str] = Form(default=[]),
                 skipped: list[str] = Form(default=[])):
    """Documents, each with its place in a dropped folder when it came in one.

    ``paths[i]`` is ``files[i]``'s path inside what was dropped; ``skipped``
    names the files the page did not send because they are not documents or
    photos, so the report can list them.
    """
    _not_while_run("given more documents")
    added: list[Path] = []
    for index, item in enumerate(files):
        given = paths[index] if index < len(paths) else ""
        relative = _safe_relative(given) or Path(item.filename or "upload").name
        name = relative.rsplit("/", 1)[-1]
        if pipeline._housekeeping(name):
            continue
        if pipeline.classify(Path(name)) is None:
            SESSION.skipped.append(relative)
            continue
        data = await item.read()
        kept = _keep(relative, lambda target: target.write_bytes(data))
        if kept is not None:
            added.append(kept)
    for name in skipped:
        relative = _safe_relative(name)
        if relative and not pipeline._housekeeping(relative.rsplit("/", 1)[-1]):
            SESSION.skipped.append(relative)
    # the scans start being read now, while the operator sets the options and
    # types the names, rather than when the first step asks for them
    _read_in_background(added)
    return {"added": len(added), **_files_payload()}


def add_paths(paths: list[str | Path], set_id: str | None = None) -> dict:
    """Add files and whole folders straight from disk - the app window's
    "Choose folder" button, which has no browser in the way to upload through.
    The documents are copied into the session; the originals are not touched.
    ``set_id`` names the set whose button it was: every set's card has one,
    and the folder goes to that set, not to whichever was made last.
    """
    if set_id:
        if set_id not in SESSION.sets:
            raise HTTPException(404, "That set has been removed.")
        marker = _current_set.set(set_id)
        try:
            return add_paths(paths)
        finally:
            _current_set.reset(marker)
    _not_while_run("given more documents")
    gathered = pipeline.gather([Path(p) for p in paths])
    added: list[Path] = []
    for source, relative in gathered.files:
        kept = _keep(relative, lambda target, source=source: shutil.copyfile(source, target))
        if kept is not None:
            added.append(kept)
    SESSION.skipped.extend(gathered.skipped)
    _read_in_background(added)
    return {"added": len(added), **_files_payload()}


def _not_while_run(what: str = "changed") -> None:
    """A running set's files are being read, and a finished set's are gone:
    neither is changed under it. A queued one may be - the run takes the
    set as it is when the run starts."""
    state = SESSION.current.run_state
    if state == "running":
        raise HTTPException(409, f"This set is running - it cannot be {what} now.")
    if state == "done":
        raise HTTPException(409, f"This set has been run - it cannot be {what}. "
                                 "Make a new set to run these documents again.")


def _read_in_background(added: list[Path]) -> None:
    """Queue the set's scans for reading, behind every set made before it."""
    doc_set = SESSION.current
    if not added or ocr_pool.worker_count() < 1:
        return
    doc_set.reading_started(added)
    doc_set.last_added = time.time()
    if ocr_pool.read_ahead_in_background(added, rank=doc_set.rank, on_page=doc_set.page_read,
                                         on_scans=doc_set.scans_found):
        # once read, its name screen is worked out ahead too - see sets.Preparer
        doc_set.prepare_wanted = True
        SESSION.run_queue()                  # keeps the Mac awake while it reads
    else:
        doc_set.unsurveyed = 0               # nothing reads ahead: the steps read in-line


@app.delete("/api/files/{index}")
def remove_file(index: int):
    _not_while_run()
    if not 0 <= index < len(SESSION.files):
        raise HTTPException(404, "no such file")
    path = SESSION.files.pop(index)
    SESSION.relative.pop(path, None)
    path.unlink(missing_ok=True)
    return _files_payload()


@app.delete("/api/files")
def remove_all_files():
    _not_while_run()
    for path in SESSION.files:
        path.unlink(missing_ok=True)
    SESSION.files.clear()
    SESSION.relative.clear()
    SESSION.skipped.clear()
    return _files_payload()


# --------------------------------------------------------------------------
# names
# --------------------------------------------------------------------------


@app.post("/api/captions")
def captions():
    files = list(SESSION.files)
    prepared = SESSION.current.ready_prepared()

    def work(progress):
        if prepared is not None:
            # worked out ahead, as soon as the set was read
            found = prepared["caption_names"]
            SESSION.caption_names = found
            SESSION.officials = prepared["officials"]
            SESSION.protection = prepared["protection"]
        else:
            found = pipeline.collect_caption_names(files, progress)
            SESSION.caption_names = found
            # the bench, harvested at the same time: their names are shielded
            # for the whole run rather than proposed as parties
            SESSION.officials = pipeline.collect_officials(files, progress)
            SESSION.protection = officials.protected_terms(
                SESSION.officials, avoid=officials.parties_of(found))
        return {"captions": [
            {"name": c.name, "role": c.role, "confidence": c.confidence,
             "source": c.source, "category": c.category} for c in found],
            **_guard_payload()}

    # the children and the bench are looked for on every page, so this step
    # reads them all and counts them all - unless they were read ahead
    return {"job": _start_job(work, None if prepared else lambda: pipeline.count_pages(files))}


@app.post("/api/suggestions")
async def suggestions(request: Request):
    body = await request.json()
    files = list(SESSION.files)
    settings, store, _entries = _prepare(body)
    captions_now = list(SESSION.caption_names)
    prepared = SESSION.current.ready_prepared()
    if prepared is not None and prepared["use_ner"] != settings.use_ner:
        prepared = None                      # worked out with the model on, or off

    def work(progress):
        if prepared is not None:
            found = pipeline.refine_suggestions(prepared["suggestions"], store, settings,
                                                captions_now)
            notes = list(prepared["notes"])
        else:
            found, notes = pipeline.collect_suggestions(
                files, store, settings, progress, caption_names=captions_now)
        SESSION.suggestions = found
        # the words of the names already listed: a suggestion sharing one is
        # one of them, or their business
        known = frozenset(
            w for name in [*(e[0] for e in _entries), *(c.name for c in captions_now)]
            for w in (ner._plain(t) for t in name.split()) if len(w) > 2)
        return {"suggestions": [
            {"text": s.text, "category": s.category, "count": s.count,
             "documents": sorted(s.documents), "nickname_for": s.nickname_for,
             "likely": bool(s.nickname_for) or ner.likely_name(s.text, s.category, known)}
            for s in found],
            "notes": notes, **_guard_payload()}

    return {"job": _start_job(work, None if prepared else lambda: pipeline.count_pages(files))}


# --------------------------------------------------------------------------
# review
# --------------------------------------------------------------------------


@app.post("/api/review")
async def build_review(request: Request):
    _not_while_run()
    body = await request.json()
    files = list(SESSION.files)
    carryover = review.snapshot_decisions(SESSION.store)
    settings, store, entries = _prepare(body)

    def work(progress):
        # reaching review without ever opening the name screen would otherwise
        # run with nothing shielded at all
        if not SESSION.officials:
            SESSION.officials = pipeline.collect_officials(files, progress)
            settings.protected_names = _reprotect(entries)
        pipeline.prescan(files, store, settings, progress)
        review.carry_decisions(store, carryover)
        SESSION.store = store
        return {**_entities_payload(), **_guard_payload()}

    return {"job": _start_job(work, lambda: pipeline.count_pages(files))}


@app.patch("/api/entities")
async def edit_entity(request: Request):
    _not_while_run()
    body = await request.json()
    entity = SESSION.store.entities.get(body.get("key", ""))
    if entity is None:
        raise HTTPException(404, "no such entity")
    if "enabled" in body:
        grouping.set_enabled(SESSION.store, entity, bool(body["enabled"]))
    if body.get("category") and body["category"] != entity.category:
        moved = review.retype_entity(SESSION.store, entity, body["category"])
        if moved is None:
            raise HTTPException(422, "That value cannot be registered under that type.")
        entity = moved
    if body.get("replacement") is not None:
        replacement = str(body["replacement"])
        if not replacement.strip():
            raise HTTPException(422, "The replacement cannot be empty.")
        review.set_replacement(entity, replacement, SESSION.store)
    return _entities_payload()


@app.post("/api/entities/split")
async def split_entity(request: Request):
    """Take one form out of its group: it is not the same thing."""
    _not_while_run()
    body = await request.json()
    entity = SESSION.store.entities.get(body.get("key", ""))
    if entity is None:
        raise HTTPException(404, "no such entity")
    grouping.split(SESSION.store, entity)
    return _entities_payload()


@app.post("/api/entities/all")
async def set_all(request: Request):
    _not_while_run()
    body = await request.json()
    enabled = bool(body.get("enabled", True))
    for entity in SESSION.store.entities.values():
        entity.enabled = enabled
    return _entities_payload()


# --------------------------------------------------------------------------
# feedback
# --------------------------------------------------------------------------


@app.post("/api/feedback")
async def report_error(request: Request):
    body = await request.json()
    report = feedback.build_report(
        error_type=str(body.get("error_type", "Other")),
        text=str(body.get("text", "")),
        predicted_category=str(body.get("predicted_category", "")),
        corrected_category=str(body.get("corrected_category", "")),
        corrected_replacement=body.get("corrected_replacement"),
        source=str(body.get("source", "")),
        occurrences=int(body.get("occurrences", 0)),
        documents=[str(d) for d in body.get("documents", [])],
        origin=str(body.get("origin", "review")),
    )
    path = feedback.log_report(report)
    return {"logged_to": str(path), "mailto": feedback.mailto_url(report),
            "address": feedback.REPORT_ADDRESS}


# --------------------------------------------------------------------------
# run
# --------------------------------------------------------------------------


@app.post("/api/run")
async def run(request: Request):
    body = await request.json()
    password = str(body.get("password", ""))
    if not password.strip():
        raise HTTPException(422, "Set a mapping-key password first.")
    files = list(SESSION.files)
    if not files:
        raise HTTPException(422, "Add at least one document first.")
    settings = _settings(_options(body))
    store = SESSION.store

    def work(progress):
        result = pipeline.run_job(files, store, settings,
                                  SESSION.output, password, progress,
                                  relative=dict(SESSION.relative),
                                  skipped=list(SESSION.skipped))
        SESSION.run_result = result
        outcomes = [{
            "source": o.shown, "status": o.status,
            "delivered": o.delivered_name, "hits": o.hits,
            "warnings": o.warnings, "error": o.error,
        } for o in result.outcomes]
        return {
            "outcomes": outcomes,
            "archive": bool(result.archive),
            "key": bool(result.key_path),
            "report": bool(result.report_path),
            "failed": len(result.failed),
            "skipped": len(result.skipped),
        }

    return {"job": _start_job(work, lambda: pipeline.count_pages(files))}


# --------------------------------------------------------------------------
# sets and the run queue
# --------------------------------------------------------------------------


def _kev_note() -> str:
    from . import audit
    if audit.KEV.note.startswith("running") and audit.KEV.url is None:
        return "stopped unexpectedly - the final check uses the name matcher alone"
    return audit.KEV.note


def _sets_payload() -> dict:
    return {"sets": [s.summary() for s in sorted(SESSION.sets.values(), key=lambda s: s.rank)],
            "selected": SESSION.selected,
            "results_root": str(SESSION.results_root) if SESSION.results_root else "",
            "kev": _kev_note()}


def _find_set(set_id: str) -> sets.DocSet:
    found = SESSION.sets.get(set_id)
    if found is None:
        raise HTTPException(404, "no such set")
    return found


@app.get("/api/sets")
def list_sets():
    return _sets_payload()


@app.post("/api/sets")
async def create_set(request: Request):
    body = await request.json() if await request.body() else {}
    doc_set = SESSION.new_set(str(body.get("name", "")))
    SESSION.selected = doc_set.id
    return _sets_payload()


@app.post("/api/sets/{set_id}/select")
def select_set(set_id: str):
    SESSION.selected = _find_set(set_id).id
    return _sets_payload()


@app.patch("/api/sets/{set_id}")
async def rename_set(set_id: str, request: Request):
    doc_set = _find_set(set_id)
    name = str((await request.json()).get("name", "")).strip()
    if name:
        doc_set.name = name[:120]
    return _sets_payload()


@app.delete("/api/sets/{set_id}")
def remove_set(set_id: str):
    doc_set = _find_set(set_id)
    if doc_set.run_state in ("queued", "running"):
        raise HTTPException(409, "Take it out of the queue first - or wait for it to finish.")
    job = SESSION.running_job
    if job is not None and job.set_id == set_id:
        raise HTTPException(409, "This set's step is still working - wait for it to finish.")
    pdf_processor.forget_ocr(doc_set.files)
    shutil.rmtree(doc_set.folder, ignore_errors=True)
    del SESSION.sets[set_id]
    if not SESSION.sets:
        SESSION.new_set()
    if SESSION.selected not in SESSION.sets:
        SESSION.selected = next(iter(SESSION.sets))
    return _sets_payload()


def set_results_root(path: str | Path) -> dict:
    """Where every set's results go, a folder per set. The app window's
    folder panel calls this; the page posts a typed path in a browser."""
    root = Path(path).expanduser()
    if not str(path).strip() or not root.is_absolute():
        raise HTTPException(422, "Give the whole path of the results folder.")
    if not root.is_dir():
        raise HTTPException(422, f"{root} is not a folder.")
    # found out now, not by every set failing one after another overnight
    try:
        probe = Path(tempfile.mkdtemp(prefix=".redactor-write-test-", dir=root))
        probe.rmdir()
    except OSError as exc:
        raise HTTPException(422, f"Cannot write to {root}: {exc.strerror or exc}.")
    SESSION.results_root = root.resolve()
    return _sets_payload()


@app.post("/api/results-folder")
async def results_folder(request: Request):
    return set_results_root(str((await request.json()).get("path", "")))


@app.post("/api/queue")
async def queue_run(request: Request):
    """Queue this set's run. It runs when every set queued before it has, and
    writes its archive, mapping key and report to its own folder."""
    body = await request.json()
    password = str(body.get("password", ""))
    if not password.strip():
        raise HTTPException(422, "Set a mapping-key password first.")
    if SESSION.results_root is None:
        raise HTTPException(422, "Choose the folder the results go to first.")
    doc_set = SESSION.current
    if doc_set.run_state in ("queued", "running"):
        raise HTTPException(409, "This set is already in the queue.")
    _not_while_run("queued again")
    if not doc_set.files:
        raise HTTPException(422, "Add at least one document first.")
    if not doc_set.store.entities:
        raise HTTPException(422, "Build the review list first - the run uses what it says.")
    settings = _settings(_options(body))
    SESSION.run_queue().add(doc_set, settings, password, SESSION.results_root)
    return _sets_payload()


@app.post("/api/queue/cancel")
def cancel_run():
    doc_set = SESSION.current
    if not SESSION.run_queue().cancel(doc_set):
        raise HTTPException(409, "Only a set still waiting in the queue can be taken out.")
    return _sets_payload()


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = SESSION.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "no such job")
    return {"status": job.status, "message": job.message,
            "fraction": job.fraction, "result": job.result, "error": job.error,
            "pages_done": job.pages_done, "pages_total": job.pages_total}


@app.get("/api/download/{kind}")
def download(kind: str):
    result = SESSION.run_result
    if result is None:
        raise HTTPException(404, "run first")
    target = {"archive": result.archive, "key": result.key_path,
              "report": result.report_path}.get(kind)
    if target is None or not Path(target).exists():
        raise HTTPException(404, "not produced by this run")
    return FileResponse(target, filename=Path(target).name,
                        media_type="application/octet-stream")


app.mount("/assets", StaticFiles(directory=STATIC_DIR), name="assets")


# --------------------------------------------------------------------------
# launcher
# --------------------------------------------------------------------------


def main() -> int:
    import socket

    import uvicorn

    argv = sys.argv[1:]
    port = 0
    if "--port" in argv:
        try:
            port = int(argv[argv.index("--port") + 1])
        except (IndexError, ValueError):
            port = 0
    if not port:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

    from . import audit

    audit.KEV.start()                     # the final check's model; stopped on exit
    url = f"http://127.0.0.1:{port}/?token={TOKEN}"
    # flush: the frozen app is windowed, so stdout is block-buffered when
    # redirected and the URL would otherwise never reach a log file
    print(f"Document Redactions & Anonymization {__version__}", flush=True)
    print(f"Opening {url}", flush=True)
    print("Everything stays on this computer. Quit the app to stop it.", flush=True)
    if "--no-browser" not in argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    uvicorn.run(app, host="127.0.0.1", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
