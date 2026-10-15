"""Run orchestration: gather suggestions, process files, build the archive."""

from __future__ import annotations

import os
import re
import shutil
import traceback
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable, Iterable, Sequence

from dataclasses import replace as _replace

from . import (caption, categories, children, docx_processor, engine, grouping, mapping,
               names, ner, ocr_pool, officials, pdf_processor, photo, places,
               transactions)
from .engine import PersonMatcher, Settings
from .mapping import MappingStore

Progress = Callable[[str, float], None]

SUPPORTED = {".docx": "docx", ".pdf": "pdf", **{suffix: "image" for suffix in photo.SUFFIXES}}


def classify(path: Path) -> str | None:
    return SUPPORTED.get(Path(path).suffix.lower())


def as_document(path: Path, folder: Path) -> Path:
    """A photo or screenshot of a document, made into a PDF in ``folder``.

    Every step after this one - captions, suggestions, the review scan, the
    run - reads it as the scanned page it now is. Anything that is not an
    image comes back as it was.
    """
    path = Path(path)
    if classify(path) != "image":
        return path
    target = Path(folder) / f"{path.stem}.pdf"
    counter = 1
    while target.exists():
        target = target.with_name(f"{path.stem}-{counter}.pdf")
        counter += 1
    return photo.to_pdf(path, target)


def _noop(_message: str, _fraction: float) -> None:
    pass


def _read_ahead(files: Sequence[Path], progress: Progress, per_file: int | None = None) -> None:
    """The scanned pages of ``files`` read a few at once before a step walks
    them one by one - see :mod:`ocr_pool`. Each page is counted as it is read."""
    told = []

    def page_read(path, index) -> None:
        if not told:
            progress("Reading scanned pages", 0.0)
            told.append(True)
        pdf_processor._page_read(path, index)

    try:
        ocr_pool.read_ahead(files, per_file=per_file, on_page=page_read)
    except Exception:                              # the step reads them itself
        pass


def count_pages(files: Sequence[Path], per_file: int | None = None) -> int:
    """Pages across the PDFs among ``files`` (photos are PDFs by now), at most
    ``per_file`` from each. Word files have no pages until Word lays them out
    and are not counted."""
    import pymupdf

    total = 0
    for path in files:
        if Path(path).suffix.lower() != ".pdf":
            continue
        try:
            with pymupdf.open(path) as doc:
                total += doc.page_count if per_file is None else min(doc.page_count, per_file)
        except Exception:                          # unreadable: the run says so
            continue
    return total


def tables_in(path: Path) -> list:
    """Rows of cells, from a DOCX's own tables or a PDF's column geometry.

    Both readers return the same shape, so everything that consumes a table -
    the children roster, the counterparty payee column, the label/value
    registration pass - reads either kind of document without caring which.
    """
    kind = classify(path)
    try:
        if kind == "docx":
            return list(docx_processor.iter_tables(path))
        if kind == "pdf":
            return list(pdf_processor.iter_tables(path))
    except Exception:
        return []
    return []


def column_texts(tables) -> list[str]:
    """Every table column read top to bottom, one string per column.

    A letterhead laid out as a two-column table - the holder's name and address
    on the left, "INQUIRIES / Call: 800-555-0247" on the right - reads row by
    row as name, INQUIRIES, street, Call, city: the address block is never
    contiguous, and nothing that looks for a name over a street finds it. Read
    down the column, it is three lines in a row again.
    """
    out: list[str] = []
    for rows in tables:
        width = max((len(row) for row in rows), default=0)
        for column in range(width):
            cells = [row[column].strip() for row in rows
                     if column < len(row) and row[column].strip()]
            if len(cells) >= 2:
                out.append("\n".join(cells))
    return out


# --------------------------------------------------------------------------
# phase 1 - what the caption already tells us
# --------------------------------------------------------------------------


def collect_caption_names(files: Sequence[Path], progress: Progress = _noop) -> list[caption.CaptionName]:
    """Party names harvested from every selected document's caption and headers."""
    _read_ahead(files, progress, per_file=pdf_processor.CAPTION_PAGES)
    regions: dict[str, str] = {}
    total = max(len(files), 1)
    for index, path in enumerate(files):
        path = Path(path)
        progress(f"Reading caption of {path.name}", index / total)
        kind = classify(path)
        try:
            if kind == "docx":
                regions.update(docx_processor.caption_sources(path))
            elif kind == "pdf":
                regions.update(pdf_processor.caption_sources(path))
        except Exception:
            continue
    trimmed = {label: caption.caption_region(text) for label, text in regions.items()}
    found = caption.harvest_documents(trimmed)
    # Children are never in the caption - they sit in a roster partway down -
    # so they get their own pass and join the same list. Proposed, not applied.
    return found + collect_children(files, progress)


