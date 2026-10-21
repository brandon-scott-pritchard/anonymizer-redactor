"""Every printed form of a party's value - the app's own generators.

Lifted from the Anonymizer's ``redactor/parties.py`` (ssn_forms ... id_forms) and
``redactor/grouping.py`` (parse_address and its word lists) without change,
because those modules import the encrypted mapping store. The leak checker
searches anonymized files for exactly the forms the app replaces, and more;
tests/test_party_pii_skill.py checks these still match the app's.
"""

from __future__ import annotations

import re
from datetime import date, datetime

from . import names

_DIRECTIONS = {"north": "n", "south": "s", "east": "e", "west": "w", "northeast": "ne",
               "northwest": "nw", "southeast": "se", "southwest": "sw",
               "n": "n", "s": "s", "e": "e", "w": "w", "ne": "ne", "nw": "nw",
               "se": "se", "sw": "sw"}


_SUFFIXES = {"street": "st", "st": "st", "avenue": "ave", "ave": "ave", "av": "ave",
             "road": "rd", "rd": "rd", "drive": "dr", "dr": "dr", "lane": "ln", "ln": "ln",
             "boulevard": "blvd", "blvd": "blvd", "court": "ct", "ct": "ct",
             "circle": "cir", "cir": "cir", "place": "pl", "pl": "pl", "way": "way",
             "wy": "way", "parkway": "pkwy", "pkwy": "pkwy", "highway": "hwy", "hwy": "hwy",
             "terrace": "ter", "ter": "ter", "trail": "trl", "trl": "trl", "loop": "loop",
             "square": "sq", "sq": "sq", "plaza": "plz", "plz": "plz", "bend": "bnd",
             "row": "row", "run": "run", "crossing": "xing", "xing": "xing",
             "landing": "lndg", "lndg": "lndg", "trace": "trce", "trce": "trce",
             "mews": "mews", "alley": "aly", "route": "rte", "rte": "rte"}


_UNIT_WORDS = {"apt", "apartment", "unit", "suite", "ste", "bldg", "building", "fl",
               "floor", "rm", "room", "lot", "spc", "space"}


_STATES = {"al", "ak", "az", "ar", "ca", "co", "ct", "de", "dc", "fl", "ga", "hi", "id", "il",
           "in", "ia", "ks", "ky", "la", "me", "md", "ma", "mi", "mn", "ms", "mo", "mt",
           "ne", "nv", "nh", "nj", "nm", "ny", "nc", "nd", "oh", "ok", "or", "pa", "ri",
           "sc", "sd", "tn", "tx", "ut", "vt", "va", "wa", "wv", "wi", "wy", "utah",
           "idaho", "nevada", "arizona", "wyoming", "colorado"}


def _digits(value: str) -> str:
    return re.sub(r"\D", "", value)


class _Address:
    __slots__ = ("number", "street", "unit", "city", "zip5", "entity", "box")

    def __init__(self, entity: object):
        self.entity = entity
        self.number = self.unit = self.zip5 = self.box = ""
        self.street: tuple[str, ...] = ()
        self.city: tuple[str, ...] = ()


def parse_address(text: str) -> _Address | None:
    """House number, street, unit, city and ZIP, each written one way."""
    # OCR glues an address together: "1734W Gentile St,LaytonUT 84041" is
    # "1734 W Gentile St, Layton UT 84041" - the direction run into the house
    # number, the state into the town
    text = re.sub(r"^\s*(\d+)([NSEW])(?=\s+[A-Za-z])", r"\1 \2", text)
    text = re.sub(r"(?<=[a-z])(?=[A-Z]{2}\s*\d{5}(?:-\d{4})?\s*$)", " ", text)
    raw = text.casefold().replace("#", " # ")
    words = re.findall(r"[a-z0-9#]+(?:-\d+)?", raw)
    while words and words[-1] in {"usa", "us", "states", "united", "america"}:
        words.pop()
    if not words:
        return None
    out = _Address(None)
    # the ZIP and state at the end - but "PO Box 12345" is a box number
    box_only = words[:2] in (["po", "box"], ["p", "o"]) and len(words) <= 4
    if re.fullmatch(r"\d{5}(?:-\d{4})?", words[-1]) and not box_only:
        out.zip5 = words.pop()[:5]
        if words and words[-1] in _STATES:
            words.pop()
    elif (len(words) > 2 and len(words[-1]) == 2 and words[-1] in _STATES
          and words[-2] not in _SUFFIXES and not words[-2].isdigit()):
        words.pop()                 # "Salt Lake City, UT" with no ZIP
    if words[:2] in (["po", "box"], ["p", "o"]):
        number = next((w for w in words[2:4] if w.isdigit()), "")
        out.box = number
        return out if number else None
    if not words or not re.fullmatch(r"\d{1,6}[a-z]?", words[0]):
        # a city line with no street: "LAYTON UT 84041"
        out.city = tuple(words)
        return out if out.zip5 else None
    out.number = words[0]
    rest = words[1:]
    street: list[str] = []
    i = 0
    if rest and rest[0] in _DIRECTIONS:
        street.append(_DIRECTIONS[rest[0]])
        i = 1
    # a grid address: "620 E 1200 S"
    if (len(rest) > i + 1 and rest[i].isdigit() and rest[i + 1] in _DIRECTIONS):
        street += [rest[i], _DIRECTIONS[rest[i + 1]]]
        i += 2
    else:
        # the street type, after at least one word of name
        end = next((j for j in range(i + 1, min(len(rest), i + 6)) if rest[j] in _SUFFIXES),
                   None)
        if end is None:
            end = next((j for j in range(i, len(rest))
                        if rest[j] in _UNIT_WORDS or rest[j] == "#"), len(rest))
            street += rest[i:end]
            i = end
        else:
            street += rest[i:end] + [_SUFFIXES[rest[end]]]
            i = end + 1
            if i < len(rest) and rest[i] in _DIRECTIONS and len(rest) - i <= 3:
                street.append(_DIRECTIONS[rest[i]])        # "MAIN ST N"
                i += 1
    if i < len(rest) and (rest[i] in _UNIT_WORDS or rest[i] == "#"):
        if i + 1 < len(rest):
            out.unit = rest[i + 1]
            i += 2
        else:
            i += 1
    out.street = tuple(street)
    out.city = tuple(rest[i:])
    return out if out.street else None


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
    parsed = parse_address(value)
    if m is None or parsed is None or m["suffix"].casefold() not in _SUFFIXES:
        street = value.split(",")[0].strip()
        return _unique([value, street, value.replace(",", "")])
    number = m["number"]
    suffix_key = _SUFFIXES[m["suffix"].casefold()]
    suffixes = [suffix_key.title(), f"{suffix_key.title()}.", _SUFFIX_FULL.get(suffix_key, "")]
    if m["dir"]:
        key = _DIRECTIONS[m["dir"].casefold()]
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
    if len(bare) >= 6 and (re.search(r"[\d._\-]", bare) or not names.ordinary_word(bare)):
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
