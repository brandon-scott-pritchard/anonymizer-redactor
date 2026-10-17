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


def files_holding(result, text: str) -> list[str]:
    """Every delivered file whose original has ``text`` in it, however it is
    spaced or cased: the fix is carried to each."""
    from . import pipeline

    pattern = re.compile(r"\s+".join(re.escape(w) for w in text.split()), re.IGNORECASE)
    out = []
    for outcome in getattr(result, "outcomes", []):
        if not outcome.delivered_name or not Path(outcome.source).exists():
            continue
        try:
            texts = pipeline.document_texts([Path(outcome.source)])
        except Exception:
            continue
        if any(pattern.search(t) for t in texts.values()):
            out.append(outcome.delivered_name)
    return out


def fix(doc_set, member: str, number: int, text: str, category: str, target_key: str,
        progress=lambda message, fraction: None) -> dict:
    """Register the choice, make every file holding it again, write the
    mapping key again, read the changed files back. What happened, for the
    screen: a page is clean only when its file was made again, neither the
    matcher nor Kev flags it, and the chosen text is no longer on it."""
    from . import audit, mapping, models

    result, store = doc_set.run_result, doc_set.store
    if result is None or result.archive is None:
        return {"error": "This set has no delivered archive to fix."}
    if category not in {k["key"] for k in kinds(doc_set.run_settings)}:
        return {"error": "That type is left as written in this run, so it would not be "
                         "replaced - choose the type it is replaced as."}
    refused = register(store, text, category, target_key)
    if refused:
        return {"error": refused}
    progress("Finding every file it is written in", 0.05)
    members = sorted(set(files_holding(result, text)) | {member})
    progress(f"Making {len(members)} file(s) again", 0.2)
    failed = audit.remake(members, result, store, doc_set.run_settings, Path(result.archive))
    if doc_set.run_password and result.key_path:
        mapping.write_encrypted_key(store, Path(result.key_path), doc_set.run_password)
    progress("Reading the fixed pages back", 0.6)
    remade = [m for m in members if m not in failed]
    again, texts = [], {}
    if remade:
        def waiting() -> None:
            progress("Waiting for Kev - another set's final check is using it", 0.6)
        with models.SLOT.use("kev", on_wait=waiting) as kev_url:
            again, texts = audit.recheck(Path(result.archive), store, remade, kev_url, progress)
    still = {(f.file, f.page) for f in again}
    # the files the chosen text is still written in, read back
    left = sorted({m for (m, _n), t in texts.items() if _still_written(text, t)})
    text_gone = not _still_written(text, texts.get((member, number), ""))
    # every page of a remade file is as it now reads: flagged, unsure, or clean
    kept = [f for f in doc_set.final_flags if f["file"] not in remade]
    kept += [{"file": f.file, "page": f.page, "kev": f.kev, "matched": list(f.matched),
              "unsure": bool(f.doubtful)} for f in again]
    if member in remade and not text_gone and (member, number) not in still:
        # the chosen text is still on the page: it stays on the list
        kept.append({"file": member, "page": number, "kev": None, "matched": [text],
                     "unsure": False})
    kept.sort(key=lambda f: (f["file"], f["page"]))
    doc_set.final_flags = kept
    clean = member in remade and text_gone and (member, number) not in still
    if member in failed:
        how = f"could not be made again - {failed[member]}"
    elif not text_gone:
        how = "made again, but the chosen text is still on the page"
    elif not clean:
        how = "fixed, still flagged when read back"
    else:
        how = "fixed and read back clean"
    _log(doc_set, member, number, how)
    _write(doc_set)
    return {"remade": len(remade), "failed": failed, "page_clean": clean, "how": how,
            "still_written_in": left, "to_look_at": len(kept)}


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