def collect_children(files: Sequence[Path],
                     progress: Progress = _noop) -> list[caption.CaptionName]:
    """Children and their birth dates, as proposals for the same name screen.

    Reads every page, not the caption's six, so every page is read ahead
    first, a few at once - one page at a time it took hours on a large batch
    while the page counter, which had counted only the caption pages, sat full.
    """
    seen: dict[str, caption.CaptionName] = {}
    _read_ahead(files, progress)
    total = max(len(files), 1)
    for index, raw_path in enumerate(files):
        path = Path(raw_path)
        progress(f"Reading {path.name} for children", index / total)
        kind = classify(path)
        try:
            if kind == "docx":
                text = "\n".join(docx_processor.extract_text(path).values())
            elif kind == "pdf":
                text = "\n".join(pdf_processor.extract_text(path).values())
            else:
                continue
            tables = tables_in(path)
        except Exception:
            continue
        for child in children.harvest(text, tables, path.name):
            seen.setdefault(child.key, caption.CaptionName(
                child.name, child.role, child.source, "high", child.category))
            if child.born:
                seen.setdefault(f"dob:{child.born.casefold()}", caption.CaptionName(
                    child.born, "Child's date of birth", child.source, "high", "dob"))
        # a decree that restores a maiden name prints both, and both identify
        # the same woman - "shall return to her former name of Rowena Radcliffe"
        for current, former in caption.former_names(text):
            seen.setdefault(former.casefold(), caption.CaptionName(
                former, f"Former name of {current}", path.name, "high", "person"))
    return list(seen.values())


def collect_officials(files: Sequence[Path],
                      progress: Progress = _noop) -> list[officials.Official]:
    """Judicial officers named anywhere in the batch.

    Reads whole documents rather than the caption region: the caption carries
    the assigned judge, but the officer who actually signed an order is in the
    block at the very bottom, and both have to survive the run untouched.
    """
    found: dict[str, str] = {}
    _read_ahead(files, progress)
    total = max(len(files), 1)
    for index, raw_path in enumerate(files):
        path = Path(raw_path)
        progress(f"Reading {path.name} for judicial officers", index / total)
        kind = classify(path)
        try:
            if kind == "docx":
                parts = docx_processor.extract_text(path)
            elif kind == "pdf":
                parts = pdf_processor.extract_text(path)
            else:
                continue
        except Exception:
            continue
        for part, text in parts.items():
            if text.strip():
                # the full part path, not its basename: word/document.xml and
                # word/glossary/document.xml share a basename, and keying on it
                # let a 67-byte glossary stub overwrite the entire document body
                found[f"{path.name} [{part}]"] = text
    return officials.harvest_documents(found)


# --------------------------------------------------------------------------
# phase 2 - what the model thinks we missed
# --------------------------------------------------------------------------


def document_texts(files: Sequence[Path], progress: Progress = _noop,
                   readings: bool = True) -> dict[str, str]:
    """Each file's text, joined. ``readings=False`` leaves out the PDF reader's
    extra readings - the visual rows and the second OCR engine - which the
    rules want and the language model does not: it reads "Jenna Harrington
    <column gap> Colton Hughes" as one person."""
    texts: dict[str, str] = {}
    _read_ahead(files, progress)
    total = max(len(files), 1)
    for index, path in enumerate(files):
        path = Path(path)
        progress(f"Reading {path.name}", index / total)
        kind = classify(path)
        try:
            if kind == "docx":
                parts = docx_processor.extract_text(path)
            elif kind == "pdf":
                parts = pdf_processor.extract_text(path)
            else:
                continue
        except Exception:
            continue
        if not readings:
            parts = {label: text for label, text in parts.items()
                     if not label.endswith("(rows)") and "(ocr " not in label}
        joined = "\n".join(text for text in parts.values() if text.strip())
        if joined.strip():
            texts[path.name] = joined
    return texts


