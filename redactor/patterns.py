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


# --------------------------------------------------------------------------
# value shapes used by the labeled detectors
# --------------------------------------------------------------------------

# a generic identifier that must contain at least one digit
V_ALNUM = r"(?=[A-Za-z0-9][A-Za-z0-9\-/.]*\d)[A-Za-z0-9][A-Za-z0-9\-/.]{2,29}"
V_DIGITS = r"\d[\d\-\s]{2,25}\d"
V_MASKED = r"(?:[Xx*•#]{3,}[\-\s]?\d{2,6}|\d{2,6}[\-\s]?[Xx*•#]{3,})"
V_ANY_ID = rf"(?:{V_MASKED}|{V_ALNUM})"

# separator between a label and its value: "No.", "Number", "#", ":", "-"
_SEP = (
    r"(?:\s*(?:num(?:ber)?|no\.?|nos\.?|#|id(?:entification)?|acct\.?|account|"
    r"ending(?:\s+in)?|handle|profile|user\s?name|username|login|tag|cash\s?tag|"
    r"is|are|was|were|of|for|at|with)){0,4}\s*[:\-–—=#]?\s*"
)

_MONTHS = (
    r"Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|Jul(?:y)?|"
    r"Aug(?:ust)?|Sep(?:t)?(?:ember)?|Oct(?:ober)?|Nov(?:ember)?|Dec(?:ember)?"
)
V_DATE = (
    rf"(?:\d{{1,2}}[/\-.]\d{{1,2}}[/\-.]\d{{2,4}}"
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


def not_all_same(value: str) -> bool:
    digits = "".join(c for c in value if c.isdigit())
    return len(set(digits)) > 1


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
    # rules of procedure / evidence
    re.compile(
        r"\b(?:Fed\.\s*R\.\s*(?:Civ|Crim|App|Evid|Bankr)\.\s*P\.?|"
        r"(?:Utah|Federal|Local|Local\s+Civil)?\s*R(?:ule)?s?\.?\s*(?:of\s+[A-Z][A-Za-z]+\s+"
        r"(?:Procedure|Evidence)\s*)?)\s*\d+(?:\.\d+)*(?:\([\da-zA-Z]+\))*",
        re.IGNORECASE,
    ),
    # reporter citations: 505 U.S. 833, 2019 UT App 12
    re.compile(rf"\b\d{{1,4}}\s+(?:{_REPORTERS})\s+\d{{1,4}}\b"),
    # a party pair followed by a reporter citation is cited authority, not our client
    re.compile(
        rf"\b[A-Z][\w'.\-]+(?:\s+[\w'.\-]+){{0,4}}\s+v\.?s?\.\s+[A-Z][\w'.\-]+"
        rf"(?:\s+[\w'.\-]+){{0,4}},?\s+\d{{1,4}}\s+(?:{_REPORTERS})"
    ),
    # judicial officers by title - the name after the title is protected
    re.compile(
        r"\b(?:Judge|Chief[^\S\n]+Judge|Commissioner|Justice|Chief[^\S\n]+Justice|"
        r"Magistrate(?:[^\S\n]+Judge)?|Hon(?:orable)?\.?)"
        r"[^\S\n]+[A-Z][\w'\-]+(?:[^\S\n]+[A-Z]\.)?(?:[^\S\n]+[A-Z][\w'\-]+){0,2}"
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


def allowlist_spans(text: str, extra: Iterable[str] = ()) -> list[tuple[int, int]]:
    """Character spans that must survive untouched."""
    spans: list[tuple[int, int]] = []
    for pat in ALLOWLIST_PATTERNS:
        for m in pat.finditer(text):
            spans.append((m.start(), m.end()))
    for term in extra:
        term = term.strip()
        if len(term) < 2:
            continue
        for m in re.finditer(re.escape(term), text, re.IGNORECASE):
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


def _d(category, pattern, group=0, validator=None, priority=50, flags=_I):
    return Detector(category, re.compile(pattern, flags), group, validator, priority)


DETECTORS: tuple[Detector, ...] = (
    # ------------------------------------------------------------- contact --
    _d("email", r"\b[\w!#$%&'*+/=?^`{|}~.-]+@[A-Za-z0-9-]+(?:\.[A-Za-z0-9-]+)+\b", priority=90),
    _d("url", r"\b(?:https?://|ftp://|www\.)[^\s<>\"'\)\]]+", priority=88),
    _d("fax", rf"(?:fax(?:simile)?|telecopier){_SEP}((?:\+?1[\s.\-]?)?(?:\(\d{{3}}\)\s?|\d{{3}}[\s.\-])\d{{3}}[\s.\-]?\d{{4}})", group=1, priority=86),
    _d("phone", r"(?:\+?1[\s.\-]?)?\(\d{3}\)\s?\d{3}[\s.\-]?\d{4}(?:\s*(?:x|ext\.?|extension)\s*\d{1,6})?", priority=80),
    _d("phone", r"(?<![\d\-])(?:\+?1[\s.\-])?\d{3}[\s.\-]\d{3}[\s.\-]\d{4}(?:\s*(?:x|ext\.?|extension)\s*\d{1,6})?(?![\d\-])", priority=80),
    _d("phone", labeled(("telephone", "phone", "cell(?:ular)?", "mobile", "tel\\.?"),
                        r"\d{10}"), group=1, priority=79),
    _d("ip_address", r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b", priority=85),
    _d("ip_address", r"\b(?:[0-9A-Fa-f]{1,4}:){7}[0-9A-Fa-f]{1,4}\b", priority=85),
    _d("mac_address", r"\b(?:[0-9A-Fa-f]{2}[:-]){5}[0-9A-Fa-f]{2}\b", priority=85),
    _d("gps", r"[-+]?\d{1,3}\.\d{4,}\s*[,\s]\s*[-+]?\d{1,3}\.\d{4,}", priority=85),
    _d("social_handle",
       rf"\b(?:facebook|fb|instagram|insta|ig|twitter|x\.com|tiktok|snapchat|snap|linkedin|"
       rf"reddit|discord|telegram|whatsapp|youtube|onlyfans|tinder|bumble|hinge)\b\s*"
       rf"(?:profile|account|handle|user\s?name|username|id)?\s*(?:is|:|=|@|-)?\s*(@?[A-Za-z0-9._\-]{{3,30}})",
       group=1, priority=84),
    _d("social_handle", r"(?<![\w./@])@[A-Za-z][A-Za-z0-9_.\-]{2,29}(?<![.\-])", priority=60),
    _d("username", labeled(("user\\s?name", "user\\s?id", "login(?:\\s?name|\\s?id)?", "screen\\s?name", "display\\s?name"),
                           r"[A-Za-z0-9._@\-]{3,40}"), group=1, priority=70),

    # -------------------------------------------------------- government ID --
    _d("ssn", r"(?<!\d)\d{3}-\d{2}-\d{4}(?!\d)", validator=valid_ssn, priority=95),
    _d("ssn", labeled(("s\\.?s\\.?n\\.?", "social\\s+security", "soc\\.?\\s*sec\\.?"),
                      r"(?:\d{3}[-\s]?\d{2}[-\s]?\d{4}|" + V_MASKED + r")"), group=1, priority=96),
    _d("ein", labeled(("e\\.?i\\.?n\\.?", "f\\.?e\\.?i\\.?n\\.?", "employer\\s+identification",
                       "tax\\s*(?:payer)?\\s*i\\.?d\\.?", "t\\.?i\\.?n\\.?", "federal\\s+tax\\s*id"),
                      r"(?:\d{2}-?\d{7}|" + V_MASKED + r")"), group=1, priority=94),
    _d("ein", r"(?<!\d)\d{2}-\d{7}(?!\d)", priority=62),
    _d("drivers_license", labeled(("driver'?s?\\s*licen[sc]e", "d\\.?l\\.?", "operator'?s?\\s*licen[sc]e",
                                   "state\\s+id(?:entification)?\\s*card?")), group=1, priority=90),
    _d("passport", labeled(("passport",)), group=1, priority=90),
    _d("alien_number", r"\bA[-#\s]?\d{8,9}\b", priority=88),
    _d("alien_number", labeled(("alien\\s*(?:registration)?", "a-?number", "uscis", "uscis\\s*#?", "visa", "i-?94")), group=1, priority=89),
    _d("military_id", labeled(("military\\s*(?:service|id)", "dod\\s*id", "service\\s*number", "edipi")), group=1, priority=88),
    _d("inmate_number", labeled(("inmate", "booking", "offender", "prisoner", "doc\\s*(?:id)?", "jail\\s*id")), group=1, priority=88),
    _d("student_id", labeled(("student\\s*(?:id)?", "pupil\\s*id", "school\\s*id", "enrollment")), group=1, priority=86),
    _d("voter_id", labeled(("voter\\s*(?:registration|reg\\.?|id)?",)), group=1, priority=86),
    _d("bar_number", labeled(("bar\\s*(?:no\\.?|number|#|id)", "utah\\s+bar", "state\\s+bar", "attorney\\s*(?:reg(?:istration)?)?")), group=1, priority=86),
    _d("notary_id", labeled(("notary\\s*(?:commission|id|public)?", "commission\\s*(?:no\\.?|number|#)")), group=1, priority=86),
    _d("professional_license", labeled(("licen[sc]e\\s*(?:no\\.?|number|#)", "certification\\s*(?:no\\.?|number|#)",
                                        "npi", "registration\\s*(?:no\\.?|number|#)")), group=1, priority=84),
    _d("tribal_id", labeled(("tribal\\s*(?:enrollment|id|member(?:ship)?)", "enrollment\\s*(?:no\\.?|number|#)",
                             "cdib", "certificate\\s+of\\s+degree\\s+of\\s+indian\\s+blood")), group=1, priority=86),

    # ------------------------------------------------------------- health ----
    _d("mrn", labeled(("m\\.?r\\.?n\\.?", "medical\\s+record", "patient\\s*(?:id|number|no\\.?|#)",
                       "chart\\s*(?:no\\.?|number|#)", "health\\s+record")), group=1, priority=90),
    _d("health_plan", labeled(("member\\s*(?:id|no\\.?|number|#)", "subscriber\\s*(?:id|no\\.?|number|#)",
                               "group\\s*(?:no\\.?|number|#)", "health\\s+plan", "insurance\\s+id",
                               "medicaid", "medicare", "hicn", "mbi")), group=1, priority=88),
    _d("diagnosis_code", labeled(("icd(?:-?10|-?9)?\\s*(?:cm)?\\s*(?:code)?", "cpt\\s*(?:code)?",
                                  "dsm(?:-?5|-?iv)?\\s*(?:code)?", "diagnosis\\s+code", "hcpcs"),
                                 r"[A-TV-Z]?\d{2,5}(?:\.\d{1,4})?"), group=1, priority=88),
    _d("prescription", labeled(("rx", "prescription", "ndc")), group=1, priority=86),

    # ---------------------------------------------------------- financial ----
    _d("credit_card", r"(?<![\d\-])(?:\d[ -]?){12,18}\d(?![\d\-])", validator=luhn_valid, priority=93),
    _d("credit_card", labeled(("credit\\s*card", "debit\\s*card", "visa", "mastercard", "master\\s*card",
                               "amex", "american\\s+express", "discover", "card\\s*(?:no\\.?|number|#)"),
                              rf"(?:{V_MASKED}|(?:\d[ -]?){{12,18}}\d)"), group=1, priority=92),
    _d("routing_number", labeled(("routing", "aba", "rtn", "transit")), group=1, priority=91),
    _d("iban", r"\b[A-Z]{2}\d{2}[ ]?(?:[A-Z0-9]{4}[ ]?){2,7}[A-Z0-9]{1,4}\b", priority=90, flags=0),
    _d("swift", labeled(("swift", "bic", "swift\\s*/\\s*bic"), r"[A-Z]{6}[A-Z0-9]{2}(?:[A-Z0-9]{3})?"), group=1, priority=90),
    _d("crypto_wallet", r"\b(?:bc1[a-z0-9]{25,62}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})\b", priority=90, flags=0),
    _d("crypto_wallet", r"\b0x[a-fA-F0-9]{40}\b", priority=90),
    _d("payment_handle",
       labeled(("pay\\s?pal", "venmo", "cash\\s?app", "zelle", "square\\s*cash", "apple\\s*pay",
                "google\\s*pay", "wise", "revolut", "chime", "stripe", "coinbase", "robinhood"),
               r"[$@]?[A-Za-z0-9._\-]{3,40}"), group=1, priority=89),
    _d("investment_account", labeled(("401\\s?\\(?k\\)?", "403\\s?\\(?b\\)?", "457", "ira", "roth(?:\\s+ira)?",
                                      "pension", "annuity", "brokerage", "tsp", "hsa", "529",
                                      "retirement\\s+(?:account|plan)", "investment\\s+account",
                                      "securities\\s+account", "mutual\\s+fund")), group=1, priority=87),
    _d("bank_account", labeled(("bank\\s+account", "checking\\s*(?:account)?", "savings\\s*(?:account)?",
                                "deposit\\s+account", "acct", "account", "a/c", "money\\s+market",
                                "certificate\\s+of\\s+deposit", "\\bcd\\b")), group=1, priority=82),
    _d("loan_number", labeled(("loan", "mortgage", "escrow", "note\\s*(?:no\\.?|number|#)", "deed\\s+of\\s+trust",
                               "heloc", "line\\s+of\\s+credit", "promissory\\s+note")), group=1, priority=86),
    _d("policy_number", labeled(("polic(?:y|ies)", "insurance\\s+polic(?:y|ies)", "coverage\\s*(?:no\\.?|number|#)",
                                 "certificate\\s+of\\s+insurance")), group=1, priority=86),
    _d("claim_number", labeled(("claim", "file\\s*(?:no\\.?|number|#)\\s*\\(claim\\)", "adjuster\\s*file")), group=1, priority=86),
    _d("check_number", labeled(("check", "cheque", "draft", "wire\\s*(?:confirmation|reference|transfer)?",
                                "invoice", "transaction", "confirmation", "reference\\s*(?:no\\.?|number|#)",
                                "receipt")), group=1, priority=80),

    # ----------------------------------------------------------- property ----
    _d("street_address",
       r"\b\d{1,6}[A-Z]?\s+(?:(?:North|South|East|West|N\.?|S\.?|E\.?|W\.?|NE|NW|SE|SW)\s+)?"
       r"(?:[A-Z0-9][\w'\-]*\.?\s+){0,4}"
       r"(?:Street|St|Avenue|Ave|Road|Rd|Boulevard|Blvd|Lane|Ln|Drive|Dr|Court|Ct|Circle|Cir|Way|Wy|"
       r"Place|Pl|Terrace|Ter|Parkway|Pkwy|Highway|Hwy|Trail|Trl|Loop|Square|Sq|Plaza|Alley|Route|Rte)\b\.?"
       r"(?:\s*,?\s*(?:Apt\.?|Apartment|Unit|Suite|Ste\.?|Bldg\.?|Building|Floor|Fl\.?|Rm\.?|Room|#)\s*[\w\-]+)?"
       r"(?:\s*,?\s*[A-Z][\w'\-]*(?:\s+[A-Z][\w'\-]*){0,2}\s*,?\s*(?:A[LKZR]|C[AOT]|D[EC]|FL|GA|HI|I[DLNA]|"
       r"K[SY]|LA|M[EDAINSOT]|N[EVHJMYCD]|O[HKR]|PA|RI|S[CD]|T[NX]|UT|V[TA]|W[AVIY])\.?\s*\d{5}(?:-\d{4})?)?",
       priority=75),
    _d("street_address",
       r"(?<![\w\-])\d{2,5}\s+(?:North|South|East|West|N|S|E|W)\.?\s+\d{2,5}\s+"
       r"(?:North|South|East|West|N|S|E|W)\b\.?"
       r"(?:\s*,?\s*(?:Apt\.?|Apartment|Unit|Suite|Ste\.?|Bldg\.?|#)\s*[\w\-]+)?"
       r"(?:\s*,?\s*[A-Z][\w'\-]*(?:\s+[A-Z][\w'\-]*){0,2}\s*,?\s*"
       r"(?:[A-Z]{2}|Utah|Idaho|Nevada|Arizona|Wyoming|Colorado)\.?\s*\d{5}(?:-\d{4})?)?",
       priority=76),
    _d("street_address",
       r"\b(?:P\.?\s?O\.?\s+Box|Post\s+Office\s+Box)\s+\d{1,7}"
       r"(?:\s*,?\s*[A-Z][\w'\-]*(?:\s+[A-Z][\w'\-]*){0,2}\s*,?\s*[A-Z]{2}\.?\s*\d{5}(?:-\d{4})?)?",
       priority=82),
    _d("parcel_number", labeled(("a\\.?p\\.?n\\.?", "parcel", "assessor'?s?\\s+parcel", "tax\\s+parcel",
                                 "property\\s+id", "sidwell", "pin\\s*(?:no\\.?|number|#)")), group=1, priority=88),
    _d("deed_reference", r"\bBook\s+[\w\-]+,?\s+(?:at\s+)?Pages?\s+[\w\-]+", priority=86),
    _d("deed_reference", labeled(("entry\\s*(?:no\\.?|number|#)", "instrument\\s*(?:no\\.?|number|#)",
                                  "recording\\s*(?:no\\.?|number|#)", "document\\s*(?:no\\.?|number|#)",
                                  "reception\\s*(?:no\\.?|number|#)")), group=1, priority=86),
    _d("legal_description",
       r"\bLot\s+[\w\-]+,?\s+Block\s+[\w\-]+(?:,?\s+(?:of\s+)?[A-Z][\w'\-]*(?:\s+[A-Z][\w'\-]*){0,5}"
       r"(?:\s+(?:Subdivision|Addition|Plat|Amended))?)?", priority=86),
    _d("legal_description",
       r"\b(?:Section\s+\d{1,2},?\s+)?Township\s+\d{1,3}\s*[NSns]\.?,?\s+Range\s+\d{1,3}\s*[EWew]\.?", priority=86),
    _d("vin", r"\b[A-HJ-NPR-Z0-9]{17}\b", validator=valid_vin, priority=88, flags=0),
    _d("vin", labeled(("v\\.?i\\.?n\\.?", "vehicle\\s+identification")), group=1, priority=89),
    _d("license_plate", labeled(("licen[sc]e\\s+plate", "plate\\s*(?:no\\.?|number|#)", "tag\\s*(?:no\\.?|number|#)",
                                 "registration\\s+plate"), r"[A-Z0-9][A-Z0-9\- ]{1,9}[A-Z0-9]"), group=1, priority=88),
    _d("vessel_number", labeled(("hull\\s*(?:id|identification)?", "hin", "tail\\s*(?:no\\.?|number|#)",
                                 "aircraft\\s*(?:registration)?", "vessel\\s*(?:no\\.?|number|#)",
                                 "boat\\s*(?:registration|no\\.?|number|#)")), group=1, priority=88),
    _d("safe_deposit", labeled(("safe\\s*deposit\\s*(?:box)?", "safety\\s*deposit\\s*(?:box)?", "vault\\s*box")), group=1, priority=88),
    _d("storage_unit", labeled(("storage\\s*unit", "storage\\s*locker", "unit\\s*(?:no\\.?|number|#)\\s*\\(storage\\)")), group=1, priority=86),

    # --------------------------------------------------------------- case ----
    _d("case_number", labeled(("case", "civil\\s*(?:case)?", "criminal\\s*(?:case)?", "docket", "cause",
                               "court\\s+file", "matter", "index", "action", "file",
                               "probate", "juvenile", "adversary", "appellate", "appeal"),
                              r"(?=[A-Za-z0-9\-:/.]*\d)[A-Za-z0-9][A-Za-z0-9\-:/.]{3,28}[A-Za-z0-9]"),
       group=1, priority=93),
    _d("case_number", r"(?<![\w\-])\d{2}[A-Z]{2,4}\d{3,6}(?![\w\-])", priority=70, flags=0),
    _d("case_number", r"(?<![\w\-])\d{4}-[A-Z]{2,4}-\d{3,6}(?![\w\-])", priority=70, flags=0),
    _d("case_name",
       r"(?-i:\b[A-Z][A-Za-z'\-]+(?:[^\S\n]+[A-Z][A-Za-z'.\-]+){0,3}[^\S\n]+v\.?s?\.[^\S\n]+"
       r"[A-Z][A-Za-z'\-]+(?:[^\S\n]+[A-Z][A-Za-z'.\-]+){0,3})", priority=64),
    _d("case_designator", labeled(("bar\\s+code", "tracking\\s*(?:no\\.?|number|#)", "efiling\\s*(?:id|no\\.?|number|#)",
                                   "e-?filed\\s+document", "envelope\\s*(?:no\\.?|number|#)",
                                   "submission\\s*(?:id|no\\.?|number|#)", "exhibit\\s*(?:no\\.?|number|#)\\s*\\(case\\)")),
       group=1, priority=80),

    # -------------------------------------------------------------- vital ----
    _d("dob", labeled(("d\\.?o\\.?b\\.?", "date\\s+of\\s+birth", "birth\\s*date", "born\\s+on", "date\\s+born",
                       "birthday"), V_DATE), group=1, priority=95),
    _d("dob", rf"\bborn\s+(?:on\s+)?({V_DATE})", group=1, priority=94),
    _d("pob", labeled(("place\\s+of\\s+birth", "birth\\s*place", "born\\s+(?:in|at)", "city\\s+of\\s+birth",
                       "state\\s+of\\s+birth", "country\\s+of\\s+birth"),
                      r"(?-i:[A-Z][\w'\-]*(?:\s+[A-Z][\w'\-]*){0,3}(?:,\s*[A-Z][\w'\-]*(?:\s+[A-Z][\w'\-]*){0,3}){0,2})"),
       group=1, priority=92),
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
    """.split()
)


def _value_is_junk(value: str) -> bool:
    cleaned = value.strip().strip(".,;:#-").lower()
    if not cleaned:
        return True
    return cleaned in _REJECT_VALUES


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

    for det in DETECTORS:
        if allowed is not None and det.category not in allowed:
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
