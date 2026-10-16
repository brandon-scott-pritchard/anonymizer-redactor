"""The party sheet: who the case is about, and every way their details are printed.

The overnight run of a real night's sets was unusable: thousands of review
rows, one person's address in five rows, the same account under nine
placeholders, transaction numbers and phone numbers taken as accounts, and
every merchant on every statement proposed as a person. What the attorney
wants replaced is narrower than "everything that looks sensitive": anything a
member of the public could use to identify one of the parties or their
children. So the operator says who they are first, and the run works from
that:

* every value on the sheet is one row - "[RESPONDENT-SSN]" - and every way
  it is printed ("528-41-9963", "XXX-XX-9963", "528419963") is a form under
  that row, matched exactly wherever it appears;
* a value the detectors find that is not on the sheet is the party's only
  when it is anchored to them: on a line with their name, in the block under
  it, or in a statement's header - else it is left as written, except a
  valid SSN and a masked account tail in a transfer line, where a leak costs
  more than a box;
* third parties - payees, merchants, landlords, other attorneys - are left as
  written, except where the family's surname is part of them.

The sheet lives in memory with its set only. It is never written to disk, and
the mapping key holds no more of it than the original -> replacement table it
always did.
"""

from __future__ import annotations

import contextvars
import re
from dataclasses import dataclass, field
from datetime import date, datetime

from . import grouping, names as _names, patterns
from .mapping import Entity, MappingStore

ROLES = ("petitioner", "respondent", "child", "other")
# the review list's order: petitioner, respondent, children, parties, others
_ROLE_ORDER = {"petitioner": 0, "respondent": 1, "child": 2, "parties": 3, "other": 4}

TEXT_FIELDS = ("names", "dob", "ssn", "tin", "passport", "addresses", "phones", "emails",
               "employers", "businesses", "handles", "case_numbers", "parcels", "other")
RECORD_FIELDS = {"dl": ("number", "state"),
                 "accounts": ("number", "institution", "kind"),
                 "cards": ("number", "institution"),
                 "vehicles": ("vin", "plate")}
FIELDS = ("names", "dob", "ssn", "tin", "dl", "passport", "addresses", "phones", "emails",
          "employers", "businesses", "accounts", "cards", "handles", "vehicles",
          "case_numbers", "parcels", "other")

# Which page of a PDF the text being scanned is from - set by the PDF reader
# as it walks a document's pages. None for a Word document, whose paragraphs
# have no page.
PAGE: contextvars.ContextVar[int | None] = contextvars.ContextVar("party_page", default=None)


class PartyError(ValueError):
    """A value on the sheet that is not what its field says it is.

    ``where`` is the path to it ("parties[1].ssn[0]"); :meth:`detail` is what
    the window is told - which party, which field, and what is wrong."""

    def __init__(self, where: str, message: str) -> None:
        super().__init__(f"{where}: {message}")
        self.where = where
        self.message = message
        self.role = ""
        m = re.match(r"parties\[(\d+)\](?:\.(\w+))?", where)
        self.party = int(m[1]) if m else None
        self.field = (m[2] if m else where.split("[")[0]) or ""

    def detail(self) -> dict:
        who = (self.role or "").title() or (f"Party {self.party + 1}"
                                           if self.party is not None else "")
        readable = " ".join(p for p in (who, self.field.replace("_", " ")) if p)
        return {"party": self.party, "role": self.role, "field": self.field,
                "where": self.where,
                "message": f"{readable}: {self.message}" if readable else self.message}


# --------------------------------------------------------------------------
# the sheet
# --------------------------------------------------------------------------


@dataclass
class Party:
    role: str
    names: list[str] = field(default_factory=list)
    dob: list[str] = field(default_factory=list)
    ssn: list[str] = field(default_factory=list)
    tin: list[str] = field(default_factory=list)
    dl: list[dict] = field(default_factory=list)
    passport: list[str] = field(default_factory=list)
    addresses: list[str] = field(default_factory=list)
    phones: list[str] = field(default_factory=list)
    emails: list[str] = field(default_factory=list)
    employers: list[str] = field(default_factory=list)
    businesses: list[str] = field(default_factory=list)
    accounts: list[dict] = field(default_factory=list)
    cards: list[dict] = field(default_factory=list)
    handles: list[str] = field(default_factory=list)
    vehicles: list[dict] = field(default_factory=list)
    case_numbers: list[str] = field(default_factory=list)
    parcels: list[str] = field(default_factory=list)
    other: list[str] = field(default_factory=list)
    # who this is on the review list and in the labels: "petitioner",
    # "respondent", "child-1", "other-2" - children and others numbered in
    # the order they were entered
    owner: str = ""
    # each field's values as they were typed, beside the canonical forms above
    entered: dict = field(default_factory=dict)

    def empty(self) -> bool:
        return not any(getattr(self, name) for name in FIELDS)

    def to_json(self) -> dict:
        """The party as the window shows it: a formatted kind's values as
        {"value": as typed, "canonical": as the run reads it}."""
        out: dict = {"role": self.role}
        for name in FIELDS:
            values = getattr(self, name)
            typed = self.entered.get(name, [])
            if name in RECORD_FIELDS:
                out[name] = [{**v, "canonical": _record_canonical(name, v)} for v in values]
            elif name in FORMATTED:
                out[name] = [{"value": typed[i] if i < len(typed) and isinstance(typed[i], str)
                              else v, "canonical": v} for i, v in enumerate(values)]
            else:
                out[name] = list(values)
        return out


@dataclass
class PartySheet:
    parties: list[Party] = field(default_factory=list)
    family_surnames: list[str] = field(default_factory=list)

    def empty(self) -> bool:
        return all(p.empty() for p in self.parties) and not self.family_surnames

    def to_json(self) -> dict:
        return {"parties": [p.to_json() for p in self.parties],
                "family_surnames": list(self.family_surnames)}

    def people(self) -> list[Party]:
        """The petitioner, the respondent and the children - the family."""
        return [p for p in self.parties if p.role in ("petitioner", "respondent", "child")]

    @classmethod
    def from_json(cls, data) -> "PartySheet":
        """A validated, normalised sheet; :class:`PartyError` naming the field."""
        if not isinstance(data, dict):
            raise PartyError("sheet", "the party sheet is a JSON object")
        raw = data.get("parties", [])
        if not isinstance(raw, list):
            raise PartyError("parties", "a list of parties")
        sheet = cls()
        counts: dict[str, int] = {}
        for index, item in enumerate(raw):
            where = f"parties[{index}]"
            if not isinstance(item, dict):
                raise PartyError(where, "each party is a JSON object")
            role = str(item.get("role", "")).strip().casefold()
            if role not in ROLES:
                raise PartyError(f"{where}.role", "one of petitioner, respondent, child, other")
            counts[role] = counts.get(role, 0) + 1
            if role in ("petitioner", "respondent") and counts[role] > 1:
                raise PartyError(f"{where}.role", f"there is already a {role}")
            party = Party(role=role)
            party.owner = role if role in ("petitioner", "respondent") \
                else f"{role}-{counts[role]}"
            try:
                for name in FIELDS:
                    values = item.get(name, [])
                    if values in (None, ""):
                        values = []
                    if not isinstance(values, list):
                        values = [values]          # one value typed where a list goes
                    out, typed = [], []
                    for position, value in enumerate(values):
                        raw = _entered(value)
                        normal = _normalise(name, raw, f"{where}.{name}[{position}]")
                        if normal and normal not in out:
                            out.append(normal)
                            typed.append(raw)
                    setattr(party, name, out)
                    party.entered[name] = typed
            except PartyError as exc:
                exc.role = role
                raise
            sheet.parties.append(party)
        surnames = data.get("family_surnames")
        if surnames is None:
            sheet.family_surnames = derived_surnames(sheet)
        else:
            if not isinstance(surnames, list):
                raise PartyError("family_surnames", "a list of surnames")
            out = []
            for position, value in enumerate(surnames):
                text = " ".join(str(value or "").split())
                if not text:
                    continue
                if not any(ch.isalpha() for ch in text):
                    raise PartyError(f"family_surnames[{position}]", "a surname is letters")
                if text.casefold() not in {s.casefold() for s in out}:
                    out.append(text)
            sheet.family_surnames = out
        return sheet


# the kinds whose canonical form differs from what is typed
FORMATTED = ("ssn", "tin", "dob", "phones", "addresses", "emails")


def _entered(value):
    """A value as the window sends it: a string, or {"value", "canonical"}
    sent back as it was shown."""
    if isinstance(value, dict) and ("value" in value or "canonical" in value) \
            and not any(k in value for k in ("number", "vin", "plate")):
        return value.get("value") if value.get("value") not in (None, "") \
            else value.get("canonical", "")
    if isinstance(value, dict):
        return {k: v for k, v in value.items() if k != "canonical"}
    return value