def collect_suggestions(
    files: Sequence[Path],
    store: MappingStore,
    settings: Settings,
    progress: Progress = _noop,
    caption_names: Sequence[caption.CaptionName] | None = None,
) -> tuple[list[ner.Suggestion], list[str]]:
    """Proposals for people, organisations and places, minus what we have."""
    notes: list[str] = []
    texts = document_texts(files, progress)

    from . import patterns

    protected = {
        label: patterns.allowlist_spans(text, settings.do_not_change)
        for label, text in texts.items()
    }

    known = _known_names(store, settings, caption_names)

    # Towns and cities come from rules and a gazetteer, not from the model, so
    # they are gathered before the model is consulted and survive a run where
    # it is switched off or missing. Everything above is a veto: a town that
    # shares its name with somebody in this document is that person here.
    progress("Looking for town and city names", 0.55)
    found = places.harvest_documents(texts, protected, frozenset(known))
    rules_found = [ner.Suggestion(place.name, "location", 1, {place.source})
                   for place in found]

    # The people a bank statement names and a pleading never does: whoever is on
    # the other end of a transfer. Proposed as people, so a ticked one gets an
    # invented name and the statement still reads as a statement.
    tables: dict[str, list] = {}
    total = max(len(files), 1)
    for index, raw_path in enumerate(files):
        path = Path(raw_path)
        if path.name in texts:
            # its own message: on a large set this pass went on for hours
            # under "Looking for town and city names"
            progress(f"Reading the tables in {path.name}", 0.55 + 0.05 * index / total)
            tables[path.name] = tables_in(path)
    # a blank line between readings and between columns: joined line to line,
    # the end of one column ran into the start of the next - "Daily Cash"
    # (a description) over "Sent" (the type column's first cell) - and a rule
    # reading a row's two lines took the app's cash-back line for a person
    columns = {name: "\n\n".join([texts[name], *column_texts(found_tables)])
               for name, found_tables in tables.items()}
    rules_found += [
        ner.Suggestion(party.name, party.category, 1, {party.source})
        for party in transactions.harvest_documents(columns, tables, frozenset(known),
                                                    feeds=texts)
    ]

    if not settings.use_ner:
        return rules_found, ["Model suggestions were turned off for this run."]

    ok, note = ner.available()
    if not ok:
        return rules_found, [note]

    progress("Reviewing documents for additional names", 0.6)
    plain = document_texts(files, readings=False)
    raw = ner.suggest(plain, {label: patterns.allowlist_spans(text, settings.do_not_change)
                              for label, text in plain.items()})

    # On a statement, a bill or a pay stub the rules already propose the two
    # organizations that identify the client - the employer and their own
    # business - and every other organization is a merchant, which must be
    # left for the reader to look up. The model's organizations are kept only
    # where some non-financial document named them too.
    financial = {name for name, text in plain.items() if _is_financial(text)}
    raw = [s for s in raw
           if s.category != "organization" or not s.documents <= financial]

    seen = {item.key for item in rules_found}
    filtered = rules_found + [s for s in raw
                              if s.text.casefold() not in known
                              and s.key not in seen]
    filtered = ner.drop_bare_names(filtered)

    notes.append(note)
    return _as_diminutives(filtered, store, caption_names), notes


def _known_names(store: MappingStore, settings: Settings,
                 caption_names: Sequence[caption.CaptionName] | None) -> set[str]:
    """Names never to propose: what the list has, the caption's, the bench's."""
    known: set[str] = set()
    for entity in store.entities.values():
        known.add(entity.canonical.casefold())
        for variant in entity.variants:
            known.add(variant.text.casefold())
    # Caption parties may not be in the store yet (the operator has not ticked
    # them), but their surnames must never resurface as "organization" rows.
    for item in caption_names or ():
        known.add(item.name.casefold())
        known.update(token.casefold() for token in item.name.split())
    # a judge proposed as a person is the one suggestion nobody should be able
    # to tick by accident
    for term in settings.protected_names:
        known.add(term.casefold())
        known.update(token.casefold() for token in term.split())
    return known


def _as_diminutives(found: list, store: MappingStore,
                    caption_names: Sequence[caption.CaptionName] | None) -> list:
    """A diminutive of a party's first name is that party, and the model has no
    idea - it offered "Chrissy" as an organization while Christine sat on the
    name list, and the nickname shipped. Retype it and say who it belongs to."""
    parties = [entity.canonical for entity in store.persons()]
    parties += [item.name for item in caption_names or ()]
    out = list(found)
    for index, suggestion in enumerate(out):
        # a confirmed town is not somebody's diminutive, whatever the table
        # says: Bill, Jack and Vernal are all real places
        if suggestion.category == "location":
            continue
        owner = names.nickname_for(suggestion.text, parties)
        if owner:
            out[index] = _replace(suggestion, category="person", nickname_for=owner)
    return out


def refine_suggestions(found: list, store: MappingStore, settings: Settings,
                       caption_names: Sequence[caption.CaptionName] | None = None) -> list:
    """Suggestions worked out before the name list existed, fitted to it.

    A set's suggestions are gathered overnight, as soon as its pages are read,
    with no names typed yet - so in the morning its name screen opens at once.
    What the list now holds is taken back out, and a diminutive of a party is
    retyped as that party, exactly as a fresh scan would.
    """
    known = _known_names(store, settings, caption_names)
    return _as_diminutives([s for s in found if s.text.casefold() not in known],
                           store, caption_names)


_FIGURE = re.compile(r"(?<![\d,.])\$?\d{1,3}(?:,\d{3})*\.\d{2}(?!\d)")
# a pleading quotes a few figures; a statement, a bill or a stub is made of them
FINANCIAL_FIGURES = 12


def _is_financial(text: str) -> bool:
    return len(_FIGURE.findall(text)) >= FINANCIAL_FIGURES


