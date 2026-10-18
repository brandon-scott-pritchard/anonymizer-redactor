"""The final check's flagged pages, looked at and fixed in the app.

Kev answers yes or no for a page - is a ticked person still named on it - and
cannot say where. So each flagged page is shown as delivered, with its words
where they sit, and the likeliest places are marked: what the name matcher
read there, a word a letter or two off a ticked name, a word a ticked name is
run into. The operator picks the words - a marked one, or any others - says
what they are (a ticked person by default, the kind Kev looks for, or any
other type) and whose; they become one more written form of that person or
row, every file of the set they are written in is made again from its
original, the mapping key is written again, and the page is read back by the
matcher and Kev.

Nothing here writes the page's text anywhere: the image and the words are
made for the screen from the delivered archive, in memory.
"""

from __future__ import annotations

import re
import tempfile
import zipfile
from pathlib import Path

from . import grouping, merges

ZOOM = 1.6                      # the page image, against PDF points
SUSPECT_SIMILARITY = 0.85       # Jaro-Winkler to a ticked name part
PEOPLE = ("person", "minor")


def _member_bytes(archive: Path, member: str) -> bytes:
    with zipfile.ZipFile(archive) as zf:
        return zf.read(member)


# The page pictures made by the last page reads, for the image requests that
# follow: the picture is drawn inside the job (one step at a time in this
# process, as the PDF library needs), and only handed out here.
_PICTURES: dict[tuple[str, str, int], bytes] = {}
_PICTURES_KEPT = 8


def read_page(doc_set, member: str, number: int, matched: list[str]) -> dict:
    """The page's words, its marked words and - drawn and kept for the image
    request - its picture. Run as a job."""
    archive = Path(doc_set.run_result.archive)
    out = page_words(archive, member, number)
    out["suspects"] = suspects(out["words"], doc_set.store, matched)
    if out["image"]:
        _PICTURES[(doc_set.id, member, number)] = page_image(archive, member, number)
        while len(_PICTURES) > _PICTURES_KEPT:
            _PICTURES.pop(next(iter(_PICTURES)))
    return out


def picture(set_id: str, member: str, number: int) -> bytes | None:
    return _PICTURES.get((set_id, member, number))


def forget_pictures(set_id: str) -> None:
    for key in [k for k in _PICTURES if k[0] == set_id]:
        _PICTURES.pop(key, None)


def page_image(archive: Path, member: str, number: int) -> bytes:
    """The delivered page as a PNG."""
    import pymupdf

    with pymupdf.open(stream=_member_bytes(archive, member), filetype="pdf") as doc:
        page = doc[number - 1]
        return page.get_pixmap(matrix=pymupdf.Matrix(ZOOM, ZOOM)).tobytes("png")


def page_words(archive: Path, member: str, number: int) -> dict:
    """The page's words where they sit, in image pixels - its text layer, or
    what OCR reads off it when it has none (a redacted scan). A Word file
    has no pages to draw: its words come as text, without places."""
    if member.lower().endswith(".docx"):
        return {"image": False, "words": _docx_words(archive, member, number)}
    import pymupdf

    from . import pdf_processor

    with pymupdf.open(stream=_member_bytes(archive, member), filetype="pdf") as doc:
        page = doc[number - 1]
        raw = [w[:5] for w in page.get_text("words")]
        if not any(str(w[4]).strip() for w in raw):
            raw = [w[:5] for w in (pdf_processor._ocr_words(page) or [])]
        width, height = page.rect.width * ZOOM, page.rect.height * ZOOM
    words = [{"i": i, "text": str(w[4]), "x0": w[0] * ZOOM, "y0": w[1] * ZOOM,
              "x1": w[2] * ZOOM, "y1": w[3] * ZOOM}
             for i, w in enumerate(raw) if str(w[4]).strip()]
    for i, w in enumerate(words):
        w["i"] = i
    return {"image": True, "width": width, "height": height, "words": words}


def _docx_words(archive: Path, member: str, number: int) -> list[dict]:
    from . import audit

    with tempfile.TemporaryDirectory(prefix="final-page-") as tmp:
        path = Path(tmp) / Path(member).name
        path.write_bytes(_member_bytes(archive, member))
        text = next((t for n, t in audit._page_texts(path) if n == number), "")
    return [{"i": i, "text": t} for i, t in enumerate(text.split())]


# --------------------------------------------------------------------------
# where on the page a ticked name may still be
# --------------------------------------------------------------------------

