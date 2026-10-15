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

Kev runs as a separate local service, started with the app and stopped with
it (:class:`KevService`), offline - its weights are on disk and it is never
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
import sys
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
# test: 0.5 flagged no clean page and, with the matcher, caught 70 of 77
# leaks; 0.4 caught 71 and flagged 2 of 120 clean pages. Over-checking beats
# leaking, so the lower one.
KEV_THRESHOLD = 0.4
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
    """Kev, started with the app and stopped with it.

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
        return f"http://127.0.0.1:{self.port}" if self.ready.is_set() and self.port else None

    def start(self) -> bool:
        """Start Kev in the background; False when there is no Kev to start."""
        if os.environ.get("REDACTOR_KEV", "1") == "0":
            self.note = "turned off (REDACTOR_KEV=0)"
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
        threading.Thread(target=self._wait_until_up, daemon=True, name="kev-start").start()
        return True

    def _wait_until_up(self) -> None:
        deadline = time.time() + KEV_START_SECONDS
        while time.time() < deadline:
            if self.process is None or self.process.poll() is not None:
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
            time.sleep(1)
        self.note = "did not start in time"

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
        if self.note.startswith("running") or self.note == "starting":
            self.note = "stopped"


KEV = KevService()


def wait_for_kev(seconds: float) -> str | None:
    """Kev's address once it is up, or None if it is not within ``seconds``."""
    if KEV.process is None:
        return None
    KEV.ready.wait(timeout=seconds)
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


@dataclass
class Check:
    pages: int = 0
    flags: list[Flag] = field(default_factory=list)
    kev_note: str = ""
    path: Path | None = None

    def summary(self) -> dict:
        return {"pages": self.pages, "flagged": len(self.flags), "kev": self.kev_note,
                "file": str(self.path) if self.path else ""}


def _ticked_people(store) -> list[str]:
    return sorted({e.canonical for e in store.entities.values()
                   if e.enabled and e.category in ("person", "minor")})


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


def check_archive(archive: Path, store, settings, out_dir: Path, kev_url: str | None,
                  progress=lambda message, fraction: None) -> Check:
    """Read every delivered page back and list the pages to look at."""
    from . import engine, ocr_pool, pdf_processor, review

    check = Check(kev_note=("Kev " + KEV_RUN + " and the name matcher") if kev_url
                  else "the name matcher only - Kev was not running")
    people = _ticked_people(store)
    if not people or archive is None or not Path(archive).exists():
        return check
    matcher_store, _ = review.build_store([(name, "person") for name in people])
    plain = engine.Settings(use_ner=False)
    matcher = engine.PersonMatcher(matcher_store, plain)

    with tempfile.TemporaryDirectory(prefix="final-check-") as tmp:
        with zipfile.ZipFile(archive) as zf:
            members = [m for m in zf.namelist() if m.lower().endswith((".pdf", ".docx"))]
            for member in members:
                target = Path(tmp) / member
                target.parent.mkdir(parents=True, exist_ok=True)
                target.write_bytes(zf.read(member))
        files = [Path(tmp) / m for m in members]
        # a redacted scan has no text to check but its picture: read it, a few
        # pages at once, as the run read the originals
        ocr_pool.read_ahead(files)
        total = max(pipeline_pages(files), 1)
        done = 0
        for member, path in zip(members, files):
            for number, text in _page_texts(path):
                done += 1
                progress(f"Final check: page {done:,} of {total:,}", done / total)
                check.pages += 1
                if not text.strip():
                    continue
                hits = engine.scan_text(text, matcher_store, plain, matcher)
                matched = sorted({h.text for h in hits if h.category in ("person", "minor")})
                score = None
                if kev_url:
                    try:
                        score = ask_kev(kev_url, text, people)
                    except Exception:
                        kev_url, check.kev_note = None, "the name matcher only - Kev stopped answering"
                if matched or (score is not None and score >= KEV_THRESHOLD):
                    check.flags.append(Flag(member, number, score, matched))
        pdf_processor.forget_ocr(files)

    check.path = Path(out_dir) / "final-check.txt"
    check.path.write_text(render(check), encoding="utf-8")
    return check


def pipeline_pages(files) -> int:
    from . import pipeline
    return pipeline.count_pages(files) + sum(1 for f in files if f.suffix.lower() == ".docx")


def render(check: Check) -> str:
    lines = [
        "FINAL CHECK",
        "",
        f"Every delivered page was read back - {check.pages:,} page(s) - by {check.kev_note},",
        "for the names that were ticked. Nothing was changed. Look at each page below",
        "before the archive goes anywhere.",
        "",
        "This list names the real people it looked for: keep it with the mapping key,",
        "away from anything you deliver.",
        "",
    ]
    if not check.flags:
        lines.append("No page was flagged.")
    else:
        lines.append(f"{len(check.flags)} page(s) to look at:")
        lines.append("")
        for flag in check.flags:
            why = []
            if flag.matched:
                why.append("still reads " + ", ".join(f'"{m}"' for m in flag.matched))
            if flag.kev is not None and flag.kev >= KEV_THRESHOLD:
                why.append(f"Kev thinks a ticked name is still there ({flag.kev:.0%})")
            lines.append(f"  {flag.file}, page {flag.page}: {'; '.join(why)}")
    return "\n".join(lines) + "\n"