# A column heading is a few words. Anything longer is prose that happened to
# land in the first row, and pairing a paragraph with every cell beneath it
# invents label/value pairs that were never on the page.
_TABLE_LABEL_MAX = 60


def register_table_context(files: Sequence[Path], store: MappingStore,
                           settings: Settings, progress: Progress = _noop) -> int:
    """Read the label/value pairs a table splits across two cells.

    A DOCX table cell is its own paragraph, so the scanner is handed
    ``'Savings account'`` and ``'000488227145'`` as two separate strings, and
    every labelled detector in patterns.py needs them in one. On a financial
    declaration's asset schedule that is not an edge case, it is the whole
    document: every account number in the assets and debts tables survived.

    Four readings, because a table puts the label in four different places: the
    column heading, the cell directly above, the cell to the left, and the
    first cell of the row. All four register only - the strings are
    reconstructions and appear nowhere on the page, so applying anything to
    them would be applying it to text that does not exist.

    The cell directly above is what an IRS form needs. A 1099 is a grid of
    alternating label and value rows, so the heading for "HCS-0044-2211" is not
    in row 0 at all - row 0 says "RECIPIENT'S TIN" and the real label,
    "Account number (see instructions)", is in the row immediately above. Only
    reading row 0 as the header shipped the account number.

    Every pair is joined with a colon, and that is load-bearing rather than
    cosmetic. Reading a whole row across with spaces let the bare-digits card
    detector run from the account column into the money column and register
    "44-8821-0033 950" as a credit card. A colon cannot appear inside any
    identifier shape, so it fences each cell off from the next.
    """
    found = 0
    total = max(len(files), 1)
    for index, raw_path in enumerate(files):
        path = Path(raw_path)
        kind = classify(path)
        if kind not in ("docx", "pdf"):
            continue
        progress(f"Reading tables in {path.name}", index / total)
        # A PDF has no table elements, only words with coordinates, so its
        # columns are reconstructed from the gaps between them. Statements
        # arrive as PDFs far more often than as DOCX, and without this the
        # commonest input gets none of this pass.
        tables = tables_in(path)
        for rows in tables:
            if not rows:
                continue
            header = rows[0]
            for index, row in enumerate(rows):
                above = rows[index - 1] if index else []
                for column, cell in enumerate(row):
                    if not cell:
                        continue
                    for label in (header[column] if column < len(header) else "",
                                  above[column] if column < len(above) else "",
                                  row[column - 1] if column else "",
                                  row[0] if column else ""):
                        if (label and label != cell
                                and len(label) <= _TABLE_LABEL_MAX):
                            found += engine.register_text(
                                f"{label}: {cell}", store, settings)
    return found


# The last line of a mailing address: a state and a ZIP, and nothing after.
_CITY_LINE = re.compile(r"\b[A-Z]{2}\.?[^\S\n]+\d{5}(?:-\d{4})?[^\S\n]*$", re.IGNORECASE)
_MONEY = re.compile(r"\d[\d,]*\.\d{2}|\$\s?\d")      # cents, or a $ on whole dollars
# the same lockbox and institution tests the PDF path's geometric cover uses
_LOCKBOX = pdf_processor._LOCKBOX
_INSTITUTION = pdf_processor._INSTITUTION_LINE
ADDRESS_BLOCK_LINES = 3
ADDRESS_LINE_CHARS = pdf_processor.ADDRESS_LINE_CHARS
_COLUMN_GAP = re.compile(r"\S {%d,}\S" % pdf_processor.MIN_GAP_SPACES)


def register_address_blocks(files: Sequence[Path], store: MappingStore,
                            settings: Settings, progress: Progress = _noop) -> int:
    """Register the lines of every mailing-address block, as text.

    The PDF path covers an address block by its geometry - the line ending in
    a state and a ZIP and the short lines above it - which is the only
    reliable way to take a coupon's address whole. A Word document has no
    geometry to read, and the same block typed into one came out with its
    city, state and ZIP+4 intact under a redacted street. This is the same
    rule applied to text: the city line and the street lines above it become
    known values, which the literal pass then finds wherever they are - in a
    paragraph, a table cell, or a text box. A lockbox and the institution that
    owns it are left alone, as they are on the PDF path.
    """
    if not settings.category_enabled("street_address"):
        return 0
    found = 0
    for raw_path in files:
        path = Path(raw_path)
        kind = classify(path)
        try:
            if kind == "docx":
                parts = docx_processor.extract_text(path)
            elif kind == "pdf":
                parts = pdf_processor.extract_text(path)
            else:
                continue
        except Exception:
            continue
        for text in [*parts.values(), *column_texts(tables_in(path))]:
            # A line with a column gap in it is a table row, not an address
            # line: squeezing the gap out made "Bath Set   8542 N ORCHARD DR
            # 1734 W GENTILE ST" - a title's tail and two cells - one value.
            # The column readings carry each cell on a line of its own.
            lines = ["" if _COLUMN_GAP.search(line.strip()) else " ".join(line.split())
                     for line in text.splitlines()]
            for index, line in enumerate(lines):
                if (not _CITY_LINE.search(line) or _MONEY.search(line)
                        or len(line) > ADDRESS_LINE_CHARS or _LOCKBOX.search(line)):
                    continue
                block = [line]
                for above in range(index - 1, max(index - 1 - ADDRESS_BLOCK_LINES, -1), -1):
                    candidate = lines[above]
                    if (not candidate or len(candidate) > ADDRESS_LINE_CHARS
                            or _MONEY.search(candidate) or _INSTITUTION.search(candidate)
                            or _LOCKBOX.search(candidate) or not caption.is_address(candidate)):
                        break
                    block.append(candidate)
                if len(block) < 2:
                    continue            # a city line with no street is not a block
                for value in block:
                    if store.get("street_address", value) is None:
                        store.add_value("street_address", value, source="address block")
                        found += 1
    return found