def _record_canonical(name: str, record: dict) -> str:
    if name == "vehicles":
        return " / ".join(v for v in (record.get("vin", ""), record.get("plate", "")) if v)
    number = record.get("number", "")
    if name in ("accounts", "cards"):
        digits = _digits(number)
        shown = number if _MASKED_ENTRY.search(number) else digits
        if len(digits) <= 4:
            shown = f"****{digits}"
        return " ".join(p for p in (record.get("institution", ""), shown) if p)
    return " ".join(p for p in (record.get("state", ""), number) if p)


_MASKED_ENTRY = re.compile(r"[xX*•#]|\.\.")


def derived_surnames(sheet: PartySheet) -> list[str]:
    """The petitioner's, the respondent's and the children's surnames, once each."""
    out: list[str] = []
    for party in sheet.people():
        for name in party.names[:1]:
            last = _names.parse(name).last.strip(".,")
            if len(last) >= 2 and last.casefold() not in {s.casefold() for s in out}:
                out.append(last)
    return out


# ------------------------------------------------------------ normalising --

def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


def _normalise(name: str, value, where: str):
    """The canonical form of one value, or "" for a blank one."""
    if name in RECORD_FIELDS:
        if isinstance(value, str):
            value = {RECORD_FIELDS[name][0]: value}
        if not isinstance(value, dict):
            raise PartyError(where, "a JSON object")
        record = {key: " ".join(str(value.get(key) or "").split()) for key in RECORD_FIELDS[name]}
        if name == "vehicles":
            record["vin"] = record["vin"].upper().replace(" ", "")
            record["plate"] = record["plate"].upper()
            if not record["vin"] and not record["plate"]:
                return ""
            if record["vin"] and not re.fullmatch(r"[A-Z0-9]{11,17}", record["vin"]):
                raise PartyError(f"{where}.vin", "a VIN is 17 letters and digits")
            return record
        number = record[RECORD_FIELDS[name][0]]
        if not number:
            return ""
        if name in ("accounts", "cards") and len(_digits(number)) < 4:
            raise PartyError(f"{where}.number",
                             "an account or card number needs at least its last 4 digits")
        if name == "dl":
            if len(re.sub(r"[^A-Za-z0-9]", "", number)) < 4:
                raise PartyError(f"{where}.number", "a licence number is 4 characters or more")
            record["number"] = number.upper()
            record["state"] = record["state"].upper()
        return record
    text = " ".join(str(value or "").split())
    if not text:
        return ""
    if name == "names":
        if not any(ch.isalpha() for ch in text):
            raise PartyError(where, "a name is letters")
        return text
    if name == "ssn":
        digits = _digits(text)
        if len(digits) != 9:
            raise PartyError(where, f"an SSN is 9 digits - got {len(digits)}")
        return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"
    if name == "tin":
        digits = _digits(text)
        if len(digits) != 9:
            raise PartyError(where, f"a TIN is 9 digits - got {len(digits)}")
        if re.fullmatch(r"\d{2}-\d{7}", text):
            return text                                  # an EIN keeps its shape
        return f"{digits[:3]}-{digits[3:5]}-{digits[5:]}"
    if name == "dob":
        parsed = parse_date(text)
        if parsed is None:
            raise PartyError(where, f"a date of birth is a date - got {text!r}")
        return parsed.isoformat()
    if name == "phones":
        digits = _digits(text.split("x")[0].split("ext")[0])
        if len(digits) == 11 and digits.startswith("1"):
            digits = digits[1:]
        if len(digits) != 10:
            raise PartyError(where, f"a phone is 10 digits - got {len(digits)}")
        return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"
    if name == "emails":
        if not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", text):
            raise PartyError(where, f"not an email address: {text!r}")
        return text.casefold()
    if name == "addresses":
        parsed = grouping.parse_address(text)
        if parsed is None or not (parsed.number or parsed.box):
            raise PartyError(where, "an address starts with its house number and street")
        return text
    if name == "handles":
        if len(text.lstrip("@$")) < 3:
            raise PartyError(where, "a handle is 3 characters or more")
        return text
    return text


_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def parse_date(text: str) -> date | None:
    """A written date, in the forms a person types or a document prints."""
    cleaned = " ".join(text.replace(",", " ").split()).strip(" .")
    cleaned = re.sub(r"(?i)(\d)(st|nd|rd|th)\b", r"\1", cleaned)
    m = re.fullmatch(r"(\d{4})[-/.](\d{1,2})[-/.](\d{1,2})", cleaned)
    if m:
        return _date(int(m[1]), int(m[2]), int(m[3]))
    m = re.fullmatch(r"(\d{1,2})[-/.](\d{1,2})[-/.](\d{2}|\d{4})", cleaned)
    if m:
        year = int(m[3])
        if year < 100:
            year += 1900 if year > datetime.now().year % 100 else 2000
        return _date(year, int(m[1]), int(m[2]))
    m = re.fullmatch(r"(?i)([a-z]{3,9})\.?[ -]?(\d{1,2})[ -]+(\d{4})", cleaned)
    if m and m[1][:3].casefold() in _MONTHS:
        return _date(int(m[3]), _MONTHS[m[1][:3].casefold()], int(m[2]))
    m = re.fullmatch(r"(?i)(\d{1,2})[ -]?([a-z]{3,9})\.?[ -]?(\d{2}|\d{4})", cleaned)
    if m and m[2][:3].casefold() in _MONTHS:
        year = int(m[3])
        if year < 100:
            year += 1900 if year > datetime.now().year % 100 else 2000
        return _date(year, _MONTHS[m[2][:3].casefold()], int(m[1]))
    return None


def _date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


# --------------------------------------------------------------------------
# every printed form of a value
# --------------------------------------------------------------------------

def ssn_forms(value: str) -> list[str]:
    """"528-41-9963" as statements, forms and scans print it."""
    d = _digits(value)
    last = d[-4:]
    return _unique([f"{d[:3]}-{d[3:5]}-{d[5:]}", f"{d[:3]} {d[3:5]} {d[5:]}", d,
                    f"***-**-{last}", f"XXX-XX-{last}", f"*** ** {last}", f"XXX XX {last}",
                    f"*****{last}", f"XXXXX{last}"])


def tin_forms(value: str) -> list[str]:
    d = _digits(value)
    return _unique([value, f"{d[:2]}-{d[2:]}", *ssn_forms(d)])


def phone_forms(value: str) -> list[str]:
    d = _digits(value)
    if len(d) == 11 and d.startswith("1"):
        d = d[1:]
    a, b, c = d[:3], d[3:6], d[6:]
    return _unique([f"({a}) {b}-{c}", f"({a}){b}-{c}", f"{a}-{b}-{c}", f"{a}.{b}.{c}",
                    f"{a} {b} {c}", d, f"+1 {a} {b} {c}", f"1-{a}-{b}-{c}", f"+1{d}",
                    f"+1 ({a}) {b}-{c}", f"1 ({a}) {b}-{c}", f"+1-{a}-{b}-{c}",
                    f"+1.{a}.{b}.{c}", f"{a}/{b}-{c}"])


_MONTH_NAMES = ("January", "February", "March", "April", "May", "June", "July", "August",
                "September", "October", "November", "December")


def dob_forms(value: str) -> list[str]:
    """Every common way the one date is written - each of them a date to
    patterns.looks_like_date, and none of them any other day. A day-first
    numeric form is written only when it cannot be read month-first: "06/12"
    is the twelfth of June to everyone else in the set."""
    d = parse_date(value) if not isinstance(value, date) else value
    if d is None:
        return []
    y, yy = f"{d.year}", f"{d.year % 100:02d}"
    months = [f"{d.month:02d}", f"{d.month}"]
    days = [f"{d.day:02d}", f"{d.day}"]
    full = _MONTH_NAMES[d.month - 1]
    short = full[:3]
    out: list[str] = []
    for sep in ("/", "-", "."):
        for m in months:
            for day in days:
                out += [f"{m}{sep}{day}{sep}{y}", f"{m}{sep}{day}{sep}{yy}"]
        out += [f"{y}{sep}{d.month:02d}{sep}{d.day:02d}", f"{y}{sep}{d.month}{sep}{d.day}"]
        if d.day > 12:
            out += [f"{day}{sep}{m}{sep}{y}" for day in days for m in months]
    names = [full, short, f"{short}."] + (["Sept", "Sept."] if d.month == 9 else [])
    suffix = "th" if 11 <= d.day <= 13 else {1: "st", 2: "nd", 3: "rd"}.get(d.day % 10, "th")
    for month in names:
        for day in days:
            out += [f"{month} {day}, {y}", f"{month} {day} {y}"]
        out += [f"{month} {d.day}{suffix}, {y}"]
    for day in days:
        out += [f"{day} {full} {y}", f"{day} {short} {y}", f"{day}-{short}-{y}",
                f"{day}-{short}-{yy}", f"{day}{short.upper()}{y}"]
    return _unique(out)


_DIRECTION_WORDS = {"n": "North", "s": "South", "e": "East", "w": "West", "ne": "Northeast",
                    "nw": "Northwest", "se": "Southeast", "sw": "Southwest"}
