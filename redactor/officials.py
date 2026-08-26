"""Find the judicial officers in a document and keep their names out of the run.

A judge or a commissioner is not a client.  Their name is a matter of public
record, it identifies the forum rather than a party, and a pleading that comes
back with the judge renamed is worse than useless - it reads as though someone
tampered with the record.

The inline allowlist in :mod:`redactor.patterns` protects a name only where the
title sits immediately in front of it, which covers exactly one of the layouts
courts actually use::

    Judge Amber M. Cordova              <- protected by the inline pattern
    Judge: Amber M. Cordova             <- colon, not whitespace
    AMBER M. CORDOVA, District Court Judge
    BY THE COURT:
    ____________________
    Amber M. Cordova
    District Court Judge                <- title on the following line

So this module harvests the officers up front, from headers, captions and
signature blocks alike, and hands back a do-not-change list that shields every
occurrence of the name anywhere in the batch - title adjacent or not.

The list is subtracted against the party names before it is used
(:func:`protected_terms`).  That direction matters: shielding a surname the
judge happens to share with a client would leak the client everywhere, which is
a far worse failure than renaming a judge.  When the two collide, the party
wins and the officer is dropped with a note.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import caption, names as _names

# --------------------------------------------------------------------------
# what a judicial title looks like
# --------------------------------------------------------------------------

# Matched case-sensitively, in Title Case or ALL CAPS.  Lowercase "judge" in
# running prose ("if the judge agrees") is not a signature block, and letting it
# through would put whatever noun follows onto the protection list.
_TITLE_WORDS = ("Judge", "Justice", "Commissioner", "Magistrate", "Referee",
                "Hearing Officer")
_T = "|".join(f"{w}|{w.upper()}" for w in _TITLE_WORDS)

# "Magistrate Judge Delia Farnsworth" doubles the title word. Matching only the
# first half leaves "Judge Delia Farnsworth" as the candidate name, which
# plausible_name then rejects as caption furniture - so the officer vanished.
_T_LEADING = rf"(?:(?:Magistrate|MAGISTRATE)[^\S\n]+)?(?:{_T})"

_HON = r"(?:Hon\.?|HON\.?|Honorable|HONORABLE|Hon\.?orable)"

# Words allowed to sit in front of the title word: "Third District Court Judge",
# "Chief Justice", "PRESIDING JUDGE".  Capitalised or an ordinal, nothing else.
_LEAD = r"(?:[A-Z][A-Za-z.'\-]{0,14}|\d{1,2}(?:st|nd|rd|th))"

TITLE_PHRASE = rf"(?:{_LEAD}[^\S\n]+){{0,4}}(?:{_T})"

# caption.NAME_RE carries its own boundaries and exactly one capture group, so
# it drops straight into a larger pattern.
_NAME = caption.NAME_RE.pattern

# "Judge Amber M. Cordova", "Judge: Amber M. Cordova", "Hon. Amber M. Cordova",
# "Before the Honorable Amber M. Cordova". A separator is required, so
# "Judgement" cannot masquerade as a title.
TITLE_THEN_NAME = re.compile(
    rf"\b(?:{_HON}|(?:Chief[^\S\n]+|Presiding[^\S\n]+|Assigned[^\S\n]+)?{_T_LEADING})"
    rf"(?:[^\S\n]*[:,][^\S\n]*|[^\S\n]+)(?:{_HON}[^\S\n]+)?{_NAME}"
)

# The bench written as a bare surname: "Judge: Kelly", "Commissioner Blomquist".
# Utah captions print it this way almost every time. Only ever consulted where
# the full-name pattern found nothing at the same position, or it would reduce
# "Judge Amber M. Cordova" to "Amber".
_TITLE_LEAD = (rf"\b(?:{_HON}|(?:Chief[^\S\n]+|Presiding[^\S\n]+|Assigned[^\S\n]+)?"
               rf"{_T_LEADING})(?:[^\S\n]*[:,][^\S\n]*|[^\S\n]+)")
TITLE_THEN_SOLO = re.compile(rf"{_TITLE_LEAD}([A-Z][A-Za-z'’\-]{{2,20}})(?![\w'’])")

# "Amber M. Cordova, District Court Judge" and the same thing with the title on
# the next line, which is how every signature block in the state is laid out.
NAME_THEN_TITLE = re.compile(
    rf"{_NAME}[^\S\n]*(?:,[^\S\n]*|[^\S\n]*\n[^\S\n]*)(?:{TITLE_PHRASE})\b"
)

# The block a court signs off in. Whatever name appears in the next few lines is
# the officer, even when the title never repeats.
#
# The commissioner headings matter as much as the judge ones: in domestic
# practice most of what gets signed is a commissioner's recommendation, and
# those blocks routinely carry the name with no title line under it.
#
# Anchored to a line that IS the heading, and nothing else. Unanchored and
# case-insensitive, "BY THE COURT" matched inside ordinary prose - "said Decree
# to be signed by the court and entered" - and whatever name came next was
# registered as a judicial officer. In a real divorce file that put both
# parties on the bench, and the do-not-change list would have shielded the
# clients from redaction had the party-collision guard not caught it.
_BY_THE_COURT = re.compile(
    r"^[\s_]*(?:BY\s+THE\s+COURT|DATED\s+AND\s+SIGNED|"
    r"(?:IT\s+IS\s+)?SO\s+ORDERED|"
    r"RECOMMENDED\s+BY(?:\s+THE)?(?:\s+COURT)?(?:\s+COMMISSIONER)?|"
    r"(?:COURT\s+)?COMMISSIONER['’]S\s+RECOMMENDATION|"
    r"RECOMMENDATION\s+OF\s+THE(?:\s+COURT)?\s+COMMISSIONER|"
    r"SIGNED\s+BY(?:\s+THE)?(?:\s+COURT)?\s+COMMISSIONER)"
    r"[\s_]*[:.]?[\s_]*$",
    re.IGNORECASE,
)
_BY_THE_COURT_LINES = 6

# Trailing furniture that rides along with a harvested name.
_TRAILING = re.compile(
    rf"[^\S\n]*(?:,)?[^\S\n]*(?:{TITLE_PHRASE})\s*$")


@dataclass(frozen=True)
class Official:
    """A judicial officer whose name must survive the run untouched."""

    name: str
    title: str                  # "Judge", "Commissioner", "District Court Judge"
    source: str = ""            # which document / region it came from
    confidence: str = "high"    # "high" | "medium"

    @property
    def key(self) -> str:
        return " ".join(self.name.split()).casefold()

    @property
    def surname(self) -> str:
        return _names.parse(self.name).last


# --------------------------------------------------------------------------
# harvesting
# --------------------------------------------------------------------------


# Words that legitimately qualify a title. Kept to a closed list so the display
# title cannot swallow the officer's own surname ("Cordova District Court Judge").
_QUALIFIERS = (
    r"(?:Chief|Presiding|Senior|Associate|Assigned|Acting|Retired|Visiting|Pro\s+Tem|"
    r"Magistrate|"
    r"District|Circuit|Superior|Juvenile|Family|Probate|Municipal|Appellate|Bankruptcy|"
    r"Federal|State|County|Trial|Supreme|Court|Judicial|"
    r"First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth|Tenth|Eleventh|"
    r"\d{1,2}(?:st|nd|rd|th))"
)


def _title_of(match: re.Match, name: str) -> str:
    """The title text out of a whole-match, tidied for display.

    The officer's own name is removed first: the lead-word run in the patterns is
    deliberately loose so it matches real captions, and left alone it would report
    the surname as part of the title.
    """
    whole = " ".join(match.group(0).split())
    if name:
        whole = whole.replace(" ".join(name.split()), " ")
    # Longest phrase wins, not longest title word: "Magistrate Judge" reads as
    # the federal title it is, rather than being cut back to "Magistrate"
    # because that word happens to be longer than "Judge".
    best = ""
    for word in _TITLE_WORDS:
        found = re.search(rf"\b((?:{_QUALIFIERS}[^\S\n]+){{0,3}}{word})\b", whole,
                          re.IGNORECASE)
        if found and len(found.group(1)) > len(best):
            best = found.group(1)
    if best:
        return " ".join(best.split()).title()
    if re.search(_HON, whole):
        return "Judge"
    return "Judicial officer"


# A single capitalised word this short is a word as often as a surname.
MIN_SOLO_SURNAME = 3


def _acceptable(raw: str, solo: bool = False) -> str | None:
    """A cleaned name, or None when the match swept up caption furniture.

    ``solo`` admits a one-word surname. Utah captions print the bench that way
    almost universally - "Commissioner: Blomquist", "Judge: Kelly" - and
    demanding two tokens left eight of eleven real pleadings with no judicial
    protection at all. It is only ever set where an explicit title sits directly
    beside the name; the proximity rule under a signing heading still requires a
    full name, because there the title is not there to vouch for it.
    """
    if not raw:
        return None
    name = _TRAILING.sub("", raw).strip(" ,.;:-\n\t")
    if caption.is_address(name):
        return None
    if caption.plausible_name(name):
        return caption._clean(name)
    if not solo:
        return None
    token = name.strip(" ,.;:-")
    if (len(token.split()) == 1 and len(token.strip(".")) >= MIN_SOLO_SURNAME
            and token[:1].isupper() and token.isalpha()
            and token.casefold() not in caption.BOILERPLATE):
        return token
    return None


def harvest(text: str, source: str = "") -> list[Official]:
    """Every judicial officer named anywhere in ``text``."""
    found: dict[str, Official] = {}

    def add(name: str, title: str, confidence: str) -> None:
        key = name.casefold()
        existing = found.get(key)
        if existing is None or (existing.confidence == "medium" and confidence == "high"):
            found[key] = Official(name, title, source, confidence)

    titled_at: set[int] = set()
    for pattern in (TITLE_THEN_NAME, NAME_THEN_TITLE):
        for match in pattern.finditer(text):
            name = _acceptable(match.group(1))
            if name:
                add(name, _title_of(match, name), "high")
                if pattern is TITLE_THEN_NAME:
                    titled_at.add(match.start())

    # bare surnames, but only where the full-name pattern came up empty
    for match in TITLE_THEN_SOLO.finditer(text):
        if match.start() in titled_at:
            continue
        # judge the whole line, not the captured word: "Judge: 8080 S. Redwood
        # Road, West Jordan" would otherwise hand back an officer called West
        line_start = text.rfind("\n", 0, match.start()) + 1
        line_end = text.find("\n", match.end())
        line = text[line_start: line_end if line_end != -1 else len(text)]
        if caption.is_address(line):
            continue
        name = _acceptable(match.group(1), solo=True)
        if name:
            add(name, _title_of(match, name), "high")

    # the signing block: the first plausible name under "BY THE COURT:"
    lines = text.splitlines()
    starts: list[int] = []
    offset = 0
    for index, line in enumerate(lines):
        if _BY_THE_COURT.search(line):
            starts.append(index)
        offset += len(line) + 1
    for index in starts:
        for line in lines[index + 1: index + 1 + _BY_THE_COURT_LINES]:
            stripped = line.strip(" _\t")
            if not stripped:
                continue
            candidate = caption._first_name_in(stripped.lstrip("/s "))
            if candidate:
                name = _acceptable(candidate)
                if name:
                    add(name, "Signing judicial officer", "medium")
                break

    return list(found.values())


def harvest_documents(regions: dict[str, str]) -> list[Official]:
    """Harvest across several documents, keeping the best evidence per name."""
    merged: dict[str, Official] = {}
    rank = {"high": 2, "medium": 1}
    for label, text in regions.items():
        for official in harvest(text, label):
            existing = merged.get(official.key)
            if existing is None or rank[official.confidence] > rank[existing.confidence]:
                merged[official.key] = official
    return sorted(merged.values(), key=lambda o: (o.confidence != "high", o.name))


# --------------------------------------------------------------------------
# the do-not-change list
# --------------------------------------------------------------------------

# A surname this short is a word as often as it is a name ("Ng", "Ho"); shielding
# one everywhere would punch holes in the scan.
MIN_SURNAME_LENGTH = 4


def _party_tokens(avoid: list[str]) -> set[str]:
    """Every single word any party name is written with, casefolded."""
    out: set[str] = set()
    for entry in avoid:
        parsed = _names.parse(entry)
        for _key, value in parsed.ordered:
            out.add(value.strip(".").casefold())
        for variant in _names.variants(parsed):
            if variant.token_count == 1:
                out.add(variant.text.strip(".").casefold())
    return out


@dataclass(frozen=True)
class Protection:
    """What is being shielded, and what had to be given up to keep parties safe."""

    terms: list[str]
    dropped: list[str]          # officer names a party name collided with
    partial: list[str]          # officers protected by full name only

    def notes(self) -> list[str]:
        out: list[str] = []
        for name in self.dropped:
            out.append(
                f"{name} is on the party list as well as the bench, so the name is "
                f"anonymized like any other. Check the result before you send it."
            )
        for name in self.partial:
            out.append(
                f"{name} shares a surname with a party, so only the full name is "
                f"protected. A bare surname there follows the party's pseudonym."
            )
        return out


def protected_terms(officials: list[Official], avoid: list[str] | None = None) -> Protection:
    """Literal strings to shield, given the party names they must not cover.

    Full names are always protected.  A bare surname is protected only when no
    party is written with that word: a judge and a client can share a surname,
    and shielding it would ship the client's name in every sentence it appears.
    """
    party_words = _party_tokens(list(avoid or []))
    parsed_parties = [_names.parse(a) for a in (avoid or [])]

    terms: list[str] = []
    dropped: list[str] = []
    partial: list[str] = []
    seen: set[str] = set()

    def put(term: str) -> None:
        term = " ".join(term.split())
        if len(term) < 2 or term.casefold() in seen:
            return
        seen.add(term.casefold())
        terms.append(term)

    for official in officials:
        parsed = _names.parse(official.name)
        canonical = " ".join(parsed.canonical.split())
        # same_person rather than string equality: "Amber M. Smith" on the bench
        # and "Amber Smith" on the party list are the same human being, and
        # shielding the name would ship her everywhere she appears.
        if any(_names.same_person(parsed, party) for party in parsed_parties):
            dropped.append(official.name)
            continue

        # An officer known only by surname IS the bare surname, so it has to
        # clear the same party check the derived surname does below. Without
        # this, a judge called Kelly shielded a party's child called Kelly.
        if len(canonical.split()) == 1:
            solo = canonical.strip(".")
            if solo.casefold() in party_words or len(solo) < MIN_SURNAME_LENGTH:
                partial.append(official.name)
                continue

        put(official.name)
        if canonical and canonical.casefold() != official.name.casefold():
            put(canonical)
        # written forms a court order actually uses for the same officer
        if parsed.first and parsed.last:
            put(f"{parsed.first} {parsed.last}")
            put(f"{parsed.first[0]}. {parsed.last}")

        surname = parsed.last.strip(".")
        if not surname:
            continue
        if surname.casefold() in party_words:
            partial.append(official.name)
            continue
        if len(surname) < MIN_SURNAME_LENGTH:
            partial.append(official.name)
            continue
        put(surname)
        for word in _TITLE_WORDS:
            put(f"{word} {surname}")

    return Protection(terms=terms, dropped=dropped, partial=partial)
