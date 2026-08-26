"""Harvest party names out of a legal document's caption / running headers.

Pleadings put the people who matter in a predictable place: the caption block
at the top of page one, and the running header on every page after it.  This
module reads that region and proposes full names, each tagged with the role it
was found under, so the name-entry screen opens pre-populated instead of blank.

Nothing here mutates a document - it only proposes.  The operator confirms.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

# --------------------------------------------------------------------------

ROLE_ALT = (
    r"(?:Co-)?(?:Petitioner|Respondent|Plaintiff|Defendant|Appellant|Appellee|Petitioners|"
    r"Respondents|Plaintiffs|Defendants|Movant|Intervenor|Claimant|Applicant|Obligor|Obligee|"
    r"Counter-?claimant|Counter-?defendant|Cross-?claimant|Cross-?defendant|"
    r"Third-?Party\s+(?:Plaintiff|Defendant)|Guardian\s+ad\s+Litem|Custodial\s+Parent|"
    r"Non-?custodial\s+Parent|Decedent|Ward|Trustee|Beneficiary|Personal\s+Representative|"
    r"Executor|Executrix|Administrator|Conservator|Minor\s+Child(?:ren)?|Subject\s+Minor)"
)

_ROLE_RE = re.compile(rf"\b{ROLE_ALT}\b", re.IGNORECASE)

MINOR_ROLES = {"minor child", "minor children", "subject minor", "ward"}

# A personal name: 2-5 capitalised tokens, tolerating middle initials, particles
# and generational suffixes.  Works for Title Case and ALL CAPS captions alike.
_TOKEN = r"(?:[A-Z][A-Za-z'’\-]{1,20}|[A-Z]{2,20}|[A-Z]\.)"
_PARTICLE = r"(?:van|von|de|del|della|di|da|du|la|le|el|bin|ibn|al|st\.?|mc|mac|o')"
_SUFFIX = r"(?:Jr\.?|Sr\.?|II|III|IV|V|Esq\.?)"
_WS = r"[^\S\n]+"   # whitespace that is not a line break - names never wrap
NAME_RE = re.compile(
    rf"(?<![\w'’])({_TOKEN}(?:{_WS}(?:{_PARTICLE}{_WS})?{_TOKEN}){{1,4}}(?:,?{_WS}{_SUFFIX})?)(?![\w'’])"
)

# Words that mean "this line is caption furniture, not a person".
BOILERPLATE = {
    "court", "courts", "county", "state", "states", "united", "judicial", "district",
    "division", "department", "dept", "case", "cause", "docket", "no", "nos", "number",
    "judge", "commissioner", "justice", "honorable", "hon", "clerk", "filed", "electronically",
    "in", "the", "of", "and", "for", "to", "v", "vs", "re", "matter", "marriage", "estate",
    "findings", "fact", "conclusions", "law", "decree", "divorce", "order", "motion", "notice",
    "affidavit", "declaration", "memorandum", "stipulation", "stipulated", "agreement",
    "petition", "complaint", "answer", "reply", "response", "objection", "exhibit", "summons",
    "certificate", "service", "proposed", "amended", "verified", "supplemental", "temporary",
    "permanent", "final", "ex", "parte", "attorney", "attorneys", "counsel", "esq", "bar",
    "law", "office", "offices", "firm", "group", "associates", "partners", "llc", "llp",
    "pllc", "pc", "inc", "corp", "corporation", "company", "co", "ltd", "trust", "trustee",
    "plaintiff", "defendant", "petitioner", "respondent", "appellant", "appellee",
    "telephone", "facsimile", "fax", "email", "e-mail", "address", "phone",
    "page", "pages", "sheet", "caption", "title", "date", "dated", "signature", "signed",
    "salt", "lake", "utah", "america", "government", "people", "commonwealth",
    # Utah cities that appear in courthouse and pro-se address blocks. Without
    # these, "West Jordan—Third District Court, 8080 S. Redwood Road" proposed
    # "West Jordan" as a high-confidence party, ticked by default, and
    # "Pleasant Grove" reached the judicial do-not-change list - which would
    # have shielded a party's home city from redaction.
    "jordan", "provo", "orem", "roosevelt", "grove", "pleasant", "sandy",
    "ogden", "logan", "layton", "murray", "sciences", "vernal", "duchesne",
    "altonah", "springville", "lehi", "draper", "tooele", "heber", "moab",
}

# A line carrying a street address or a ZIP is furniture, whatever names the
# name regex can find inside it.
_DIRECTION = r"(?:North|South|East|West|N|S|E|W)"
_ADDRESS_RE = re.compile(
    # a grid address, spelled out or abbreviated: 721 West 1800 North, 8080 S. Redwood
    rf"\b\d{{1,6}}\s+{_DIRECTION}\.?\s"
    # a named street with a suffix
    rf"|\b\d{{1,6}}\s+(?:{_DIRECTION}\.?\s+)?[\w'\-]+\s+"
    r"(?:Road|Rd|Street|St|Avenue|Ave|Drive|Dr|Lane|Ln|Way|Boulevard|Blvd|"
    r"Circle|Cir|Court|Ct|Place|Pl|Parkway|Pkwy)\b"
    r"|\bSuite\s+\d|\bSte\.?\s+\d|\bP\.?\s*O\.?\s+Box\b|\b[A-Z]{2}\s+\d{5}\b"
    r"|\b\d{5}(?:-\d{4})?\s*$",
    re.IGNORECASE,
)


def is_address(text: str) -> bool:
    """True when this text is a street address rather than a person."""
    return bool(_ADDRESS_RE.search(text))


@dataclass(frozen=True)
class CaptionName:
    """A name proposed by the caption reader."""

    name: str
    role: str            # "Petitioner", "Attorney for Respondent", "Caption party"…
    source: str          # which document / region it came from
    confidence: str      # "high" | "medium" | "low"
    category: str = "person"   # "person" or "minor"

    @property
    def key(self) -> str:
        return " ".join(self.name.split()).casefold()


# --------------------------------------------------------------------------
# region selection
# --------------------------------------------------------------------------

_BODY_START = re.compile(
    r"^\s*(?:COMES\s+NOW|The\s+(?:Court|Petitioner|Respondent|parties)|Pursuant\s+to|"
    r"BASED\s+(?:UPON|ON)|NOW\s+THEREFORE|THE\s+COURT\s+(?:FINDS|HEREBY)|"
    r"\d+\.\s|I\.\s|INTRODUCTION|JURISDICTION\s+AND\s+VENUE|FINDINGS\s+OF\s+FACT)",
    re.IGNORECASE,
)


def caption_region(text: str, max_lines: int = 70) -> str:
    """The top-of-document block where the caption lives."""
    lines = text.splitlines()
    cut = min(len(lines), max_lines)
    for i, line in enumerate(lines[:max_lines]):
        # don't cut before we've seen the caption's case number or a role word
        if i > 8 and _BODY_START.match(line):
            cut = i
            break
    return "\n".join(lines[:cut])


# --------------------------------------------------------------------------
# candidate hygiene
# --------------------------------------------------------------------------


def _clean(raw: str) -> str:
    name = " ".join(raw.replace("’", "'").split())
    name = name.strip(" ,.;:-")
    # Drop a trailing role word that got swept into the match. After a comma
    # it is always furniture ("JOHN WARD, Petitioner"); bare at the end it is
    # only dropped when at least two name tokens remain, so a party actually
    # surnamed Ward or Decedent keeps their name.
    comma_role = re.search(rf",\s*(?:{ROLE_ALT})\s*$", name, re.IGNORECASE)
    if comma_role:
        name = name[:comma_role.start()]
    else:
        stripped = re.sub(rf"\s+(?:{ROLE_ALT})\s*$", "", name,
                          flags=re.IGNORECASE).strip(" ,.;:-")
        if len(stripped.split()) >= 2:
            name = stripped
    return " ".join(name.strip(" ,.;:-").split())


def plausible_name(raw: str) -> bool:
    name = _clean(raw)
    tokens = [t for t in re.split(r"[\s,]+", name) if t]
    if not 2 <= len(tokens) <= 5:
        return False
    for tok in tokens:
        bare = tok.strip(".,'-").casefold()
        if bare in BOILERPLATE:
            return False
        if any(ch.isdigit() for ch in tok):
            return False
    # at least two tokens must be real words, not lone initials
    if sum(1 for t in tokens if len(t.strip(".")) > 1) < 2:
        return False
    return True


def _role_category(role: str) -> str:
    return "minor" if role.strip().casefold() in MINOR_ROLES else "person"


# --------------------------------------------------------------------------
# harvesters
# --------------------------------------------------------------------------

_IN_RE = re.compile(
    r"\bIn\s+(?:re\b:?|the\s+(?:Matter|Interest)\s+of)\s+(?:the\s+)?"
    r"(?:Marriage|Matter|Estate|Adoption|Guardianship|Paternity|Parentage|Custody|"
    r"Name\s+Change|Conservatorship)\s+of\s*[:,]?\s*(.{3,140})",
    re.IGNORECASE | re.DOTALL,
)
_V_LINE = re.compile(r"^\s*(?:v\.?|vs\.?)\s*$", re.IGNORECASE)
_INLINE_V = re.compile(r"(.{3,60}?)\s+(?:v\.?|vs\.?)\s+(.{3,60}?)(?:,|\s*$|\s+Case\b)", re.IGNORECASE)
_ATTORNEY_FOR = re.compile(rf"\bAttorneys?\s+for\s+(?:the\s+)?({ROLE_ALT})", re.IGNORECASE)
_SIGNATURE = re.compile(r"/s/\s*(.{3,60})")
_NAME_ROLE_SAME_LINE = re.compile(rf"^\s*(.{{3,70}}?)\s*,\s*({ROLE_ALT})\b", re.IGNORECASE)
_ROLE_ONLY_LINE = re.compile(rf"^\s*({ROLE_ALT})[\s.,;:]*$", re.IGNORECASE)


def _left_column(line: str) -> str:
    """The left-hand caption column - text before the first run of 3+ spaces."""
    return re.split(r"\s{3,}", line.strip())[0].strip()


def _bare_role(line: str) -> str | None:
    m = _ROLE_ONLY_LINE.match(_left_column(line))
    return m.group(1) if m else None


_SURNAME_RE = re.compile(r"(?<![\w'’])([A-Z][A-Za-z'’\-]{2,20})(?![\w'’])")


def _first_surname_in(text: str) -> str | None:
    """A single capitalised word - enough for a running header like 'Smith v. Jones'."""
    for m in _SURNAME_RE.finditer(text):
        token = m.group(1)
        if token.strip(".,'-").casefold() in BOILERPLATE:
            continue
        if any(ch.isdigit() for ch in token):
            continue
        return token
    return None


def _first_name_in(text: str) -> str | None:
    if is_address(text):
        return None
    for m in NAME_RE.finditer(text):
        if plausible_name(m.group(1)):
            return _clean(m.group(1))
    return None


def _all_names_in(text: str) -> list[str]:
    out: list[str] = []
    if is_address(text):
        return out
    for m in NAME_RE.finditer(text):
        if plausible_name(m.group(1)):
            cleaned = _clean(m.group(1))
            if cleaned not in out:
                out.append(cleaned)
    return out


def harvest(text: str, source: str = "") -> list[CaptionName]:
    """Propose party names from a caption / header region."""
    found: list[CaptionName] = []

    def add_raw(name: str, role: str, confidence: str):
        cleaned = " ".join(name.split()).strip(" ,.;:-")
        if not cleaned:
            return
        key = cleaned.casefold()
        for existing in found:
            if existing.key == key or key in existing.key.split():
                return
        found.append(CaptionName(cleaned, role, source, confidence, _role_category(role)))

    def add(name: str, role: str, confidence: str):
        if not name or not plausible_name(name):
            return
        cleaned = _clean(name)
        key = cleaned.casefold()
        for existing in found:
            if existing.key == key:
                return
        found.append(CaptionName(cleaned, role, source, confidence, _role_category(role)))

    lines = text.splitlines()

    # 1. "NAME, Petitioner," on one line
    for line in lines:
        m = _NAME_ROLE_SAME_LINE.match(line)
        if m:
            add(m.group(1), m.group(2).title(), "high")

    # 2. a name line followed within three lines by a bare role line
    for i, line in enumerate(lines):
        role_word = _bare_role(line)
        if not role_word:
            continue
        role = role_word.title()
        for back in range(1, 4):
            j = i - back
            if j < 0:
                break
            candidate = lines[j].strip()
            if not candidate:
                continue
            # caption columns: the party sits left of the document title
            left = _left_column(candidate)
            name = _first_name_in(left) or _first_name_in(candidate)
            if name:
                add(name, role, "high")
                break

    # 3. "In re the Marriage of X and Y"
    for m in _IN_RE.finditer(text):
        tail = m.group(1)
        parts = re.split(r"\s+and\s+|\s*&\s*", tail)
        for part in parts[:2]:
            name = _first_name_in(part)
            if name:
                add(name, "Caption party", "high")

    # 4. a bare "v." line separates the two sides of the caption
    for i, line in enumerate(lines):
        if not _V_LINE.match(line):
            continue
        for j in range(max(0, i - 4), i):
            name = _first_name_in(_left_column(lines[j]))
            if name:
                add(name, "Caption party", "high")
                break
        for j in range(i + 1, min(len(lines), i + 5)):
            name = _first_name_in(_left_column(lines[j]))
            if name:
                add(name, "Caption party", "high")
                break

    # 5. running header style: "Smith v. Jones, Case No. 1234"
    for m in _INLINE_V.finditer(text):
        for side in (m.group(1), m.group(2)):
            name = _first_name_in(side)
            if name:
                add(name, "Caption party", "medium")
                continue
            surname = _first_surname_in(side)
            if surname:
                add_raw(surname, "Caption party (surname only)", "low")

    # 6. attorney signature blocks
    for m in _SIGNATURE.finditer(text):
        name = _first_name_in(m.group(1))
        if name:
            add(name, "Signatory", "medium")
    for m in _ATTORNEY_FOR.finditer(text):
        window = text[max(0, m.start() - 260): m.start()]
        names = _all_names_in(window)
        if names:
            add(names[-1], f"Attorney for {m.group(1).title()}", "medium")

    return found


def harvest_documents(regions: dict[str, str]) -> list[CaptionName]:
    """Harvest across several documents, merging duplicates by name.

    ``regions`` maps a display label (usually the file name plus which part of
    it the text came from) to the caption text for that label.
    """
    merged: dict[str, CaptionName] = {}
    rank = {"high": 3, "medium": 2, "low": 1}
    for label, text in regions.items():
        for cand in harvest(text, label):
            existing = merged.get(cand.key)
            if existing is None:
                merged[cand.key] = cand
            elif rank[cand.confidence] > rank[existing.confidence]:
                merged[cand.key] = cand
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(merged.values(), key=lambda c: (order[c.confidence], c.name))