def prescan(
    files: Sequence[Path],
    store: MappingStore,
    settings: Settings,
    progress: Progress = _noop,
) -> int:
    """Populate the store with everything the run will find, changing nothing.

    Walks the same text units in the same order as the real run, so placeholder
    numbering and the review screen both match what actually gets written.
    """
    matcher = PersonMatcher(store, settings)
    total = max(len(files), 1)
    if settings.ocr_scanned_pdfs:
        _read_ahead(files, progress)
    # Tables first: a label/value pair split across two cells has to be a known
    # value before the paragraph walk reaches the cell holding the bare number.
    found = register_table_context(files, store, settings, progress)
    found += register_address_blocks(files, store, settings, progress)
    for index, raw_path in enumerate(files):
        path = Path(raw_path)
        kind = classify(path)
        progress(f"Scanning {path.name}", index / total)
        if kind == "docx":
            units = docx_processor.iter_text_units(path)
        elif kind == "pdf":
            units = pdf_processor.iter_text_units(path)
        else:
            continue
        try:
            for text in units:
                hits = engine.scan_text(text, store, settings, matcher, register=True)
                found += len(hits)
                # record occurrences, or the review screen shows Times = 0 on
                # every row and warns that nothing appeared in any document
                for hit in hits:
                    entity = store.entities.get(hit.entity_key)
                    if entity is not None:
                        store.record_hit(entity, path.name)
        except Exception:
            continue
    # one row per person, address or account however many ways it was written
    grouping.group(store)
    store.grouped = True
    progress("Scan complete", 1.0)
    return found


# --------------------------------------------------------------------------
# phase 3 - do the work
# --------------------------------------------------------------------------


@dataclass
class FileOutcome:
    source: Path
    kind: str
    status: str                    # processed | refused | error | skipped
    delivered_name: str = ""       # relative path inside the archive, "/"-separated
    hits: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    detail: object | None = None
    original: str = ""             # where it sat in what was dropped in

    @property
    def shown(self) -> str:
        return self.original or self.source.name


@dataclass
class RunResult:
    output_dir: Path
    archive: Path | None = None
    key_path: Path | None = None
    report_path: Path | None = None
    outcomes: list[FileOutcome] = field(default_factory=list)
    started: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    finished: datetime | None = None
    notes: list[str] = field(default_factory=list)
    skipped: list[str] = field(default_factory=list)   # left out: not PDF or Word

    @property
    def processed(self) -> list[FileOutcome]:
        return [o for o in self.outcomes if o.status == "processed"]

    @property
    def failed(self) -> list[FileOutcome]:
        return [o for o in self.outcomes if o.status in {"refused", "error"}]


_SEPARATORS = re.compile(r"[_\-.\s]+")

# A name glued to its neighbours has no word boundary for the matcher to find:
# SmithDivorcePetition, Smith2024Decree, JSmith-Findings. Splitting on case and
# letter/digit transitions gives those a boundary. It is only used as a second
# attempt, because splitting unconditionally would mangle names that legitimately
# carry an internal capital - MacDonald, DeLuca, O'Brien.
_GLUE = re.compile(
    r"(?<=[a-z0-9])(?=[A-Z])"          # smithDivorce -> smith Divorce
    r"|(?<=[A-Z])(?=[A-Z][a-z])"       # JSmith       -> J Smith
    r"|(?<=[A-Za-z])(?=\d)"            # Smith2024    -> Smith 2024
    r"|(?<=\d)(?=[A-Za-z])"            # 2024Smith    -> 2024 Smith
)