_SUFFIX_FULL = {"st": "Street", "ave": "Avenue", "rd": "Road", "dr": "Drive", "ln": "Lane",
                "blvd": "Boulevard", "ct": "Court", "cir": "Circle", "pl": "Place", "way": "Way",
                "pkwy": "Parkway", "hwy": "Highway", "ter": "Terrace", "trl": "Trail",
                "loop": "Loop", "sq": "Square", "plz": "Plaza", "bnd": "Bend", "row": "Row",
                "run": "Run", "xing": "Crossing", "lndg": "Landing", "trce": "Trace",
                "mews": "Mews", "aly": "Alley", "rte": "Route"}
_STREET_LINE = re.compile(
    r"^\s*(?P<number>\d{1,6}[A-Za-z]?)\s+"
    r"(?:(?P<dir>north|south|east|west|northeast|northwest|southeast|southwest|"
    r"n|s|e|w|ne|nw|se|sw)\.?\s+)?"
    r"(?P<name>.+?)\s+(?P<suffix>[A-Za-z]+)\.?"
    r"(?:\s+(?P<post>n|s|e|w)\.?)?"
    r"(?:\s*,?\s*(?P<unitword>apt|apartment|unit|suite|ste|#)\.?\s*(?P<unit>[\w\-]+))?"
    r"\s*(?:,\s*|\s+|$)(?P<rest>.*)$", re.IGNORECASE)


def address_forms(value: str) -> list[str]:
    """The street line in each way it is abbreviated, with and without its
    unit, and the address as entered with its town. Every other printing -
    the city on the next line, a ZIP+4, a dropped direction - is folded in
    when the detectors find it (see :func:`fold`)."""
    out = [value]
    m = _STREET_LINE.match(value)
    parsed = grouping.parse_address(value)
    if m is None or parsed is None or m["suffix"].casefold() not in grouping._SUFFIXES:
        street = value.split(",")[0].strip()
        return _unique([value, street, value.replace(",", "")])
    number = m["number"]
    suffix_key = grouping._SUFFIXES[m["suffix"].casefold()]
    suffixes = [suffix_key.title(), f"{suffix_key.title()}.", _SUFFIX_FULL.get(suffix_key, "")]
    if m["dir"]:
        key = grouping._DIRECTIONS[m["dir"].casefold()]
        directions = [key.upper(), f"{key.upper()}.", _DIRECTION_WORDS.get(key, ""), ""]
    else:
        directions = [""]
    units = [""]
    if m["unit"]:
        word = m["unitword"].casefold()
        unit = m["unit"]
        units = ([f"Suite {unit}", f"Ste {unit}", f"Ste. {unit}", f"#{unit}"]
                 if word in ("suite", "ste") else
                 [f"Apt {unit}", f"Apt. {unit}", f"Unit {unit}", f"#{unit}"])
    post = f" {m['post'].upper()}" if m["post"] else ""
    lines = []
    for direction in directions:
        for suffix in suffixes:
            if not suffix:
                continue
            base = " ".join(p for p in (number, direction, m["name"], suffix) if p) + post
            for unit in units:
                if unit:
                    lines += [f"{base} {unit}", f"{base}, {unit}"]
                else:
                    lines.append(base)
    rest = m["rest"].strip()
    entered_line = value[:m.start("rest")].rstrip(" ,")
    out += [entered_line, *lines]
    if rest:
        out += [f"{entered_line}, {rest}", f"{entered_line} {rest}",
                f"{entered_line} {rest.replace(',', '')}"]
    zip4 = re.search(r"\b\d{5}-\d{4}\b", value)
    if zip4:
        out.append(zip4.group(0))
    return _unique(out)


_X_RUNS = ("XXXX", "XXXXXX", "XXXXXXXX", "XXXXXXXXXXXX")


def account_forms(number: str) -> list[str]:
    """The number whole, in groups, and every masked form of its last four.
    "ending in 1234" and "last four 1234" are matched by the label in front
    of the tail (see :func:`extra_hits`), so only the digits are replaced."""
    digits = _digits(number)
    last = digits[-4:]
    masked = bool(re.search(r"[xX*•#]|\.\.", number)) or len(digits) <= 4
    out = [number]
    if not masked and len(digits) >= 5:
        groups = [digits[i:i + 4] for i in range(0, len(digits), 4)]
        out += [digits, " ".join(groups), "-".join(groups)]
        stripped = digits.lstrip("0")
        if len(stripped) >= 6 and stripped != digits:
            out.append(stripped)
    out += [f"****{last}", f"***{last}", f"**{last}", f"*{last}", f"...{last}", f"..{last}",
            f"…{last}", f"•••• {last}", f"••••{last}", f"x{last}", f"x-{last}", f"#{last}",
            *(f"{run}{last}" for run in _X_RUNS),
            f"XXXX-XXXX-XXXX-{last}", f"****-****-****-{last}", f"**** **** **** {last}",
            f"XXXX XXXX XXXX {last}", f"************{last}"]
    return _unique([form for form in out if len(form) >= 4])


def email_forms(value: str) -> list[str]:
    return _unique([value, f"mailto:{value}"])


def handle_forms(value: str) -> list[str]:
    bare = value.lstrip("@$")
    out = [value, f"@{bare}"]
    if value.startswith("$"):
        out.append(f"${bare}")
    # bare, only where it cannot be an ordinary word
    if len(bare) >= 6 and (re.search(r"[\d._\-]", bare) or not _names.ordinary_word(bare)):
        out.append(bare)
    return _unique(out)


def id_forms(value: str) -> list[str]:
    """A licence, passport, case, parcel or plate number: as entered, and
    without (or with other) separators."""
    out = [value]
    squeezed = re.sub(r"[\s\-]", "", value)
    out.append(squeezed)
    if "-" in value:
        out.append(value.replace("-", " "))
    if " " in value:
        out.append(value.replace(" ", "-"))
    return _unique(out)


def _unique(forms) -> list[str]:
    seen: set[str] = set()
    out = []
    for form in forms:
        form = " ".join(str(form).split())
        if form and form.casefold() not in seen:
            seen.add(form.casefold())
            out.append(form)
    return out


# --------------------------------------------------------------------------
# into the store
# --------------------------------------------------------------------------

# what each kind is registered as, and its label stem
CATEGORY = {"ssn": "ssn", "tin": "ein", "phone": "phone", "dob": "dob",
            "address": "street_address", "account": "bank_account", "card": "credit_card",
            "email": "email", "handle": "social_handle", "dl": "drivers_license",
            "passport": "passport", "vehicle": "vin", "case_number": "case_number",
            "parcel": "parcel_number", "employer": "employer", "business": "organization",
            "other": "party_value"}
TAG = {"ssn": "SSN", "tin": "TIN", "dob": "DOB", "dl": "DL", "passport": "PASSPORT",
       "phone": "PHONE", "address": "ADDRESS", "account": "ACCOUNT", "card": "CARD",
       "email": "EMAIL", "handle": "HANDLE", "vehicle": "VEHICLE", "employer": "EMPLOYER",
       "business": "BUSINESS", "other": "OTHER", "case_number": "CASE-NUMBER",
       "parcel": "PARCEL"}
# one of these per person, so the first goes unnumbered: [PETITIONER-SSN]
SINGLE = {"ssn", "tin", "dob", "dl", "passport"}
# the case's, not one party's: [CASE-NUMBER-1], [PARCEL-1]
CASE_LEVEL = {"case_number", "parcel"}
KIND_LABEL = {"name": "name", "family_surname": "family surname", "ssn": "SSN", "tin": "TIN",
              "dob": "date of birth", "dl": "driver's licence", "passport": "passport",
              "phone": "phone", "address": "address", "account": "account", "card": "card",
              "email": "email", "handle": "handle", "vehicle": "vehicle",
              "case_number": "case number", "parcel": "parcel", "employer": "employer",
              "business": "business", "other": "other"}
_KIND_ORDER = ("name", "family_surname", "ssn", "tin", "dob", "address", "phone", "email",
               "account", "card", "dl", "passport", "handle", "vehicle", "employer",
               "business", "case_number", "parcel", "other")

# What a detector's find is, as a kind of party value. Anything else is left as
# written however it is anchored: policy, claim, member and group numbers,
# loans, payroll IDs, check, transaction, confirmation, reference, routing,
# order and tracking numbers, health plans.
KIND_OF = {"ssn": "ssn", "ein": "tin", "phone": "phone", "fax": "phone", "dob": "dob",
           "street_address": "address", "bank_account": "account",
           "masked_account": "account", "investment_account": "account",
           "credit_card": "card", "email": "email", "social_handle": "handle",
           "payment_handle": "handle", "username": "handle", "drivers_license": "dl",
           "passport": "passport", "vin": "vehicle", "license_plate": "vehicle",
           "case_number": "case_number", "parcel_number": "parcel",
           "deed_reference": "parcel", "legal_description": "parcel",
           # rare in a family case, and each names one person outright
           "alien_number": "other", "military_id": "other", "mrn": "other",
           "student_id": "other", "voter_id": "other", "inmate_number": "other",
           "tribal_id": "other", "vehicle_title": "vehicle", "vessel_number": "vehicle",
           "iban": "account", "crypto_wallet": "account"}