def _letters(text: str) -> str:
    from . import name_match
    return name_match._letters(text)


def _ticked_heads(store) -> list:
    sheet = getattr(store, "parties", None) is not None
    return [e for e in store.entities.values()
            if e.enabled and e.category in PEOPLE and e.parent is None
            and e.person is not None and (not sheet or (e.fixed and e.party))]


def _parts(head) -> list[str]:
    person = head.person
    return [p for p in (_letters(x) for x in (person.first, *person.middles, person.last))
            if len(p) >= 3]


def _nicknames(head) -> list[str]:
    """The usual short forms of the first name: "abby" for Abigail."""
    from . import names

    first = _letters(head.person.first or "")
    return [n for n in names.NICKNAMES.get(first, ()) if len(n) >= 3]


def suspects(words: list[dict], store, matched: list[str]) -> dict:
    """{"words": indices to mark, "person": the head they point to most}."""
    from rapidfuzz.distance import JaroWinkler

    heads = _ticked_heads(store)
    parts = [(p, h) for h in heads for p in _parts(h)]
    nicknames = {n: h for h in heads for n in _nicknames(h)}
    said = {_letters(w) for m in matched for w in m.split()}
    marked, votes = set(), {}
    for w in words:
        word = _letters(w["text"])
        if len(word) < 3:
            continue
        hit = nicknames.get(word)
        if hit is None and word in said:
            hit = next((h for p, h in parts if p == word), None) or (heads[0] if heads else None)
        for part, head in parts:
            if hit is not None:
                break
            if len(word) >= 4 and (JaroWinkler.similarity(word, part) >= SUSPECT_SIMILARITY
                                   or (len(part) >= 4 and part in word and word != part)):
                hit = head
        if hit is not None or word in said:
            marked.add(w["i"])
            if hit is not None:
                votes[hit.key] = votes.get(hit.key, 0) + 1
    person = max(votes, key=votes.get) if votes else (heads[0].key if heads else "")
    return {"words": sorted(marked), "person": person}


def choices(store) -> list[dict]:
    """The rows a chosen text can be made a form of: every ticked head."""
    from . import categories

    out = []
    for e in store.entities.values():
        if e.parent is None and e.enabled:
            out.append({"key": e.key, "category": e.category, "label": categories.label_for(e.category),
                        "text": e.canonical, "replacement": e.replacement})
    out.sort(key=lambda r: (0 if r["category"] in PEOPLE else 1, r["category"], r["text"].casefold()))
    return out


# --------------------------------------------------------------------------
# the fix
# --------------------------------------------------------------------------

def kinds(settings) -> list[dict]:
    """The types a chosen text may be given: those the run replaces. A kind
    left as written by default - a loan or check number - would be filed and
    never replaced."""
    from . import categories

    on = set(getattr(settings, "enabled_categories", ()) or ())
    return [{"key": c.key, "label": c.label} for c in categories.CATEGORIES if c.key in on]


def register(store, text: str, category: str, target_key: str = "") -> str:
    """``text`` made a value the run replaces: a written form of the target
    row, or a row of its own. "" when done, else why not."""
    text = " ".join(text.split())
    if not text:
        return "Nothing was chosen."
    target = store.entities.get(target_key) if target_key else None
    if target is not None:
        target = grouping.head_of(store, target)
    if category in PEOPLE:
        if target is None or target.category not in PEOPLE:
            entity = store.add_person(text, category=category, source=merges.FINAL_CHECK)
            return "" if entity is not None else "That is not a name the app can read."
        inner = _name_inside(text, target)
        if inner is not None and target.person is not None:
            # a name glued into more - "toABIGAILOKAFOR", "OKAFORLAW" - is
            # matched whole, and only the name in it swapped
            a, b = inner
            written = text[:a] + merges.stand_in(store, target, text[a:b]) + text[b:]
            merges.add_form(store, target, text, written=written)
        else:
            merges.add_form(store, target, text)
        grouping.sync(store)
        return ""
    entity = store.add_value(category, text, source=merges.FINAL_CHECK)
    entity.enabled = True
    if target is not None and target is not entity:
        refused = merges.merge_by_hand(store, entity, target)
        if refused:
            return refused
    return ""


