"""The final check: every finished page read back for the names that were ticked.

Two readers, because each is blind where the other sees:

- the app's own name matcher, run over the delivered text - exact, and sure of
  every written form of a ticked name, but blind to a name OCR glued into
  another word or misspelled ("HARRINGTONENTERPRISES", "Pritchrd");
- Kev, a small local classification model (github.com/jaredpalmer/kev), asked
  of each page whether any of the ticked people is still named on it. On the
  corpus test it caught 18 of the 25 leaks the matcher missed - glued and
  misspelled names - and flagged none of 120 clean anonymized pages at the
  threshold used here.

Neither changes anything. A flagged page is listed for the operator to look at;
Kev answers yes or no for a page and cannot say where, so nothing it says is
safe to apply blind.

The local language model that once read the flagged pages for a fix was
taken out (2026-10-16): every flagged page is listed for the operator.

Kev runs as a separate local service (:class:`KevService`), started when a
final check needs it (see models.py) and stopped with the app, offline - its weights are on disk and it is never
allowed to reach the network. It lives in its own folder with its own Python
(``REDACTOR_KEV_DIR``, by default ``~/Coding/kev``); where it is missing the
check runs with the matcher alone and says so.
"""

from __future__ import annotations

import atexit
import json
import os
import shutil
import socket
import subprocess
import tempfile
import threading
import time
import urllib.error
import urllib.request
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

KEV_RUN = "jaredpalmer/kev-4b@v1.0"
# Kev's "yes" probability at or above which a page is flagged. On the corpus
# test: 0.5 flagged none of 120 clean anonymized pages and, with the matcher,
# caught 70 of 77 leaks; 0.4 caught one more and flagged two clean pages.
# Short pages score higher at random - a one-line letter carrying only its
# invented name scored 0.61 - so the higher one: a flag should be worth a look.
# (Telling Kev the invented names lowered leaks' scores as much as clean ones.)
KEV_THRESHOLD = 0.5
# Kev's score at or above which the local model reads the page too. On the
# corpus test, the model reading pages Kev scored 0.3 or more, beside the
# matcher's, caught 74 of 77 leaks against Kev's 70, flagged none of 120
# clean pages, and read under half of a test set a third leaks - far less of a
# real one: 107 of the 120 clean pages scored under 0.3. Reading every page
# caught one more leak, flagged three clean pages and took four times longer.
KEV_ASK_LLM = 0.3
# Names asked about in one question - a long list dilutes the question, so a
# long list is asked in parts, all against the same page (Kev keeps the page).
NAMES_PER_QUESTION = 20
PAGE_CHARS = 12000                 # a page's text, comfortably inside Kev's window
KEV_START_SECONDS = 240


def kev_dir() -> Path:
    return Path(os.environ.get("REDACTOR_KEV_DIR", Path.home() / "Coding" / "kev")).expanduser()


def _kev_python() -> Path | None:
    python = kev_dir() / ".venv" / "bin" / "python"
    return python if python.is_file() else None