LEAVE = frozenset({"policy_number", "claim_number", "health_plan", "loan_number",
                   "employee_id", "check_number", "order_number", "tracking_number",
                   "routing_number", "case_designator"})

SHEET_SOURCE = "party sheet"
FORM_SOURCE = "party form"          # generated: listed only once it is found


def owners(sheet: PartySheet) -> list[Party]:
    """The parties with their ``owner`` set, in the sheet's order."""
    counts: dict[str, int] = {}
    for party in sheet.parties:
        counts[party.role] = counts.get(party.role, 0) + 1
        if not party.owner:
            party.owner = party.role if party.role in ("petitioner", "respondent") \
                else f"{party.role}-{counts[party.role]}"
    return list(sheet.parties)


def label(store: MappingStore, owner: str, kind: str) -> str:
    """The next role-based replacement: [RESPONDENT-PHONE-2], [CHILD-1-DOB]."""
    tag = TAG.get(kind, kind.upper())
    prefix = "" if kind in CASE_LEVEL else owner.upper()
    counter = f"party:{prefix}:{tag}"
    store._counters[counter] = store._counters.get(counter, 0) + 1
    n = store._counters[counter]
    stem = f"{prefix}-{tag}" if prefix else tag
    if kind in SINGLE and owner != "parties" and n == 1:
        return f"[{stem}]"
    return f"[{stem}-{n}]"


def _key(category: str, text: str) -> str:
    return f"{category}:{' '.join(text.split()).casefold()}"


def _new_head(store: MappingStore, owner: str, kind: str, category: str, text: str,
              forms: list[str], source: str = SHEET_SOURCE,
              replacement: str | None = None) -> Entity:
    """One row for one value of one party, its printed forms under it."""
    key = _key(category, text)
    existing = store.entities.get(key)
    if existing is not None and existing.fixed:
        # another party entered the same value: it is one row, the first's
        return grouping.head_of(store, existing)
    head = Entity(key=key, category=category, canonical=" ".join(text.split()),
                  replacement=replacement or label(store, owner, kind), source=source,
                  fixed=True, party=owner, kind=kind)
    if existing is not None:
        head.occurrences, head.documents = existing.occurrences, existing.documents
        head.enabled, head.contexts = existing.enabled, existing.contexts
    store.entities[key] = head
    for form in forms:
        add_form(store, head, form, category)
    return head


def add_form(store: MappingStore, head: Entity, text: str, category: str | None = None,
             source: str = FORM_SOURCE) -> Entity | None:
    """``text`` as another printed form of ``head`` - one more way the literal
    pass finds it, under the head's row and replacement."""
    category = category or head.category
    key = _key(category, text)
    if key == head.key:
        return head
    existing = store.entities.get(key)
    if existing is not None:
        if existing.fixed and grouping.head_of(store, existing) is not head:
            return existing                  # another party's already
        entity = existing
    else:
        entity = Entity(key=key, category=category, canonical=" ".join(text.split()),
                        replacement=head.replacement, source=source)
        store.entities[key] = entity
    entity.parent, entity.fixed = head.key, True
    entity.party, entity.kind = head.party, head.kind
    entity.replacement = head.replacement
    entity.candidates, entity.group_note = (), ""
    return entity


def _sheet_value(store, owner, kind, text, forms, category=None, replacement=None) -> Entity:
    return _new_head(store, owner, kind, category or CATEGORY[kind], text, forms,
                     replacement=replacement)


# Pieces of a name never searched for alone: an initial, a particle, a suffix,
# a function word. "Maria De La Cruz" had "La" replaced in every sentence.
def _searchable_piece(word: str) -> bool:
    bare = word.strip(".,").casefold()
    return (len(bare) > 1 and bare not in _names.PARTICLES and bare not in _names.FUNCTION_WORDS
            and not re.fullmatch(r"(?:jr|sr|ii|iii|iv|v|esq)", bare))


def _trim_variants(entity: Entity) -> None:
    entity.variants = [v for v in entity.variants
                       if v.token_count > 1 or v.layout != "plain"
                       or (_searchable_piece(v.text)
                           and v.text.strip(".").casefold() not in _names.NOT_A_NAME_TAIL)]


def _person(store, party: Party, text: str, primary: Entity | None) -> Entity | None:
    child = party.role == "child"
    entity = store.add_person(text, category="minor" if child else "person",
                              role=party.owner, source=SHEET_SOURCE,
                              distinct=child or primary is not None)
    if entity is None:
        return None
    if primary is not None and entity is primary:
        return entity
    entity.fixed, entity.party, entity.kind = True, party.owner, "name"
    entity.source = SHEET_SOURCE
    if primary is not None:
        entity.parent = primary.key
        entity.group_note = "a former name"
    _trim_variants(entity)
    return entity


def _form_of(primary: Entity | None, text: str) -> bool:
    """Is ``text`` the primary name written another way - each word one of
    its words misread, shortened or a nickname, or the whole name glued -
    rather than a former name with a surname of its own?"""
    if primary is None or primary.person is None:
        return False
    parsed = _names.parse(text)
    words = [w for _k, w in parsed.ordered]
    real = [w for _k, w in primary.person.ordered]
    if not words:
        return False
    if all(any(_names.alike(w, r) or _names.same_given(w, r)
               or (len(w.strip(".")) == 1 and r[:1].casefold() == w[:1].casefold())
               for r in real) for w in words):
        return True
    if len(words) == 1:
        glued = _names._ocr_folded(words[0])
        folded = [_names._ocr_folded(r) for r in real]
        return len(glued) >= 6 and glued in ("".join(folded), folded[0] + folded[-1],
                                            folded[-1] + folded[0])
    return False


def api_owner(owner: str) -> str:
    """The owner as the window names it: "child 1", "other 2"."""
    return owner.replace("-", " ")


def owner_from_api(owner: str) -> str:
    return "-".join(str(owner or "").strip().casefold().split()).replace("--", "-")


def register(sheet: PartySheet, store: MappingStore) -> MappingStore:
    """Put the sheet into ``store``: the parties as people, every value as
    one row with its printed forms under it."""
    store.parties = sheet
    people = owners(sheet)
    # names first, so the stand-ins are settled before anything is shaped
    # from them; children registered apart, as off a roster
    found_forms: list[tuple[str, str]] = []
    for party in people:
        primary = None
        for text in party.names:
            text = _names.without_tail(text)
            if primary is None:
                primary = _person(store, party, text, None)
            elif _form_of(primary, text):
                # the name screen's "Add ticked" puts a misreading, a glued
                # form or a nickname on the party's names: one person still
                found_forms.append((party.owner, text))
            else:
                _person(store, party, text, primary)        # a former name
    family = None
    later = []
    for surname in sheet.family_surnames:
        surname = _names.without_tail(surname)
        # "van der Berg" is one surname; "Smith Landscaping LLC" is not
        if len(surname.split()) > 1 \
                and surname.split()[0].casefold() not in _names.PARTICLES:
            later.append(surname)                    # a business or a relative: below
            continue
        if family is not None and family.person is not None and any(
                e.kind == "family_surname" and _names.alike(surname, e.canonical)
                for e in store.entities.values()):
            found_forms.append(("parties", surname))       # the surname misread
            continue
        entity = store.add_person(surname, category="person", role="family surname",
                                  source=SHEET_SOURCE, distinct=True)
        if entity is not None:
            entity.fixed, entity.party, entity.kind = True, "parties", "family_surname"
            _trim_variants(entity)
            family = family or entity
    for owner, text in found_forms:
        add_name_form(store, owner, text, "a form of the name the documents use")
    settle(store)
    for text in later:
        if family is not None:
            _family_form(store, text)
    invented = family_invented(store)
    for party in people:
        owner = party.owner
        for value in party.ssn:
            _sheet_value(store, owner, "ssn", value, ssn_forms(value))
        for value in party.tin:
            _sheet_value(store, owner, "tin", value, tin_forms(value))
        for value in party.dob:
            _sheet_value(store, owner, "dob", value, dob_forms(value))
        for value in party.addresses:
            _sheet_value(store, owner, "address", value, address_forms(value))
        for value in party.phones:
            _sheet_value(store, owner, "phone", value, phone_forms(value))
        for value in party.emails:
            _sheet_value(store, owner, "email", value, email_forms(value))
        for record in party.accounts:
            head = _sheet_value(store, owner, "account", record["number"],
                                account_forms(record["number"]))
            head.notes = record.get("institution", "")
        for record in party.cards:
            head = _sheet_value(store, owner, "card", record["number"],
                                account_forms(record["number"]))
            head.notes = record.get("institution", "")
        for value in party.handles:
            category = "payment_handle" if value.startswith("$") else "social_handle"
            _sheet_value(store, owner, "handle", value, handle_forms(value), category)
        for record in party.dl:
            _sheet_value(store, owner, "dl", record["number"], id_forms(record["number"]))
        for value in party.passport:
            _sheet_value(store, owner, "passport", value, id_forms(value))
        for record in party.vehicles:
            head = None
            for text, category in ((record.get("vin", ""), "vin"),
                                   (record.get("plate", ""), "license_plate")):
                if not text:
                    continue
                if head is None:
                    head = _sheet_value(store, owner, "vehicle", text, id_forms(text), category)
                else:
                    for form in id_forms(text):
                        add_form(store, head, form, category)
        for value in party.case_numbers:
            _sheet_value(store, owner, "case_number", value, id_forms(value))
        for value in party.parcels:
            _sheet_value(store, owner, "parcel", value, id_forms(value))
        for kind, values in (("employer", party.employers), ("business", party.businesses)):
            for value in values:
                swapped = surname_swapped(value, invented)
                _sheet_value(store, owner, kind, value, [], replacement=swapped)
        for value in party.other:
            _sheet_value(store, owner, "other", value, id_forms(value))
    settle(store)
    return store