def _sensitive_strings(store: MappingStore) -> set[str]:
    """Every string that must not survive into a delivered filename.

    Includes each person's individual written forms, not just the full name -
    a filename almost never carries "Jane Elizabeth Smith", it carries "Smith".
    """
    out: set[str] = set()
    for entity in store.entities.values():
        if not entity.enabled:
            continue
        # two-letter surnames (Ng, Li) are real; a 3-char floor let them ship
        if len(entity.canonical) >= 2:
            out.add(entity.canonical.casefold())
        for variant in entity.variants:
            if len(variant.text) >= 2:
                out.add(variant.text.casefold())
    return out


def _leaks(candidate: str, sensitive: set[str]) -> bool:
    """Substring test, deliberately ignoring word boundaries.

    This is the last line of defence, so it is stricter than the matcher: if a
    client's name appears anywhere in the proposed filename, in any form, the
    name is rejected outright in favour of a neutral one.
    """
    lowered = candidate.casefold()
    return any(term in lowered for term in sensitive)


def _tidy(text: str) -> str:
    # only whitespace collapses here - the stem was already split on separators
    # before scanning, so any hyphen left is part of a replacement like [CASENO-1]
    cleaned = re.sub(r"\s+", "_", text.strip())
    cleaned = re.sub(r"[^\w\[\]\-]+", "_", cleaned)
    cleaned = re.sub(r"_{2,}", "_", cleaned)
    return cleaned.strip("_")


def anonymized_filename(path: Path, store: MappingStore, settings: Settings,
                        matcher: PersonMatcher, index: int) -> str:
    """A delivered filename with client identifiers taken out of it.

    Filenames leak as readily as document bodies - ``Smith_Divorce_Petition.docx``
    names the client before anyone opens it.
    """
    stem, suffix = path.stem, path.suffix.lower()
    if not settings.anonymize_filenames:
        return f"{stem}{suffix}"
    cleaned = _anonymized_stem(stem, store, settings, matcher)
    # Nothing safe could be salvaged from the original name.
    return f"{cleaned or f'document_{index:02d}'}{suffix}"


def anonymized_folder(name: str, store: MappingStore, settings: Settings,
                      matcher: PersonMatcher, index: int) -> str:
    """A delivered folder name, scrubbed the way a filename is.

    "Harrington Divorce/2024 Bank Statements" names the client twice before a
    single document is opened.
    """
    if not settings.anonymize_filenames:
        return name
    return _anonymized_stem(name, store, settings, matcher) or f"folder_{index:02d}"


def _anonymized_stem(stem: str, store: MappingStore, settings: Settings,
                     matcher: PersonMatcher) -> str:
    """The stem with client identifiers replaced, or "" if none can be made safe."""
    sensitive = _sensitive_strings(store)
    spaced = _SEPARATORS.sub(" ", stem).strip()

    # First a conservative pass; if anything recognisable survives it, try again
    # with the glued forms broken apart.
    for candidate in (spaced, _GLUE.sub(" ", spaced)):
        hits = engine.scan_text(candidate, store, settings, matcher, register=True)
        # nothing to take out: the name goes through exactly as written, so a
        # dropped folder comes back as the same tree ("2024 Bank Statements",
        # not "2024_Bank_Statements")
        if not hits and not _leaks(stem, sensitive):
            return stem
        cleaned = _tidy(engine.apply_hits(candidate, hits))
        if cleaned and not _leaks(cleaned, sensitive):
            return cleaned
    return ""


class _FolderNamer:
    """Each source folder's delivered path, the same for every file inside it.

    Two different folders that scrub to the same name in one parent are kept
    apart ("[NAME-1]_Divorce", "[NAME-1]_Divorce_2"): merging them would mix
    two sets of documents under one heading.
    """

    def __init__(self, store: MappingStore, settings: Settings, matcher: PersonMatcher):
        self.store, self.settings, self.matcher = store, settings, matcher
        self.done: dict[tuple[str, ...], tuple[str, ...]] = {(): ()}
        self.used: dict[tuple[str, ...], set[str]] = {}

    def __call__(self, parts: tuple[str, ...]) -> tuple[str, ...]:
        if parts not in self.done:
            parent = self(parts[:-1])
            name = anonymized_folder(parts[-1], self.store, self.settings, self.matcher,
                                     len(self.done))
            self.done[parts] = parent + (_unique(name, self.used.setdefault(parent, set())),)
        return self.done[parts]


# --------------------------------------------------------------------------
# folders dropped in whole
# --------------------------------------------------------------------------

# Never documents and never worth a line in the report: Finder's and Windows'
# own bookkeeping, and the lock file Word leaves beside an open document.
_HOUSEKEEPING = {"thumbs.db", "desktop.ini", "__macosx", "icon\r"}


def _housekeeping(name: str) -> bool:
    return name.startswith((".", "~$")) or name.casefold() in _HOUSEKEEPING