def _name_inside(text: str, head) -> tuple[int, int] | None:
    """Where the person's own name sits in a longer word, or None when the
    text is a name and nothing more."""
    if head.person is None or " " in text.strip():
        return None
    folded = text.casefold()
    spans = []
    for part in _parts(head):
        at = folded.find(part)
        if at >= 0:
            spans.append((at, at + len(part)))
    if not spans:
        return None
    a, b = min(s for s, _e in spans), max(e for _s, e in spans)
    return None if (a, b) == (0, len(text)) else (a, b)


def files_holding(result, text: str, progress=None) -> list[str]:
    """Every delivered file whose original has ``text`` in it, however it is
    spaced or cased."""
    from . import pipeline

    pattern = re.compile(r"\s+".join(re.escape(w) for w in text.split()), re.IGNORECASE)
    outcomes = [o for o in getattr(result, "outcomes", [])
                if o.delivered_name and Path(o.source).exists()]
    out = []
    for i, outcome in enumerate(outcomes):
        if progress is not None:
            progress(f"Looking for it in the set's originals: file {i + 1} of {len(outcomes)}",
                     0.5 + 0.1 * i / max(len(outcomes), 1))
        try:
            texts = pipeline.document_texts([Path(outcome.source)])
        except Exception:
            continue
        if any(pattern.search(t) for t in texts.values()):
            out.append(outcome.delivered_name)
    return out


# --------------------------------------------------------------------------
# what a choice will read as, before anything is changed
# --------------------------------------------------------------------------

# One or two letters - an initial left of a name - are replaced where they
# stand on the page and nowhere else: as a form, "C" read as a surname took a
# whole invented name, matched no lone "C" anywhere, and stands alone in every
# document of a set, which the fix then set about making again whole.
SHORT = 2


def is_short(text: str) -> bool:
    return len(re.sub(r"[\W_]", "", text)) <= SHORT


def _redacting(settings, member: str) -> bool:
    mode = getattr(settings, "docx_mode" if member.lower().endswith(".docx") else "pdf_mode",
                   "redact")
    return mode != "anonymize"


def _initial_stand_in(target, text: str) -> str:
    """An initial of a ticked person written as their invented name's
    initial - the part whose real initial it is - in the same case and with
    the same dots: "C." of Cody becomes "L." of the invented Lise."""
    if target is None or target.person is None or target.surrogate is None:
        return ""
    real, fake = target.person, target.surrogate
    pairs = [(real.first, fake.first), *zip(real.middles, fake.middles), (real.last, fake.last)]
    out = []
    for ch in text:
        if not ch.isalpha():
            out.append(ch)
            continue
        hit = next((f for r, f in pairs if r and f
                    and r.strip(".")[:1].casefold() == ch.casefold()), None)
        if hit is None:
            return ""
        letter = hit.strip(".")[:1]
        out.append(letter.upper() if ch.isupper() else letter.lower())
    return "".join(out)


def _ticked_owner(store, text: str):
    """The ticked person whose name has ``text`` in it, word for word."""
    words = {_letters(w) for w in text.split() if _letters(w)}
    for head in _ticked_heads(store):
        parts = set(_parts(head)) | {p[:1] for p in _parts(head)}
        if words and words <= parts:
            return head
    return None


def preview(doc_set, member: str, text: str, category: str, target_key: str) -> dict:
    """What the chosen text will read as, where it will change, and anything
    to look at before pressing Replace. Changes nothing."""
    import copy

    from . import engine

    text = " ".join(text.split())
    store, settings = doc_set.store, doc_set.run_settings
    if not text:
        return {"error": "Choose words on the page."}
    if category not in {k["key"] for k in kinds(settings)}:
        return {"error": "That type is left as written in this run - choose the type it is "
                         "replaced as."}
    target = grouping.head_of(store, store.entities[target_key]) \
        if target_key in store.entities else None
    warnings = []
    owner = _ticked_owner(store, text) if category in PEOPLE else None
    if owner is not None and (target is None or target is not owner):
        warnings.append(f"This is part of {owner.canonical}'s name - choose their row under "
                        "Whose, or it becomes someone else.")
    redact = _redacting(settings, member)
    if is_short(text):
        if not member.lower().endswith(".pdf"):
            return {"error": "One or two letters on their own can be replaced only on a PDF "
                             "page - choose them with the word beside them."}
        if redact:
            reads = ""
        elif category in PEOPLE:
            reads = _initial_stand_in(target, text)
            if not reads:
                return {"error": "Choose whose initial this is under Whose - it becomes the "
                                 "first letter of their invented name.",
                        "warnings": warnings}
        else:
            reads = copy.deepcopy(store).add_value(category, text).replacement
        return {"reads_as": reads, "blacked_out": redact, "short": True, "warnings": warnings,
                "scope": "Only the letters you chose, on this page."}
    trial = copy.deepcopy(store)
    refused = register(trial, text, category, target_key)
    if refused:
        return {"error": refused, "warnings": warnings}
    hits = engine.scan_text(text, trial, settings)
    if not hits:
        return {"error": "The run would not find this text on its own - choose the words "
                         "around it too.", "warnings": warnings}
    reads = text
    for hit in sorted(hits, key=lambda h: -h.start):
        reads = reads[:hit.start] + hit.replacement + reads[hit.end:]
    return {"reads_as": "" if redact else reads, "blacked_out": redact, "short": False,
            "warnings": warnings,
            "scope": "This file is made again. Then the set's other files that hold it are "
                     "read back, and any page where it still shows is listed here."}


