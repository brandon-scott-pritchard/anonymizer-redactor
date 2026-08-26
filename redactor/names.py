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
from typing import Sequence

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


def _part_compatible(x: str, y: str) -> bool:
    """Equal, one missing, or one the initial of the other."""
    x, y = x.strip(".").casefold(), y.strip(".").casefold()
    if not x or not y or x == y:
        return True
    return (len(x) == 1 or len(y) == 1) and x[0] == y[0]


def same_person(a: PersonName, b: PersonName) -> bool:
    """Could these be one person written with more or fewer parts?

    "John Smith", "John Michael Smith", "J. Smith" and "John M. Smith" are
    the same person; "Jane Smith" and "John Smith Jr." vs "John Smith Sr."
    are not. Registering them as separate entities would hand one person
    two different pseudonyms.
    """
    if not a.last or not b.last or a.last.casefold() != b.last.casefold():
        return False
    if a.suffix and b.suffix and a.suffix.casefold() != b.suffix.casefold():
        return False
    if not _part_compatible(a.first, b.first):
        return False
    if not (a.first and b.first):
        # surname-only forms are too ambiguous to merge automatically
        return False
    for mine, theirs in zip(a.middles, b.middles):
        if not _part_compatible(mine, theirs):
            return False
    return True


def richness(person: PersonName) -> tuple[int, int, int]:
    """How much of the name is written out; the richer form wins a merge."""
    return (len(person.middles),
            sum(len(m.strip(".")) for m in person.middles),
            len(person.first.strip(".")))


@dataclass(frozen=True)
class Overlap:
    """A ticked name that is already a written form of another ticked name."""

    inner: str          # the shorter form, e.g. "Smith"
    outer: str          # the name it is a written form of, e.g. "Jane E. Smith"
    merge: bool         # True when exactly one longer name claims it

    @property
    def note(self) -> str:
        if self.merge:
            return (f"“{self.inner}” is already how “{self.outer}” gets "
                    f"written. Kept as one person so both spellings get the same "
                    f"pseudonym.")
        return (f"“{self.inner}” could be {self.outer} or someone else on the "
                f"list. Left on its own, so it gets its own pseudonym - spell it out "
                f"if that is wrong.")


def overlapping_names(entries: Sequence[str]) -> list[Overlap]:
    """Ticked names that are contained in other ticked names.

    ``Smith`` on its own alongside ``Jane Elizabeth Smith`` is not a second
    person: it is one of the forms the matcher already generates for the first.
    Registered separately it becomes a second entity with a second surrogate, so
    the same surname comes back as ``Doe`` in one sentence and ``[PERSON-2]`` in
    the next.

    Pairs :func:`same_person` already merges are left out - the store handles
    those itself, and reporting them would be noise.  What comes back is the
    residue: partial forms nothing else resolves.
    """
    cleaned: list[str] = []
    for raw in entries:
        text = " ".join(str(raw).split())
        if text and not any(text.casefold() == seen.casefold() for seen in cleaned):
            cleaned.append(text)

    parsed = {text: parse(text) for text in cleaned}
    forms = {text: {v.text.casefold() for v in variants(parsed[text])}
             for text in cleaned}

    claims: dict[str, list[str]] = {}
    for inner in cleaned:
        key = inner.casefold()
        for outer in cleaned:
            if outer.casefold() == key:
                continue
            if key not in forms[outer]:
                continue
            if same_person(parsed[inner], parsed[outer]):
                continue          # the store merges these on its own
            claims.setdefault(inner, []).append(outer)

    out: list[Overlap] = []
    for inner, outers in claims.items():
        merge = len(outers) == 1
        for outer in outers:
            out.append(Overlap(inner=inner, outer=outer, merge=merge))
    return out


def escape_token(token: str) -> str:
    """``re.escape`` that also accepts the typographic apostrophe.

    Operators type O'Brien with a straight quote; Word's autocorrect stores
    O’Brien with U+2019. One written form must match both or the surname
    leaks everywhere the document uses the other.
    """
    return re.escape(token).replace("'", "['’]")


# Where a name is allowed to start and stop.
#
# ``\w`` counts an underscore as a letter, and a pleading's signature block is
# a run of them with the name flush against it - "________Jane Smith________"
# arrives as a single run out of Word. Treating "_" as a letter meant the
# surname on every signature line failed to match and shipped intact, so the
# boundary is written against alphanumerics instead.
#
# The apostrophe is excluded on the left only: "Brien" must not match inside
# "O'Brien", while "Smith" must still match inside the possessive "Smith's".
BOUNDARY_LEFT = r"(?<![^\W_])(?<!['’])"
BOUNDARY_RIGHT = r"(?![^\W_])"


def variant_regex(text: str) -> re.Pattern:
    """A word-boundary regex for one variant, tolerant of runs of whitespace."""
    parts = [escape_token(tok) for tok in text.split()]
    body = r"\s+".join(parts)
    return re.compile(rf"{BOUNDARY_LEFT}{body}{BOUNDARY_RIGHT}", re.IGNORECASE)
