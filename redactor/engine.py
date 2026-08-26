"""Scan text, decide replacements, apply them.

Both document processors funnel through here so a DOCX and a PDF from the same
batch always resolve the same string to the same substitution.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import categories, names as _names, patterns
from .mapping import Entity, MappingStore

REDACTION_TEXT = "[REDACTED]"

# person variants rank just under the high-confidence structured detectors, and
# a longer written form always beats a shorter one
PERSON_BASE_PRIORITY = 68
PERSON_TOKEN_BONUS = 4
# typo forms of a name rank below every exact written form
FUZZY_PRIORITY = 66
# only tokens this long get typo-matched; short names would collide with
# ordinary words
MIN_FUZZY_LENGTH = 4

# Same underscore reasoning as the person boundary: a misspelled name flush
# against a signature rule has to be reachable too.
_FUZZY_WORD_RE = re.compile(
    r"(?<![^\W_])(?<!['’])[A-Za-z][A-Za-z'’\-]{3,}(?![^\W_])")


def _is_adjacent_swap(a: str, b: str) -> bool:
    """True when ``a`` is ``b`` with exactly one adjacent pair transposed."""
    if len(a) != len(b) or a == b:
        return False
    diffs = [i for i, (x, y) in enumerate(zip(a, b)) if x != y]
    return (len(diffs) == 2 and diffs[1] == diffs[0] + 1
            and a[diffs[0]] == b[diffs[1]] and a[diffs[1]] == b[diffs[0]])


@dataclass
class Settings:
    """Everything the operator chose in the GUI."""

    docx_mode: str = "anonymize"                 # "anonymize" | "redact"
    enabled_categories: set[str] = field(default_factory=lambda: {c.key for c in categories.CATEGORIES})
    include_single_token_names: bool = True
    use_ner: bool = True
    extra_allowlist: list[str] = field(default_factory=list)
    # Judicial officers harvested off the documents themselves - see
    # redactor.officials. Kept apart from the operator's own allowlist so the
    # report can say which names the bench put there and why.
    protected_names: list[str] = field(default_factory=list)
    scrub_metadata: bool = True
    scrub_comments: bool = True
    scrub_embedded: bool = True
    anonymize_filenames: bool = True
    label_redaction_boxes: bool = False
    ocr_scanned_pdfs: bool = True
    # black out every embedded image and drop drawn-ink handwriting; a photo,
    # a scanned signature, or a screenshot of a statement is confidential by
    # default and no text scan can read it
    redact_images: bool = True

    def category_enabled(self, key: str) -> bool:
        return key in self.enabled_categories

    @property
    def do_not_change(self) -> list[str]:
        """Every literal string this run must leave alone."""
        return [*self.extra_allowlist, *self.protected_names]


@dataclass(frozen=True)
class Hit:
    """One place in one piece of text that will be changed."""

    start: int
    end: int
    text: str
    category: str
    replacement: str
    entity_key: str
    priority: int = 50

    @property
    def length(self) -> int:
        return self.end - self.start


# --------------------------------------------------------------------------
# person matching
# --------------------------------------------------------------------------


# A value already registered is matched literally wherever it appears again.
# Without this, "Case No. 224900871" is caught by its label while a bare
# "224900871" three paragraphs later is not, and ships unredacted.
VALUE_LITERAL_PRIORITY = 86
MIN_LITERAL_LENGTH = 4


class EntityMatcher:
    """Compiled regexes for every enabled entity - people and known values."""

    def __init__(self, store: MappingStore, settings: Settings):
        self.store = store
        self.settings = settings
        self._value_rules: list[tuple[re.Pattern, Entity]] = []
        self._value_version = -1
        # One combined alternation per entity instead of one regex per written
        # form - a person expands into dozens of variants, and running each as
        # its own finditer pass over every paragraph was the scan's hot spot.
        # Longest variant first so the alternation prefers the longest form at
        # any position, which is what the per-variant priorities resolved to.
        self.rules: list[tuple[re.Pattern, Entity,
                               dict[str, tuple[_names.NameVariant, int]]]] = []
        for entity in store.persons():
            if not entity.enabled or not settings.category_enabled(entity.category):
                continue
            usable: list[tuple[_names.NameVariant, int]] = []
            for variant in entity.variants:
                # "Mr. Smith" and "the Smiths" carry one name component but
                # are multi-word and unambiguous; only the bare single word
                # is what the single-token switch is meant to exclude
                bare_single = (variant.token_count == 1
                               and variant.layout not in {"title", "plural"})
                if bare_single and not settings.include_single_token_names:
                    continue
                if variant.risky and not settings.include_single_token_names:
                    continue
                priority = PERSON_BASE_PRIORITY + PERSON_TOKEN_BONUS * variant.token_count
                usable.append((variant, priority))
            if not usable:
                continue
            usable.sort(key=lambda item: (-len(item[0].text), -item[1]))
            bodies = []
            lookup: dict[str, tuple[_names.NameVariant, int]] = {}
            for variant, priority in usable:
                bodies.append(r"\s+".join(_names.escape_token(tok)
                                          for tok in variant.text.split()))
                lookup.setdefault(" ".join(variant.text.split()).casefold(),
                                  (variant, priority))
            regex = re.compile(
                rf"{_names.BOUNDARY_LEFT}(?:{'|'.join(bodies)})"
                rf"{_names.BOUNDARY_RIGHT}", re.IGNORECASE)
            self.rules.append((regex, entity, lookup))

        # Typo index: "Johhn" or "Smiith" (an inserted letter) and "Jonh"
        # (an adjacent transposition) must still resolve to the registered
        # person. Keyed by the casefolded correct token; only plain
        # single-component forms long enough not to collide with ordinary
        # words take part.
        self._fuzzy: dict[str, tuple[Entity, _names.NameVariant]] = {}
        self._fuzzy_by_length: dict[int, list[str]] = {}
        if settings.include_single_token_names:
            for regex, entity, lookup in self.rules:
                for token_key, (variant, _priority) in lookup.items():
                    if (variant.token_count != 1 or variant.risky
                            or variant.layout != "plain"
                            or len(token_key) < MIN_FUZZY_LENGTH):
                        continue
                    if token_key not in self._fuzzy:
                        self._fuzzy[token_key] = (entity, variant)
                        self._fuzzy_by_length.setdefault(len(token_key), []).append(token_key)

    def _state_fingerprint(self) -> int:
        """Changes whenever any entity's identity, tick or replacement does.

        A bare entity count missed equal-count mutations - a retype swaps the
        key, a toggle flips enabled - and served stale literal rules.
        """
        return hash(tuple(sorted(
            (key, entity.enabled, entity.replacement)
            for key, entity in self.store.entities.items()
        )))

    def _refresh_values(self) -> None:
        """Rebuild the literal rules when the store changed since last time."""
        if self._value_version == self._state_fingerprint():
            return
        rules: list[tuple[re.Pattern, Entity]] = []
        for entity in self.store.entities.values():
            if entity.is_person or not entity.enabled:
                continue
            if not self.settings.category_enabled(entity.category):
                continue
            text = entity.canonical.strip()
            if len(text) < MIN_LITERAL_LENGTH:
                continue
            body = r"\s+".join(_names.escape_token(tok) for tok in text.split())
            # same underscore reasoning as the person boundary, plus the
            # address characters that must not abut a matched value
            rules.append((
                re.compile(rf"(?<![^\W_])(?<![@.\-]){body}(?![^\W_])(?![@\-])",
                           re.IGNORECASE),
                entity,
            ))
        rules.sort(key=lambda rule: -len(rule[1].canonical))
        self._value_rules = rules
        self._value_version = self._state_fingerprint()

    def find_values(self, text: str, protected: list[tuple[int, int]], redact: bool) -> list[Hit]:
        self._refresh_values()
        hits: list[Hit] = []
        for regex, entity in self._value_rules:
            for m in regex.finditer(text):
                start, end = m.span()
                if _overlaps(start, end, protected):
                    continue
                replacement = REDACTION_TEXT if redact else entity.replacement
                hits.append(Hit(start, end, m.group(0), entity.category,
                                replacement, entity.key, VALUE_LITERAL_PRIORITY))
        return hits

    def find(self, text: str, protected: list[tuple[int, int]], redact: bool) -> list[Hit]:
        hits: list[Hit] = []
        for regex, entity, lookup in self.rules:
            for m in regex.finditer(text):
                start, end = m.span()
                if _overlaps(start, end, protected):
                    continue
                found = lookup.get(
                    " ".join(m.group(0).replace("’", "'").split()).casefold())
                if found is None:      # pragma: no cover - defensive
                    continue
                variant, priority = found
                if redact:
                    replacement = REDACTION_TEXT
                else:
                    rendered = _names.render(variant, entity.surrogate)
                    replacement = _names.match_case(m.group(0), rendered)
                hits.append(
                    Hit(start, end, m.group(0), entity.category, replacement,
                        entity.key, priority)
                )
        hits.extend(self._find_typos(text, protected, redact))
        return hits

    def _find_typos(self, text: str, protected, redact: bool) -> list[Hit]:
        """Words one inserted letter or one adjacent swap away from a name."""
        if not self._fuzzy:
            return []
        hits: list[Hit] = []
        for m in _FUZZY_WORD_RE.finditer(text):
            word = m.group(0)
            key = word.replace("’", "'").casefold()
            if key in self._fuzzy:
                continue           # the exact regexes already covered it
            if _overlaps(m.start(), m.end(), protected):
                continue
            found = None
            if len(key) > MIN_FUZZY_LENGTH:
                for i in range(len(key)):          # drop the inserted letter
                    found = self._fuzzy.get(key[:i] + key[i + 1:])
                    if found:
                        break
            if found is None:
                for candidate in self._fuzzy_by_length.get(len(key), ()):
                    if _is_adjacent_swap(key, candidate):
                        found = self._fuzzy[candidate]
                        break
            if found is None:
                continue
            entity, variant = found
            if redact:
                replacement = REDACTION_TEXT
            else:
                rendered = _names.render(variant, entity.surrogate)
                replacement = _names.match_case(word, rendered)
            hits.append(Hit(m.start(), m.end(), word, entity.category,
                            replacement, entity.key, FUZZY_PRIORITY))
        return hits


def _overlaps(start: int, end: int, spans) -> bool:
    return any(start < e and s < end for s, e in spans)


# --------------------------------------------------------------------------
# scanning
# --------------------------------------------------------------------------


def scan_text(
    text: str,
    store: MappingStore,
    settings: Settings,
    matcher: EntityMatcher | None = None,
    register: bool = True,
) -> list[Hit]:
    """Every change to make in ``text``, non-overlapping, left to right."""
    if not text or not text.strip():
        return []

    redact = settings.docx_mode == "redact"
    protected = patterns.allowlist_spans(text, settings.do_not_change)
    matcher = matcher or EntityMatcher(store, settings)

    hits: list[Hit] = list(matcher.find(text, protected, redact))

    enabled = [k for k in settings.enabled_categories
               if categories.style_for(k) != "person"]
    for match in patterns.scan(text, enabled=enabled, protected=protected):
        entity = store.get(match.category, match.text)
        if entity is None:
            if not register:
                continue
            entity = store.add_value(match.category, match.text)
        if not entity.enabled:
            continue
        replacement = REDACTION_TEXT if redact else entity.replacement
        hits.append(
            Hit(match.start, match.end, match.text, match.category,
                replacement, entity.key, match.priority)
        )

    # Last, because the pattern pass above is what registers new values, and a
    # bare recurrence of one must be matched in this same piece of text.
    hits.extend(matcher.find_values(text, protected, redact))

    return _resolve(hits)


def _resolve(hits: list[Hit]) -> list[Hit]:
    ordered = sorted(hits, key=lambda h: (-h.priority, -h.length, h.start))
    kept: list[Hit] = []
    taken: list[tuple[int, int]] = []
    for hit in ordered:
        if _overlaps(hit.start, hit.end, taken):
            continue
        kept.append(hit)
        taken.append((hit.start, hit.end))
    kept.sort(key=lambda h: h.start)
    return kept


def apply_hits(text: str, hits: list[Hit]) -> str:
    """Rewrite ``text`` with every hit substituted (right to left)."""
    out = text
    for hit in sorted(hits, key=lambda h: h.start, reverse=True):
        out = out[: hit.start] + hit.replacement + out[hit.end:]
    return out


def scan_and_apply(
    text: str,
    store: MappingStore,
    settings: Settings,
    document: str,
    matcher: EntityMatcher | None = None,
) -> tuple[str, list[Hit]]:
    hits = scan_text(text, store, settings, matcher)
    for hit in hits:
        entity = store.entities.get(hit.entity_key)
        if entity is not None:
            store.record_hit(entity, document)
    return apply_hits(text, hits), hits


# The class used to cover people only; the name is kept so existing imports and
# any saved workflows continue to work.
PersonMatcher = EntityMatcher
