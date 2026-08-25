"""Optional spaCy suggestions.

The model never edits anything.  It proposes people, organisations and places
that the pattern rules cannot see, and those proposals land on the review
screen for the operator to accept or reject.  If the model is unavailable the
tool degrades to rules plus the operator's own name list and says so.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

MODEL_NAME = "en_core_web_sm"

# spaCy entity label -> our category
LABEL_MAP = {
    "PERSON": "person",
    "ORG": "organization",
    "GPE": "location",
    "LOC": "location",
    "FAC": "location",
    "NORP": "organization",
}

_nlp = None
_load_error: str | None = None


def available() -> tuple[bool, str]:
    """(is the model usable, human-readable explanation)."""
    load()
    if _nlp is not None:
        return True, f"spaCy model '{MODEL_NAME}' loaded"
    return False, _load_error or "spaCy model not loaded"


def load():
    """Load the model once; remember the failure if it will not load."""
    global _nlp, _load_error
    if _nlp is not None or _load_error is not None:
        return _nlp
    try:
        import spacy
    except Exception as exc:                      # pragma: no cover - env dependent
        _load_error = f"spaCy is not installed ({exc})"
        return None
    try:
        _nlp = spacy.load(MODEL_NAME, disable=["lemmatizer", "textcat"])
    except Exception as exc:                      # pragma: no cover - env dependent
        _load_error = (
            f"spaCy model '{MODEL_NAME}' is not installed. "
            f"Install it with:  python -m spacy download {MODEL_NAME}   ({exc})"
        )
        return None
    return _nlp


@dataclass
class Suggestion:
    text: str
    category: str
    count: int = 1
    documents: set[str] = None      # type: ignore[assignment]

    def __post_init__(self):
        if self.documents is None:
            self.documents = set()

    @property
    def key(self) -> str:
        return f"{self.category}:{' '.join(self.text.split()).casefold()}"


MAX_CHARS_PER_DOC = 400_000      # spaCy's default parser limit is 1_000_000


def suggest(documents: dict[str, str], protected: dict[str, list[tuple[int, int]]] | None = None
            ) -> list[Suggestion]:
    """Propose entities across ``documents`` ({label: text})."""
    nlp = load()
    if nlp is None:
        return []
    protected = protected or {}
    merged: dict[str, Suggestion] = {}

    for label, text in documents.items():
        if not text:
            continue
        guard = protected.get(label, [])
        for chunk_start, chunk in _chunks(text):
            try:
                doc = nlp(chunk)
            except Exception:                     # pragma: no cover - defensive
                continue
            for ent in doc.ents:
                category = LABEL_MAP.get(ent.label_)
                if not category:
                    continue
                value = " ".join(ent.text.split()).strip(" .,;:'\"")
                value = re.sub(r"['\u2019]s$", "", value).strip()   # drop possessives
                if not _plausible(value, category):
                    continue
                start = chunk_start + ent.start_char
                end = chunk_start + ent.end_char
                if any(start < e and s < end for s, e in guard):
                    continue
                key = f"{category}:{value.casefold()}"
                item = merged.get(key)
                if item is None:
                    merged[key] = item = Suggestion(value, category, 0)
                item.count += 1
                item.documents.add(label)

    return sorted(merged.values(), key=lambda s: (s.category, -s.count, s.text.casefold()))


def _chunks(text: str):
    """Yield (offset, chunk) slices small enough for the model, split on blank lines."""
    if len(text) <= MAX_CHARS_PER_DOC:
        yield 0, text
        return
    offset = 0
    while offset < len(text):
        end = min(len(text), offset + MAX_CHARS_PER_DOC)
        if end < len(text):
            split = text.rfind("\n", offset + MAX_CHARS_PER_DOC // 2, end)
            if split > offset:
                end = split
        yield offset, text[offset:end]
        offset = end


_MIN_LEN = {"person": 4, "organization": 4, "location": 3}

_NOISE = {
    "the court", "court", "the state", "state", "county", "district court", "petitioner",
    "respondent", "plaintiff", "defendant", "exhibit", "appendix", "esq", "llc", "llp",
    "inc", "the parties", "parties", "usa", "u.s.", "united states", "america",
}


def _plausible(value: str, category: str) -> bool:
    if len(value) < _MIN_LEN.get(category, 3):
        return False
    if value.casefold() in _NOISE:
        return False
    if not any(ch.isalpha() for ch in value):
        return False
    if sum(ch.isdigit() for ch in value) > len(value) / 3:
        return False
    if category == "person":
        # a person suggestion worth reviewing has at least one capitalised word
        if not any(tok[:1].isupper() for tok in value.split()):
            return False
    return True