@dataclass
class Gathered:
    """What a set of dropped files and folders holds."""

    files: list[tuple[Path, str]] = field(default_factory=list)   # (on disk, relative)
    skipped: list[str] = field(default_factory=list)              # relative, left out


def gather(paths: Sequence[Path]) -> Gathered:
    """Every PDF and Word file under ``paths``, with where each sat.

    A folder's own name is the first part of each relative path, so the tree
    comes back out under the same top folder. A photo or screenshot is
    gathered like a document, to be made into a PDF. Anything else is left out
    and listed, never delivered: a CSV export or an old .doc would otherwise
    ship with its contents intact. Links are not
    followed - a link inside a case folder can point anywhere on the disk.
    """
    out = Gathered()
    for raw in paths:
        root = Path(raw)
        if root.is_file():
            if not _housekeeping(root.name):
                (out.files.append((root, root.name)) if classify(root)
                 else out.skipped.append(root.name))
            continue
        if not root.is_dir():
            continue
        for dirpath, dirnames, filenames in os.walk(root, followlinks=False):
            dirnames[:] = sorted(d for d in dirnames if not _housekeeping(d))
            for name in sorted(filenames):
                if _housekeeping(name):
                    continue
                path = Path(dirpath) / name
                relative = path.relative_to(root.parent).as_posix()
                if path.is_symlink() or not classify(path):
                    out.skipped.append(relative)
                else:
                    out.files.append((path, relative))
    return out


def _unique(name: str, used: set[str]) -> str:
    if name.casefold() not in used:
        used.add(name.casefold())
        return name
    stem, suffix = Path(name).stem, Path(name).suffix
    counter = 2
    while f"{stem}_{counter}{suffix}".casefold() in used:
        counter += 1
    final = f"{stem}_{counter}{suffix}"
    used.add(final.casefold())
    return final


def run_job(
    files: Sequence[Path],
    store: MappingStore,
    settings: Settings,
    output_dir: Path,
    key_password: str,
    progress: Progress = _noop,
    relative: dict[Path, str] | None = None,
    skipped: Sequence[str] = (),
) -> RunResult:
    """Process every file, archive the results, write the encrypted key.

    ``relative`` gives each file's place in a dropped folder ("Case/2024/
    statement.pdf"); the archive repeats that tree, folder names scrubbed like
    filenames. ``skipped`` lists what the folders held that was not processed,
    for the report.
    """
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    result = RunResult(output_dir=output_dir)
    stamp = result.started.strftime("%Y%m%d-%H%M%S")
    work_dir = output_dir / f"_work-{stamp}"
    work_dir.mkdir(parents=True, exist_ok=True)

    matcher = PersonMatcher(store, settings)
    used_names: dict[tuple[str, ...], set[str]] = {}
    folders = _FolderNamer(store, settings, matcher)
    relative = relative or {}
    result.skipped = list(skipped)
    total = max(len(files), 1)

    # the review prescan recorded occurrences into this same store; the run
    # re-records everything it applies, so start the counters clean
    for entity in store.entities.values():
        entity.occurrences = 0
        entity.documents.clear()

    if settings.ocr_scanned_pdfs:
        _read_ahead(files, progress)
    for index, raw_path in enumerate(files, start=1):
        path = Path(raw_path)
        kind = classify(path)
        original = relative.get(raw_path) or relative.get(path) or path.name
        progress(f"Processing {original}", (index - 1) / total)

        if kind is None:
            result.outcomes.append(
                FileOutcome(path, "unknown", "skipped", error="unsupported file type",
                            original=original)
            )
            continue
        if kind == "image":
            # the front ends convert a photo when it is added; this is for a
            # caller that hands one straight to the run
            try:
                path, kind = as_document(path, work_dir / "_photos"), "pdf"
            except Exception as exc:
                result.outcomes.append(FileOutcome(
                    path, "image", "error", error=f"could not read the image: {exc}",
                    original=original))
                continue

        place = folders(tuple(PurePosixPath(original).parts[:-1]))
        delivered = "/".join(place + (_unique(
            anonymized_filename(path, store, settings, matcher, index),
            used_names.setdefault(place, set())),))
        target = work_dir / delivered
        target.parent.mkdir(parents=True, exist_ok=True)

        try:
            if kind == "docx":
                detail = docx_processor.process(path, target, store, settings, matcher)
            else:
                detail = pdf_processor.process(path, target, store, settings, matcher)
            if detail.refused:
                outcome = FileOutcome(path, kind, "refused", "", len(detail.hits),
                                      list(detail.warnings), detail=detail, original=original)
            else:
                outcome = FileOutcome(path, kind, "processed", delivered,
                                      len(detail.hits), list(detail.warnings), detail=detail,
                                      original=original)
        except Exception as exc:
            outcome = FileOutcome(path, kind, "error", error=f"{type(exc).__name__}: {exc}",
                                  original=original)
            outcome.warnings.append(traceback.format_exc(limit=3))

        if outcome.status == "processed":
            store.register_filename(original, delivered)
        result.outcomes.append(outcome)

    progress("Building the archive", 0.9)
    processed = result.processed
    if processed:
        archive = output_dir / f"anonymized-{stamp}.zip"
        with zipfile.ZipFile(archive, "w", zipfile.ZIP_DEFLATED) as zf:
            for outcome in processed:
                zf.write(work_dir / outcome.delivered_name, arcname=outcome.delivered_name)
        result.archive = archive

    if key_password:
        result.key_path = mapping.write_encrypted_key(
            store, output_dir / f"mapping-key-{stamp}.json", key_password
        )

    result.finished = datetime.now(timezone.utc)
    result.report_path = write_report(result, store, settings,
                                      output_dir / f"redaction-report-{stamp}.txt")

    shutil.rmtree(work_dir, ignore_errors=True)

    progress("Done", 1.0)
    return result


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------


