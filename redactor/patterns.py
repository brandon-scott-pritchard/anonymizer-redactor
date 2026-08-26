"""Deterministic pattern detectors.

Two kinds of detector live here:

*   **self-identifying** - the value's own shape is distinctive enough to act
    on (``123-45-6789``, an email address, a VIN, a Luhn-valid card number).
*   **labeled** - the value is a generic run of characters that only becomes
    sensitive because of the words in front of it ("Account No. 44821").
    Requiring the label is what keeps false positives out of legal prose.

Everything is plain ``re``.  Given the same input text the same spans come out
every time - no model, no randomness, no network.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from functools import lru_cache
from typing import Callable, Iterable, Sequence

# --------------------------------------------------------------------------
# match record
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class Match:
    start: int
    end: int
    text: str
    category: str
    source: str = "pattern"   # pattern | name-list | ner | caption
    priority: int = 50

    @property
    def length(self) -> int:
        return self.end - self.start


@dataclass(frozen=True)
class Detector:
    category: str
    regex: re.Pattern
    group: int = 0
    validator: Callable[[str], bool] | None = None
    priority: int = 50
    # cheap prechecks: skip the regex when the text cannot possibly match.
    # needs_digit is set only where the pattern provably requires a digit;
    # keywords only where every possible match contains one of them.
    needs_digit: bool = False
    keywords: tuple[str, ...] = ()


# Test hook: turning this off must never change scan()/allowlist_spans()
# output - the prefilters are pure go-faster stripes.
PREFILTER = True


# --------------------------------------------------------------------------
# value shapes used by the labeled detectors
# --------------------------------------------------------------------------

# a generic identifier that must contain at least one digit
V_ALNUM = r"(?=[A-Za-z0-9][A-Za-z0-9\-/.]*\d)[A-Za-z0-9][A-Za-z0-9\-/.]{2,29}"
V_DIGITS = r"\d[\d\-\s]{2,25}\d"
# Mask runs may repeat per group ("XXX-XX-6789", "****-****-1234") - a single
# adjacent run would miss the standard formats and ship the real last four.
# The ellipsis run is a mask too: statements print "Acct #: ...3907" and
# "...6613" as often as they print asterisks. Two dots minimum, so a decimal
# point can never open one.
V_MASKED = (r"(?:(?:[Xx*•#]{2,}|\.{2,4})[\-\s]?)+\d{2,6}"
            r"|\d{2,6}(?:[\-\s]?(?:[Xx*•#]{2,}|\.{2,4}))+")
# Digits printed in groups, which is how utility, cable and wireless accounts
# are written: "8102 4477 9301". V_ALNUM forbids the space, so only the first
# group was captured and "Account Number: [ACCOUNT-14] 4477 9301" shipped
# two thirds of the number while looking redacted. Groups of three or more
# digits, so a date ("03 14 2026") cannot enter.
V_GROUPED = r"\d{3,6}(?:[\-\s]\d{3,6}){1,4}"
V_ANY_ID = rf"(?:{V_MASKED}|{V_ALNUM})"
# What the financial detectors accept: the grouped form as well.
V_ACCOUNT_ID = rf"(?:{V_MASKED}|{V_GROUPED}|{V_ALNUM})"

# separator between a label and its value: "No.", "Number", "#", ":", "-"
# The bracketed aside is not decoration: IRS forms and bank statements are full
# of them - "Share Certificate (CD) number 44-8821-0033",
# "Account number (see instructions) HCS-0044-2211" - and without it the same
# line without its parenthesis matched while the real one did not.
_SEP = (
    r"(?:\s*(?:num(?:ber)?|no\.?|nos\.?|#|id(?:entif(?:ication|ying))?|acct\.?|"
    r"account|ending(?:\s+in)?|handle|profile|user\s?name|username|login|tag|"
    r"cash\s?tag|is|are|was|were|of|for|at|with|"
    # A transaction line puts a verb between the rail and the reference:
    # "VENMO PAYMENT 3948217364" shipped its transaction id because nothing
    # could step over the word PAYMENT.
    r"payment|pmt|transfer|transaction|ref|reference|posted|to|from|"
    # a label qualifies itself before it gets to the value: "Student ID at
    # school of record:", "Loan number of record:"
    r"school|record|primary|current|assigned)"
    # a column heading writes its alternatives with a slash - "Account /
    # identifying number" is the heading on the Utah asset schedule, and
    # without the slash the whole column read as unlabelled
    r"|\s*/|\s*\([^)\n]{1,30}\)|\s*\)){0,6}\s*[:\-–—=#]?\s*"
)

_MONTHS = (
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:t)?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
)
V_DATE = (
    rf"(?:\d{{1,2}}[/\-.]\d{{1,2}}[/\-.]\d{{2,4}}"
    rf"|\d{{4}}[/\-.]\d{{1,2}}[/\-.]\d{{1,2}}"          # ISO, routine in records
    rf"|(?:{_MONTHS})\.?\s+\d{{1,2}},?\s+\d{{4}}"
    rf"|\d{{1,2}}\s+(?:{_MONTHS})\.?,?\s+\d{{4}})"
)


def labeled(labels: Sequence[str], value: str = V_ANY_ID) -> str:
    """Build a ``label + separator + (value)`` regex with the value in group 1."""
    alt = "|".join(labels)
    return rf"(?:{alt}){_SEP}({value})"


# --------------------------------------------------------------------------
# validators
# --------------------------------------------------------------------------


def luhn_valid(value: str) -> bool:
    digits = [int(c) for c in value if c.isdigit()]
    if not 13 <= len(digits) <= 19:
        return False
    checksum, parity = 0, len(digits) % 2
    for i, d in enumerate(digits):
        if i % 2 == parity:
            d *= 2
            if d > 9:
                d -= 9
        checksum += d
    return checksum % 10 == 0


def valid_ssn(value: str) -> bool:
    digits = "".join(c for c in value if c.isdigit())
    if len(digits) != 9:
        return False
    area, group, serial = digits[:3], digits[3:5], digits[5:]
    if area in {"000", "666"} or area.startswith("9"):
        return False
    return group != "00" and serial != "0000"


def valid_routing(value: str) -> bool:
    digits = "".join(c for c in value if c.isdigit())
    if len(digits) != 9:
        return False
    weights = (3, 7, 1, 3, 7, 1, 3, 7, 1)
    return sum(int(d) * w for d, w in zip(digits, weights)) % 10 == 0


def valid_vin(value: str) -> bool:
    v = value.upper()
    return len(v) == 17 and not any(c in v for c in "IOQ") and any(c.isdigit() for c in v)


# --------------------------------------------------------------------------
# allowlist - spans the tool is forbidden to alter
# --------------------------------------------------------------------------

_REPORTERS = (
    r"U\.\s?S\.|S\.\s?Ct\.|L\.\s?Ed\.(?:\s?2d)?|F\.\s?(?:2d|3d|4th)|F\.\s?Supp\.(?:\s?[23]d)?|"
    r"F\.\s?App'x|P\.\s?(?:2d|3d)|A\.\s?(?:2d|3d)|N\.E\.\s?(?:2d|3d)|N\.W\.\s?(?:2d|3d)|"
    r"S\.E\.\s?(?:2d|3d)|S\.W\.\s?(?:2d|3d)|So\.\s?(?:2d|3d)|Cal\.\s?(?:2d|3d|4th|5th)|"
    r"Utah(?:\s?2d)?|UT(?:\s?App)?"
)

ALLOWLIST_PATTERNS: tuple[re.Pattern, ...] = (
    # statutes and codes: Utah Code Ann. § 30-3-5(1)(a), 42 U.S.C. § 1983
    re.compile(
        r"\b(?:[A-Z][A-Za-z.]*\s+){0,4}(?:Code|Const\.|Constitution|Stat\.|Statutes?|"
        r"U\.?S\.?C\.?|C\.?F\.?R\.?|Ann\.)[A-Za-z.\s]{0,20}(?:§+|Sections?|Secs?)\.?\s*[\d\-.:()a-zA-Z]+"
        r"(?:\s*(?:et seq\.|\([\da-zA-Z]+\))*)*",
        re.IGNORECASE,
    ),
    # any bare section symbol reference
    re.compile(r"§+\s*[\d\-.:]+(?:\([\da-zA-Z]+\))*(?:\s*(?:and|through|to|-)\s*§*\s*[\d\-.:]+)*"),
    # "Section 30-3-5", "Sec. 1983", "Sec 78B-12-202" - with or without the period
    re.compile(
        r"\b(?:Sections?|Secs?)\.?\s*\d[\dA-Za-z\-.:]*(?:\([\da-zA-Z]+\))*"
        r"(?:\s*(?:and|through|to)\s*(?:Sections?|Secs?)?\.?\s*\d[\dA-Za-z\-.:]*)*",
        re.IGNORECASE,
    ),
    # rules of procedure / evidence. Every branch demands either the full word
    # "Rule(s)" or an abbreviated citation with a mandatory qualifier - a bare
    # optional "R." would degenerate to r"R\.?\s*\d+" and shield any
    # identifier that happens to start with R (license "R12345678").
    re.compile(
        r"\b(?:Fed\.\s*R\.\s*(?:Civ|Crim|App|Evid|Bankr)\.\s*P\.?|"
        r"(?:Utah|Federal|Local|Local\s+Civil)\s+R\.\s*(?:[A-Z][a-z]{0,5}\.?\s*){0,3}P\.?|"
        r"Rules?\b(?:\s+of\s+[A-Z][A-Za-z]+\s+(?:Procedure|Evidence))?)"
        r"\s*\d+(?:\.\d+)*(?:\([\da-zA-Z]+\))*",
        re.IGNORECASE,
    ),
    # reporter citations: 505 U.S. 833, 2019 UT App 12
    re.compile(rf"\b\d{{1,4}}\s+(?:{_REPORTERS})\s+\d{{1,4}}\b"),
    # a party pair followed by a reporter citation is cited authority, not our client
    re.compile(
        rf"\b[A-Z][\w'.\-]+(?:\s+[\w'.\-]+){{0,4}}\s+v\.?s?\.\s+[A-Z][\w'.\-]+"
        rf"(?:\s+[\w'.\-]+){{0,4}},?\s+\d{{1,4}}\s+(?:{_REPORTERS})"
    ),
    # Judicial officers by title - the name beside the title is protected.
    # A separator of ":" or "," as well as whitespace: "Judge: Amber M. Cordova"
    # is how a Utah caption prints the assigned judge, and requiring whitespace
    # left that name completely unshielded.
    re.compile(
        r"\b(?:(?:Chief|Presiding|Assigned|Acting)[^\S\n]+)?"
        r"(?:Judge|(?:Court[^\S\n]+)?Commissioner|Justice|Referee|"
        r"Hearing[^\S\n]+Officer|Magistrate(?:[^\S\n]+Judge)?|Hon(?:orable)?\.?)"
        r"(?:[^\S\n]*[:,][^\S\n]*|[^\S\n]+)"
        r"[A-Z][\w'\-]+(?:[^\S\n]+[A-Z]\.)?(?:[^\S\n]+[A-Z][\w'\-]+){0,2}"
    ),
    # The same thing written the other way round, which is how every signature
    # block prints it: "AMBER M. CORDOVA, District Court Judge". The title may
    # sit on the following line, so a single newline is allowed in the gap.
    re.compile(
        r"(?<![\w'’])[A-Z][\w'\-]+(?:[^\S\n]+[A-Z]\.)?(?:[^\S\n]+[A-Z][\w'\-]+){0,2}"
        r"(?:[^\S\n]*,[^\S\n]*|[^\S\n]*\n[^\S\n]*)"
        r"(?:(?:[A-Z][A-Za-z.'\-]{0,14}|\d{1,2}(?:st|nd|rd|th))[^\S\n]+){0,4}"
        r"(?:Judge|JUDGE|Justice|JUSTICE|Commissioner|COMMISSIONER|"
        r"Magistrate|MAGISTRATE|Referee|REFEREE|Hearing Officer|HEARING OFFICER)\b"
    ),
    # court names
    re.compile(
        r"\b(?:In\s+the\s+)?(?:United\s+States\s+)?(?:Supreme|District|Superior|Circuit|Juvenile|"
        r"Justice|Municipal|Probate|Family|Bankruptcy|Appellate|Tax)\s+Court"
        r"(?:\s+(?:of|for|in\s+and\s+for)\s+(?:the\s+)?[A-Z][\w'\-]+(?:\s+[A-Z][\w'\-]+){0,4})?",
        re.IGNORECASE,
    ),
    re.compile(r"\bCourt\s+of\s+Appeals?(?:\s+(?:of|for)\s+(?:the\s+)?[A-Z][\w'\-]+(?:\s+[A-Z][\w'\-]+){0,3})?", re.IGNORECASE),
    re.compile(r"\b(?:First|Second|Third|Fourth|Fifth|Sixth|Seventh|Eighth|Ninth|Tenth|Eleventh|\d+(?:st|nd|rd|th))\s+Judicial\s+District\b", re.IGNORECASE),
    re.compile(r"\bClerk\s+of\s+(?:the\s+)?Court\b", re.IGNORECASE),
)


# (needs_digit, keywords) per allowlist pattern, same order. Set only where
# provably implied by the pattern - a wrong guard here un-protects citations.
_ALLOWLIST_GUARDS: tuple[tuple[bool, tuple[str, ...]], ...] = (
    (False, ("§", "sec")),       # statutes end in § / Section / Sec
    (False, ("§",)),
    (False, ("sec",)),
    (True, ()),                  # rules of procedure require a rule number
    (True, ()),                  # reporter citations require volume digits
    (True, ()),                  # party pair + reporter, likewise
    (False, ("judge", "commissioner", "justice", "magistrate", "referee",
             "hearing officer", "hon")),
    (False, ("judge", "commissioner", "justice", "magistrate", "referee",
             "hearing officer")),
    (False, ("court",)),
    (False, ("court",)),
    (False, ("judicial",)),
    (False, ("clerk",)),
)
assert len(_ALLOWLIST_GUARDS) == len(ALLOWLIST_PATTERNS)


@lru_cache(maxsize=64)
def _extra_regex(terms: tuple[str, ...]) -> re.Pattern | None:
    """One alternation for every literal term to shield.

    Compiled once per distinct list rather than per text unit: the judicial
    do-not-change list runs against every paragraph of every document, and a
    separate ``finditer`` per term was the whole gain of the prefilter work
    handed straight back.

    Boundaries match the ones the person matcher uses, so a term protects the
    word and its possessive ("Judge Cordova's order") without protecting a
    longer word that merely starts the same way ("Cordovan").
    """
    # local import: names imports nothing from here, so there is no cycle
    from .names import BOUNDARY_LEFT, BOUNDARY_RIGHT, escape_token

    bodies = [
        r"\s+".join(escape_token(token) for token in term.split())
        for term in sorted({t.strip() for t in terms if len(t.strip()) >= 2},
                           key=len, reverse=True)
    ]
    if not bodies:
        return None
    return re.compile(rf"{BOUNDARY_LEFT}(?:{'|'.join(bodies)}){BOUNDARY_RIGHT}",
                      re.IGNORECASE)


def allowlist_spans(text: str, extra: Iterable[str] = ()) -> list[tuple[int, int]]:
    """Character spans that must survive untouched."""
    spans: list[tuple[int, int]] = []
    lower = text.lower()
    has_digit = any(ch.isdigit() for ch in text)
    for pat, (needs_digit, keywords) in zip(ALLOWLIST_PATTERNS, _ALLOWLIST_GUARDS):
        if PREFILTER:
            if needs_digit and not has_digit:
                continue
            if keywords and not any(k in lower for k in keywords):
                continue
        for m in pat.finditer(text):
            spans.append((m.start(), m.end()))
    regex = _extra_regex(tuple(extra))
    if regex is not None:
        for m in regex.finditer(text):
            spans.append((m.start(), m.end()))
    return _merge_spans(spans)


def _merge_spans(spans: list[tuple[int, int]]) -> list[tuple[int, int]]:
    if not spans:
        return []
    spans.sort()
    merged = [spans[0]]
    for start, end in spans[1:]:
        last_start, last_end = merged[-1]
        if start <= last_end:
            merged[-1] = (last_start, max(last_end, end))
        else:
            merged.append((start, end))
    return merged


# --------------------------------------------------------------------------
# detectors
# --------------------------------------------------------------------------

_I = re.IGNORECASE


def _d(category, pattern, group=0, validator=None, priority=50, flags=_I,
       digit=False, kw=()):
    return Detector(category, re.compile(pattern, flags), group, validator, priority,
                    needs_digit=digit, keywords=tuple(kw))


DETECTORS: tuple[Detector, ...] = (
    # ------------------------------------------------------------- contact --
    _d("email", r"\b[\w!#$%&'*+/=?^`{|}~.-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\b", priority=90,
       kw=("@",)),
    _d("url", r"\b(?:https?://|ftp://|www\.)[^\s<>\"'\)\]]+", priority=88,
       kw=("http", "ftp", "www.")),
    _d("fax", rf"(?:fa(?:csimile|x(?:simile)?)|telecopier){_SEP}((?:\+?1[\s.\-]?)?(?:\(\d{{3}}\)\s?|\d{{3}}[\s.\-])\d{{3}}[\s.\-]?\d{{4}})", group=1, priority=86,
       digit=True, kw=("fax", "facsimile", "telecopier")),
    _d("phone", r"(?:\+?1[\s.\-]?)?\(\d{3}\)\s?\d{3}[\s.\-]?\d{4}(?:\s*(?:x|ext\.?|extension)\s*\d{1,6})?", priority=80,
       digit=True),
    _d("phone", r"(?<![\d\-])(?:\+?1[\s.\-])?\d{3}[\s.\-]\d{3}[\s.\-]\d{4}(?:\s*(?:x|ext\.?|extension)\s*\d{1,6})?(?![\d\-])", priority=80,
       digit=True),
    _d("phone", labeled(("telephone", "phone", "cell(?:ular)?", "mobile", "tel\\.?"),
                        r"\d{10}"), group=1, priority=79,
       digit=True, kw=("tel", "phone", "cell", "mobile")),
    _d("ip_address", r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b", priority=85,
       digit=True),
    _d("ip_address", r"\b(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}\b", priority=85,
       kw=(":",)),
    # abbreviated IPv6 ("fe80::1") - the common written form uses "::"
    _d("ip_address",
       r"(?<![\w:])(?:[0-9A-Fa-f]{1,4}:){1,6}:(?:[0-9A-Fa-f]{1,4}(?::[0-9A-Fa-f]{1,4}){0,5})?(?![\w:])",
       priority=85, kw=("::",)),
    _d("mac_address", r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b", priority=85),
    _d("gps", r"[-+]?\d{1,3}\.\d{4,}\s*[,\s]\s*[-+]?\d{1,3}\.\d{4,}", priority=85,
       digit=True),
    _d("social_handle",
       rf"\b(?:facebook|fb|instagram|insta|ig|twitter|x\.com|tiktok|snapchat|snap|linkedin|"
       rf"reddit|discord|telegram|whatsapp|youtube|onlyfans|tinder|bumble|hinge)\b\s*"
       rf"(?:profile|account|handle|user\s?name|username|id)?\s*(?:is|:|=|@|-)?\s*(@?[A-Za-z0-9._\-]{{3,30}})",
       group=1, priority=84,
       kw=("facebook", "fb", "insta", "ig", "twitter", "x.com", "tiktok", "snap",
           "linkedin", "reddit", "discord", "telegram", "whatsapp", "youtube",
           "onlyfans", "tinder", "bumble", "hinge")),
    _d("social_handle", r"(?<![\w./@])@[A-Za-z][A-Za-z0-9_.\-]{2,29}(?<![.\-])", priority=60,
       kw=("@",)),
    _d("username", labeled(("user\\s?name", "user\\s?id", "login(?:\\s?name|\\s?id)?", "screen\\s?name", "display\\s?name"),
                           r"[A-Za-z0-9._@\-]{3,40}"), group=1, priority=70,
       kw=("user", "login", "screen", "display")),

    # -------------------------------------------------------- government ID --
    _d("ssn", r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)", validator=valid_ssn, priority=95,
       digit=True),
    _d("ssn", labeled(("s\\.?s\\.?n\\.?", "social\\s+security", "soc\\.?\\s*sec\\.?"),
                      r"(?:\d{3}[-\s]?\d{2}[-\s]?\d{4}|" + V_MASKED + r")"), group=1, priority=96,
       digit=True, kw=("soc", "ssn", "s.s", "ss.")),
    _d("ein", labeled(("e\\.?i\\.?n\\.?", "f\\.?e\\.?i\\.?n\\.?", "employer\\s+identification",
                       "tax\\s*(?:payer)?\\s*i\\.?d\\.?", "t\\.?i\\.?n\\.?", "federal\\s+tax\\s*id",
                       # a state employer account, a business registration and a
                       # paid preparer's PTIN all identify the filer as surely
                       # as the federal EIN does
                       "state\\s+id(?:entification)?\\s*(?:number)?", "state\\s+employer",
                       "entity\\s*(?:no\\.?|number|#)", "ptin"),
                      r"(?:\d{2}-?\d{7}|[A-Z]{0,2}-?\d{2,10}(?:-\d{2,4})?|" + V_MASKED + r")"),
       group=1, priority=94,
       digit=True, kw=("ein", "e.i", "ei.", "fein", "f.e", "fe.", "tax",
                       "tin", "t.i", "ti.", "employer", "state", "entity", "ptin")),
    _d("ein", r"(?<!\d)\d{2}-\d{7}(?!\d)", priority=62,
       digit=True),
    # "card?" parsed as "car" + optional "d", so this branch could never match
    # without the letters c-a-r in the text. It wanted the whole word optional.
    _d("drivers_license", labeled(("driver'?s?\\s*licen[sc]e", "d\\.?l\\.?", "operator'?s?\\s*licen[sc]e",
                                   "state\\s+id(?:entification)?(?:\\s*card)?")), group=1, priority=90,
       digit=True, kw=("licen", "dl", "d.l", "operator", "state")),
    _d("passport", labeled(("passport",)), group=1, priority=90,
       digit=True, kw=("passport",)),
    _d("alien_number", r"\bA[-#\s]?\d{8,9}\b", priority=88,
       digit=True),
    _d("alien_number", labeled(("alien\\s*(?:registration)?", "a-?number", "uscis", "uscis\\s*#?", "visa", "i-?94")), group=1, priority=89,
       digit=True, kw=("alien", "numb", "uscis", "visa", "i94", "i-94")),
    _d("military_id", labeled(("military\\s*(?:service|id)", "dod\\s*id", "service\\s*number", "edipi")), group=1, priority=88,
       digit=True, kw=("military", "dod", "service", "edipi")),
    _d("inmate_number", labeled(("inmate", "booking", "offender", "prisoner", "doc\\s*(?:id)?", "jail\\s*id")), group=1, priority=88,
       digit=True, kw=("inmate", "booking", "offender", "prisoner", "doc", "jail")),
    _d("student_id", labeled(("student\\s*(?:id)?", "pupil\\s*id", "school\\s*id", "enrollment")), group=1, priority=86,
       digit=True, kw=("student", "pupil", "school", "enrollment")),
    _d("employee_id", labeled(("employee\\s*(?:no\\.?|number|id|#)?", "emp\\s*(?:no\\.?|id|#)",
                               "payroll\\s*(?:no\\.?|number|id|#)", "associate\\s*(?:no\\.?|id)",
                               "badge", "personnel\\s*(?:no\\.?|number|id)")), group=1, priority=85,
       digit=True, kw=("employee", "emp", "payroll", "associate", "badge", "personnel")),
    _d("voter_id", labeled(("voter\\s*(?:registration|reg\\.?|id)?",)), group=1, priority=86,
       digit=True, kw=("voter",)),
    _d("bar_number", labeled(("bar\\s*(?:no\\.?|number|#|id)", "utah\\s+bar", "state\\s+bar", "attorney\\s*(?:reg(?:istration)?)?")), group=1, priority=86,
       digit=True, kw=("bar", "attorney")),
    _d("notary_id", labeled(("notary\\s*(?:commission|id|public)?", "commission\\s*(?:no\\.?|number|#)")), group=1, priority=86,
       digit=True, kw=("notary", "commission")),
    _d("professional_license", labeled(("licen[sc]e\\s*(?:no\\.?|number|#)?", "certification\\s*(?:no\\.?|number|#)",
                                        "npi", "registration\\s*(?:no\\.?|number|#)",
                                        # a financial adviser's identifiers, which
                                        # appear on every brokerage statement
                                        "\\bcrd\\b", "\\biard\\b")), group=1, priority=84,
       digit=True, kw=("licen", "certification", "npi", "registration", "crd", "iard")),
    _d("tribal_id", labeled(("tribal\\s*(?:enrollment|id|member(?:ship)?)", "enrollment\\s*(?:no\\.?|number|#)",
                             "cdib", "certificate\\s+of\\s+degree\\s+of\\s+indian\\s+blood")), group=1, priority=86,
       digit=True, kw=("tribal", "enrollment", "cdib", "indian")),

    # ------------------------------------------------------------- health ----
    _d("mrn", labeled(("m\\.?r\\.?n\\.?", "medical\\s+record", "patient\\s*(?:id|number|no\\.?|#)",
                       "chart\\s*(?:no\\.?|number|#)", "health\\s+record")), group=1, priority=90,
       digit=True, kw=("mrn", "m.r", "mr.", "medical", "patient", "chart", "health")),
    _d("health_plan", labeled(("member\\s*(?:id|no\\.?|number|#)", "subscriber\\s*(?:id|no\\.?|number|#)",
                               "group\\s*(?:no\\.?|number|#)", "health\\s+plan", "insurance\\s+id",
                               "medicaid", "medicare", "hicn", "mbi")), group=1, priority=88,
       digit=True, kw=("member", "subscriber", "group", "health", "insurance",
                       "medicaid", "medicare", "hicn", "mbi")),
    _d("diagnosis_code", labeled(("icd(?:-?10|-?9)?\\s*(?:cm)?\\s*(?:code)?", "cpt\\s*(?:code)?",
                                  "dsm(?:-?5|-?iv)?\\s*(?:code)?", "diagnosis\\s+code", "hcpcs"),
                                 r"[A-TV-Z]?\d{2,5}(?:\.\d{1,4})?"), group=1, priority=88,
       digit=True, kw=("icd", "cpt", "dsm", "diagnosis", "hcpcs")),
    _d("prescription", labeled(("rx", "prescription", "ndc")), group=1, priority=86,
       digit=True, kw=("rx", "prescription", "ndc")),

    # ---------------------------------------------------------- financial ----
    _d("credit_card", r"(?<![\d\-])(?:\d[ -]?){12,18}\d(?![\d\-])", validator=luhn_valid, priority=93,
       digit=True),
    # Bare "card" is admitted as a label, but only in front of a masked form or
    # a four-digit tail - "card ending in 4417" is how every statement prints
    # it, and it was the worst-caught identifier in the audit. Restricting the
    # value is what keeps "card 1 of 2" and "card holder" out.
    _d("credit_card", labeled(("credit\\s*card", "debit\\s*card", "visa", "mastercard", "master\\s*card",
                               "amex", "american\\s+express", "discover", "card\\s*(?:no\\.?|number|#)",
                               "card"),
                              # A bare 4-digit tail covers "card ending in 9876".
                              # It needs both digit boundaries: without them the
                              # bare "card" label above sliced "1234" out of
                              # "State ID card 12345678".
                              rf"(?:{V_MASKED}|(?:\d[ -]?){{12,18}}\d|(?<!\d)\d{{4}}(?!\d))"),
       group=1, priority=92,
       digit=True, kw=("card", "visa", "amex", "american", "discover")),
    _d("routing_number", labeled(("routing", "aba", "rtn", "transit")), group=1, priority=91,
       digit=True, kw=("routing", "aba", "rtn", "transit")),
    # A bare nine-digit run that passes the ABA checksum. This wires up
    # valid_routing, which had been written and then never referenced, and it
    # covers the MICR line at the foot of a cheque and the wire-instruction
    # block, neither of which carries a label. Honest cost: the checksum passes
    # roughly one in ten random nine-digit strings, so this sits at proposal
    # priority where a labelled hit always outranks it.
    _d("routing_number", r"(?<![\d\-])\d{9}(?![\d\-])", validator=valid_routing,
       priority=60, digit=True),
    _d("iban", r"\b[A-Z]{2}\d{2}[ ]?(?:[A-Z0-9]{4}[ ]?){2,7}[A-Z0-9]{1,4}\b", priority=90, flags=0,
       digit=True),
    _d("swift", labeled(("swift", "bic", "swift\\s*/\\s*bic"), r"[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?"), group=1, priority=90,
       kw=("swift", "bic")),
    _d("crypto_wallet", r"\b(?:bc1[a-z0-9]{25,62}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})\b", priority=90, flags=0,
       digit=True),
    _d("crypto_wallet", r"\b0x[a-fA-F0-9]{40}\b", priority=90,
       digit=True),
    # The transaction id on a payment-app line, which is not a handle: it
    # identifies the transfer, and a long digit run after the rail's name is
    # never the payer's username. Above payment_handle so it wins the position.
    _d("check_number",
       labeled(("venmo", "pay ?pal", "zelle", "cash ?app", "square ?cash"), r"\d{8,20}"),
       group=1, priority=90, digit=True,
       kw=("venmo", "paypal", "pay pal", "zelle", "cash", "square")),
    _d("payment_handle",
       labeled(("pay ?pal", "venmo", "cash ?app", "zelle", "square ?cash", "apple ?pay",
                "google ?pay", "wise", "revolut", "chime", "stripe", "coinbase", "robinhood"),
               r"[$@]?[A-Za-z0-9._\-]{3,40}"), group=1, priority=89,
       kw=("paypal", "pay pal", "venmo", "cash", "zelle", "square", "apple",
           "google", "wise", "revolut", "chime", "stripe", "coinbase", "robinhood")),
    # 457 and 529 are bare digit runs, and labeled() adds no word boundary of
    # its own: unguarded they matched *inside* an amount, so "Total 4571.30"
    # became "Total 457[INVACCOUNT-1]" - not a redaction but a silent rewrite,
    # with a fragment of the real figure left standing to make it look clean.
    # 401(k) and 403(b) were never exposed to this because they end in a letter.
    _d("investment_account", labeled(("401\\s?\\(?k\\)?", "403\\s?\\(?b\\)?",
                                      "(?<!\\d)457(?!\\d)", "ira", "roth(?:\\s+ira)?",
                                      "pension", "annuity", "brokerage", "tsp", "hsa",
                                      "(?<!\\d)529(?!\\d)",
                                      "retirement\\s+(?:account|plan)", "investment\\s+account",
                                      "securities\\s+account", "mutual\\s+fund",
                                      "plan\\s*(?:no\\.?|number|#)", "participant",
                                      "contract\\s*(?:no\\.?|number|#)"),
                                     V_ACCOUNT_ID), group=1, priority=87,
       digit=True, kw=("401", "403", "457", "ira", "roth", "pension", "annuity",
                       "brokerage", "tsp", "hsa", "529", "retirement", "investment",
                       "securities", "mutual", "plan", "participant", "contract")),
    _d("bank_account", labeled(("bank\\s+account", "checking\\s*(?:account)?", "savings\\s*(?:account)?",
                                "deposit\\s+account", "acct", "account", "a/c", "money\\s+market",
                                "certificate\\s+of\\s+deposit", "\\bcd\\b",
                                # a utility meter and a service account are both
                                # tied to the service address, so both identify
                                # where somebody lives
                                "meter", "service\\s*(?:account|no\\.?|number)"),
                               V_ACCOUNT_ID), group=1, priority=82,
       digit=True, kw=("bank", "checking", "savings", "deposit", "acct", "account",
                       "a/c", "money", "cd", "meter", "service")),
    _d("loan_number", labeled(("loan", "mortgage", "escrow", "note\\s*(?:no\\.?|number|#)", "deed\\s+of\\s+trust",
                               "heloc", "line\\s+of\\s+credit", "promissory\\s+note"),
                              V_ACCOUNT_ID), group=1, priority=86,
       digit=True, kw=("loan", "mortgage", "escrow", "note", "deed", "heloc",
                       "credit", "promissory")),
    _d("policy_number", labeled(("polic(?:y|ies)", "insurance\\s+polic(?:y|ies)", "coverage\\s*(?:no\\.?|number|#)",
                                 "certificate\\s+of\\s+insurance")), group=1, priority=86,
       digit=True, kw=("polic", "coverage", "insurance")),
    _d("claim_number", labeled(("claim", "file\\s*(?:no\\.?|number|#)\\s*\\(claim\\)", "adjuster\\s*file")), group=1, priority=86,
       digit=True, kw=("claim", "adjuster")),
    _d("check_number", labeled(("check", "cheque", "draft", "wire\\s*(?:confirmation|reference|transfer)?",
                                "invoice", "transaction", "confirmation", "reference\\s*(?:no\\.?|number|#)",
                                "receipt", "\\bref\\b", "trace\\s*(?:no\\.?|number|#)",
                                "auth(?:orization)?\\s*(?:code|no\\.?|number)")), group=1, priority=80,
       digit=True, kw=("check", "cheque", "draft", "wire", "invoice", "transaction",
                       "confirmation", "reference", "receipt", "ref", "trace", "auth")),
    # A masked tail with no label at all - "****3907", "XXXX-XXXX-XXXX-4417",
    # "...6613". Nothing caught these, and two of them shipped whole because the
    # value-literal boundary in engine.py refuses to match after a "-" or a "."
    # (right for emails, wrong for a masked account). Priority 72 sits below
    # every labelled financial detector, so a labelled hit still wins.
    _d("masked_account", rf"(?<![\w\-]){V_MASKED}(?![\w\-])", priority=72, digit=True),

    # ----------------------------------------------------------- property ----
    _d("street_address",
       r"\b\d{1,6}[A-Z]?\s+(?:(?:North|South|East|West|N\.?|S\.?|E\.?|W\.?|NE|NW|SE|SW)\s+)?"
       r"(?:[A-Z0-9][\w'\-]*\.?\s+){0,4}"
       r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|Circle|Cir|Way|Wy|"
       r"Place|Pl|Terrace|Ter|Parkway|Pkwy|Highway|Hwy|Trail|Trl|Loop|Square|Sq|Plaza|Alley|Route|Rte|"
       # A subdivision names its streets Bend, Row, Run and Crossing as readily
       # as Street and Avenue, and a home address that the suffix list has no
       # word for is a home address that ships. These are USPS-standard suffixes.
       r"Bend|Row|Run|Crossing|Xing|Landing|Lndg|Commons|Cove|Cv|Creek|Crk|Ridge|Rdg|"
       r"Hollow|Holw|Meadow|Meadows|Mdw|Glen|Gln|Grove|Grv|Knoll|Knl|Bluff|Blf|"
       r"Trace|Trce|Path|Walk|Way|Point|Pt|Pass|Park|Bay|Hill|Hills|Vista|Mews|"
       r"Canyon|Cyn|Valley|Vly|Summit|Spur|Bend|Chase|Reach|Bridge|Ferry|Ford)\b\.?"
       r"(?:\s*,?\s*(?:Apt\.?|Apartment|Unit|Suite|Ste\.?|Bldg\.?|Building|Floor|Fl\.?|Rm\.?|Room|#)\s*[\w\-]+)?"
       r"(?:\s*,?\s*[A-Z][\w'\-]*(?:\s+[A-Z][\w'\-]*){0,2}\s*,?\s*(?:A[LKZR]|C[AOT]|D[EC]|FL|GA|HI|I[DLNA]|"
       r"K[SY]|LA|M[EDAINSOT]|N[EVHJMYCD]|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[TA]|W[AVIY])\.?\s*\d{5}(?:-\d{4})?)?",
       priority=75, digit=True),
    _d("street_address",
       r"(?<![\w\-])\d{2,5}\s+(?:North|South|East|West|N|S|E|W)\.?\s+\d{2,5}\s+"
       r"(?:North|South|East|West|N|S|E|W)\b\.?"
       r"(?:\s*,?\s*(?:Apt\.?|Apartment|Unit|Suite|Ste\.?|Bldg\.?|#)\s*[\w\-]+)?"
       r"(?:\s*,?\s*[A-Z][\w'\-]*(?:\s+[A-Z][\w'\-]*){0,2}\s*,?\s*"
       r"(?:[A-Z]{2}|Utah|Idaho|Nevada|Arizona|Wyoming|Colorado)\.?\s*\d{5}(?:-\d{4})?)?",
       priority=76, digit=True),
    _d("street_address",
       r"\b(?:P\.?\s?O\.?\s+Box|Post\s+Office\s+Box)\s+\d{1,7}"
       r"(?:\s*,?\s*[A-Z][\w'\-]*(?:\s+[A-Z][\w'\-]*){0,2}\s*,?\s*[A-Z]{2}\.?\s*\d{5}(?:-\d{4})?)?",
       priority=82, digit=True, kw=("box",)),
    _d("parcel_number", labeled(("a\\.?p\\.?n\\.?", "parcel", "assessor'?s?\\s+parcel", "tax\\s+parcel",
                                 "property\\s+id", "sidwell", "pin\\s*(?:no\\.?|number|#)")), group=1, priority=88,
       digit=True, kw=("parcel", "apn", "a.p", "ap.", "sidwell", "pin", "property")),
    _d("deed_reference", r"\bBook\s+[\w\-]+,?\s+(?:at\s+)?Pages?\s+[\w\-]+", priority=86,
       kw=("book",)),
    _d("deed_reference", labeled(("entry\\s*(?:no\\.?|number|#)", "instrument\\s*(?:no\\.?|number|#)",
                                  "recording\\s*(?:no\\.?|number|#)", "document\\s*(?:no\\.?|number|#)",
                                  "reception\\s*(?:no\\.?|number|#)")), group=1, priority=86,
       digit=True, kw=("entry", "instrument", "recording", "document", "reception")),
    _d("legal_description",
       r"\bLot\s+[\w\-]+,?\s+Block\s+[\w\-]+(?:,?\s+(?:of\s+)?[A-Z][\w'\-]*(?:\s+[A-Z][\w'\-]*){0,5}"
       r"(?:\s+(?:Subdivision|Addition|Plat|Amended))?)?", priority=86,
       kw=("lot",)),
    _d("legal_description",
       r"\b(?:Section\s+\d{1,2},?\s+)?Township\s+\d{1,3}\s*[NSns]\.?,?\s+Range\s+\d{1,3}\s*[EWew]\.?", priority=86,
       digit=True, kw=("township",)),
    _d("vin", r"\b[A-HJ-NPR-Z0-9]{17}\b", validator=valid_vin, priority=88, flags=0,
       digit=True),
    _d("vin", labeled(("v\\.?i\\.?n\\.?", "vehicle\\s+identification")), group=1, priority=89,
       digit=True, kw=("vin", "v.i", "vi.", "vehicle")),
    _d("license_plate", labeled(("licen[sc]e\\s+plate", "plate\\s*(?:no\\.?|number|#)", "tag\\s*(?:no\\.?|number|#)",
                                 "registration\\s+plate"), r"[A-Z0-9][A-Z0-9\- ]{1,9}[A-Z0-9]"), group=1, priority=88,
       kw=("plate", "tag")),
    _d("vessel_number", labeled(("hull\\s*(?:id|identification)?", "hin", "tail\\s*(?:no\\.?|number|#)",
                                 "aircraft\\s*(?:registration)?", "vessel\\s*(?:no\\.?|number|#)",
                                 "boat\\s*(?:registration|no\\.?|number|#)")), group=1, priority=88,
       digit=True, kw=("hull", "hin", "tail", "aircraft", "vessel", "boat")),
    _d("safe_deposit", labeled(("safe\\s*deposit\\s*(?:box)?", "safety\\s*deposit\\s*(?:box)?", "vault\\s*box")), group=1, priority=88,
       digit=True, kw=("deposit", "vault")),
    _d("storage_unit", labeled(("storage\\s*unit", "storage\\s*locker", "unit\\s*(?:no\\.?|number|#)\\s*\\(storage\\)")), group=1, priority=86,
       digit=True, kw=("storage", "unit")),

    # --------------------------------------------------------------- case ----
    # "action" needs its left boundary or it matches inside Trans*action*, and
    # "Transaction 04/12/2026 posted" then registers the date as a case number.
    _d("case_number", labeled(("case", "civil\\s*(?:case)?", "criminal\\s*(?:case)?", "docket", "cause",
                               "court\\s+file", "matter", "index", "\\baction", "file",
                               "probate", "juvenile", "adversary", "appellate", "appeal"),
                              r"(?=[A-Za-z0-9\-:/.]*\d)[A-Za-z0-9][A-Za-z0-9\-:/.]{3,28}[A-Za-z0-9]"),
       group=1, priority=93,
       digit=True, kw=("case", "civil", "criminal", "docket", "cause", "court",
                       "matter", "index", "action", "file", "probate", "juvenile",
                       "adversary", "appe")),
    _d("case_number", r"(?<![\w\-])\d{2}[A-Z]{2,4}\d{3,6}(?![\w\-])", priority=70, flags=0,
       digit=True),
    _d("case_number", r"(?<![\w\-])\d{4}-[A-Z]{2,4}-\d{3,6}(?![\w\-])", priority=70, flags=0,
       digit=True),
    _d("case_name",
       # the "v" needs no period: "Smith v Jones" is a routine written form,
       # and the case-sensitive lowercase v cannot be a middle initial
       r"(?-i:\b[A-Z][A-Za-z'\-]+(?:[^\S\n]+[A-Z][A-Za-z'.\-]+){0,3}[^\S\n]+vs?\.?[^\S\n]+"
       r"[A-Z][A-Za-z'\-]+(?:[^\S\n]+[A-Z][A-Za-z'.\-]+){0,3})", priority=64,
       kw=(" v",)),
    _d("case_designator", labeled(("bar\\s+code", "tracking\\s*(?:no\\.?|number|#)", "efiling\\s*(?:id|no\\.?|number|#)",
                                   "e-?filed\\s+document", "envelope\\s*(?:no\\.?|number|#)",
                                   "submission\\s*(?:id|no\\.?|number|#)", "exhibit\\s*(?:no\\.?|number|#)\\s*\\(case\\)")),
       group=1, priority=80,
       digit=True, kw=("bar", "tracking", "filing", "filed", "envelope",
                       "submission", "exhibit")),

    # -------------------------------------------------------------- vital ----
    _d("dob", labeled(("d\\.?o\\.?b\\.?", "date\\s+of\\s+birth", "birth\\s*date", "born\\s+on", "date\\s+born",
                       "birthday"), V_DATE), group=1, priority=95,
       digit=True, kw=("dob", "d.o", "do.", "birth", "born")),
    _d("dob", rf"\bborn\s+(?:on\s+)?({V_DATE})", group=1, priority=94,
       digit=True, kw=("born",)),
    _d("pob", labeled(("place\\s+of\\s+birth", "birth\\s*place", "born\\s+(?:in|at)", "city\\s+of\\s+birth",
                       "state\\s+of\\s+birth", "country\\s+of\\s+birth"),
                      r"(?-i:[A-Z][\w'\-]*(?:\s+[A-Z][\w'\-]*){0,3}(?:,\s*[A-Z][\w'\-]*(?:\s+[A-Z][\w'\-]*){0,3}){0,2})"),
       group=1, priority=92,
       kw=("birth", "born")),
)


# --------------------------------------------------------------------------
# scanning
# --------------------------------------------------------------------------


# Words a labeled detector must never accept as the *value* - these show up when
# a label phrase is longer than the separator anticipated ("Venmo handle is @x").
_REJECT_VALUES = frozenset(
    """
    number no nos num id ident identification account acct accounts handle username
    user login name names address addresses tag is are was were the of for and or
    to at in on by with from ending code codes procedure evidence court courts rule
    rules section sections date dates type card plan group member subscriber policy
    claim file files matter case cases docket unit box record records information
    none n/a na unknown tbd various same above below herein thereof
    payment payments transfer transfers deposit deposits withdrawal withdrawals
    purchase purchases debit debits credit credits sent received pending posted
    """.split()
)


def _value_is_junk(value: str) -> bool:
    cleaned = value.strip().strip(".,;:#-").lower()
    if not cleaned:
        return True
    return cleaned in _REJECT_VALUES


# A sum of money, not an identifier. V_ALNUM permits ".", so a figure written
# without a thousands separator is a perfectly valid identifier shape, and in a
# financial document the label sitting in front of it is always a financial one:
#
#     Savings account 842.16 was moved…   -> bank_account   '842.16'
#     Escrow 412.55 collected monthly     -> loan_number    '412.55'
#     Roth IRA 640.00                     -> investment_account '640.00'
#
# A declaration whose figures have been altered is not a redacted document, it
# is a false statement filed under penalty - so this is the one place the tool
# refuses a match on the shape of the value alone. Nothing legitimate is lost:
# no real account, loan, policy or claim number is written as a decimal with
# exactly two places.
_CURRENCY_SHAPE = re.compile(r"^\$?\d{1,3}(?:,\d{3})*\.\d{2}$|^\$?\d+\.\d{2}$")

# Stated as an exception list rather than as a list of financial categories,
# because the financial ones are not the only labels that sit next to money.
# "Summit Ridge Visa    8,412.19    212.00" on a debt schedule matched the
# *alien_number* detector, whose label vocabulary contains "visa" for the
# immigration document, and took the monthly payment with it. Guessing which
# categories can end up beside a dollar figure is a game that keeps being lost,
# so the rule is the other way round: a currency shape is money unless the
# category is one where a decimal numeral is the native notation. An ICD-9
# diagnosis code really is written "250.00"; GPS coordinates really are
# decimals; nothing else here is.
_DECIMAL_NATIVE = frozenset({"diagnosis_code", "gps"})

# A cheque number is never a date, and neither is a case number. "Transaction
# 04/12/2026 posted" registered the date as a case number and blacked it out.
_DATE_ONLY = re.compile(rf"^{V_DATE}$|^\d{{1,2}}[/\-]\d{{4}}$")
_DATE_REJECTING = frozenset({"check_number", "case_number", "case_designator"})


def _wrong_shape(category: str, value: str) -> bool:
    """True when the value is money or a date and the category cannot be either."""
    cleaned = value.strip()
    if category not in _DECIMAL_NATIVE and _CURRENCY_SHAPE.match(cleaned):
        return True
    if category in _DATE_REJECTING and _DATE_ONLY.match(cleaned):
        return True
    return False


def _overlaps(start: int, end: int, spans: Sequence[tuple[int, int]]) -> bool:
    for s, e in spans:
        if start < e and s < end:
            return True
    return False


def scan(
    text: str,
    enabled: Sequence[str] | None = None,
    protected: Sequence[tuple[int, int]] | None = None,
) -> list[Match]:
    """Run every enabled detector over ``text`` and return resolved matches."""
    if not text:
        return []
    protected = list(protected or [])
    allowed = set(enabled) if enabled is not None else None
    found: list[Match] = []
    lower = text.lower()
    has_digit = any(ch.isdigit() for ch in text)

    for det in DETECTORS:
        if allowed is not None and det.category not in allowed:
            continue
        if PREFILTER:
            if det.needs_digit and not has_digit:
                continue
            if det.keywords and not any(k in lower for k in det.keywords):
                continue
        for m in det.regex.finditer(text):
            try:
                value = m.group(det.group)
            except IndexError:  # pragma: no cover - defensive
                continue
            if not value:
                continue
            start, end = m.span(det.group)
            value = value.rstrip(" .,;:")
            end = start + len(value)
            if not value.strip() or _value_is_junk(value):
                continue
            if _wrong_shape(det.category, value):
                continue
            if det.validator and not det.validator(value):
                continue
            if _overlaps(start, end, protected):
                continue
            found.append(Match(start, end, value, det.category, "pattern", det.priority))

    return resolve(found)



def resolve(matches: list[Match]) -> list[Match]:
    """Drop overlaps, keeping the highest priority then longest match."""
    ordered = sorted(matches, key=lambda m: (-m.priority, -m.length, m.start))
    kept: list[Match] = []
    taken: list[tuple[int, int]] = []
    for m in ordered:
        if _overlaps(m.start, m.end, taken):
            continue
        kept.append(m)
        taken.append((m.start, m.end))
        taken.sort()
    kept.sort(key=lambda m: m.start)
    return kept
