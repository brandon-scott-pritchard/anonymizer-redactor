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

A third reader, the local language model (:mod:`redactor.llm`), is asked of
each flagged page what on it still names a ticked person, word for word. A
quote really on the page becomes one more written form of that person, the
file is made again and its pages read back (:func:`read_flagged`). What it
cannot fix stays listed; it never clears a flag while LLM_MAY_CLEAR is off.

Kev runs as a separate local service (:class:`KevService`), started when a
final check needs it - never beside the local model (see models.py) - and
stopped with the app, offline - its weights are on disk and it is never
allowed to reach the network. It lives in its own folder with its own Python
(``REDACTOR_KEV_DIR``, by default ``~/Coding/kev``); where it is missing the
check runs with the matcher alone and says so.
"""

from __future__ import annotations

import atexit
import copy
import json
import os
import re
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
                "llm": self.llm_note}


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
        if check.flags and check.llm_note:
            lines += ["", f"The local model did not read the flagged pages: {check.llm_note}."]
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
# the local model's reading of the flagged pages
# --------------------------------------------------------------------------

# A flagged page the local model found nothing on, and the matcher nothing
# either, may be cleared - moved to the end of the list - once that has been
# tried against a labelled corpus. Until then it is listed like any other.
LLM_MAY_CLEAR = False
LLM_ROUNDS = 2                    # fix and read again, at most this many times
FORMS_SHOWN = 12                  # a person's known written forms put to the model

PAGE_SYSTEM = (
    "You check pages that were anonymized: the real people listed below should have been "
    "removed or replaced with invented names everywhere. Quote, word for word exactly as "
    "printed on the page, every place where one of these real people is still named - in "
    "full, by surname or first name alone, by initials, misspelled or misread by OCR "
    "(HARRlNGTON, Harringten), glued into other words (4417IntuPymdHARRINGTON), or in "
    "capitals. Copy each quote character for character from the page, including any "
    "letters it is glued to, and with it the three or four words just before it and "
    "just after it, also exactly as on the page, so the place can be found. Do not "
    "quote invented names, placeholders such as [PERSON-1] or [REDACTED], or other "
    "people's names. If no listed person is named, answer with an empty list.")
PAGE_SCHEMA = ('{"found": [{"before": "<3-4 words just before it>", '
               '"quote": "<the text exactly as on the page>", '
               '"after": "<3-4 words just after it>", '
               '"person": "<which listed person, by number>"}]}')


def _heads(store) -> list:
    sheet = getattr(store, "parties", None) is not None
    return [e for e in store.entities.values()
            if e.enabled and e.category in ("person", "minor") and e.parent is None
            and (not sheet or (e.fixed and e.party))]


def _forms(store, head) -> list[str]:
    from . import grouping

    return [m.canonical for m in grouping.members(store, head)][:FORMS_SHOWN]


def _ask_page(url: str, text: str, heads: list, store) -> list[tuple[str, str, str, str]]:
    """(quote, person, before, after) the model says still names one of
    ``heads`` on the page."""
    from . import llm

    listing = "\n".join(
        f"{i}. {head.canonical}"
        + (f" (also written: {', '.join(_forms(store, head))})" if _forms(store, head) else "")
        for i, head in enumerate(heads, 1))
    answer = llm.chat(url, PAGE_SYSTEM,
                      "The real people:\n" + listing + "\n\nThe page:\n" + text[:PAGE_CHARS],
                      PAGE_SCHEMA, max_tokens=1024)
    found = answer.get("found", []) if isinstance(answer, dict) else answer
    out = []
    for item in found if isinstance(found, list) else []:
        if isinstance(item, dict):
            out.append((str(item.get("quote") or "").strip(), str(item.get("person") or ""),
                        _trimmed(item.get("before")), _trimmed(item.get("after"))))
    return out


def _trimmed(context) -> str:
    return str(context or "").strip().strip(".…").strip()


def _person(said: str, heads: list, store):
    said = said.strip().rstrip(".")
    if said.isdigit() and 1 <= int(said) <= len(heads):
        return heads[int(said) - 1]
    folded = said.casefold()
    for head in heads:
        if folded == head.canonical.casefold() or folded in (f.casefold()
                                                            for f in _forms(store, head)):
            return head
    return None


def _squash(text: str) -> str:
    return " ".join(text.split()).casefold()


def _locate(text: str, before: str, quote: str, after: str) -> tuple[int, int] | None:
    """Where on the page ``quote`` is, with ``before`` and ``after`` either
    side of it - spaced, or glued on as OCR glues - compared with the
    whitespace collapsed and case ignored: (start, end) in ``text``, or None
    when the three are not found together."""
    squashed, where = [], []                       # the page squashed, and each char's place
    for index, char in enumerate(text):
        if char.isspace():
            if squashed and squashed[-1] != " ":
                squashed.append(" ")
                where.append(index)
            continue
        squashed.append(char.casefold())
        where.append(index)
    page = "".join(squashed)
    head, body, tail = _squash(before), _squash(quote), _squash(after)
    if not body:
        return None
    for left in ((" ", "") if head else ("",)):
        for right in ((" ", "") if tail else ("",)):
            found = page.find(head + left + body + right + tail)
            if found >= 0:
                start = found + len(head + left)
                end = start + len(body)
                return where[start], where[end - 1] + 1
    return None


def _invented_words(store) -> set[str]:
    out: set[str] = set()
    for entity in store.entities.values():
        out.update(w.casefold() for w in re.findall(r"[A-Za-z]+", entity.replacement or ""))
    return out | {"redacted"}


def _name_parts(store, head) -> set[str]:
    from . import grouping

    parts: set[str] = set()
    for entity in [head, *grouping.members(store, head)]:
        person = entity.person
        if person is None:
            continue
        for piece in (person.first, *person.middles, person.last):
            for bit in re.split(r"[-\s]", piece or ""):
                bit = re.sub(r"[^a-z]", "", bit.casefold())
                if bit:
                    parts.add(bit)
    return parts


def _resembles(quote: str, parts: set[str]) -> bool:
    """Whether a quote looks like the person's name at all: holding one of
    its words, a misreading of one, or its initials. A model that quotes
    "the" must not have every "the" replaced."""
    from .llm_grouping import similar

    letters = re.sub(r"[^a-z]", "", quote.casefold())
    words = [w.casefold() for w in re.findall(r"[A-Za-z0-9|!]+", quote)]
    if any(len(p) >= 4 and p in letters for p in parts):
        return True
    if any(len(p) >= 4 and similar(w, p) for w in words for p in parts):
        return True
    if any(len(p) < 4 and w == p for w in words for p in parts):
        return True
    initials = {p[0] for p in parts}
    return 2 <= len(letters) <= 4 and set(letters) <= initials and len(set(letters)) == len(letters)


def _confirmed(found, text: str, heads: list, store) -> tuple[list, int]:
    """The quotes that are really on the page, where the model said, and
    name a listed person: (quote, head, (start, end)), and how many quotes
    were not on the page."""
    invented = _invented_words(store)
    out, missing = [], 0
    for quote, said, before, after in found:
        head = _person(said, heads, store)
        if head is None or len(quote) < 2:
            continue
        span = _locate(text, before, quote, after)
        if span is None:
            missing += 1                        # not on the page as quoted: never applied
            continue
        words = {w.casefold() for w in re.findall(r"[A-Za-z]+", quote)}
        if words and words <= invented:
            continue                            # an invented name, or a placeholder
        if not _resembles(quote, _name_parts(store, head)):
            continue
        if (quote, head, span) not in out:
            out.append((quote, head, span))
    return out, missing


def _register(store, quote: str, head, text: str, span: tuple[int, int]) -> bool:
    """The quote, and the word it is glued into at ``span``, made written
    forms of the person in ``store``. True when anything new was added."""
    from . import llm_grouping

    added = False
    # a quote that is the name glued to more - "HARRINGTONENTERPRISES" - is
    # narrowed to the name: registered whole it read "TOLK", the business lost
    inner = _name_inside(quote, head)
    if inner is not None:
        a, b = inner
        quote, span = quote[a:b], (span[0] + a, span[0] + b)
    known = {v.text.casefold() for e in [head, *_members(store, head)] for v in e.variants}
    if quote.casefold() not in known:
        added |= llm_grouping.add_form(store, head, quote, "found by the final check") \
            is not None
    # the word a glued quote sits in: matched whole, only the name swapped
    return _register_glued(store, head, text, *span) or added


def _name_inside(quote: str, head) -> tuple[int, int] | None:
    """Where the person's own name sits in a longer glued quote, or None
    when the quote is the name (or a misspelling of it) and nothing more."""
    person = head.person
    if person is None:
        return None
    folded = quote.casefold()
    spans = []
    for part in (person.first, *person.middles, person.last):
        part = (part or "").strip(".").casefold()
        if len(part) >= 3:
            at = folded.find(part)
            if at >= 0:
                spans.append((at, at + len(part)))
    if not spans:
        return None
    a, b = min(s for s, _e in spans), max(e for _s, e in spans)
    if (a, b) == (0, len(quote)):
        return None
    return a, b


def _register_glued(store, head, text: str, at: int, stop: int) -> bool:
    from . import llm_grouping

    added = False
    start, end = at, stop
    while start > 0 and not text[start - 1].isspace():
        start -= 1
    while end < len(text) and not text[end].isspace():
        end += 1
    for token in re.finditer(r"\S+", text[start:end]):
        word = token.group(0).strip(".,;:()[]\"'")
        lo = start + token.start() + token.group(0).find(word)
        hi = lo + len(word)
        inner_lo, inner_hi = max(lo, at), min(hi, stop)
        if inner_lo >= inner_hi or (lo, hi) == (inner_lo, inner_hi):
            continue
        piece = text[inner_lo:inner_hi]
        stand_in = llm_grouping.stand_in(store, head, piece)
        written = text[lo:inner_lo] + stand_in + text[inner_hi:hi]
        added |= llm_grouping.add_form(store, head, word, "glued, found by the final check",
                                       written=written) is not None
    return added


def _members(store, head) -> list:
    from . import grouping
    return grouping.members(store, head)


def _rerun(members: list[str], result, store, settings, archive: Path) -> dict[str, str]:
    """The delivered files ``members`` made again from their originals with
    ``store``, put back into the archive. {member: why} for any that could
    not be."""
    from . import docx_processor, engine, pdf_processor

    by_member = {o.delivered_name: o for o in getattr(result, "processed", [])}
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
            if detail.refused or not target.exists():
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


def read_flagged(check: Check, result, store, settings, out_dir: Path, kev_url: str | None,
                 llm_url: str, progress=lambda message, fraction: None):
    """The local model reads each flagged page for what is still a real name.

    Each quote found word for word on the page becomes one more written form
    of its person, the files those pages came from are made again with it,
    and their pages are read back by all three readers - at most
    LLM_ROUNDS times. Returns the store with the forms added, or None when
    nothing was added. final-check.txt is written again either way.
    """
    from . import llm

    check.still, check.llm_note = [], f"{llm.llm_model()}, offline"
    check.path = check.path or Path(out_dir) / "final-check.txt"
    archive = Path(result.archive) if result is not None and result.archive else None
    heads = _heads(store)
    if not (check.flags or check.doubtful) or archive is None or not heads:
        _write(check)
        return None
    fixed_store = copy.deepcopy(store)
    reader = _Reader(store, check, kev_url)
    pending = list(check.flags) + list(check.doubtful)
    applied: dict[tuple[str, int], list] = {}      # page -> quotes applied to it
    changed = False
    try:
        for round_ in range(LLM_ROUNDS + 1):
            to_fix: dict[tuple[str, int], tuple[Flag, list]] = {}
            for index, flag in enumerate(pending):
                progress(f"The local model is checking page {index + 1:,} of "
                         f"{len(pending):,}", index / max(len(pending), 1))
                found, missing = _confirmed(_ask_page(llm_url, flag.text, heads, store),
                                            flag.text, heads, store)
                page = (flag.file, flag.page)
                if not found and flag.doubtful and page not in applied:
                    continue                    # Kev unsure, the model found nothing: as it was
                if not found:
                    if _clean(flag) and page in applied:   # read back clean
                        check.fixed.append(Fix(flag.file, flag.page, applied[page]))
                        continue
                    if not flag.matched and LLM_MAY_CLEAR and page not in applied:
                        check.cleared.append(flag)
                        continue
                    if page in applied:
                        flag.quotes = applied[page]
                        flag.note = "made again with what the local model found, still flagged"
                    else:
                        flag.note = flag.note or (
                            "the local model found no ticked name on it"
                            + (f" ({missing} quote(s) it gave were not on the page)"
                               if missing else ""))
                    check.still.append(flag)
                    continue
                if round_ == LLM_ROUNDS:                 # no more making again
                    flag.quotes = [(q, h.canonical) for q, h, _at in found]
                    check.still.append(flag)
                    continue
                if not any([_register(fixed_store, q, fixed_store.entities[h.key], flag.text,
                                      at) for q, h, at in found]):
                    flag.quotes = [(q, h.canonical) for q, h, _at in found]
                    flag.note = "its names are already forms the run replaces"
                    check.still.append(flag)
                    continue
                changed = True
                to_fix[page] = (flag, found)
            if not to_fix:
                break
            members = sorted({member for member, _number in to_fix})
            progress("Making the fixed files again", 0.9)
            failed = _rerun(members, result, fixed_store, settings, archive)
            for page, (flag, found) in to_fix.items():
                applied.setdefault(page, []).extend(
                    (q, h.canonical) for q, h, _at in found
                    if (q, h.canonical) not in applied.get(page, []))
            pending = []
            for page, (flag, found) in list(to_fix.items()):
                if page[0] in failed:
                    flag.quotes = [(q, h.canonical) for q, h, _at in found]
                    flag.note = failed[page[0]]
                    check.still.append(flag)
                    del to_fix[page]
            again = {member for member, _number in to_fix}
            if not again:
                break
            for member, number, text in _read_members(archive, sorted(again), progress,
                                                      "Reading the fixed pages back"):
                if (member, number) not in to_fix:
                    continue
                pending.append(reader.flag(member, number, text)
                               or Flag(member, number, None, [], text=text))
    except Exception as exc:                        # what was not read stays listed
        check.llm_note += f" - stopped answering ({type(exc).__name__})"
        done = {(f.file, f.page) for f in check.still} | {(f.file, f.page) for f in check.fixed}
        for flag in pending + check.flags:
            if (flag.file, flag.page) not in done:
                done.add((flag.file, flag.page))
                check.still.append(flag)
    check.still.sort(key=lambda f: (f.file, f.page))
    _write(check)
    return fixed_store if changed else None


def _clean(flag: Flag) -> bool:
    """Neither the matcher nor Kev sees a ticked name on the page."""
    return not flag.matched and (flag.kev is None or flag.kev < KEV_THRESHOLD)


def _write(check: Check) -> None:
    if check.path is not None:
        check.path.write_text(render(check), encoding="utf-8")
