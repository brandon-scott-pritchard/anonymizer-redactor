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

import secrets
import sys
import tempfile
import threading
import uuid
import webbrowser
from dataclasses import dataclass, field
from pathlib import Path

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import (__version__, caption, categories, feedback, names as _names, ner,
               officials, pipeline, review)
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


class Session:
    """One operator, one batch. The web app is single-user by design."""

    def __init__(self) -> None:
        self.workdir = Path(tempfile.mkdtemp(prefix="anonymizer-web-"))
        self.uploads = self.workdir / "uploads"
        self.output = self.workdir / "output"
        self.uploads.mkdir()
        self.output.mkdir()
        self.files: list[Path] = []
        self.store = MappingStore()
        self.caption_names: list[caption.CaptionName] = []
        self.officials: list[officials.Official] = []
        self.protection = officials.Protection([], [], [])
        self.suggestions: list[ner.Suggestion] = []
        self.overlaps: list[_names.Overlap] = []
        self.run_result: pipeline.RunResult | None = None
        self.jobs: dict[str, Job] = {}
        self.busy = threading.Lock()


SESSION = Session()


def _start_job(work) -> str:
    """Run ``work(progress)`` on a thread; the browser polls the job."""
    if not SESSION.busy.acquire(blocking=False):
        raise HTTPException(409, "Still working on the previous step.")
    job_id = uuid.uuid4().hex
    job = SESSION.jobs[job_id] = Job()

    def progress(message: str, fraction: float) -> None:
        job.message, job.fraction = message, fraction

    def runner() -> None:
        try:
            job.result = work(progress)
            job.status = "done"
        except Exception as exc:                   # surfaced to the operator
            job.status = "error"
            job.error = f"{type(exc).__name__}: {exc}"
        finally:
            SESSION.busy.release()

    threading.Thread(target=runner, daemon=True).start()
    return job_id


def _settings(options: dict) -> Settings:
    extra = [line.strip() for line in options.get("allowlist", "").splitlines()
             if line.strip()]
    return Settings(
        docx_mode=options.get("docx_mode", "anonymize"),
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
        SESSION.officials, avoid=[name for name, _category in entries])
    return list(SESSION.protection.terms)


def _prepare(body: dict) -> tuple[Settings, MappingStore, list[tuple[str, str]]]:
    """Settings and a store for one request, do-not-change list refreshed.

    The judicial protection list depends on the party names - a judge sharing a
    surname with a client gets the full name shielded and nothing more - so it
    is rebuilt whenever the name list changes, not once when the files landed.
    """
    entries = _name_entries(body.get("names", []))
    _reprotect(entries)
    settings = _settings(body.get("options", {}))
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


def _entity_row(entity) -> dict:
    return {
        "key": entity.key,
        "category": entity.category,
        "label": entity.label,
        "canonical": entity.canonical,
        "replacement": entity.replacement,
        "occurrences": entity.occurrences,
        "source": entity.source,
        "enabled": entity.enabled,
        "is_person": entity.is_person,
    }


def _entities_payload() -> dict:
    store = SESSION.store
    rows = sorted(store.entities.values(),
                  key=lambda e: (0 if e.is_person else 1, e.category,
                                 -e.occurrences, e.canonical.casefold()))
    risky = sorted({v.text for e in store.persons() for v in e.variants if v.risky})
    unused = sum(1 for e in store.entities.values() if e.occurrences == 0)
    return {"entities": [_entity_row(e) for e in rows],
            "risky": risky[:12], "unused": unused}


# --------------------------------------------------------------------------
# app and auth
# --------------------------------------------------------------------------


app = FastAPI(title="Document Redactions & Anonymization", version=__version__)


@app.middleware("http")
async def _require_token(request: Request, call_next):
    if request.url.path == "/" or request.url.path.startswith("/assets/"):
        return await call_next(request)
    supplied = request.cookies.get(COOKIE) or request.headers.get("x-auth-token")
    if not secrets.compare_digest(supplied or "", TOKEN):
        return JSONResponse({"detail": "unauthorized"}, status_code=401)
    return await call_next(request)


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
        "files": [{"name": p.name, "kind": pipeline.classify(p)} for p in SESSION.files],
        "categories": [{"key": c.key, "label": c.label, "group": c.group}
                       for c in categories.CATEGORIES],
        "name_categories": ["person", "minor", "organization", "location"],
        "error_types": list(feedback.ERROR_TYPES),
        "ocr": {"ok": ocr_ok, "note": ocr_note},
    }