def family_invented(store: MappingStore) -> dict[str, str]:
    """Each family surname, casefolded -> the family's invented surname."""
    out = {}
    for entity in store.entities.values():
        if entity.kind == "family_surname" and entity.surrogate is not None:
            out[entity.canonical.casefold()] = entity.surrogate.last
    return out


def surname_swapped(text: str, invented: dict[str, str]) -> str | None:
    """"Smith Landscaping LLC" -> "Tolk Landscaping LLC", or None when the
    family's surname is not in it."""
    changed = False
    out = []
    for word in text.split():
        bare = re.sub(r"(?i)['’]s$", "", word).strip(".,'’")
        fake = next((f for real, f in invented.items()
                     if len(bare) >= 3 and _names.alike(bare, real)), None)
        if fake:
            at = word.find(bare)
            out.append(word[:at] + _names.match_case(bare, fake) + word[at + len(bare):])
            changed = True
        else:
            out.append(word)
    return " ".join(out) if changed else None


def settle(store: MappingStore) -> None:
    """Every form of a party's name wears the party's invented name in its
    own shape: a former name keeps the party's invented given names, and a
    former surname its own invented surname."""
    for entity in store.entities.values():
        if entity.kind == "family_surname" and entity.parent is None \
                and entity.surrogate is not None:
            # the row reads as what it becomes: "Smith" -> "Tolk"
            entity.replacement = entity.surrogate.last
    for entity in store.entities.values():
        if not (entity.fixed and entity.is_person and entity.parent):
            continue
        head = grouping.head_of(store, entity)
        if head.surrogate is None or entity.person is None or head.person is None:
            continue
        if entity.source == FORM_SOURCE:
            entity.surrogate = _found_surrogate(entity.person, head)
            entity.replacement = entity.surrogate.canonical
            continue
        mine = entity.surrogate
        own_last = (mine.last if mine is not None and mine.last
                    and not _names.alike(entity.person.last, head.person.last)
                    else head.surrogate.last)
        pieces = _shaped_pieces(entity.person, head, own_last)
        entity.surrogate = pieces
        entity.replacement = pieces.canonical


def _shaped_pieces(person: _names.PersonName, head: Entity,
                   own_last: str) -> _names.PersonName:
    fake = head.surrogate
    real = head.person
    middles = []
    for i, middle in enumerate(person.middles):
        target = fake.middles[i] if i < len(fake.middles) else (fake.middles[-1] if fake.middles
                                                                  else fake.first)
        middles.append(target[:1].upper() + "." if len(middle.strip(".")) == 1 else target)
    first = fake.first if person.first else ""
    if person.first and real.first and len(person.first.strip(".")) == 1:
        first = fake.first[:1].upper() + "."
    last = own_last if person.last else ""
    if person.last and not person.first and real.first \
            and _names.same_given(person.last, real.first):
        last = fake.first                 # a lone first name, parsed as a surname
    return _names.PersonName(raw=" ".join(p for p in (first, *middles, last) if p),
                             first=first, middles=tuple(middles), last=last,
                             suffix=person.suffix)


def add_name_form(store: MappingStore, owner: str, text: str, why: str = "") -> Entity | None:
    """A form of a party's name found in the documents - a misreading, a
    nickname - put under the party's row and given its invented name."""
    head = next((e for e in store.entities.values()
                 if e.fixed and e.kind in ("name", "family_surname") and e.parent is None
                 and e.party == owner), None)
    if head is None or head.person is None:
        return None
    parsed = _names.parse(text)
    if not parsed.canonical:
        return None
    key = f"{head.category}:{parsed.canonical.casefold()}"
    if key in store.entities:
        return store.entities[key]
    entity = Entity(key=key, category=head.category, canonical=parsed.canonical,
                    replacement=head.replacement, role=owner, source="party form",
                    person=parsed, variants=_names.variants(parsed), parent=head.key,
                    fixed=True, party=owner, kind=head.kind, group_note=why)
    _trim_variants(entity)
    entity.surrogate = _found_surrogate(parsed, head)
    entity.replacement = entity.surrogate.canonical
    store.entities[key] = entity
    return entity


def _found_surrogate(parsed: _names.PersonName, head: Entity) -> _names.PersonName:
    """Each word of a found form as the head's matching invented word."""
    fake, real = head.surrogate, head.person
    if fake is None or real is None:
        return parsed
    lookup = dict(fake.ordered)
    pieces = {}
    for key, word in parsed.ordered:
        target = None
        for real_key, real_word in real.ordered:
            if _names.alike(word, real_word) or _names.same_given(word, real_word):
                target = lookup.get(real_key) or fake.last
                break
        if target is None:
            joined = _names._ocr_folded(word)
            if joined == _names._ocr_folded(real.first + real.last):
                target = fake.first + fake.last
        pieces[key] = target or lookup.get(key) or fake.last
    middles = tuple(pieces[k] for k, _w in parsed.ordered if k.startswith("middle"))
    return _names.PersonName(raw=" ".join(pieces.values()), first=pieces.get("first", ""),
                             middles=middles, last=pieces.get("last", ""))


# --------------------------------------------------------------------------
# a value the detectors found: is it a party's?
# --------------------------------------------------------------------------


def heads(store: MappingStore, kinds) -> list[Entity]:
    kinds = {kinds} if isinstance(kinds, str) else set(kinds)
    return [e for e in store.entities.values()
            if e.fixed and e.parent is None and e.kind in kinds and e.party]


