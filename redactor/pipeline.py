"""Run orchestration: gather suggestions, process files, build the archive."""

from __future__ import annotations

import re
import traceback
import zipfile
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Iterable, Sequence

from dataclasses import replace as _replace

from . import (caption, categories, children, docx_processor, engine, mapping,
               names, ner, officials, pdf_processor, places)
from .engine import PersonMatcher, Settings
from .mapping import MappingStore

Progress = Callable[[str, float], None]

SUPPORTED = {".docx": "docx", ".pdf": "pdf"}


def classify(path: Path) -> str | None:
    return SUPPORTED.get(Path(path).suffix.lower())


def _noop(_message: str, _fraction: float) -> None:
    pass


# --------------------------------------------------------------------------
# phase 1 - what the caption already tells us
# --------------------------------------------------------------------------


def collect_caption_names(files: Sequence[Path], progress: Progress = _noop) -> list[caption.CaptionName]:
    """Party names harvested from every selected document's caption and headers."""
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
    """Children and their birth dates, as proposals for the same name screen."""
    seen: dict[str, caption.CaptionName] = {}
    total = max(len(files), 1)
    for index, raw_path in enumerate(files):
        path = Path(raw_path)
        progress(f"Reading {path.name} for children", index / total)
        kind = classify(path)
        try:
            if kind == "docx":
                text = "\n".join(docx_processor.extract_text(path).values())
                tables = list(docx_processor.iter_tables(path))
            elif kind == "pdf":
                text = "\n".join(pdf_processor.extract_text(path).values())
                tables = []
            else:
                continue
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


def document_texts(files: Sequence[Path], progress: Progress = _noop) -> dict[str, str]:
    texts: dict[str, str] = {}
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

    # Towns and cities come from rules and a gazetteer, not from the model, so
    # they are gathered before the model is consulted and survive a run where
    # it is switched off or missing. Everything above is a veto: a town that
    # shares its name with somebody in this document is that person here.
    progress("Looking for town and city names", 0.55)
    found = places.harvest_documents(texts, protected, frozenset(known))
    place_items = [ner.Suggestion(place.name, "location", 1, {place.source})
                   for place in found]

    if not settings.use_ner:
        return place_items, ["Model suggestions were turned off for this run."]

    ok, note = ner.available()
    if not ok:
        return place_items, [note]

    progress("Reviewing documents for additional names", 0.6)
    raw = ner.suggest(texts, protected)

    seen = {item.key for item in place_items}
    filtered = place_items + [s for s in raw
                              if s.text.casefold() not in known
                              and s.key not in seen]

    # A diminutive of a party's first name is that party, and the model has no
    # idea - it offered "Chrissy" as an organization while Christine sat on the
    # name list, and the nickname shipped. Retype it and say who it belongs to.
    parties = [entity.canonical for entity in store.persons()]
    parties += [item.name for item in caption_names or ()]
    for index, suggestion in enumerate(filtered):
        # a confirmed town is not somebody's diminutive, whatever the table
        # says: Bill, Jack and Vernal are all real places
        if suggestion.category == "location":
            continue
        owner = names.nickname_for(suggestion.text, parties)
        if owner:
            filtered[index] = _replace(suggestion, category="person",
                                      nickname_for=owner)

    notes.append(note)
    return filtered, notes


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
    found = 0
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
    delivered_name: str = ""
    hits: int = 0
    warnings: list[str] = field(default_factory=list)
    error: str = ""
    detail: object | None = None


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

    sensitive = _sensitive_strings(store)
    spaced = _SEPARATORS.sub(" ", stem).strip()

    # First a conservative pass; if anything recognisable survives it, try again
    # with the glued forms broken apart.
    for candidate in (spaced, _GLUE.sub(" ", spaced)):
        replaced = engine.apply_hits(
            candidate, engine.scan_text(candidate, store, settings, matcher, register=True)
        )
        cleaned = _tidy(replaced)
        if cleaned and not _leaks(cleaned, sensitive):
            return f"{cleaned}{suffix}"

    # Nothing safe could be salvaged from the original name.
    return f"document_{index:02d}{suffix}"


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
) -> RunResult:
    """Process every file, archive the results, write the encrypted key."""
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    result = RunResult(output_dir=output_dir)
    stamp = result.started.strftime("%Y%m%d-%H%M%S")
    work_dir = output_dir / f"_work-{stamp}"
    work_dir.mkdir(parents=True, exist_ok=True)

    matcher = PersonMatcher(store, settings)
    used_names: set[str] = set()
    total = max(len(files), 1)

    # the review prescan recorded occurrences into this same store; the run
    # re-records everything it applies, so start the counters clean
    for entity in store.entities.values():
        entity.occurrences = 0
        entity.documents.clear()

    for index, raw_path in enumerate(files, start=1):
        path = Path(raw_path)
        kind = classify(path)
        progress(f"Processing {path.name}", (index - 1) / total)

        if kind is None:
            result.outcomes.append(
                FileOutcome(path, "unknown", "skipped", error="unsupported file type")
            )
            continue

        delivered = _unique(
            anonymized_filename(path, store, settings, matcher, index), used_names
        )
        target = work_dir / delivered

        try:
            if kind == "docx":
                detail = docx_processor.process(path, target, store, settings, matcher)
            else:
                detail = pdf_processor.process(path, target, store, settings, matcher)
            if detail.refused:
                outcome = FileOutcome(path, kind, "refused", "", len(detail.hits),
                                      list(detail.warnings), detail=detail)
            else:
                outcome = FileOutcome(path, kind, "processed", delivered,
                                      len(detail.hits), list(detail.warnings), detail=detail)
        except Exception as exc:
            outcome = FileOutcome(path, kind, "error", error=f"{type(exc).__name__}: {exc}")
            outcome.warnings.append(traceback.format_exc(limit=3))

        if outcome.status == "processed":
            store.register_filename(path.name, delivered)
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

    for leftover in work_dir.glob("*"):
        try:
            leftover.unlink()
        except OSError:
            pass
    try:
        work_dir.rmdir()
    except OSError:
        pass

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
    add("PDF mode:           redact (always)")
    add(f"Filenames:          {'anonymized' if settings.anonymize_filenames else 'unchanged'}")
    add(f"Metadata scrubbed:  {settings.scrub_metadata}")
    add(f"Comments/revisions: {settings.scrub_comments}")
    add(f"Links/attachments:  {settings.scrub_embedded}")
    add(f"OCR for scans:      {settings.ocr_scanned_pdfs}")
    add("")

    add("FILES")
    add("-" * 62)
    for outcome in result.outcomes:
        add(f"  {outcome.source.name}")
        add(f"      status:    {outcome.status}")
        if outcome.delivered_name:
            add(f"      delivered: {outcome.delivered_name}")
        add(f"      changes:   {outcome.hits}")
        if outcome.error:
            add(f"      error:     {outcome.error}")
        for warning in outcome.warnings:
            add(f"      WARNING:   {warning.splitlines()[0]}")
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
