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
#
# The separator is a hyphen or ONE SPACE, never a newline or a tab, and the
# digits can be neither the cents of a dollar figure nor the start of one. A
# statement prints a masked account in the column beside the amount, and with
# "\s" here "xxxxxxxxxx" plus the next line's "150.00" registered as the
# account "xxxxxxxxxx 150", and "4,053.29" plus the mask after it as
# "29 xxxxxxxxxx" - after which the literal pass boxed those digits out of
# every figure that contained them.
#
# "(?!\d)" as well: without it the digits backtrack to dodge the money guard,
# and a pay stub's check-protected "**832.81" registered "**83".
#
# Between two runs the separator is mandatory. With it optional, a run of N
# stars could be cut into runs in 2^N ways, and the engine tried every one of
# them before giving up on a line that does not end in a mask's digits: a
# check-protected amount line, "PAY " and 36 asterisks before "832.81",
# took ten seconds, and each further asterisk nearly doubled it. A run is
# already as long as it likes, so nothing it used to match is lost.
_MASK_RUN = r"(?:[Xx*•#]{2,}|\.{2,4})"
V_MASKED = (rf"{_MASK_RUN}(?:[\- ]{_MASK_RUN})*[\- ]?\d{{2,6}}(?!\d)(?![.,]\d)"
            rf"|(?<![\d.,$])\d{{2,6}}[\- ]?{_MASK_RUN}(?:[\- ]{_MASK_RUN})*")
# Digits printed in groups, which is how utility, cable and wireless accounts
# are written: "8102 4477 9301". V_ALNUM forbids the space, so only the first
# group was captured and "Account Number: [ACCOUNT-14] 4477 9301" shipped
# two thirds of the number while looking redacted. Groups of three or more
# digits, so a date ("03 14 2026") cannot enter.
#
# One space or a hyphen between groups, never a line break: "Transfer From
# Checking 6323" over a "100.00" on the next line registered "6323\n100" as the
# account, and the literal pass then boxed the "100" out of the figure.
#
# "(?!\d)" for the same reason as in V_MASKED: without it the last group
# gives back digits until the money guard is satisfied, and "Checking 4417
# 1500.00" registered the account "4417 150" - and the literal pass then took
# "150" out of the balance.
V_GROUPED = r"\d{3,6}(?:[\- ]\d{3,6}){1,4}(?!\d)(?![.,]\d)"
V_ANY_ID = rf"(?:{V_MASKED}|{V_ALNUM})"
# What the financial detectors accept: the grouped form as well.
V_ACCOUNT_ID = rf"(?:{V_MASKED}|{V_GROUPED}|{V_ALNUM})"

# A card number as cards print it: unbroken, in groups of four, or Amex's 4-6-5.
# Named, not numbered, groups: this is embedded inside labeled(), whose value
# is group 1.
V_CARD = (r"(?:\d{13,19}|\d{4}(?P<cg>[ \-])\d{4}(?P=cg)\d{4}(?P=cg)\d{1,7}"
          r"|\d{4}(?P<ax>[ \-])\d{6}(?P=ax)\d{4,5})")