def _alnum(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", text.casefold())


def _ocr_same(a: str, b: str, longer_than: int = 8) -> bool:
    """One identifier, allowing OCR's confusions only in a long one."""
    x, y = _alnum(a), _alnum(b)
    if x == y:
        return bool(x)
    if min(len(x), len(y)) < longer_than:
        return False
    fx, fy = _names._ocr_folded(x), _names._ocr_folded(y)
    return fx == fy or (fx[:1] == fy[:1] and len(fx) >= 10 and _names._one_edit(fx, fy))


_MASKED = re.compile(r"[xX*•#]|\.\.")


def _tail(value: str) -> str | None:
    """The last four of a masked or four-digit account, else None."""
    digits = _digits(value)
    if len(digits) >= 4 and (_MASKED.search(value) or len(digits) <= 6):
        return digits[-4:]
    return None


def fold(store: MappingStore, kind: str, value: str, line: str = "") -> Entity | None:
    """The party row ``value`` is another printed form of, or None."""
    if kind in ("account", "card"):
        digits = _digits(value)
        tail = _tail(value)
        found = []
        for head in heads(store, ("account", "card")):
            theirs = _digits(head.canonical)
            if tail is not None:
                if theirs[-4:] == tail:
                    found.append(head)
            elif len(digits) >= 6 and (theirs == digits or theirs.lstrip("0") == digits.lstrip("0")
                                       or (len(theirs) > len(digits) and theirs.endswith(digits))):
                found.append(head)
        if len(found) > 1 and line:
            # two accounts with one last four: the institution named on the
            # line says which
            named = [h for h in found if h.notes and h.notes.casefold() in line.casefold()]
            found = named or found
        return found[0] if found else None
    if kind == "phone":
        key = grouping._phone_key(value)
        return next((h for h in heads(store, "phone")
                     if key and grouping._phone_key(h.canonical) == key), None)
    if kind in ("ssn", "tin"):
        digits = _digits(value)
        pool = heads(store, ("ssn", "tin"))
        if len(digits) == 9:
            return next((h for h in pool if _digits(h.canonical) == digits), None)
        if len(digits) == 4:
            owners_ = [h for h in pool if _digits(h.canonical).endswith(digits)]
            return owners_[0] if len(owners_) == 1 else None
        return None
    if kind == "dob":
        found = parse_date(value)
        return next((h for h in heads(store, "dob") if found and parse_date(h.canonical) == found),
                    None)
    if kind == "address":
        return _fold_address(store, value)
    if kind == "email":
        key = grouping._email_key(value)
        return next((h for h in heads(store, "email")
                     if grouping._email_key(h.canonical) == key
                     or (len(key) >= 10 and _names._ocr_folded(key)
                         == _names._ocr_folded(grouping._email_key(h.canonical)))), None)
    if kind == "handle":
        return next((h for h in heads(store, "handle")
                     if _ocr_same(value.lstrip("@$"), h.canonical.lstrip("@$"))), None)
    if kind in ("dl", "passport", "vehicle", "case_number", "parcel", "other"):
        pool = heads(store, kind)
        for head in pool:
            texts = [head.canonical] + [m.canonical for m in grouping.members(store, head)]
            if any(_ocr_same(value, t) for t in texts):
                return head
    return None


def _fold_address(store: MappingStore, value: str) -> Entity | None:
    found = grouping.parse_address(value)
    if found is None:
        return None
    zip4 = re.fullmatch(r"\s*(\d{5}-\d{4})\s*", value)
    for head in heads(store, "address"):
        theirs = grouping.parse_address(head.canonical)
        if theirs is None:
            continue
        if zip4:
            if zip4.group(1) in head.canonical:
                return head
            continue
        if found.box:
            if found.box == theirs.box and grouping._same_place(found, theirs):
                return head
            continue
        if not found.number:
            continue                     # a city line alone: see _city_lines
        if not grouping._same_street(found, theirs) or not grouping._same_place(found, theirs):
            continue
        # the same building: a different unit is somebody else's door, the
        # bare street line is this one
        if found.unit and found.unit != theirs.unit:
            continue
        return head
    return None


# ------------------------------------------------------------ anchoring --

HEADER_LINES = 15
BLOCK_LINES = 4
_HEADER_LABEL = re.compile(
    r"(?i)\b(?:account\s+holders?|account\s+(?:number|no\.?|#)|acct\.?\s*(?:number|no\.?|#)"
    r"|account\s+ending|card\s+(?:ending|number|no\.?|#)|statement\s+for)")
# a masked tail is the parties' other account only where money moves
_TRANSFER_LINE = re.compile(
    r"(?i)\b(?:transfer|xfer|trnsfr|tfr|zelle|venmo|cash\s?app|paypal|withdrawal|deposit|"
    r"payment|pmt|ach|online|mobile|to|from|card|acct|account|checking|savings)\b")
# ...and never on a line about what is left as written: a credit report's
# loan and mortgage tails, a policy, a claim, a member number
_LEAVE_LINE = re.compile(
    r"(?i)\b(?:loans?|mortgages?|heloc|polic(?:y|ies)|claims?|members?(?:ship)?|lease|lien|"
    r"escrow|student|auto\s+fin(?:ance)?)\b")
# A statement's activity: a row that opens on a date with no year ("12/03",
# "Jan 20") - the period and closing dates in the header carry theirs - and
# the heading over the rows.
_ACTIVITY_ROW = re.compile(
    r"^\s*(?:\d{1,2}/\d{1,2}(?![/\-.]?\d)|(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)"
    r"[a-z]*\.?[^\S\n]+\d{1,2}(?!\d)(?!,?[^\S\n]*\d{4}))", re.IGNORECASE)
_ACTIVITY_HEADING = re.compile(
    r"^\s*(?:(?:account\s+)?activity|transactions?(?:\s+(?:history|detail))?|"
    r"date\s+description|deposits(?:\s+and\s+(?:other\s+)?credits)?|withdrawals|"
    r"purchases|payments\s+and\s+credits)\b", re.IGNORECASE)
_LOCKBOX_OR_BANK = re.compile(
    r"(?i)\b(?:p\.?\s?o\.?\s+box|post\s+office\s+box|lockbox|bank|bancorp|credit\s+union|"
    r"n\.a\.|mortgage|servicing|lending|insurance|financial|card\s+services|remit)\b")


class Context:
    """Where the parties are named in one piece of text, for anchoring."""

    def __init__(self, text: str, store: MappingStore, person_hits=(), page=None) -> None:
        self.text = text
        self.store = store
        self.page = PAGE.get() if page is None else page
        self.starts = [0] + [m.end() for m in re.finditer("\n", text)]
        self.lines = text.split("\n")
        self._header_end: int | None = None
        self.owners: dict[int, set[str]] = {}
        surnames = {(e.person.last or "").casefold() for e in store.entities.values()
                    if e.fixed and e.kind == "name" and e.person is not None}
        for hit in person_hits:
            entity = store.entities.get(hit.entity_key)
            if entity is None or not entity.is_person:
                continue
            head = grouping.head_of(store, entity)
            if not head.party:
                continue
            owner = head.party
            if head.kind == "family_surname" or (
                    " " not in hit.text.strip() and hit.text.strip(".,'’s").casefold() in surnames):
                owner = "parties"        # a surname alone is the family's, not one person's
            self.owners.setdefault(self.line_of(hit.start), set()).add(owner)

    def line_of(self, at: int) -> int:
        lo, hi = 0, len(self.starts) - 1
        while lo < hi:
            mid = (lo + hi + 1) // 2
            if self.starts[mid] <= at:
                lo = mid
            else:
                hi = mid - 1
        return lo

    def line(self, at: int) -> str:
        return self.lines[self.line_of(at)]

    @staticmethod
    def _one(found: set[str]) -> str:
        # a full name says whose it is; the family surname beside it (or a
        # bare one alone) says only that it is the family's
        specific = {o for o in found if o != "parties"}
        return next(iter(specific)) if len(specific) == 1 else "parties"

    def header_end(self) -> int:
        """Where page 1's header block stops: fifteen lines, or the first
        line of activity - a row opening on a date with no year, or the
        activity's heading - if that comes sooner."""
        if self._header_end is None:
            self._header_end = HEADER_LINES
            for index, line in enumerate(self.lines[:HEADER_LINES]):
                if _ACTIVITY_ROW.match(line) or _ACTIVITY_HEADING.match(line):
                    self._header_end = index
                    break
        return self._header_end

    def anchor(self, start: int, kind: str) -> str | None:
        """The owner the value at ``start`` is anchored to, or None."""
        index = self.line_of(start)
        if index in self.owners:
            return self._one(self.owners[index])
        # the block under a name is an address block, a letterhead, a
        # header - never a statement's activity: a merchant's phone three
        # rows under the holder's name is the merchant's
        if not _ACTIVITY_ROW.match(self.lines[index]):
            for step in range(1, BLOCK_LINES + 1):
                above = index - step
                if above < 0 or not self.lines[above].strip() \
                        or _ACTIVITY_ROW.match(self.lines[above]):
                    break
                if above in self.owners:
                    return self._one(self.owners[above])
        header = self.page == 0 and index < self.header_end()
        labelled = kind in ("account", "card") and (
            _HEADER_LABEL.search(self.lines[index])
            or (index and _HEADER_LABEL.search(self.lines[index - 1])))
        if kind == "address" and _LOCKBOX_OR_BANK.search(self.lines[index]):
            header = False               # the bank's own address, not the holder's
        if header or labelled:
            found = set().union(*(self.owners.get(i, set())
                                  for i in range(self.header_end()))) \
                if self.page == 0 else set()
            return self._one(found) if found else "parties"
        return None


def file_match(store: MappingStore, match, context: Context) -> Entity | None:
    """Register a detector's find only when it is a party's - an exact form,
    a fold into a party's row, an anchored value - or one of the two kinds
    replaced wherever they are found. None leaves it as written."""
    kind = KIND_OF.get(match.category)
    if match.category in LEAVE or kind is None:
        return None
    value = match.text
    if match.bare:
        _left(store, kind, value)
        return None
    line = context.line(match.start)
    head = fold(store, kind, value, line)
    if head is not None:
        return add_form(store, head, value, match.category, source="pattern")
    if kind in ("account", "card") and _LEAVE_LINE.search(line):
        _left(store, kind, value)                  # a loan's, a policy's: as written
        return None
    if kind == "address":
        parsed = grouping.parse_address(value)
        if parsed is not None and not parsed.number and not parsed.box:
            # "OGDEN UT 84403-7079" alone is an address only directly under
            # the street it finishes - and then it is that street's
            index = context.line_of(match.start)
            above = context.lines[index - 1] if index else ""
            street = fold(store, "address", above.strip()) if above.strip() else None
            if street is not None:
                return add_form(store, street, value, match.category, source="pattern")
            _left(store, kind, value)
            return None
    owner = context.anchor(match.start, kind)
    if owner is None and kind == "ssn" and (
            patterns.valid_ssn(value) or len(_digits(value)) == 4):
        owner = "parties"        # a third party's SSN in a family case is near unheard of
    if owner is None and kind == "account" and _tail(value) and _TRANSFER_LINE.search(line):
        owner = "parties"        # the far end of a transfer is the parties' other account
    if owner is None:
        _left(store, kind, value)
        return None
    if kind in ("account", "card"):
        forms = account_forms(value)
    elif kind == "phone":
        forms = phone_forms(value) if len(grouping._phone_key(value)) == 10 else []
    elif kind == "ssn" and len(_digits(value)) == 9:
        forms = ssn_forms(value)
    elif kind == "dob":
        forms = dob_forms(value)
    else:
        forms = []
    head = _new_head(store, owner, kind, match.category, value, [], source="pattern")
    for form in forms:
        add_form(store, head, form, match.category)
    return head


def file_address_line(store: MappingStore, value: str) -> Entity | None:
    """A line of a mailing-address block: registered only as a form of a
    party's address."""
    head = fold(store, "address", value)
    return add_form(store, head, value, "street_address", source="address block") \
        if head is not None else None


def _left(store: MappingStore, kind: str, value: str) -> None:
    left = getattr(store, "left_as_written", None)
    if left is None:
        store.left_as_written = left = {}
    left.setdefault(kind, set()).add(" ".join(value.split()).casefold())


# --------------------------------------------------------------------------
# the hits only the sheet can make
# --------------------------------------------------------------------------

_SSN_LABEL = r"(?:ssn|s\.s\.n\.?|social\s+security(?:\s+(?:no\.?|number|#))?|soc\.?\s*sec\.?)"
_TAIL_LABEL = (r"(?:ending(?:\s+in)?|last\s*(?:4|four)(?:\s*digits?)?|acct\.?|account|card"
               r"|x)")
_DIGIT_RUN = re.compile(r"(?<![\d\-.,/])\d{6,}(?![\d\-/])(?![.,]\d)")
_PIECE_WORD = re.compile(r"(?<![A-Za-z0-9|!$'’])[A-Za-z0-9|!$][A-Za-z0-9|!$'’\-]{2,}"
                         r"(?![A-Za-z0-9|!$'’])")
PIECE_PRIORITY = 66                     # engine.FUZZY_PRIORITY: below every exact form


def extra_hits(text: str, store: MappingStore, redact: bool, protected, hits) -> list:
    """Hits for what the literal pass cannot find on its own: a party's tail
    after its label ("SSN ending 9963", "card ending in 4417"), a known
    account's digits written bare, a party's name piece misread by OCR, and
    the city line directly under a party's street."""
    from .engine import Hit, REDACTION_TEXT, VALUE_LITERAL_PRIORITY

    out = []

    def hit(start, end, head, replacement=None, priority=VALUE_LITERAL_PRIORITY):
        if any(start < e and s < end for s, e in protected):
            return
        out.append(Hit(start, end, text[start:end], head.category,
                       REDACTION_TEXT if redact else (replacement or head.replacement),
                       head.key, priority))

    ssn_heads = [h for h in heads(store, ("ssn", "tin")) if h.enabled]
    if ssn_heads and re.search(r"(?i)ss|soc", text):
        for head in ssn_heads:
            tail = _digits(head.canonical)[-4:]
            for m in re.finditer(rf"(?i)\b{_SSN_LABEL}[^\n\d]{{0,25}}?(?<!\d)({tail})(?!\d)",
                                 text):
                hit(m.start(1), m.end(1), head)
    money = [h for h in heads(store, ("account", "card")) if h.enabled]
    if money:
        for head in money:
            digits = _digits(head.canonical)
            tail = digits[-4:]
            if tail not in text:
                continue
            for m in re.finditer(
                    rf"(?i)\b{_TAIL_LABEL}\.?(?:\s*(?:no\.?|number|#))?\s*[:#\-]?\s*"
                    rf"(?<!\d)({tail})(?![\d\-/])(?![.,]\d)", text):
                hit(m.start(1), m.end(1), head)
        known = [(h, _digits(h.canonical)) for h in money if len(_digits(h.canonical)) >= 6]
        if known:
            for m in _DIGIT_RUN.finditer(text):
                run = m.group(0)
                for head, digits in known:
                    if run == digits or (len(digits) > len(run) and digits.endswith(run)) \
                            or run.lstrip("0") == digits.lstrip("0"):
                        hit(m.start(), m.end(), head)
                        break
    out += _misread_pieces(text, store, redact, protected)
    out += _city_lines(text, store, redact, protected, hits)
    return out


def _pieces(store: MappingStore) -> list[tuple[str, str, str, Entity]]:
    """(piece, folded, invented piece, head) for every searchable word of a
    ticked party's name and the family surname."""
    out = []
    for entity in store.entities.values():
        if not (entity.fixed and entity.is_person and entity.enabled
                and entity.person is not None and entity.surrogate is not None):
            continue
        fake = dict(entity.surrogate.ordered)
        for key, word in entity.person.ordered:
            if not _searchable_piece(word) or len(word.strip(".")) < 4:
                continue
            invented = fake.get(key) or entity.surrogate.last
            out.append((word.strip("."), _names._ocr_folded(word), invented, entity))
    return out


def _misread_pieces(text: str, store: MappingStore, redact: bool, protected) -> list:
    """A word OCR made of a party's name piece - "Harr1ngton", "HARRlNGTON",
    "Smlth" - replaced by the party's invented piece. The exact spellings are
    the matcher's; this only adds what the OCR folding says is the same."""
    from .engine import Hit, REDACTION_TEXT

    pieces = _pieces(store)
    if not pieces:
        return []
    exact = {p[0].casefold() for p in pieces}
    out = []
    for m in _PIECE_WORD.finditer(text):
        word = m.group(0).strip("'’-")
        if not any(ch.isalpha() for ch in word) or word.casefold() in exact:
            continue
        if any(m.start() < e and s < m.end() for s, e in protected):
            continue
        left = text.rfind(" ", 0, m.start()) + 1
        right = text.find(" ", m.end())
        chunk = text[left:right if right >= 0 else len(text)]
        if "@" in chunk or "://" in chunk or "www." in chunk.casefold():
            continue
        folded = _names._ocr_folded(word)
        for piece, piece_folded, invented, entity in pieces:
            if folded[:1] != piece_folded[:1] or abs(len(folded) - len(piece_folded)) > 1:
                continue
            if not _names.alike(word, piece):
                continue
            # an everyday word is a name only with its capital
            if (_names.ordinary_word(piece) or _names.ordinary_word(word)) \
                    and not word[:1].isupper():
                break
            if _names.ordinary_word(word) and word.casefold() != piece.casefold():
                break                    # "Mills" is not a misread "Miles"
            start = m.start() + m.group(0).find(word)
            out.append(Hit(start, start + len(word), word, entity.category,
                           REDACTION_TEXT if redact else _names.match_case(word, invented),
                           entity.key, PIECE_PRIORITY))
            break
    return out


def _city_lines(text: str, store: MappingStore, redact: bool, protected, hits) -> list:
    """"LAYTON UT 84041" alone is a party's only directly under their street."""
    from .engine import Hit, REDACTION_TEXT, VALUE_LITERAL_PRIORITY

    out = []
    for found in hits:
        entity = store.entities.get(found.entity_key)
        if entity is None:
            continue
        head = grouping.head_of(store, entity)
        if head.kind != "address" or not head.fixed:
            continue
        line_end = text.find("\n", found.end)
        if line_end < 0 or text[found.end:line_end].strip():
            continue
        next_end = text.find("\n", line_end + 1)
        next_end = len(text) if next_end < 0 else next_end
        raw = text[line_end + 1:next_end]
        line = raw.strip()
        if not line:
            continue
        city = grouping.parse_address(line)
        theirs = grouping.parse_address(head.canonical)
        if city is None or theirs is None or city.number or city.box or not city.zip5:
            continue
        if city.zip5 != theirs.zip5 or not grouping._same_place(city, theirs):
            continue
        start = line_end + 1 + raw.find(line)
        if any(start < e and s < start + len(line) for s, e in protected):
            continue
        out.append(Hit(start, start + len(line), line, head.category,
                       REDACTION_TEXT if redact else head.replacement, head.key,
                       VALUE_LITERAL_PRIORITY))
    return out


# --------------------------------------------------------------------------
# what the documents call the parties - the name screen's suggestions
# --------------------------------------------------------------------------

_WORD = re.compile(r"(?<![A-Za-z0-9])[A-Za-z][A-Za-z0-9|!$'’\-]{1,}(?![A-Za-z0-9])")


def party_forms(texts: dict[str, str], store: MappingStore) -> list[dict]:
    """Forms of the parties' names, and the family surname, the documents
    use and the sheet does not spell out: OCR misreadings, glued and run
    together, nicknames, the former names, the surname inside a business or
    on a relative. One entry per (party, written form)."""
    from . import engine

    found: dict[tuple[str, str], dict] = {}

    def add(owner, text, why, document, ticked):
        key = (owner, text.casefold())
        entry = found.get(key)
        if entry is None:
            entry = found[key] = {"party": owner, "text": text, "kind": "name", "why": why,
                                  "count": 0, "documents": set(), "preticked": ticked}
        entry["count"] += 1
        entry["documents"].add(document)

    people = [e for e in store.entities.values()
              if e.fixed and e.kind == "name" and e.parent is None and e.person is not None]
    formers = [e for e in store.entities.values()
               if e.fixed and e.kind == "name" and e.parent and e.person is not None]
    surnames = [e for e in store.entities.values() if e.kind == "family_surname"]
    pieces = [(word.strip("."), key, head, _names._ocr_folded(word)) for head in people
              for key, word in head.person.ordered if _searchable_piece(word)]
    exact = {w.casefold() for w, _k, _h, _f in pieces}
    exact |= {e.canonical.casefold() for e in surnames}
    parts = engine._person_parts(store)
    nick_of: dict[str, list[Entity]] = {}
    for head in people:
        first = head.person.first.strip(".").casefold()
        for nickname, formals in _names._BY_NICKNAME.items():
            if first in formals:
                nick_of.setdefault(nickname, []).append(head)
    for document, text in texts.items():
        for former in formers:
            for m in re.finditer(rf"(?i)(?<![A-Za-z]){re.escape(former.canonical)}(?![A-Za-z])",
                                 text):
                add(former.party, m.group(0), "former name", document, True)
        for surname in surnames:
            # capitalised words either side - a business, a relative - the
            # surname itself in any case
            rx = rf"(?<![A-Za-z])((?:[A-Z][\w&'’\-]*[^\S\n]+){{0,2}}" \
                 rf"(?i:{re.escape(surname.canonical)})" \
                 rf"(?:[^\S\n]+[A-Z][\w&'’\-]*){{0,3}})(?![A-Za-z])"
            for m in re.finditer(rx, text):
                # "Derek L Nakamura Account" is the holder and a heading word:
                # the word off, it is Derek's own name and nothing to offer
                phrase = _names.without_tail(" ".join(m.group(1).split()))
                words = phrase.split()
                if len(words) < 2:
                    continue
                if any(_form_of(h, phrase) for h in people):
                    continue                      # a party's own name, not a relative's
                if _names.is_business(phrase) or _a_relative(words, surname.canonical):
                    add("parties", phrase, "family surname", document, True)
        for m in _WORD.finditer(text):
            word = m.group(0).strip("'’-")
            folded = word.casefold()
            if len(word) < 3 or folded in exact:
                continue
            owner = None
            if folded in nick_of and word[:1].isupper() and len(nick_of[folded]) == 1:
                add(nick_of[folded][0].party, word, "nickname", document, False)
                continue
            folded_word = _names._ocr_folded(word)
            for piece, key, head, piece_folded in pieces:
                # a cheap first cut: alike keeps the first letter and the
                # length within one
                if folded_word[:1] != piece_folded[:1] \
                        or abs(len(folded_word) - len(piece_folded)) > 1:
                    continue
                if len(piece) >= 4 and _names.alike(word, piece) and word[:1].isupper() \
                        and not _names.ordinary_word(word):
                    owner = head.party
                    if key == "last" and sum(1 for p in people if p.person.last
                                             and _names.alike(p.person.last, piece)) > 1:
                        owner = "parties"
                    add(owner, word, "OCR misreading", document, True)
                    break
            if owner is None and len(word) >= 6:
                # the whole word one party's name run together - not with a
                # payment line's "to" or "Conf", which the run's own pass takes
                glued = [h for h in people if _form_of(h, word)]
                if len(glued) == 1:
                    add(glued[0].party, word, "glued", document, True)
    out = []
    for entry in found.values():
        entry["documents"] = sorted(entry["documents"])
        out.append(entry)
    out.sort(key=lambda e: (_owner_rank(e["party"]), -e["count"], e["text"].casefold()))
    for entry in out:
        entry["party"] = api_owner(entry["party"])
    return out


def _a_relative(words: list[str], surname: str) -> bool:
    """"Mary Smith": a given name and the family surname, nothing else."""
    from . import ner

    if len(words) != 2:
        return False
    _common, given = ner._lists()
    first, last = words
    return _names.alike(last, surname) and first.strip(".").casefold() in given


def add_found_forms(store: MappingStore, forms) -> int:
    """The ticked suggestions from the name screen, each under its party.
    ``forms`` is [{"party", "text"}] or [(text, category)] off the old list."""
    added = 0
    for item in forms or []:
        if isinstance(item, dict):
            owner = owner_from_api(item.get("party", ""))
            text = str(item.get("text") or item.get("name") or "")
        else:
            owner, text = "", str(item[0])
        text = " ".join(text.split())
        if not text:
            continue
        owner = owner or owner_of_name(store, text) or ""
        if owner == "parties":
            owner = next((e.party for e in store.entities.values()
                          if e.kind == "family_surname"), "")
            entity = _family_form(store, text) if owner else None
        else:
            entity = add_name_form(store, owner, text) if owner else None
        if entity is None and not isinstance(item, dict):
            # a name typed that is nobody on the sheet: the operator asked
            entity = store.add_person(text, category=item[1] if item[1] in ("person", "minor")
                                      else "person", source="name-list")
        if entity is not None:
            added += 1
    return added


def _family_form(store: MappingStore, text: str) -> Entity | None:
    """"Smith Landscaping LLC", "Mary Smith": the family surname in it swapped."""
    invented = family_invented(store)
    swapped = surname_swapped(text, invented)
    if swapped is None:
        return None
    head = next(e for e in store.entities.values() if e.kind == "family_surname")
    entity = add_form(store, head, text, "organization", source="party form")
    if entity is not None:
        entity.replacement = swapped
    return entity


def owner_of_name(store: MappingStore, text: str) -> str | None:
    """Which party a written name is a form of, or None."""
    parsed = _names.parse(text)
    words = [w for _k, w in parsed.ordered]
    if not words:
        return None
    candidates = set()
    for head in store.entities.values():
        if not (head.fixed and head.kind == "name" and head.person is not None):
            continue
        real = [w for _k, w in head.person.ordered]
        if all(any(_names.alike(w, r) or _names.same_given(w, r) for r in real) for w in words):
            candidates.add(head.party)
    if len(candidates) == 1:
        return next(iter(candidates))
    return "parties" if candidates else None


# --------------------------------------------------------------------------
# the review list
# --------------------------------------------------------------------------


def _owner_rank(owner: str) -> tuple:
    role, _, number = owner.partition("-")
    return (_ROLE_ORDER.get(role, 5), int(number) if number.isdigit() else 0)


def owner_label(owner: str) -> str:
    if not owner:
        return ""
    role, _, number = owner.partition("-")
    return f"{role.title()} {number}".strip() if number else role.title()


def kind_of(entity: Entity) -> str:
    if entity.kind:
        return entity.kind
    if entity.is_person:
        return "name"
    return {"organization": "business", "employer": "employer"}.get(
        entity.category, KIND_OF.get(entity.category, "other"))


def groups(store: MappingStore) -> list[dict]:
    """One row per party per value - see the review list in webapp.py."""
    out = []
    numbered: dict[tuple[str, str], int] = {}
    order = {key: index for index, key in enumerate(store.entities)}
    heads_ = [e for e in store.entities.values() if e.parent is None]
    # the sheet's order, then the order they were found in
    heads_.sort(key=lambda e: (_owner_rank(e.party) if e.party else (9, 0),
                               _KIND_ORDER.index(kind_of(e)) if kind_of(e) in _KIND_ORDER else 99,
                               order[e.key]))
    for head in heads_:
        kind = kind_of(head)
        owner = head.party
        members = grouping.members(store, head)
        shown = [m for m in members if m.occurrences or m.source != FORM_SOURCE]
        if kind == "family_surname":
            text = f"Family surname - {head.canonical}"
        else:
            what = KIND_LABEL.get(kind, kind)
            if kind not in SINGLE and kind not in ("name",):
                numbered[(owner, kind)] = numbered.get((owner, kind), 0) + 1
                if kind in ("account", "card"):
                    tail = _digits(head.canonical)[-4:]
                    what += f" ****{tail}" + (f" ({head.notes})" if head.notes else "")
                else:
                    what += f" {numbered[(owner, kind)]}"
            text = f"{owner_label(owner)} - {what}" if owner else what[:1].upper() + what[1:]
        out.append({
            "key": head.key, "party": api_owner(owner), "kind": kind, "label": text,
            "replacement": head.replacement, "count": grouping.group_total(store, head),
            "enabled": head.enabled,
            # the head's own text is the row itself: unticking the row
            # (PATCH its key) carries every form, as grouping.set_enabled does
            "text": head.canonical,
            "forms": [{"key": e.key, "text": e.canonical, "count": e.occurrences,
                       "enabled": e.enabled} for e in shown],
        })
    return out


def left_counts(store: MappingStore) -> dict[str, int]:
    return {kind: len(values)
            for kind, values in sorted((getattr(store, "left_as_written", None) or {}).items())}