def write_report(result: RunResult, store: MappingStore, settings: Settings,
                 path: Path) -> Path:
    """A defensible summary of the run.

    Redacted values never appear - counts by category only. It DOES name the
    original source files, so the report itself is confidential: keep it with
    the mapping key, never inside anything delivered.
    """
    lines: list[str] = []
    add = lines.append

    add("DOCUMENT REDACTIONS & ANONYMIZATION - RUN REPORT")
    add("This report names the original files. Keep it with the mapping key,")
    add("never inside anything you deliver.")
    add("=" * 62)
    add(f"Started (UTC):      {result.started.isoformat(timespec='seconds')}")
    if result.finished:
        add(f"Finished (UTC):     {result.finished.isoformat(timespec='seconds')}")
    add(f"DOCX mode:          {settings.docx_mode}")
    add(f"PDF mode:           {settings.pdf_mode}"
        + ("  (scanned pages and images still redacted)" if settings.pdf_mode == "anonymize" else ""))
    add(f"Filenames:          {'anonymized' if settings.anonymize_filenames else 'unchanged'}")
    add(f"Metadata scrubbed:  {settings.scrub_metadata}")
    add(f"Comments/revisions: {settings.scrub_comments}")
    add(f"Links/attachments:  {settings.scrub_embedded}")
    add(f"OCR for scans:      {settings.ocr_scanned_pdfs}")
    add("")

    add("FILES")
    add("-" * 62)
    for outcome in result.outcomes:
        add(f"  {outcome.shown}")
        add(f"      status:    {outcome.status}")
        if outcome.delivered_name:
            add(f"      delivered: {outcome.delivered_name}")
        add(f"      changes:   {outcome.hits}")
        if outcome.error:
            add(f"      error:     {outcome.error}")
        for warning in outcome.warnings:
            add(f"      WARNING:   {warning.splitlines()[0]}")
    add("")

    if result.skipped:
        add("LEFT OUT - NOT A DOCUMENT OR PHOTO, NOTHING FROM THESE WAS DELIVERED")
        add("-" * 62)
        for name in result.skipped:
            add(f"  {name}")
        add("")

    add("CHANGES BY CATEGORY")
    add("-" * 62)
    counts: dict[str, tuple[int, int]] = {}
    for entity in store.entities.values():
        if not entity.occurrences:
            continue
        entities, hits = counts.get(entity.category, (0, 0))
        counts[entity.category] = (entities + 1, hits + entity.occurrences)
    if counts:
        for key in sorted(counts, key=lambda k: (-counts[k][1], k)):
            entities, hits = counts[key]
            add(f"  {categories.label_for(key):38s} {entities:4d} distinct   {hits:5d} replaced")
    else:
        add("  (nothing matched)")
    add("")

    add("LEFT UNTOUCHED ON PURPOSE")
    add("-" * 62)
    if settings.protected_names:
        add("  Judicial officers found in these documents. A judge is not a party,")
        add("  and an order that comes back with the bench renamed reads as tampered")
        add("  with. Every form of these names was shielded:")
        for term in settings.protected_names:
            add(f"      {term}")
    else:
        add("  No judicial officer was found in these documents.")
    if settings.extra_allowlist:
        add("  Terms you added to the allowlist yourself:")
        for term in settings.extra_allowlist:
            add(f"      {term}")
    add("")

    add("NOT COVERED BY THIS RUN")
    add("-" * 62)
    add("  * Text inside embedded images (scanned signatures, photographed")
    add("    statements, screenshots) is not read by the DOCX path.")
    add("  * Handwriting is not read by OCR.")
    add("  * Anything the reviewer excluded on the review screen.")
    for note in result.notes:
        add(f"  * {note}")
    add("")
    add("The mapping key is written separately and encrypted. Do not place it")
    add("in the delivered archive.")

    path = Path(path)
    path.write_text("\n".join(lines), encoding="utf-8")
    return path