# --------------------------------------------------------------------------
# the fix: this page's letters in place, or this file made again
# --------------------------------------------------------------------------

def _replace_in_place(archive: Path, member: str, number: int, indices: list[int],
                      texts: list[str], reads: str, redact: bool) -> str:
    """The chosen words covered where they stand on the delivered page - a
    black box, or the stand-in written over a white one. "" when done."""
    import shutil

    import pymupdf

    from . import audit

    found = page_words(archive, member, number)
    words = {w["i"]: w for w in found["words"]}
    chosen = [words.get(i) for i in indices]
    if not chosen or any(w is None for w in chosen) \
            or [w["text"] for w in chosen] != list(texts):
        return "The page's words have changed - read the page again and choose them again."
    rect = pymupdf.Rect(*(chosen[0][k] / ZOOM for k in ("x0", "y0", "x1", "y1")))
    for w in chosen[1:]:
        rect |= pymupdf.Rect(*(w[k] / ZOOM for k in ("x0", "y0", "x1", "y1")))
    rect = rect + (-0.5, -0.5, 0.5, 0.5)
    work = Path(tempfile.mkdtemp(prefix="final-here-"))
    try:
        target = work / Path(member).name
        with pymupdf.open(stream=_member_bytes(archive, member), filetype="pdf") as doc:
            page = doc[number - 1]
            if redact:
                page.add_redact_annot(rect, fill=(0, 0, 0))
            else:
                page.add_redact_annot(rect, text=reads, fill=(1, 1, 1), text_color=(0, 0, 0),
                                      fontsize=max(5.0, min(rect.height * 0.75, 12.0)))
            page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS)
            doc.save(target, garbage=3, deflate=True)
        audit._replace_members(archive, {member: target})
    finally:
        shutil.rmtree(work, ignore_errors=True)
    return ""


