"""Expand a full name into every written form it might appear in.

The operator types one full name per line.  This module turns
``John Michael Smith`` into the set of strings a legal document might actually
contain - ``Smith``, ``John Smith``, ``Smith, John``, ``J. Smith``,
``John M. Smith``, ``Mr. Smith``, ``John``, ``Michael`` - and, critically,
remembers which *components* each form is built from, so the replacement can be
built from the matching components of the surrogate name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from itertools import combinations

TITLES = ("Mr", "Mrs", "Ms", "Miss", "Dr", "Prof", "Rev", "Hon", "Sir", "Madam", "Mx")
SUFFIXES = ("Jr", "Sr", "II", "III", "IV", "V", "Esq", "MD", "PhD", "JD", "CPA", "RN")
PARTICLES = ("van", "von", "de", "del", "della", "di", "da", "du", "la", "le",
             "el", "bin", "ibn", "al", "st", "mc", "mac")

# Single-token variants matching one of these would shred ordinary prose, so
# they are generated but flagged so the review screen can warn.
RISKY_SINGLE_TOKENS = {
    "will", "mark", "grace", "hope", "faith", "rich", "young", "long", "short", "white",
    "black", "brown", "green", "gray", "grey", "king", "price", "bill", "may", "june",
    "art", "chase", "case", "law", "court", "judge", "moore", "best", "day", "field",
    "love", "joy", "summer", "autumn", "winter", "sunny", "star", "rose", "lily", "iris",
    "victor", "major", "miles", "penny", "frank", "earnest", "ernest", "amber", "jean",
    "dean", "sterling", "cash", "bond", "banks", "church", "cross", "stone", "wood",
    "woods", "rivers", "hunter", "carter", "porter", "parker", "baker", "cook", "fisher",
    "gardner", "mason", "miller", "taylor", "turner", "walker", "wheeler",
}


@dataclass(frozen=True)
class NameVariant:
    """One written form of a name."""

    text: str                 # the literal string to search for
    components: tuple         # e.g. ("first", "last") or ("initial:first", "last")
    layout: str               # "plain" | "comma" | "title"
    risky: bool = False

    @property
    def token_count(self) -> int:
        return len(self.components)


@dataclass
class PersonName:
    """A full name broken into components."""

    raw: str
    title: str = ""
    first: str = ""
    middles: tuple[str, ...] = ()
    last: str = ""
    suffix: str = ""

    @property
    def canonical(self) -> str:
        parts = [self.first, *self.middles, self.last]
        return " ".join(p for p in parts if p)

    @property
    def ordered(self) -> list[tuple[str, str]]:
        """(component key, value) in written order, excluding title/suffix."""
        out: list[tuple[str, str]] = []
        if self.first:
            out.append(("first", self.first))
        for i, mid in enumerate(self.middles):
            out.append((f"middle{i}", mid))
        if self.last:
            out.append(("last", self.last))
        return out


_SUFFIX_RE = re.compile(rf"^(?:{'|'.join(SUFFIXES)})\.?$", re.IGNORECASE)
_TITLE_RE = re.compile(rf"^(?:{'|'.join(TITLES)})\.?$", re.IGNORECASE)


def parse(raw: str) -> PersonName:
    """Split ``raw`` into title / first / middles / last / suffix."""
    text = " ".join(raw.replace("’", "'").split())
    if not text:
        return PersonName(raw="")

    # "Smith, John Michael" -> "John Michael Smith"
    if text.count(",") == 1:
        left, right = (p.strip() for p in text.split(","))
        if right and not _SUFFIX_RE.match(right.split()[0]) and len(left.split()) <= 2:
            text = f"{right} {left}"
    text = text.replace(",", " ")

    tokens = [t for t in text.split() if t]
    title = ""
    if tokens and _TITLE_RE.match(tokens[0]):
        title = tokens.pop(0).rstrip(".")
    suffix = ""
    if tokens and _SUFFIX_RE.match(tokens[-1]):
        suffix = tokens.pop().rstrip(".")

    # Glue particles onto the surname: "van der Berg". Only a token written
    # lowercase reads as a particle - capitalised "Al"/"St"/"Mc" are given
    # names ("Mary Al Smith") - except in ALL-CAPS captions, where case
    # carries no signal. The first token is never a particle: it is the
    # first name, even for people actually named Van.
    all_caps = raw.isupper()
    for i in range(1, len(tokens) - 1):
        tok = tokens[i]
        if (tok.lower().strip(".") in PARTICLES
                and (tok.islower() or all_caps)):
            tokens = tokens[:i] + [" ".join(tokens[i:])]
            break

    if not tokens:
        return PersonName(raw=raw, title=title, suffix=suffix)
    if len(tokens) == 1:
        return PersonName(raw=raw, title=title, last=tokens[0], suffix=suffix)
    return PersonName(
        raw=raw,
        title=title,
        first=tokens[0],
        middles=tuple(tokens[1:-1]),
        last=tokens[-1],
        suffix=suffix,
    )


def _initial(value: str) -> str:
    return f"{value[0].upper()}."


def pluralize(word: str) -> str:
    """Family-name plural: Smith -> Smiths, Jones -> Joneses."""
    lower = word.lower()
    if lower.endswith(("s", "x", "z", "ch", "sh")):
        return word + "es"
    return word + "s"


def variants(name: PersonName, include_single_tokens: bool = True) -> list[NameVariant]:
    """Every written form of ``name``, longest first."""
    parts = name.ordered
    if not parts:
        return []

    out: dict[str, NameVariant] = {}

    def put(text: str, components: tuple, layout: str = "plain"):
        text = " ".join(text.split()).strip(" ,")
        if len(text) < 2:
            return
        risky = len(components) == 1 and text.strip(".").casefold() in RISKY_SINGLE_TOKENS
        key = text.casefold()
        existing = out.get(key)
        if existing is None or len(components) > existing.token_count:
            out[key] = NameVariant(text, components, layout, risky)

    n = len(parts)
    # every ordered subset of the components: first, last, first+last, first+middle, …
    for size in range(n, 0, -1):
        if size == 1 and not include_single_tokens:
            continue
        for combo in combinations(range(n), size):
            keys = tuple(parts[i][0] for i in combo)
            values = [parts[i][1] for i in combo]
            put(" ".join(values), keys)

            if size >= 2:
                # initial forms: "J. Smith", "John M. Smith", "J. M. Smith"
                for init_count in range(1, size):
                    for init_idx in combinations(range(size - 1), init_count):
                        rendered = [
                            _initial(v) if i in init_idx else v
                            for i, v in enumerate(values)
                        ]
                        comps = tuple(
                            f"initial:{keys[i]}" if i in init_idx else keys[i]
                            for i in range(size)
                        )
                        put(" ".join(rendered), comps)

    # "Smith, John Michael" and "Smith, John"
    if name.last and name.first:
        put(f"{name.last}, {name.first}", ("last", "first"), "comma")
        if name.middles:
            mids = " ".join(name.middles)
            put(f"{name.last}, {name.first} {mids}",
                ("last", "first") + tuple(f"middle{i}" for i in range(len(name.middles))),
                "comma")
        put(f"{name.last}, {_initial(name.first)}", ("last", "initial:first"), "comma")

    # "the Smiths" - a plural surname names the whole family
    if name.last and include_single_tokens:
        put(pluralize(name.last), ("last",), "plural")

    # "Mr. Smith"
    if name.last:
        for title in ("Mr.", "Mrs.", "Ms.", "Miss", "Dr."):
            put(f"{title} {name.last}", ("last",), "title")

    ordered = sorted(
        out.values(),
        key=lambda v: (-v.token_count, -len(v.text), v.text.casefold()),
    )
    return ordered


def render(variant: NameVariant, surrogate: PersonName) -> str:
    """Build the replacement string for ``variant`` from ``surrogate``."""
    lookup = dict(surrogate.ordered)
    pieces: list[str] = []
    for comp in variant.components:
        as_initial = comp.startswith("initial:")
        key = comp.split(":", 1)[1] if as_initial else comp
        value = lookup.get(key)
        if value is None:
            # surrogate has fewer middles than the original - fall back sensibly
            value = lookup.get("last") if key.startswith("middle") else surrogate.canonical
            if value is None:
                continue
        pieces.append(_initial(value) if as_initial else value)

    if variant.layout == "plural" and pieces:
        return pluralize(pieces[0])
    if variant.layout == "comma" and len(pieces) >= 2:
        return f"{pieces[0]}, {' '.join(pieces[1:])}"
    if variant.layout == "title":
        title = variant.text.split()[0]
        return f"{title} {' '.join(pieces)}"
    return " ".join(pieces)


def match_case(original: str, replacement: str) -> str:
    """Mirror the capitalisation of ``original`` onto ``replacement``."""
    letters = [c for c in original if c.isalpha()]
    if not letters:
        return replacement
    if all(c.isupper() for c in letters):
        return replacement.upper()
    if all(c.islower() for c in letters):
        return replacement.lower()
    return replacement


def escape_token(token: str) -> str:
    """``re.escape`` that also accepts the typographic apostrophe.

    Operators type O'Brien with a straight quote; Word's autocorrect stores
    O’Brien with U+2019. One written form must match both or the surname
    leaks everywhere the document uses the other.
    """
    return re.escape(token).replace("'", "['’]")


def variant_regex(text: str) -> re.Pattern:
    """A word-boundary regex for one variant, tolerant of runs of whitespace."""
    parts = [escape_token(tok) for tok in text.split()]
    body = r"\s+".join(parts)
    # A trailing apostrophe is let through so possessives ("Smith's") match,
    # while a genuine continuation ("Smithers") still does not.
    return re.compile(rf"(?<![\w'’]){body}(?!\w)", re.IGNORECASE)
