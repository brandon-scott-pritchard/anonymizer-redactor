"""Find the children a pleading names, and their birth dates.

The caption reader only ever looks at the caption, and children are never in
it - they are in a roster partway down the document. Across a real sample of
divorce, paternity and parentage decrees, not one child was proposed for
redaction: a minor's full name and date of birth sat in the delivered file
unless the operator happened to notice and type them in by hand.

Two layouts cover all of them, and both are anchored on something specific
rather than on the word "children", which appears six to ten times per document
as ordinary prose ("the parties' children", "access to records concerning their
children") and would drag half the text in with it.

Everything here proposes; nothing is applied without a tick. A child is
proposed as a minor, which is also what keeps a son from being merged into the
father he is named after - see :func:`redactor.names.same_person`.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import caption

# The line that introduces a roster. Deliberately specific.
CUE = re.compile(
    r"(?:the\s+)?(?:name|names)[^\n]{0,60}?(?:birth|born)"
    r"|children\s+born\s+as\s+issue"
    r"|following\s+(?:minor\s+)?child(?:ren)?\b"
    r"|(?:minor\s+)?child(?:ren)?\s+(?:is|are)\s+listed"
    r"|is\s+the\s+legal\s+(?:mother|father)",
    re.IGNORECASE,
)
CUE_LINES = 12

# "Marcus Shai Ashdown Born: December 2017", "Duke Marchetti Born 07/15/2018"
BORN = re.compile(
    r"^[\s\w.]*?(?<![\w'’])([A-Z][A-Za-z'’\-]+(?:\s+[A-Z][A-Za-z'’\-.]+){1,4})"
    r"\s*[,\-]?\s*Born\s*:?\s*"
    r"([A-Za-z]*\.?\s*\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4}|[A-Za-z]+,?\s+\d{4}|\d{1,2}/\d{4})",
    re.IGNORECASE,
)

# A roster table: the header row names both halves of what it holds.
# "Name", but a roster is as likely to head the column "Child" or "Minor
# Child" - keying on the one wording the sample happened to use would have
# missed every roster laid out the other way.
_HEADER_NAME = re.compile(r"\bname\b|\bchild(?:ren)?\b", re.IGNORECASE)
_HEADER_BIRTH = re.compile(r"\bbirth\b|\bborn\b|\bd\.?o\.?b\b", re.IGNORECASE)

# "JQA", "C.E.F." - initials standing in for a child, already the form the
# rules ask for. Two to four letters; longer runs are acronyms.
INITIALS = re.compile(r"^[A-Z]\.?(?:[A-Z]\.?){1,3}$")

# "LRA legally emancipated on January 26, 2025 when she turned 18."
_EMANCIPATED = re.compile(
    r"emancipat|turned\s+18|reached\s+(?:the\s+)?age\s+of\s+majority|no\s+longer\s+a\s+minor",
    re.IGNORECASE,
)


@dataclass(frozen=True)
class Child:
    """A child named in a document, with the birth text beside them."""

    name: str
    born: str = ""
    source: str = ""
    emancipated: bool = False

    @property
    def key(self) -> str:
        return " ".join(self.name.split()).casefold()

    @property
    def is_initials(self) -> bool:
        return bool(INITIALS.match(self.name.replace(" ", "")))

    @property
    def category(self) -> str:
        """Initials get a placeholder; a full name gets an invented one."""
        return "minor_initials" if self.is_initials else "minor"

    @property
    def role(self) -> str:
        if self.emancipated:
            return "Child (now an adult)"
        return "Minor child"


def _plausible(name: str) -> bool:
    if INITIALS.match(name.replace(" ", "")):
        return True
    if caption.is_address(name):
        return False
    return caption.plausible_name(name)


def from_text(text: str, source: str = "") -> list[Child]:
    """Children introduced by a cue line and written out with a birth date."""
    found: dict[str, Child] = {}
    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not CUE.search(line):
            continue
        for follower in lines[index: index + CUE_LINES]:
            match = BORN.search(follower)
            if not match:
                continue
            name = " ".join(match.group(1).split()).strip(" ,.")
            if not _plausible(name):
                continue
            found.setdefault(name.casefold(), Child(
                name=name, born=" ".join(match.group(2).split()).strip(" ,"),
                source=source, emancipated=bool(_EMANCIPATED.search(follower))))
    return list(found.values())


def from_tables(tables, text: str = "", source: str = "") -> list[Child]:
    """Children listed in a roster table - "Name | Month & Year of Birth"."""
    found: dict[str, Child] = {}
    for rows in tables:
        if len(rows) < 2:
            continue
        header = rows[0]
        name_col = next((i for i, cell in enumerate(header) if _HEADER_NAME.search(cell)), None)
        birth_col = next((i for i, cell in enumerate(header) if _HEADER_BIRTH.search(cell)), None)
        if name_col is None or birth_col is None or name_col == birth_col:
            continue
        for row in rows[1:]:
            if name_col >= len(row):
                continue
            name = row[name_col].strip(" ,.")
            if not name or not _plausible(name):
                continue
            born = row[birth_col].strip() if birth_col < len(row) else ""
            # the note that one of them has aged out usually sits under the table
            emancipated = any(
                _EMANCIPATED.search(line) and name in line
                for line in text.splitlines()
            )
            found.setdefault(name.casefold(), Child(name, born, source, emancipated))
    return list(found.values())


# Children named in prose with no birth date beside them, which is how a
# custody or tax clause does it: "primary custodial parent of Theo and Rory
# Ashdown", "Petitioner shall claim Rory each year". A decree can name every
# child this way and never print a single date of birth.
# The cue is case-insensitive; the names after it are not. Under a blanket
# IGNORECASE the capitalised-token requirement stops meaning anything and the
# match runs on into the sentence - "Rory each", "Theo in".
CUSTODY_CLAUSE = re.compile(
    r"(?i:(?:custodial\s+parent\s+of|custody\s+of|shall\s+claim|claiming|"
    r"primary\s+(?:physical\s+)?custody\s+of|residence\s+of)\s+)"
    r"((?:[A-Z][A-Za-z'’\-]+(?:\s+[A-Z][A-Za-z'’\-]+){0,2}"
    r"(?:\s*(?:,|(?i:and))\s*)?){1,6})"
)
_SPLIT = re.compile(r"\s*(?:,|\band\b)\s*", re.IGNORECASE)


def from_custody_clauses(text: str, source: str = "") -> list[Child]:
    """Children a custody or tax clause names, with no date to go on."""
    found: dict[str, Child] = {}
    for match in CUSTODY_CLAUSE.finditer(text):
        for part in _SPLIT.split(match.group(1)):
            name = " ".join(part.split()).strip(" ,.")
            if not name or len(name.split()) > 3:
                continue
            first = name.split()[0]
            if (first.casefold() in caption.BOILERPLATE
                    or not first[:1].isupper() or not name.replace(
                        " ", "").replace("'", "").replace("-", "").isalpha()):
                continue
            found.setdefault(name.casefold(), Child(name=name, source=source))
    return list(found.values())


def harvest(text: str, tables=(), source: str = "") -> list[Child]:
    """Every child this document names, from either layout."""
    merged: dict[str, Child] = {}
    for child in [*from_tables(tables, text, source), *from_text(text, source),
                  *from_custody_clauses(text, source)]:
        existing = merged.get(child.key)
        if existing is None or (not existing.born and child.born):
            merged[child.key] = child
    return list(merged.values())