def fix(doc_set, member: str, number: int, text: str, category: str, target_key: str,
        words: list[int] | None = None, word_texts: list[str] | None = None,
        progress=lambda message, fraction: None) -> dict:
    """One file at a time. One or two letters are covered where they stand on
    this page; anything longer becomes a form of the chosen row and this file
    is made again from its original. Then the file is read back, and - for a
    form - the set's other files that hold the text are read back too: any
    page where it still shows, or that Kev flags, comes onto the list."""
    from . import audit, mapping, models

    result, store, settings = doc_set.run_result, doc_set.store, doc_set.run_settings
    if result is None or result.archive is None:
        return {"error": "This set has no delivered archive to fix."}
    text = " ".join(text.split())
    seen = preview(doc_set, member, text, category, target_key)
    if seen.get("error"):
        return {"error": seen["error"]}
    archive = Path(result.archive)
    failed: dict[str, str] = {}
    if seen["short"]:
        if not words:
            return {"error": "Choose the letters on the page itself."}
        progress("Covering the letters you chose", 0.2)
        refused = _replace_in_place(archive, member, number, list(words),
                                    list(word_texts or []), seen["reads_as"],
                                    seen["blacked_out"])
        if refused:
            return {"error": refused}
        _record_here(store, text, category, target_key, seen["reads_as"])
    else:
        refused = register(store, text, category, target_key)
        if refused:
            return {"error": refused}
        progress("Making this file again", 0.2)
        failed = audit.remake([member], result, store, settings, archive)
    if doc_set.run_password and result.key_path:
        mapping.write_encrypted_key(store, Path(result.key_path), doc_set.run_password)

    def waiting() -> None:
        progress("Waiting for Kev - another set's final check is using it", 0.4)

    added = 0
    with models.SLOT.use("kev", on_wait=waiting) as kev_url:
        again, texts = [], {}
        if member not in failed:
            progress("Reading this file back", 0.4)
            again, texts = audit.recheck(archive, store, [member], kev_url, progress)
        still = {(f.file, f.page) for f in again}
        text_gone = seen["short"] or not _still_written(text, texts.get((member, number), ""))
        kept = [f for f in doc_set.final_flags if f["file"] != member or member in failed]
        kept += [_entry(f) for f in again]
        if member not in failed and not text_gone and (member, number) not in still:
            kept.append({"file": member, "page": number, "kev": None, "matched": [text],
                         "unsure": False})
        # the same text elsewhere in the set: read back, never changed here
        if not seen["short"] and member not in failed:
            others = [m for m in files_holding(result, text, progress) if m != member]
            for i, other in enumerate(others):
                progress(f"Checking the set's other files for it: file {i + 1} of {len(others)}",
                         0.6 + 0.4 * i / max(len(others), 1))
                flags, other_texts = audit.recheck(archive, store, [other], kev_url)
                listed = {(f["file"], f["page"]) for f in kept}
                for f in flags:
                    if (f.file, f.page) not in listed:
                        kept.append(_entry(f))
                        listed.add((f.file, f.page))
                        added += 1
                for (m, n), page_text in other_texts.items():
                    if (m, n) not in listed and _still_written(text, page_text):
                        kept.append({"file": m, "page": n, "kev": None, "matched": [text],
                                     "unsure": False})
                        listed.add((m, n))
                        added += 1
    kept.sort(key=lambda f: (f.get("unsure", False), f["file"], f["page"]))
    doc_set.final_flags = kept
    clean = member not in failed and text_gone and (member, number) not in still
    if member in failed:
        how = f"could not be made again - {failed[member]}"
    elif not text_gone:
        how = "made again, but the chosen text is still on the page"
    elif not clean:
        how = "fixed, still flagged when read back"
    else:
        how = ("covered where it stood and read back clean" if seen["short"]
               else "fixed and read back clean")
    _log(doc_set, member, number, how)
    _write(doc_set)
    return {"page_clean": clean, "how": how, "added": added, "failed": failed,
            "to_look_at": len(kept)}


def _entry(flag) -> dict:
    return {"file": flag.file, "page": flag.page, "kev": flag.kev,
            "matched": list(flag.matched), "unsure": bool(flag.doubtful)}


def _record_here(store, text: str, category: str, target_key: str, reads: str) -> None:
    """Letters covered in place go into the mapping key as what they became -
    unticked, so no other file is ever changed by them."""
    target = grouping.head_of(store, store.entities[target_key]) \
        if target_key in store.entities else None
    if category in PEOPLE and target is not None:
        entity = merges.add_form(store, target, text, written=reads)
        if entity is not None:
            entity.enabled = False
        return
    if store.get(category, text) is None:
        entity = store.add_value(category, text, source=merges.FINAL_CHECK)
        entity.enabled = False


def _still_written(text: str, page_text: str) -> bool:
    words = text.split()
    if not words or not page_text:
        return False
    pattern = r"\s*".join(re.escape(w) for w in words)
    return re.search(pattern, page_text, re.IGNORECASE) is not None


def dismiss(doc_set, member: str, number: int) -> int:
    """The page looked at, and nothing on it to fix: off the list."""
    doc_set.final_flags = [f for f in doc_set.final_flags
                           if not (f["file"] == member and f["page"] == number)]
    _log(doc_set, member, number, "looked at - nothing to fix")
    _write(doc_set)
    return len(doc_set.final_flags)


def _log(doc_set, member: str, number: int, how: str) -> None:
    summary = doc_set.audit if doc_set.audit is not None else {}
    log = summary.setdefault("looked_at", [])
    log[:] = [d for d in log if not (d["file"] == member and d["page"] == number)]
    log.append({"file": member, "page": number, "how": how})


def _write(doc_set) -> None:
    from . import audit

    summary = doc_set.audit or {}
    if summary.get("file"):
        audit.write_pending(Path(summary["file"]), summary.get("pages", 0),
                            summary.get("kev", ""), doc_set.final_flags,
                            summary.get("looked_at", []))
    summary["still"] = len(doc_set.final_flags)