@app.post("/api/files")
async def upload(files: list[UploadFile]):
    added = 0
    for item in files:
        name = Path(item.filename or "upload").name
        target = SESSION.uploads / name
        counter = 1
        while target.exists():
            target = SESSION.uploads / f"{target.stem}-{counter}{target.suffix}"
            counter += 1
        target.write_bytes(await item.read())
        if pipeline.classify(target) is None:
            target.unlink()
            continue
        SESSION.files.append(target)
        added += 1
    return {"added": added,
            "files": [{"name": p.name, "kind": pipeline.classify(p)}
                      for p in SESSION.files]}


@app.delete("/api/files/{index}")
def remove_file(index: int):
    if not 0 <= index < len(SESSION.files):
        raise HTTPException(404, "no such file")
    path = SESSION.files.pop(index)
    path.unlink(missing_ok=True)
    return {"files": [{"name": p.name, "kind": pipeline.classify(p)}
                      for p in SESSION.files]}


# --------------------------------------------------------------------------
# names
# --------------------------------------------------------------------------


@app.post("/api/captions")
def captions():
    files = list(SESSION.files)

    def work(progress):
        found = pipeline.collect_caption_names(files, progress)
        SESSION.caption_names = found
        # the bench, harvested at the same time: their names are shielded for
        # the whole run rather than proposed as parties
        SESSION.officials = pipeline.collect_officials(files, progress)
        SESSION.protection = officials.protected_terms(SESSION.officials)
        return {"captions": [
            {"name": c.name, "role": c.role, "confidence": c.confidence,
             "source": c.source, "category": c.category} for c in found],
            **_guard_payload()}

    return {"job": _start_job(work)}


@app.post("/api/suggestions")
async def suggestions(request: Request):
    body = await request.json()
    files = list(SESSION.files)
    settings, store, _entries = _prepare(body)
    captions_now = list(SESSION.caption_names)

    def work(progress):
        found, notes = pipeline.collect_suggestions(
            files, store, settings, progress, caption_names=captions_now)
        SESSION.suggestions = found
        return {"suggestions": [
            {"text": s.text, "category": s.category, "count": s.count,
             "documents": sorted(s.documents)} for s in found],
            "notes": notes, **_guard_payload()}

    return {"job": _start_job(work)}


# --------------------------------------------------------------------------
# review
# --------------------------------------------------------------------------


@app.post("/api/review")
async def build_review(request: Request):
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

    return {"job": _start_job(work)}


@app.patch("/api/entities")
async def edit_entity(request: Request):
    body = await request.json()
    entity = SESSION.store.entities.get(body.get("key", ""))
    if entity is None:
        raise HTTPException(404, "no such entity")
    if "enabled" in body:
        entity.enabled = bool(body["enabled"])
    if body.get("category") and body["category"] != entity.category:
        moved = review.retype_entity(SESSION.store, entity, body["category"])
        if moved is None:
            raise HTTPException(422, "That value cannot be registered under that type.")
        entity = moved
    if body.get("replacement") is not None:
        replacement = str(body["replacement"])
        if not replacement.strip():
            raise HTTPException(422, "The replacement cannot be empty.")
        review.set_replacement(entity, replacement)
    return _entities_payload()


@app.post("/api/entities/all")
async def set_all(request: Request):
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
    settings = _settings(body.get("options", {}))
    store = SESSION.store

    def work(progress):
        result = pipeline.run_job(files, store, settings,
                                  SESSION.output, password, progress)
        SESSION.run_result = result
        outcomes = [{
            "source": o.source.name, "status": o.status,
            "delivered": o.delivered_name, "hits": o.hits,
            "warnings": o.warnings, "error": o.error,
        } for o in result.outcomes]
        return {
            "outcomes": outcomes,
            "archive": bool(result.archive),
            "key": bool(result.key_path),
            "report": bool(result.report_path),
            "failed": len(result.failed),
        }

    return {"job": _start_job(work)}


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = SESSION.jobs.get(job_id)
    if job is None:
        raise HTTPException(404, "no such job")
    return {"status": job.status, "message": job.message,
            "fraction": job.fraction, "result": job.result, "error": job.error}


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