def _free_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class KevService:
    """Kev, started when it is needed and stopped with the app.

    Started in its own process group and watched: a small watcher process
    stops it the moment the app's process is gone, so an app that crashed or
    was force-quit cannot leave a model holding ten gigabytes of memory.
    """

    def __init__(self) -> None:
        self.process: subprocess.Popen | None = None
        self.watcher: subprocess.Popen | None = None
        self.port: int | None = None
        self.note = "not started"
        self.ready = threading.Event()

    @property
    def url(self) -> str | None:
        alive = self.process is not None and self.process.poll() is None
        return f"http://127.0.0.1:{self.port}" if alive and self.ready.is_set() and self.port \
            else None

    def start(self) -> bool:
        """Start Kev in the background; False when there is no Kev to start.
        Started once: a second call while it runs or starts changes nothing."""
        if self.process is not None and self.process.poll() is None:
            return True
        if os.environ.get("REDACTOR_KEV", "1") == "0":
            self.note = "turned off (REDACTOR_KEV=0)"
            return False
        if os.name == "nt":
            self.note = "not available on Windows"
            return False
        python = _kev_python()
        if python is None:
            self.note = f"not installed (no {kev_dir() / '.venv'})"
            return False
        self.port = _free_port()
        env = dict(os.environ, HF_HUB_OFFLINE="1", TRANSFORMERS_OFFLINE="1",
                   HF_DATASETS_OFFLINE="1")
        env.pop("PYTHONHOME", None)               # a frozen app's own, not Kev's
        env.pop("PYTHONPATH", None)
        self.process = subprocess.Popen(
            [str(python), "-m", "kev.serve", "--run", KEV_RUN, "--port", str(self.port)],
            cwd=str(kev_dir()), env=env, start_new_session=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        # the watcher: when this process is gone, so is Kev
        self.watcher = subprocess.Popen(
            ["/bin/sh", "-c",
             f"while kill -0 {os.getpid()} 2>/dev/null && kill -0 {self.process.pid} 2>/dev/null;"
             f" do sleep 2; done; kill -TERM -{self.process.pid} 2>/dev/null; sleep 5;"
             f" kill -KILL -{self.process.pid} 2>/dev/null"],
            start_new_session=True, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        atexit.register(self.stop)
        self.note = "starting"
        threading.Thread(target=self._wait_until_up, args=(self.process,), daemon=True,
                         name="kev-start").start()
        return True

    def _wait_until_up(self, process) -> None:
        """Watch Kev come up. One slow to load - nine gigabytes off a cold
        disk - is still taken when it does: "slow to start" is not the end."""
        started = time.time()
        while self.process is process:
            if process.poll() is not None:
                self.note = "stopped while starting"
                return
            try:
                with urllib.request.urlopen(f"http://127.0.0.1:{self.port}/v1/models",
                                            timeout=2) as response:
                    if response.status == 200:
                        self.note = f"running ({KEV_RUN}, offline)"
                        self.ready.set()
                        return
            except (urllib.error.URLError, OSError):
                pass
            if time.time() - started > KEV_START_SECONDS:
                self.note = "slow to start - still loading"
            time.sleep(1)

    @property
    def starting(self) -> bool:
        """Kev is on its way up: running, not answering yet."""
        return (self.process is not None and self.process.poll() is None
                and not self.ready.is_set())

    def stop(self) -> None:
        process, self.process = self.process, None
        self.ready.clear()
        if process is not None and process.poll() is None:
            try:
                os.killpg(process.pid, 15)
                process.wait(timeout=10)
            except Exception:
                try:
                    os.killpg(process.pid, 9)
                except Exception:
                    pass
        watcher, self.watcher = self.watcher, None
        if watcher is not None and watcher.poll() is None:
            watcher.terminate()
        if self.note.startswith(("running", "starting", "slow to start")):
            self.note = "stopped"


KEV = KevService()


def wait_for_kev(seconds: float) -> str | None:
    """Kev's address once it is up, or None if it is not within ``seconds``.
    A Kev that is not running - never started, or gone - is not waited for."""
    deadline = time.time() + seconds
    while KEV.starting and time.time() < deadline:
        KEV.ready.wait(timeout=1.0)
    return KEV.url


# --------------------------------------------------------------------------
# asking
# --------------------------------------------------------------------------


def ask_kev(url: str, text: str, names: list[str]) -> float:
    """Kev's probability that any of ``names`` is still named in ``text``."""
    questions = {}
    for start in range(0, len(names), NAMES_PER_QUESTION):
        part = names[start:start + NAMES_PER_QUESTION]
        questions[f"q{start}"] = {
            "type": "noul",
            "instructions": (
                "This page should have had these people's names removed or replaced with "
                "invented ones: " + "; ".join(part) + ". Is any of these real people still "
                "named on the page - in full, by surname or first name alone, by initials, "
                "misspelled, in capitals, or run together with other words?"),
            "criteria": {
                "true": "One of the listed real names, or a form of it, appears in the text.",
                "false": "None of the listed names appears; any names present are different "
                         "(invented) names, or placeholders such as [PERSON-1] or black boxes.",
            },
        }
    body = json.dumps({"state": text[:PAGE_CHARS], "model": "kev-latest",
                       "questions": questions}).encode()
    request = urllib.request.Request(f"{url}/v1/systemone", data=body,
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=300) as response:
        answers = json.loads(response.read())["answers"]
    return max(float(answer["noul"]) for answer in answers.values())


# --------------------------------------------------------------------------
# the check
# --------------------------------------------------------------------------


@dataclass
class Flag:
    file: str
    page: int                      # 1-based
    kev: float | None              # None when Kev was not asked
    matched: list[str] = field(default_factory=list)
    # what the page reads, for the local model to look at; never written out
    text: str = field(default="", repr=False, compare=False)
    # (quote, person): what the local model found on it that could not be fixed
    quotes: list[tuple[str, str]] = field(default_factory=list)
    note: str = ""
    # not flagged, only doubtful: Kev scored it KEV_ASK_LLM or more. The local
    # model reads it; listed only when that model finds a real name on it
    doubtful: bool = False


@dataclass
class Fix:
    """A flagged page the local model's quotes fixed, read back clean."""
    file: str
    page: int
    quotes: list[tuple[str, str]]  # (quote, person)


@dataclass
class Check:
    pages: int = 0
    flags: list[Flag] = field(default_factory=list)
    doubtful: list[Flag] = field(default_factory=list)
    kev_note: str = ""
    path: Path | None = None
    # after the local model's reading (read_flagged): None when it did not read
    still: list[Flag] | None = None
    fixed: list[Fix] = field(default_factory=list)
    cleared: list[Flag] = field(default_factory=list)
    llm_note: str = ""

    def summary(self) -> dict:
        return {"pages": self.pages, "flagged": len(self.flags), "kev": self.kev_note,
                "file": str(self.path) if self.path else "",
                "fixed": len(self.fixed), "cleared": len(self.cleared),
                "still": len(self.flags) if self.still is None else len(self.still),
                "llm": self.llm_note,
                # each flagged page, for the app to show it: never its text
                "flags": [{"file": f.file, "page": f.page, "kev": f.kev,
                           "matched": list(f.matched)} for f in self.flags]}


def _ticked_people(store) -> list[str]:
    # With a party sheet the check reads back for the parties, the children
    # and the family surname only: everyone else was left as written on purpose
    sheet = getattr(store, "parties", None) is not None
    return sorted({e.canonical for e in store.entities.values()
                   if e.enabled and e.category in ("person", "minor")
                   and (not sheet or (e.fixed and e.party))})


def _page_texts(path: Path):
    """(page number, text) for every page of a delivered file, as anyone
    reading it would get it: a PDF's text layer, or what OCR reads off a page
    that has none - a redacted scan."""
    from . import docx_processor, pdf_processor

    if path.suffix.lower() == ".docx":
        yield 1, "\n".join(docx_processor.extract_text(path).values())
        return
    import pymupdf

    with pymupdf.open(path) as doc:
        for index, page in enumerate(doc):
            sets, scanned = pdf_processor._page_words(page, path, index)
            if scanned:
                texts = [pdf_processor._words_to_text(words)[0] for words in sets or []]
                texts.append(page.get_text("text"))
                yield index + 1, "\n".join(t for t in texts if t.strip())
            else:
                yield index + 1, page.get_text("text")


class _Reader:
    """The two readers of the final check, for one store's ticked people."""

    def __init__(self, store, check: Check, kev_url: str | None) -> None:
        from . import engine, review

        self.check = check
        self.kev_url = kev_url
        self.people = _ticked_people(store)
        self.store, _ = review.build_store([(name, "person") for name in self.people])
        self.settings = engine.Settings(use_ner=False)
        self.matcher = engine.PersonMatcher(self.store, self.settings)

    def flag(self, member: str, number: int, text: str) -> Flag | None:
        from . import engine

        if not text.strip():
            return None
        hits = engine.scan_text(text, self.store, self.settings, self.matcher)
        matched = sorted({h.text for h in hits if h.category in ("person", "minor")})
        score = None
        if self.kev_url:
            try:
                score = ask_kev(self.kev_url, text, self.people)
            except Exception:
                self.kev_url = None
                self.check.kev_note = "the name matcher only - Kev stopped answering"
        if matched or (score is not None and score >= KEV_THRESHOLD):
            return Flag(member, number, score, matched, text=text)
        if score is not None and score >= KEV_ASK_LLM:
            return Flag(member, number, score, matched, text=text, doubtful=True)
        return None


def _read_members(archive: Path, members: list[str] | None, progress, label: str):
    """(member, page number, text) for every page of the archive's PDFs and
    Word files - or of ``members`` alone."""
    from . import ocr_pool, pdf_processor

    with tempfile.TemporaryDirectory(prefix="final-check-") as tmp:
        with zipfile.ZipFile(archive) as zf:
            chosen = [m for m in zf.namelist() if m.lower().endswith((".pdf", ".docx"))
                      and (members is None or m in members)]
            for member in chosen:
                target = Path(tmp) / member
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(member))
        files = [Path(tmp) / m for m in chosen]
        # a redacted scan has no text to check but its picture: read it, a few
        # pages at once, as the run read the originals
        ocr_pool.read_ahead(files)
        total = max(pipeline_pages(files), 1)
        done = 0
        try:
            for member, path in zip(chosen, files):
                for number, text in _page_texts(path):
                    done += 1
                    progress(f"{label}: page {done:,} of {total:,}", done / total)
                    yield member, number, text
        finally:
            pdf_processor.forget_ocr(files)


def check_archive(archive: Path, store, settings, out_dir: Path, kev_url: str | None,
                  progress=lambda message, fraction: None) -> Check:
    """Read every delivered page back and list the pages to look at."""
    check = Check(kev_note=("Kev " + KEV_RUN + " and the name matcher") if kev_url
                  else "the name matcher only - Kev was not running")
    if not _ticked_people(store) or archive is None or not Path(archive).exists():
        return check
    reader = _Reader(store, check, kev_url)
    for member, number, text in _read_members(Path(archive), None, progress, "Final check"):
        check.pages += 1
        flag = reader.flag(member, number, text)
        if flag is not None:
            (check.doubtful if flag.doubtful else check.flags).append(flag)

    check.path = Path(out_dir) / "final-check.txt"
    check.path.write_text(render(check), encoding="utf-8")
    return check


def pipeline_pages(files) -> int:
    from . import pipeline
    return pipeline.count_pages(files) + sum(1 for f in files if f.suffix.lower() == ".docx")


def _why(flag: Flag) -> list[str]:
    why = []
    if flag.matched:
        why.append("still reads " + ", ".join(f'"{m}"' for m in flag.matched))
    if flag.kev is not None and flag.kev >= KEV_THRESHOLD:
        why.append(f"Kev thinks a ticked name is still there ({flag.kev:.0%})")
    return why


def _quoted(quotes) -> str:
    return ", ".join(f'"{quote}" ({person})' for quote, person in quotes)


def render(check: Check) -> str:
    changed = bool(check.fixed)
    lines = [
        "FINAL CHECK",
        "",
        f"Every delivered page was read back - {check.pages:,} page(s) - by {check.kev_note},",
        "for the names that were ticked. "
        + ("Only the pages the local model fixed were changed." if changed
           else "Nothing was changed."),
        "Look at each page below before the archive goes anywhere.",
        "",
        "This list names the real people it looked for: keep it with the mapping key,",
        "away from anything you deliver.",
        "",
    ]
    if check.still is None:
        if not check.flags:
            lines.append("No page was flagged.")
        else:
            lines.append(f"{len(check.flags)} page(s) to look at:")
            lines.append("")
            for flag in check.flags:
                lines.append(f"  {flag.file}, page {flag.page}: {'; '.join(_why(flag))}")
        return "\n".join(lines) + "\n"

    if not check.flags:
        lines.append("No page was flagged.")
    lines.append(f"The local model ({check.llm_note}) read the flagged pages and quoted")
    lines.append("what it found; only a quote found word for word on the page was used.")
    lines.append("")
    if check.fixed:
        lines.append(f"Fixed by the local model and rechecked - {len(check.fixed)} page(s):")
        lines.append("")
        for fix in check.fixed:
            lines.append(f"  {fix.file}, page {fix.page}: {_quoted(fix.quotes)} - now replaced")
        lines.append("")
    if check.still:
        lines.append(f"Still to look at - {len(check.still)} page(s):")
        lines.append("")
        for flag in check.still:
            why = _why(flag) + ([flag.note] if flag.note else [])
            if flag.quotes:
                why.append("the local model found " + _quoted(flag.quotes)
                           + " but could not fix it")
            lines.append(f"  {flag.file}, page {flag.page}: {'; '.join(why)}")
        lines.append("")
    elif check.flags:
        lines.append("Nothing is left to look at.")
        lines.append("")
    if check.cleared:
        lines.append(f"Cleared by the local model - {len(check.cleared)} page(s). It found no "
                     "ticked name on these; look if in doubt:")
        for flag in check.cleared:
            lines.append(f"  {flag.file}, page {flag.page}: {'; '.join(_why(flag))}")
    return "\n".join(lines).rstrip("\n") + "\n"


# --------------------------------------------------------------------------
# a flagged page fixed by hand: its file made again, its pages read again
# --------------------------------------------------------------------------

def remake(members: list[str], result, store, settings, archive: Path) -> dict[str, str]:
    """The delivered files ``members`` made again from their originals with
    ``store``, put back into the archive. {member: why} for any that could
    not be."""
    from . import docx_processor, engine, pdf_processor

    by_member = {o.delivered_name: o for o in getattr(result, "outcomes", [])}
    failed: dict[str, str] = {}
    made: dict[str, Path] = {}
    matcher = engine.PersonMatcher(store, settings)
    work = Path(tempfile.mkdtemp(prefix="final-fix-"))
    try:
        for member in members:
            outcome = by_member.get(member)
            if outcome is None or not Path(outcome.source).exists():
                failed[member] = "its original was not to hand to make it again"
                continue
            target = work / f"{len(made)}{Path(member).suffix}"
            try:
                process = docx_processor.process if outcome.kind == "docx" \
                    else pdf_processor.process
                detail = process(Path(outcome.source), target, store, settings, matcher)
            except Exception as exc:
                failed[member] = f"making it again failed ({type(exc).__name__})"
                continue
            if getattr(detail, "refused", False) or not target.exists():
                failed[member] = "making it again was refused"
                continue
            made[member] = target
        if made:
            _replace_members(archive, made)
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return failed


def _replace_members(archive: Path, made: dict[str, Path]) -> None:
    partial = archive.with_name(archive.name + ".part")
    with zipfile.ZipFile(archive) as source, \
            zipfile.ZipFile(partial, "w", zipfile.ZIP_DEFLATED) as target:
        for info in source.infolist():
            if info.filename in made:
                target.write(made[info.filename], arcname=info.filename)
            else:
                target.writestr(info, source.read(info.filename))
    os.replace(partial, archive)


def recheck(archive: Path, store, members: list[str], kev_url: str | None,
            progress=lambda message, fraction: None) -> list[Flag]:
    """The pages of ``members`` read back again: the flags they still raise."""
    check = Check(kev_note="")
    reader = _Reader(store, check, kev_url)
    flags = []
    for member, number, text in _read_members(Path(archive), members, progress,
                                               "Reading the fixed pages back"):
        flag = reader.flag(member, number, text)
        if flag is not None and not flag.doubtful:
            flags.append(flag)
    return flags


def write_pending(path: Path, pages: int, kev_note: str, flags: list[dict],
                  done: list[dict] = ()) -> None:
    """final-check.txt again, after the review screen: the pages still to
    look at, and each page looked at there - fixed, or nothing to fix."""
    check = Check(kev_note=kev_note, pages=pages, path=Path(path))
    check.flags = [Flag(f["file"], f["page"], f.get("kev"), list(f.get("matched") or []))
                   for f in flags]
    text = render(check)
    if done:
        text = text.replace("Nothing was changed.", "Pages fixed on the review screen were "
                            "made again and read back.").replace("No page was flagged.",
                                                                 "Nothing is left to look at.")
        lines = ["", f"Looked at on the review screen - {len(done)} page(s):"]
        for d in done:
            lines.append(f"  {d['file']}, page {d['page']}: {d['how']}")
        text = text.rstrip("\n") + "\n" + "\n".join(lines) + "\n"
    Path(path).write_text(text, encoding="utf-8")


def _clean(flag: Flag) -> bool:
    """Neither the matcher nor Kev sees a ticked name on the page."""
    return not flag.matched and (flag.kev is None or flag.kev < KEV_THRESHOLD)


def _write(check: Check) -> None:
    if check.path is not None:
        check.path.write_text(render(check), encoding="utf-8")