# separator between a label and its value: "No.", "Number", "#", ":", "-"
# The bracketed aside is not decoration: IRS forms and bank statements are full
# of them - "Share Certificate (CD) number 44-8821-0033",
# "Account number (see instructions) HCS-0044-2211" - and without it the same
# line without its parenthesis matched while the real one did not.
#
# Each run of whitespace belongs to exactly one "\s*": the word, slash or
# aside eats the gap after it. With a "\s*" in front of every repetition and
# two more around the closing punctuation, a long gap after a label - a
# padded column in a fixed-width export - was split every possible way at
# every label, and the time grew with the square of the gap.
_SEP = (
    r"\s*(?:(?:(?:num(?:ber)?|no\.?|nos\.?|#|id(?:entif(?:ication|ying))?|acct\.?|"
    r"account|ending(?:\s+in)?|handle|profile|user\s?name|username|login|tag|"
    r"cash\s?tag|is|are|was|were|of|for|at|with|"
    # A transaction line puts a verb between the rail and the reference:
    # "VENMO PAYMENT 3948217364" shipped its transaction id because nothing
    # could step over the word PAYMENT.
    # Deliberately NOT "to" or "from". With those here, payment_handle read
    # "ZELLE TO SHAWNA KIRKENDALL" as the handle "SHAWNA" - which redacted the
    # first name, shipped the surname, and left a line no one could read.
    r"payment|pmt|transfer|transaction|ref|reference|posted|"
    # a label qualifies itself before it gets to the value: "Student ID at
    # school of record:", "Loan number of record:"
    r"school|record|primary|current|assigned|"
    # How a card's tail is introduced when "ending in" is not used: Amazon's
    # invoice prints "Visa | Last digits: 4053", others "last 4 digits" and
    # "last four". Never a bare "last", which is prose: "the last 2024 filing".
    r"last\s*(?:4|four)?\s*digits?|last\s*(?:4|four)|"
    # a pipe between a label and its value is a table's cell border
    r"\|)"
    # a column heading writes its alternatives with a slash - "Account /
    # identifying number" is the heading on the Utah asset schedule, and
    # without the slash the whole column read as unlabelled
    r"|/|\([^)\n]{1,30}\)|\))\s*){0,6}(?:[:\-–—=#]\s*)?"
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


# The fifty states and DC by postal code, for the address rules.
_US_STATE = (r"A[LKZR]|C[AOT]|D[EC]|FL|GA|HI|I[DLNA]|K[SY]|LA|M[EDAINSOT]|N[EVHJMYCD]|"
             r"O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[TA]|W[AVIY]")


def labeled(labels: Sequence[str], value: str = V_ANY_ID) -> str:
    """Build a ``label + separator + (value)`` regex with the value in group 1.

    A label never starts in the middle of a word: "DL" inside the Amazon order
    code "GPBSDLHG5" read as a driver's licence label, and "HG5" was boxed.
    Nor after a digit: "RX" closing the UPS number "1Z59T2L3YUS96VY4RX" read
    as a prescription label and took the order date in the next cell.

    The one exception is a camel-cased join, a lower-case letter followed by
    a capital: an export's field names glue their words - "PatientDOB:",
    "ChkAcct", "CustomerAcct#" - and each of those shipped its value. The
    test is case-sensitive whatever the detector's flags, so an all-capitals
    code like "GPBSDLHG5" still has no label inside it.
    """
    alt = "|".join(labels)
    return rf"(?:(?<![A-Za-z0-9])|(?-i:(?<=[a-z])(?=[A-Z])))(?:{alt}){_SEP}({value})"


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


# An area of 9XX is never an SSN, but it is an ITIN when the middle group is
# one the IRS issues: 50-65, 70-88, 90-92 or 94-99. An ITIN sits in the SSN box
# of a W-2 and on the first line of a 1040 for a filer without a Social
# Security number, and it identifies that filer just as surely. Rejecting
# every 9XX shipped "922-74-1631" from the employee box of a photographed W-2.
_ITIN_GROUP = re.compile(r"5\d|6[0-5]|7\d|8[0-8]|9[0-24-9]")


def valid_ssn(value: str) -> bool:
    digits = "".join(c for c in value if c.isdigit())
    if len(digits) != 9:
        return False
    area, group, serial = digits[:3], digits[3:5], digits[5:]
    if area.startswith("9"):
        return bool(_ITIN_GROUP.fullmatch(group)) and serial != "0000"
    if area in {"000", "666"}:
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
    # Each run of whitespace is consumed by exactly one "\s*" in these two.
    # Three optional runs side by side split a long gap every possible way
    # before failing - cubic in its length - and "facebook" followed by a
    # page's worth of padding took seconds.
    _d("gps", r"[-+]?\d{1,3}\.\d{4,}(?:\s*,\s*|\s+)[-+]?\d{1,3}\.\d{4,}", priority=85,
       digit=True),
    _d("social_handle",
       rf"\b(?:facebook|fb|instagram|insta|ig|twitter|x\.com|tiktok|snapchat|snap|linkedin|"
       rf"reddit|discord|telegram|whatsapp|youtube|onlyfans|tinder|bumble|hinge)\b\s*"
       rf"(?:(?:profile|account|handle|user\s?name|username|id)\s*)?(?:(?:is|:|=|@|-)\s*)?"
       rf"(@?[A-Za-z0-9._\-]{{3,30}})",
       group=1, priority=84,
       kw=("facebook", "fb", "insta", "ig", "twitter", "x.com", "tiktok", "snap",
           "linkedin", "reddit", "discord", "telegram", "whatsapp", "youtube",
           "onlyfans", "tinder", "bumble", "hinge")),
    _d("social_handle", r"(?<![\w./@])@[A-Za-z][A-Za-z0-9_.\-]{2,29}(?<![.\-])", priority=60,
       kw=("@",)),
    # A cashtag needs no label: a dollar sign followed by a letter is never
    # money. A Cash App statement prints "$JennaHar727" in its header and says
    # "Cash App" nowhere near it. An all-capitals run of five or fewer is left
    # alone - "$USD", "$AAPL" are a currency and a ticker, not a person.
    # And it must look like a handle, not a word: a digit, an underscore or a
    # capital after the first letter. A register that writes "$million" or a
    # heading "$Total" is talking about money; "$JennaHar727" is a person. A
    # handle in plain lower case is still caught where the app is named.
    _d("payment_handle", r"(?<![\w$])\$(?![A-Z]{1,5}\b)(?=[A-Za-z][a-z]*[A-Z0-9_])"
                         r"[A-Za-z][A-Za-z0-9_]{2,19}\b",
       priority=61, flags=0, kw=("$",)),
    _d("username", labeled(("user\\s?name", "user\\s?id", "login(?:\\s?name|\\s?id)?", "screen\\s?name", "display\\s?name"),
                           r"[A-Za-z0-9._@\-]{3,40}"), group=1, priority=70,
       kw=("user", "login", "screen", "display")),

    # -------------------------------------------------------- government ID --
    _d("ssn", r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)", validator=valid_ssn, priority=95,
       digit=True),
    # an ITIN is labelled as one on a 1040 and on an IRS notice
    _d("ssn", labeled(("s\\.?s\\.?n\\.?", "social\\s+security", "soc\\.?\\s*sec\\.?",
                       "i\\.?t\\.?i\\.?n\\.?", "individual\\s+taxpayer\\s+identification"),
                      r"(?:\d{3}[-\s]?\d{2}[-\s]?\d{4}|" + V_MASKED + r")"), group=1, priority=96,
       digit=True, kw=("soc", "ssn", "s.s", "ss.", "itin", "i.t", "individual")),
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
    # Grouped the way a card is printed - unbroken, in fours, or Amex's 4-6-5 -
    # and not any run of digits with a space somewhere in it. An ACH trace on a
    # statement prints a YYMMDD date and a ten-digit company ID side by side,
    # "200110 7148234928", and one in ten of those passes the Luhn check.
    _d("credit_card", rf"(?<![\d\-]){V_CARD}(?![\d\-])", validator=luhn_valid, priority=93,
       digit=True),
    # Bare "card" is admitted as a label, but only in front of a masked form or
    # a four-digit tail - "card ending in 4417" is how every statement prints
    # it, and it was the worst-caught identifier in the audit. Restricting the
    # value is what keeps "card 1 of 2" and "card holder" out.
    # "card" needs its left boundary: "CHECKCARD 1212 SWIG 363" is a debit
    # purchase posted on 12/12, and without it every posting date on the
    # statement registered as a card's last four.
    _d("credit_card", labeled(("credit\\s*card", "debit\\s*card", "\\bvisa", "mastercard", "master\\s*card",
                               "\\bamex", "american\\s+express", "\\bdiscover", "\\bcard\\s*(?:no\\.?|number|#)",
                               "\\bcard"),
                              # A bare 4-digit tail covers "card ending in 9876".
                              # It needs both digit boundaries: without them the
                              # bare "card" label above sliced "1234" out of
                              # "State ID card 12345678".
                              # "Account Ending 2-5666", "Card Ending 1-23456" -
                              # a charge card prints its tail after a prefix digit
                              # The tail is never the start of a date or of a
                              # longer number - "Memory Card" wrapped above an
                              # order date "2023-10-25" - nor a house number,
                              # which scan() settles against the street rule.
                              # The prefixed tail is never a two-digit month
                              # and a year: "Card 12-2024" is an expiry.
                              rf"(?:{V_MASKED}|{V_CARD}|(?<!\d)\d{{4}}(?!\d|[-/.]\d)"
                              rf"|(?<![\d\-])(?!(?:0[1-9]|1[0-2])-(?:19|20)\d\d(?![\d\-]))"
                              rf"\d{{1,2}}-\d{{4,5}}(?![\d\-]))"),
       group=1, priority=92,
       digit=True, kw=("card", "visa", "amex", "american", "discover")),
    # A bank's card purchase ends "S427993066734279 Card 1848" - the network's
    # transaction reference, then the card - and OCR of a photographed
    # statement closes both gaps: "S420126815924201Card1848". The label above
    # cannot start after a digit, so the tail shipped. Capitalised "Card" (or
    # "CARD") directly after a digit and directly before exactly four is that
    # glued line and nothing else: "4CARD" in a product code is not followed
    # by a bare four-digit run.
    _d("credit_card", r"(?<=\d)(?-i:Card|CARD)[^\S\n]?(\d{4})(?!\d)(?![\-/.,]\d)",
       group=1, priority=92, digit=True, kw=("card",)),
    # The same label with a letter lost, glued to the tail: the photographed
    # statement that read "Card9916" on one line read "Cad9916" on the next,
    # and the literal pass will not find a known tail behind a letter. Only
    # glued and only these spellings - "CAD 1234" is Canadian dollars.
    _d("credit_card", r"(?<![A-Za-z])(?-i:Cad|Crd|CRD)(\d{4})(?!\d)(?![\-/.,]\d)",
       group=1, priority=92, digit=True, kw=("cad", "crd")),
    _d("routing_number", labeled(("routing", "aba", "rtn", "transit")), group=1, priority=91,
       digit=True, kw=("routing", "aba", "rtn", "transit")),
    # The same label as OCR returns it from a photograph: "Routing Number
    # (RTNj:229240574" - the closing parenthesis read as a "j" - or
    # "ROUTINGMUMIA (RTM) 229240574", the label glued to garbage. Neither
    # fits the separator above, and the bare rule below wants the checksum,
    # which one misread digit breaks. A word that starts with "routing" and,
    # within a short stretch of the same line with no digit in it, exactly
    # nine digits: nothing else follows that word. Labelled, so no checksum.
    _d("routing_number", r"\brouting[^\d\n]{0,30}?(?<!\d)(\d{9})(?![\d\-])(?![.,]\d)",
       group=1, priority=91, digit=True, kw=("routing",)),
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
    # A brokerage statement heads the account with its registration and never
    # says "account number": "INDIVIDUAL - TOD 059-600037", "JOINT WROS
    # 215-995662", "ROTH IRA 734-907489". At least six digits, so a registration
    # followed by a year or a count is left alone.
    _d("investment_account",
       r"(?i:\b(?:individual|joint(?:[^\S\n]+(?:wros|tenants))?|jtwros|tod|trust|custodial|utma|ugma|"
       r"(?:roth|traditional|rollover|sep|simple|inherited)[^\S\n]+ira))"
       r"(?:[^\S\n]*[-–][^\S\n]*(?i:tod|wros|jtwros))?[^\S\n]*[-–:#]?[^\S\n]*"
       r"((?=(?:\D?\d){6})\d{2,4}[-\s]?\d{3,8}(?:[-\s]\d{2,6})?)(?![\d.,])",
       group=1, priority=86, flags=0, digit=True,
       kw=("individual", "joint", "tod", "trust", "custodial", "utma", "ugma", "ira", "wros")),
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
    # An online order number names the account that placed it: the retailer
    # looks it up to the buyer's name, address and card. Amazon prints its
    # orders as 3-7-7 digits ("112-4749744-4480307", "D01-" for digital
    # orders) with or without a label; any other retailer's is taken when it
    # is labelled and carries a digit.
    _d("order_number", r"(?<![\w\-])(?:\d{3}|D\d{2})-\d{7}-\d{7}(?![\w\-])", priority=84,
       digit=True),
    _d("order_number", labeled(("order\\b", "purchase\\s+order\\b", "p\\.?o\\.?\\s*(?:#|no\\.?|number)"),
                              r"(?=[A-Z0-9\-]*\d)[A-Z0-9][A-Z0-9\-]{5,28}[A-Z0-9](?![\w\-])"),
       group=1, priority=83, digit=True, kw=("order", "p.o", "po")),
    # A tracking number leads to the delivery address. The carriers' own
    # shapes are taken anywhere - UPS "1Z" and sixteen, Amazon Logistics "TBA"
    # and twelve digits, a USPS 22-digit barcode - and any other when a
    # tracking label names it.
    _d("tracking_number", r"(?<![\w])(?:1Z[0-9A-Z]{16}|TBA\d{12}|9[2-5]\d{20})(?![\w])",
       priority=84, digit=True),
    _d("tracking_number", labeled(("tracking", "shipment\\s*(?:id|no\\.?|number|#)",
                                   "waybill", "air\\s*bill"),
                                  r"(?=[A-Z0-9]*\d)[A-Z0-9]{8,34}(?![\w\-])"),
       group=1, priority=83, digit=True, kw=("tracking", "shipment", "waybill", "air")),
    # A masked tail with no label at all - "****3907", "XXXX-XXXX-XXXX-4417",
    # "...6613". Nothing caught these, and two of them shipped whole because the
    # value-literal boundary in engine.py refuses to match after a "-" or a "."
    # (right for emails, wrong for a masked account). Priority 72 sits below
    # every labelled financial detector, so a labelled hit still wins.
    _d("masked_account", rf"(?<![\w\-]){V_MASKED}(?![\w\-])", priority=72, digit=True),
    # OCR reads a run of asterisks as almost any other mark - a quotation mark,
    # an apostrophe, "+*", "«*" - "Added cash from Bank "8697", "Visa +*8779".
    # Only after a word that introduces an account or a card, so a quoted year
    # in prose is left alone.
    _d("masked_account",
       r"(?i:\b(?:bank|acct|account|card|ending(?:[^\S\n]+in)?|visa|mastercard|amex|discover))"
       r"[^\S\n]*[:#]?[^\S\n]*"
       r"[\"“”'‘’*•·+«»~^°]{1,4}[^\S\n]?(\d{4})(?![\d.,])",
       group=1, priority=71, digit=True,
       kw=("bank", "acct", "account", "card", "ending", "visa", "mastercard", "amex", "discover")),
    # The same misread with no label to lean on. A photographed receipt
    # prints "DEBIT ****2628" and OCR returns 'DEBIT ***"2628', 'EBIT **""7710'
    # or 'EBIT *7710' - a stray quote inside the run, or the run shrunk to one
    # star, and the label clipped or one the list above has no word for. A
    # star or a bullet directly against exactly four digits is a mask in any
    # document: nothing else writes a figure that way. At least one real star
    # or bullet, so a quoted "2024" or a store's "#1236" is left alone.
    #
    # Not money: never after "$" or a digit, never four digits running on
    # into a longer number or a decimal ("*1234.56" is a check-protected
    # amount). And not a year behind a lone star - "*2024 figures are
    # estimates" is a footnote, and a registered "2024" would black out the
    # year in every date of the batch. Two real marks make it a mask again.
    _d("masked_account",
       r"(?<![\w\-$*•\"“”'‘’+«»~^°#])"
       r"(?=[\"“”'‘’+«»~^°#]*[*•])"
       r"(?!(?:[*•][\"“”'‘’+«»~^°#]*|[\"“”'‘’+«»~^°#]+[*•][\"“”'‘’+«»~^°#]*)"
       r"(?:19[5-9]\d|20[0-4]\d)(?![\d.,]))"
       r"[*•\"“”'‘’+«»~^°#]{1,6}(\d{4})(?![\d\-])(?![.,]\d)",
       group=1, priority=70, digit=True, kw=("*", "•")),
    # A transfer line that names the bank and not the account: an exchange or
    # a payment app prints "Withdrawal to BANK ***3233", and on a scan the
    # mask is often lost altogether - "Withdrawal to BANK 3233". "Bank" alone
    # is not a label (bank_account wants "bank account"), but after "to" or
    # "from" it is the far end of a transfer, and four digits there are that
    # account's tail. A year is left alone ("Transfer from bank 2024 bonus"),
    # and so is the start of a date or a decimal figure.
    _d("bank_account",
       r"\b(?:to|from)[^\S\n]+(?:(?:my|your|the|linked|external|[A-Z][\w&'\-]*)[^\S\n]+)?bank"
       r"[^\S\n]*[:#]?[^\S\n]*(?!(?:19[5-9]\d|20[0-4]\d)(?!\d))(\d{4})(?!\d)(?![\-/.,]\d)",
       group=1, priority=70, digit=True, kw=("bank",)),
    # The OCR scanline across the foot of a remittance coupon. It encodes the
    # account number and the amount due, and nothing else in a document is
    # twenty unbroken digits, so it needs no label.
    _d("bank_account", r"(?<![\d\-])\d{20,60}(?![\d\-])", priority=74, digit=True),

    # ----------------------------------------------------------- property ----
    # A street address wraps - "771 N" and "1200 E" land on separate lines in a
    # payment coupon all the time - so this has to cross the newline. What it
    # must not do is start on the tail of a dollar figure: it matched
    # "66\nSummit Ridge" and deleted "2,184.66" from the row above. Blocking a
    # digit, comma or period before the street number is what separates the two,
    # and it is exact: "2,184.66" cannot open an address, "771 N 1200 E" can.
    #
    # Nor may it start on the year of a date, or reach across a line break to
    # find its street. "Due 02/26/2025" on the line above a remittance address
    # opened a match at "2025", took the next line's "4417 W SAGEBRUSH LN" as
    # the street, and boxed the due date. A house number and its street are on
    # one line; only the city, state and ZIP after them may wrap.
    _d("street_address",
       # (nor on a store number: "Summit Home #0845 St George" is a shop in
       # St. George, and "0845 St" took the merchant's number and the city)
       # Its words are at most two spaces apart: the PDF reader renders a
       # column gap as three or more, and "12:47:13   Merchant Transacti
       # Complete   Sq" otherwise read as a street ending in Sq(uare).
       # A house number never opens with a zero, and a street always has a
       # name between its number and its type: "CHECKCARD 0328 SQ *BLUE MOON"
       # is a posting date and a card processor, not 0328 Square.
       # A colon blocks the number only after a digit, where it is a time's;
       # after a word it is a label's, and "Address:1734 W Gentile St" is
       # the address. A "#" still blocks it: "#4417" is a store's number, a
       # unit or a card, never a house.
       r"(?<![\d.,/\-#])(?<!\d:)\b(?!0)\d{1,6}[A-Z]?[^\S\n]{1,2}(?:(?:North|South|East|West|N\.?|S\.?|E\.?|W\.?|NE|NW|SE|SW)[^\S\n]{1,2})?"
       r"(?:[A-Z0-9][\w'\-]*\.?[^\S\n]{1,2}){1,4}"
       r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|Circle|Cir|Way|Wy|"
       r"Place|Pl|Terrace|Ter|Parkway|Pkwy|Highway|Hwy|Trail|Trl|Loop|Square|Sq|Plaza|Alley|Route|Rte|"
       # A subdivision names its streets Bend, Row, Run and Crossing as readily
       # as Street and Avenue, and a home address that the suffix list has no
       # word for is a home address that ships. Kept to suffixes that are not
       # also ordinary words in a company name: an earlier, longer list
       # included Ridge, Summit, Valley and Point, and "Summit Ridge Bank"
       # then read as a street.
       r"Bend|Row|Run|Crossing|Xing|Landing|Lndg|Mews|Trace|Trce|Esplanade)\b\.?"
       r"(?:\s*,?\s*(?:Apt\.?|Apartment|Unit|Suite|Ste\.?|Bldg\.?|Building|Floor|Fl\.?|Rm\.?|Room|#)\s*[\w\-]+)?"
       r"(?:\s*,?\s*[A-Z][\w'\-]*(?:\s+[A-Z][\w'\-]*){0,2}\s*,?\s*(?:A[LKZR]|C[AOT]|D[EC]|FL|GA|HI|I[DLNA]|"
       r"K[SY]|LA|M[EDAINSOT]|N[EVHJMYCD]|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[TA]|W[AVIY])\.?\s*\d{5}(?:-\d{4})?)?",
       priority=75, digit=True),
    _d("street_address",
       # same one-line rule as above; only the trailing city/state/ZIP is
       # allowed to wrap, because a real address block wraps exactly there
       r"(?<![\w\-.,])\d{2,5}\s+(?:North|South|East|West|N|S|E|W)\.?\s+\d{2,5}\s+"
       r"(?:North|South|East|West|N|S|E|W)\b\.?"
       r"(?:\s*,?\s*(?:Apt\.?|Apartment|Unit|Suite|Ste\.?|Bldg\.?|#)\s*[\w\-]+)?"
       r"(?:\s*,?\s*[A-Z][\w'\-]*(?:\s+[A-Z][\w'\-]*){0,2}\s*,?\s*"
       r"(?:[A-Z]{2}|Utah|Idaho|Nevada|Arizona|Wyoming|Colorado)\.?\s*\d{5}(?:-\d{4})?)?",
       priority=76, digit=True),
    # A street line that the rules above cannot read because OCR mangled it:
    # a photographed pay stub's "4417 W Sagebrush Ln, South Jordan, UT 84009"
    # came back as "4417 W Sagebrush in, ..." from one engine and as
    # "4417WSagebrushn,Southordan,UT84009" from the other - the suffix misread
    # (in for Ln, 5t for St, Or for Dr) and the spaces gone. The suffix is
    # what the rules above key on, so neither reading was an address. What
    # survives both is the shape of the tail: a comma, a capitalised town, a
    # comma, a state's two capitals and a five-digit ZIP. With that tail, a
    # house number and a capitalised street name in front of it on the same
    # line are an address whatever the suffix came back as.
    #
    # Case-sensitive, so the state is two capitals and not "in" or "or", and
    # the number cannot be the tail of a figure, a date or a store's "#".
    _d("street_address",
       r"(?<![\w.,/\-#$:])(?!0)\d{1,6}[A-Z]?[^\S\n]{0,2}(?:[NSEW]\.?[^\S\n]{0,2})?"
       r"[A-Z][A-Za-z'\-]+(?:[^\S\n]{1,2}[A-Za-z0-9][\w'\-]*\.?){0,4}"
       r"[^\S\n]*,[^\S\n]*[A-Z][A-Za-z'\-]*(?:[^\S\n]{1,2}[A-Z][A-Za-z'\-]*){0,2}[^\S\n]*,?[^\S\n]*"
       rf"(?:{_US_STATE})\.?[^\S\n]*\d{{5}}(?:-\d{{4}})?(?!\d)",
       priority=74, flags=0, digit=True),
    # A ZIP+4 names a delivery point - a block face or a single building - and
    # it turns up away from its address block: in a running header, in an OCR
    # line whose state came back as "it", in a Word table cell of its own.
    _d("street_address", r"(?<![\d\-])\d{5}-\d{4}(?![\d\-])", priority=58, digit=True),
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
    # A VIN under its own label is a VIN whatever its check digit says; a
    # hand-keyed or generated one fails the checksum and still identifies the car.
    _d("vin", r"(?i:\bv\.?i\.?n\.?)(?:[^\S\n]*(?:/[^\S\n]*HIN|#|:|no\.?|number))*[^\S\n]*"
              r"([A-HJ-NPR-Z0-9]{17})\b", group=1, priority=89, flags=0, kw=("vin", "v.i")),
    _d("license_plate", labeled(("licen[sc]e\\s+plate", "plate\\s*(?:no\\.?|number|#)", "tag\\s*(?:no\\.?|number|#)",
                                 "registration\\s+plate"), r"[A-Z0-9][A-Z0-9\- ]{1,9}[A-Z0-9]"), group=1, priority=88,
       kw=("plate", "tag")),
    # "PLATE Y777MX" - a registration renewal heads the field with the bare
    # word. Case-sensitive and it must mix letters and digits, so "plate glass"
    # and "Plate Type" stay prose.
    # The letter and the digit are both inside the plate itself, not in the
    # words after it, and a plate split in two has three or more characters
    # after the break: "Plate 1234 ST GEORGE" and "Plate 12 OZ" are a number
    # and the word that follows it, and took "ST" and "OZ" with them.
    _d("license_plate", r"(?i:\bplate\b)[^\S\n]*[:#]?[^\S\n]*"
                        r"((?:[A-Z0-9]{1,4}[\- ][A-Z0-9]{3,5}|[A-Z0-9]{3,9})(?![\w\-]))",
       group=1, priority=87, flags=0, digit=True, kw=("plate",),
       validator=lambda v: any(c.isdigit() for c in v) and any(c.isalpha() for c in v)),
    _d("vehicle_title", labeled(("(?:vehicle\\s+|certificate\\s+of\\s+)?title\\s*(?:no\\.?|number|#)",
                                 "certificate\\s+of\\s+title"),
                                r"[A-Z0-9][A-Z0-9\-]{4,17}"),
       group=1, priority=88, digit=True, kw=("title",)),
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
    _d("dob", labeled(("d\\.?o\\.?b\\.?", "date\\s+of\\s+birth", "birth\\s*date", "born\\s+on", "date\\s+born"),
                      V_DATE), group=1, priority=95,
       digit=True, kw=("dob", "d.o", "do.", "birth", "born")),
    # "Birthday" is a label only when the date follows it on its own line. It
    # is also a greeting and a product - "Amazon Gift Card - Print - Happy
    # Birthday" wraps in an order export, and the order date in the next cell
    # read as a date of birth, and then every copy of that date was redacted.
    _d("dob", rf"(?<![A-Za-z])(?<!happy\s)birthday(?:[^\S\n]*[:\-–][^\S\n]*|[^\S\n]+)({V_DATE})",
       group=1, priority=95, digit=True, kw=("birthday",)),
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
    des indn ppd ccd web tel cashout
    """.split()
    # the field names of an ACH descriptor - "VENMO DES:CASHOUT ID:... INDN:..."
    # read "DES" as the Venmo handle
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
#
# Signed every way a statement signs it, too: "636.09-" is how one layout in
# the corpus prints every debit, and without the trailing minus in this shape
# each one registered as an account number and was boxed out of the column -
# 345 figures across a single household's checking statements.
# OCR reads a comma as a period about as often as not: a scanned loan
# ledger's balance column came back "13.861.19", and the payment-handle rule
# beside its "Zelle" column took eight balances as handles and boxed them.
# A thousands group with either mark is still money.
_CURRENCY_SHAPE = re.compile(
    r"^[-+(]?\$?[-+]?(?:(?:\d{1,3}(?:,\d{3})*|\d+)\.\d{2}|\d{1,3}(?:[,.]\d{3})+[.,]\d{2})"
    r"(?:-|\)|\s?(?:CR|DR))?$",
    re.IGNORECASE)

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
_DATE_REJECTING = frozenset({"check_number", "case_number", "case_designator", "order_number",
                             "tracking_number", "prescription"})
# A bare year is the commonest number after a financial label - "Roth IRA 2024
# contribution", "401(k) 2023 limit", "Loan 2019 refinance" - and no account,
# loan or policy number is four digits that happen to be a recent year. Once
# registered, the literal pass would black out that year in every date.
_YEAR_ONLY = re.compile(r"^(?:19[5-9]\d|20[0-4]\d)$")
# Not credit_card: "card ending in 2019" is a real last four, and a blacked-out
# year costs less than a leaked card tail.
_YEAR_REJECTING = frozenset({"investment_account", "bank_account", "loan_number",
                             "policy_number", "claim_number", "check_number",
                             "employee_id", "order_number", "tracking_number"})
# ...unless the label says outright that an identifier follows. "Checking
# account ending in 2024", "Acct No. 2015", "Loan ending in 2019" are the
# last four of a real account, and the year rule shipped every one of them.
# The cue has to close the label, so "Account number for 2024" - a label
# that wandered on into prose - is still a year.
_IDENTIFIER_CUE = re.compile(
    r"(?:\bending(?:\s+in)?|\blast\s*(?:four|4)?\s*digits?|\blast\s*(?:four|4)|\bnos?\.?|\bnum(?:ber)?|#)"
    r"\s*[:\-–—=#]?\s*$", re.IGNORECASE)


def _handle_from_another_row(value: str, label: str) -> bool:
    """A word on a later line than the rail's name, with nothing marking it a handle.

    A ledger's Method column prints "Zelle" on every row, and the separator
    after a label crosses line breaks so a form's "Venmo:" can sit over its
    value. Read across rows, "Zelle" took the next row's first word - "Page",
    "See", an OCR fragment - as the payer's handle. A handle printed on its own
    line under its label carries its "@" or "$".
    """
    return "\n" in label and not value.startswith(("@", "$"))


def _wrong_shape(category: str, value: str, label: str = "") -> bool:
    """True when the value is money or a date and the category cannot be either.

    ``label`` is the text the detector matched in front of the value.
    """
    cleaned = value.strip()
    if category not in _DECIMAL_NATIVE and _CURRENCY_SHAPE.match(cleaned):
        return True
    if (category in _YEAR_REJECTING and _YEAR_ONLY.match(cleaned)
            and not _IDENTIFIER_CUE.search(label)):
        return True
    if category in _DATE_REJECTING and _DATE_ONLY.match(cleaned):
        return True
    return False


# A decimal fraction, or a thousands group: a comma, exactly three digits and
# then no more. "44821903,44821904" is two account numbers glued by a comma,
# and reading that comma as a thousands separator dropped both of them.
_NUMBER_GOES_ON = re.compile(r"\.\d|,\d{3}(?!\d)")
_NUMBER_CAME_BEFORE = re.compile(r"\d\.$")
_AFTER_A_COMMA = re.compile(r"\d,$")
_THOUSANDS_GROUP = re.compile(r"\d{3}(?!\d)")
# A hyphen or a letter inside the value makes it an identifier: no figure is
# written "123-45-6789" or "HCS0044", so "SSN 123-45-6789,234-56-7890" is two
# numbers in a list, not one figure cut in half.
_NOT_A_FIGURE = re.compile(r"[\-A-Za-z]")


def _cut_from_a_number(text: str, start: int, end: int) -> bool:
    """True when a match is the front or the back of a longer number.

    Every value shape stops at a comma, so "PRIMARY SAVINGS 111,274.97" read as
    the savings account "111" followed by the rest of the balance, and the box
    took the first three digits off the figure. No identifier is written with
    a thousands separator or a decimal fraction running on from it; a figure
    always is.
    """
    value = text[start:end]
    if _NOT_A_FIGURE.search(value):
        return False
    if value[-1:].isdigit() and _NUMBER_GOES_ON.match(text, end):
        return True
    if value[:1].isdigit():
        before = text[max(0, start - 2):start]
        if _NUMBER_CAME_BEFORE.search(before):
            return True
        if _AFTER_A_COMMA.search(before) and _THOUSANDS_GROUP.match(value):
            return True
    return False


def _overlaps(start: int, end: int, spans: Sequence[tuple[int, int]]) -> bool:
    for s, e in spans:
        if start < e and s < end:
            return True
    return False


def _unwrap_a_cell(text: str, start: int, value: str) -> str:
    """A street that does not open its line keeps to its line.

    A mailing address wraps its city under its street, and the street then
    starts the line. One that starts mid-line is a table cell read across a
    row, and the next line opens with the first column's cell: an order
    export's billing street took the shipping city under it, "2750 ASPEN
    SPRINGS DR / TWIN FALLS ID 83301", and the client's home was registered
    in a town the gift went to.

    Two things say the street is an address block after all, and then its
    town stays with it. A short label in front - "Address: 1734 W Gentile St"
    over "Layton, UT 84041" - and the city line standing alone, with nothing
    after the ZIP: the order export's next line held two towns side by side,
    one per address column, and that is what gave the table away.
    """
    line_start = text.rfind("\n", 0, start) + 1
    before = text[line_start:start].strip()
    if not before or _LABEL_BEFORE_STREET.fullmatch(before):
        return value
    tail_end = start + len(value)
    line_end = text.find("\n", tail_end)
    if not text[tail_end:len(text) if line_end < 0 else line_end].strip():
        return value
    return value.split("\n", 1)[0]


# "Address:", "Home address:", "Mailing Addr.:" - up to three words and a colon.
_LABEL_BEFORE_STREET = re.compile(r"(?:[A-Za-z][\w.'/\-]*\s+){0,2}[A-Za-z][\w.'/\-]*\s*:")


# A card's bare last four, as the labelled card detector takes it.
_CARD_TAIL = re.compile(r"\d{4}")


def _street_starts(text: str) -> set[int]:
    """Where the street-address rule would open a street in ``text``.

    A bare four-digit card tail is dropped only when the street rule itself
    begins at that very number: "Memory Card   1734 W GENTILE ST" in an order
    export's row is a title's end and an address. A lookahead copy of the
    street rule beside the card label drifted from the real one, and it
    refused tails the street rule never took - "Card ending in 4417 N Ogden",
    "VISA 4417 E ZPASS" - so they shipped as neither. Run regardless of the
    enabled categories: whether a number is a house number does not depend on
    which categories the operator ticked.
    """
    starts: set[int] = set()
    for det in DETECTORS:
        if det.category == "street_address":
            starts.update(m.start(det.group) for m in det.regex.finditer(text))
    return starts


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
            if det.category == "street_address" and "\n" in value:
                value = _unwrap_a_cell(text, start, value)
            value = value.rstrip(" .,;:")
            end = start + len(value)
            if not value.strip() or _value_is_junk(value):
                continue
            if _wrong_shape(det.category, value, text[m.start():start]):
                continue
            if det.category == "payment_handle" and _handle_from_another_row(
                    value, text[m.start():start]):
                continue
            if det.category not in _DECIMAL_NATIVE and _cut_from_a_number(text, start, end):
                continue
            if det.validator and not det.validator(value):
                continue
            if _overlaps(start, end, protected):
                continue
            found.append(Match(start, end, value, det.category, "pattern", det.priority))

    if any(m.category == "credit_card" and _CARD_TAIL.fullmatch(m.text) for m in found):
        streets = _street_starts(text)
        found = [m for m in found
                 if not (m.category == "credit_card" and _CARD_TAIL.fullmatch(m.text)
                         and m.start in streets)]
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
