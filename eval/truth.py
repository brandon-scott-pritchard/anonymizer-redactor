"""What must not survive, and what must, derived from a labelled corpus.

The corpus labels are *extraction* labels: a pay stub's JSON says who the
employee is and what the net pay was, because it was written to score a tool
that reads documents, not one that redacts them. So the secrets are assembled
from three places, and a leak found by any of them counts:

1. **The JSON fields that name somebody or something identifying.** The holder
   block of a statement, the employee on a stub, both ends of a Venmo row,
   account and loan numbers, VINs, plates, parcels, policies, handles.
2. **The corpus's own conventions, read off the source text.** Its README
   promises SSNs with a never-issued 9xx prefix and EINs with a 00 prefix, so
   anything in those shapes on the original page is a planted identifier even
   though no JSON field carries it. A 1040's JSON has no SSN at all.
3. **Shapes, swept over the output.** An SSN shape, a masked account tail, an
   ``INDN:`` field with a name still after it. Those need no label to be wrong.

Secrets are pooled per *case*, not per document: a surname taken from one
statement's holder block is checked in every other document of the same
household, because that is how a client's name actually gets out - from the
document where nobody thought to look for it.

What must SURVIVE is just as scored. The README is explicit that over-redaction
is a failure for financial exhibits, so every dollar figure on the original
page is counted, and every merchant the JSON names.
"""

from __future__ import annotations

import json
import re
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path

# --------------------------------------------------------------------------
# what a JSON field means
# --------------------------------------------------------------------------

# keys whose string value is one or more people in the household or their orbit
PERSON_KEYS = {
    "primary", "secondary", "employee", "person", "owner", "patient", "student",
    "subjects", "recipient", "beneficiary", "borrower", "me",
}
# keys that name the *other* end of a transaction on a payment rail
COUNTERPARTY_KEYS = {"frm", "to", "tenant"}
# a structured identifier, checked digit-for-digit
ID_KEYS = {
    "account_number": "account", "routing_number_fake": "routing",
    "loan_no": "account", "policy": "policy", "vin": "vin", "plate": "plate",
    "hin": "vin", "parcel": "parcel", "title_no": "title", "ein": "ein",
}
HANDLE_KEYS = {"user", "tag"}
ADDRESS_KEYS = {"addr", "property"}
ORG_KEYS = {"business", "entity"}
# things that must survive: the merchant a card line names, the institution
MERCHANT_KEYS = {"merchant", "store", "institution", "creditor", "lien"}

# payment-rail and payroll kinds from the statement generator
_P2P_KINDS = {"p2p_in", "p2p_out"}
_PAYROLL_SUBS = {"DIR DEP", "PAYROLL", "DIRECT DEP", "DIR DEP PAYROLL"}

_SPLIT_PEOPLE = re.compile(r"\s+(?:AND|and|&)\s+")
_MASKED = re.compile(r"[xX*•#]{2,}\s?(\d{4})\b")
_NOT_PERSON = re.compile(
    r"(?i)[*]|\.com\b|\b(?:food|pizza|coffee|cafe|grill|market|store|shop|restaurant|"
    r"bakery|bar|pub|tacos?|burgers?|kitchen|diner|deli|"
    r"inc|llc|pllc|corp|co|company|bank|trust|fund|index|services?|"
    r"group|store|market|insurance|law|office|title|county|state|city|dept|"
    r"department|university|univ|school|district|plan|retirement|brokerage|"
    r"savings|checking|joint|individual|account|payroll|steam|ebay|etsy|"
    r"doordash|uber|amazon|lyft|apple|google|paypal|venmo|zelle)\b")

# The corpus conventions (README.txt of every set): SSNs never issued, EINs 00-.
CORPUS_SSN = re.compile(r"\b9\d{2}-\d{2}-\d{4}\b")
CORPUS_EIN = re.compile(r"\b00-\d{7}\b")
EMAIL = re.compile(r"\b[\w.+-]+@[\w-]+(?:\.[\w-]+)+\b")
DEPENDENTS = re.compile(r"(?im)^\s*Dependents\s*\n([^\n]+)")
PHONE = re.compile(r"\(?\b(\d{3})\)?[\s.-]*(\d{3})[\s.-]*(\d{4})\b")


@dataclass
class Person:
    full: str
    first: str
    last: str
    household: bool

    @property
    def forms(self) -> list[tuple[str, str]]:
        """(form, label) pairs. Surname and first name only for the household."""
        out = [(self.full, "full"), (f"{self.first} {self.last}", "first+last")]
        if self.household:
            out.append((self.last, "surname"))
            out.append((self.first, "first"))
        return out


@dataclass
class Secrets:
    people: dict[str, Person] = field(default_factory=dict)
    ids: dict[str, str] = field(default_factory=dict)          # digits -> category
    handles: set[str] = field(default_factory=set)
    phones: set[str] = field(default_factory=set)               # 10 digits
    emails: set[str] = field(default_factory=set)
    addresses: set[str] = field(default_factory=set)            # street line
    zip4: set[str] = field(default_factory=set)
    orgs: dict[str, str] = field(default_factory=dict)          # name -> employer|business
    surnames: set[str] = field(default_factory=set)

    def household_names(self) -> list[str]:
        return sorted(p.full for p in self.people.values() if p.household)


def _norm(text: str) -> str:
    return " ".join(str(text).split())


def _person(raw: str, household: bool) -> Person | None:
    raw = _norm(raw).strip(" ,.")
    if not raw or any(ch.isdigit() for ch in raw) or raw.startswith(("@", "$")):
        return None
    if _NOT_PERSON.search(raw):
        return None
    tokens = [t for t in raw.replace(",", " ").split() if t]
    if len(tokens) < 2:
        return None
    first, last = tokens[0], tokens[-1]
    if len(last) < 2 or len(first) < 2:
        return None
    return Person(raw, first, last, household)


def _people(value: str) -> list[str]:
    """``GERALD W PACKARD AND DIANE M PACKARD``, ``Colby & Jess Morgan``."""
    parts = _SPLIT_PEOPLE.split(_norm(value))
    if len(parts) == 2 and len(parts[0].split()) == 1:
        surname = parts[1].split()[-1]
        parts[0] = f"{parts[0]} {surname}"
    return parts


def _digits(text: str) -> str:
    return re.sub(r"\D", "", str(text))


def _alnum(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z]", "", str(text)).upper()


def _walk(node, parent: dict | None, sink) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if isinstance(value, (str, int)) and not isinstance(value, bool):
                sink(key, value, node)
            _walk(value, node, sink)
    elif isinstance(node, list):
        for item in node:
            _walk(item, parent, sink)


def collect(label: dict, secrets: Secrets, merchants: set[str]) -> None:
    """Fold one JSON label into the case's secrets."""

    def add_person(value: str, household: bool) -> None:
        for part in _people(value):
            person = _person(part, household)
            if person is None:
                continue
            key = person.full.casefold()
            existing = secrets.people.get(key)
            if existing is None or (household and not existing.household):
                secrets.people[key] = person

    def sink(key: str, value, node: dict) -> None:
        if isinstance(value, int):
            value = str(value)
        if not value.strip():
            return
        kind = node.get("kind") if isinstance(node.get("kind"), str) else ""
        if key in PERSON_KEYS:
            add_person(value, household=True)
        elif key in COUNTERPARTY_KEYS:
            add_person(value, household=False)
        elif key == "name" and node.get("type") in {"Sent", "Received", "Request"}:
            add_person(value, household=False)
        elif key == "party":
            if kind in _P2P_KINDS:
                add_person(value, household=False)
            elif kind == "ach_credit" and str(node.get("sub", "")).upper() in _PAYROLL_SUBS:
                secrets.orgs.setdefault(_norm(value), "employer")
        elif key == "employer" and node is label:
            secrets.orgs.setdefault(_norm(value), "employer")
        elif key in ORG_KEYS:
            secrets.orgs.setdefault(_norm(value), "business")
        elif key in ID_KEYS:
            normal = _alnum(value)
            if len(normal) >= 6 and not _filler(normal):
                secrets.ids[normal] = ID_KEYS[key]
        elif key in HANDLE_KEYS and value[:1] in "@$":
            secrets.handles.add(value)
        elif key == "token":
            if "@" in value:
                secrets.emails.add(value.casefold())
            elif len(_digits(value)) == 10:
                secrets.phones.add(_digits(value))
        elif key == "lines":
            for match in PHONE.finditer(value):
                secrets.phones.add("".join(match.groups()))
        elif key == "line1":
            secrets.addresses.add(_norm(value))
        elif key == "zip4" and re.fullmatch(r"\d{5}-\d{4}", value):
            secrets.zip4.add(value)
        elif key in ADDRESS_KEYS:
            street = value.split(",")[0]
            if re.match(r"\d", street):
                secrets.addresses.add(_norm(street))
        elif key in MERCHANT_KEYS:
            merchants.add(_norm(value))

    _walk(label, None, sink)
    holder = label.get("holder")
    if isinstance(holder, dict):
        for who in ("primary", "secondary"):
            if holder.get(who):
                add_person(holder[who], household=True)


def _filler(value: str) -> bool:
    """A generator's placeholder - "000000000", "1WJ000000000" - not an identifier.

    Mostly one repeated character, which then "leaks" wherever a run of zeros
    appears in any trace number on the page.
    """
    most = max(value.count(ch) for ch in set(value))
    return most / len(value) > 0.6


def collect_text(text: str, secrets: Secrets) -> None:
    """Identifiers the JSON never carries but the corpus plants in the page."""
    for match in CORPUS_SSN.finditer(text):
        secrets.ids[_alnum(match.group())] = "ssn"
    for match in CORPUS_EIN.finditer(text):
        if not _filler(_alnum(match.group())):
            secrets.ids[_alnum(match.group())] = "ein"
    for match in DEPENDENTS.finditer(text):
        for name in re.split(r",\s*", match.group(1)):
            person = _person(name, household=True)
            if person is not None:
                secrets.people.setdefault(person.full.casefold(), person)


def finalize(secrets: Secrets) -> None:
    secrets.surnames = {p.last.casefold() for p in secrets.people.values() if p.household}
    # a counterparty who shares the household surname is family
    for person in secrets.people.values():
        if person.last.casefold() in secrets.surnames:
            person.household = True


# --------------------------------------------------------------------------
# scoring one output against the case
# --------------------------------------------------------------------------

MONEY = re.compile(r"(?<![\d,.])-?\$?\s?-?\d{1,3}(?:,\d{3})*\.\d{2}(?![\d])")
SSN_SHAPE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
MASKED_TAIL = re.compile(r"(?:[xX*•#]{2,}|\bending\s+(?:in\s+)?)[\s-]*\d{4}\b")
INDN = re.compile(r"INDN:\s*([A-Z][A-Z .'-]{2,})")

# First names that are also ordinary words or merchant vocabulary are reported
# apart: a surviving "Summer" in "SUMMER CAMP" is not a leaked child.
_COMMON_WORDS = {
    "grace", "hope", "faith", "summer", "rose", "mark", "bill", "will", "chase",
    "hunter", "carter", "mason", "cooper", "tanner", "taylor", "jordan", "parker",
    "wade", "dean", "grant", "price", "king", "young", "may", "june", "april",
    "dawn", "joy", "ray", "sky", "star", "brook", "river", "stone", "wood",
    "forest", "hill", "lane", "ford", "miles", "rich", "sterling", "penny",
}


@dataclass
class Finding:
    category: str
    value: str
    context: str
    form: str = ""


def _context(text: str, start: int, end: int, width: int = 40) -> str:
    return " ".join(text[max(0, start - width):end + width].split())


def _word(form: str) -> re.Pattern:
    parts = [re.escape(p) for p in form.split()]
    return re.compile(r"(?<![A-Za-z])" + r"[\s,.]+".join(parts) + r"(?![A-Za-z])",
                      re.IGNORECASE)


def _collapsed(text: str) -> str:
    """Each line with separators between digits and letters removed."""
    return "\n".join(re.sub(r"(?<=[0-9A-Za-z])[\s\-./]+(?=[0-9A-Za-z])", "", line)
                     for line in text.splitlines()).upper()


def leaks(output: str, secrets: Secrets, *, ocr: bool = False,
          anonymized: bool = False) -> list[Finding]:
    """Every secret still readable in ``output``.

    ``anonymized`` output carries invented names where the real ones were, so
    the sweeps that flag any name in a name-shaped slot are skipped for it.
    """
    found: list[Finding] = []
    seen: set[tuple[str, str]] = set()

    def add(category: str, value: str, text: str, match: re.Match, form: str = "") -> None:
        key = (category, value.casefold(), form)
        if key in seen:
            return
        seen.add(key)
        found.append(Finding(category, value, _context(text, match.start(), match.end()), form))

    for person in secrets.people.values():
        category = "household_name" if person.household else "counterparty"
        for form, label in person.forms:
            if label == "first" and (len(form) < 3 or form.casefold() in _COMMON_WORDS):
                continue
            match = _word(form).search(output)
            if match:
                sub = category if label != "first" else "household_first_name"
                add(sub, person.full, output, match, label)
                break

    collapsed = _collapsed(output)
    for value, category in secrets.ids.items():
        index = collapsed.find(value)
        if index >= 0 and _bounded(collapsed, index, len(value)):
            found.append(Finding(category, value, collapsed[max(0, index - 30):index + len(value) + 30].replace("\n", " ")))
        elif len(value) >= 10 and not ocr:
            # two-thirds of an account number is still the account number
            part = _partial(collapsed, value)
            if part:
                found.append(Finding(f"{category}_partial", value, part))

    for handle in secrets.handles:
        match = re.search(re.escape(handle), output, re.IGNORECASE)
        if match:
            add("handle", handle, output, match)
    digits_lines = [_digits(line) for line in output.splitlines()]
    for phone in secrets.phones:
        if any(phone in line for line in digits_lines):
            found.append(Finding("phone", phone, ""))
    for email in secrets.emails:
        index = output.casefold().find(email)
        if index >= 0:
            found.append(Finding("email", email, output[max(0, index - 30):index + 60]))
    for street in secrets.addresses:
        match = _word(street).search(output)
        if match is None:
            # "12469 W CENTER" without the suffix is still the house
            head = " ".join(street.split()[:3])
            if len(head.split()) == 3:
                match = _word(head).search(output)
        if match:
            add("address", street, output, match)
    for z in secrets.zip4:
        match = re.search(re.escape(z), output)
        if match:
            add("zip4", z, output, match)
    for org, category in secrets.orgs.items():
        match = _word(org).search(output)
        if match:
            add(category, org, output, match)

    # shapes that are wrong whatever the labels say
    for match in SSN_SHAPE.finditer(output):
        add("ssn_shape", match.group(), output, match)
    for match in MASKED_TAIL.finditer(output):
        add("masked_tail", match.group(), output, match)
    for match in EMAIL.finditer(output):
        add("email_shape", match.group(), output, match)
    if not anonymized:
        for match in INDN.finditer(output):
            add("indn_name", match.group(1).strip(), output, match)
    return found


def _bounded(text: str, index: int, length: int) -> bool:
    before = text[index - 1] if index else " "
    after = text[index + length] if index + length < len(text) else " "
    return not (before.isalnum() and after.isalnum() and before.isdigit() == after.isdigit())


def _partial(collapsed: str, value: str) -> str:
    size = max(7, (len(value) * 2 + 2) // 3)
    for start in range(0, len(value) - size + 1):
        chunk = value[start:start + size]
        index = collapsed.find(chunk)
        if index >= 0:
            return collapsed[max(0, index - 30):index + size + 30].replace("\n", " ")
    return ""


def money(text: str) -> Counter:
    return Counter(re.sub(r"[\s$]", "", m.group()).lstrip("-") for m in MONEY.finditer(text))


def destroyed_money(source: str, output: str) -> Counter:
    return money(source) - money(output)


def lost_merchants(source: str, output: str, merchants: set[str],
                   secrets: Secrets) -> list[str]:
    lost = []
    src, out = source.casefold(), output.casefold()
    for merchant in merchants:
        m = merchant.casefold()
        if len(m) < 4 or m not in src:
            continue
        if any(re.search(rf"\b{re.escape(s)}\b", m) for s in secrets.surnames):
            continue
        if src.count(m) > out.count(m):
            lost.append(merchant)
    return sorted(lost)


def load_label(pdf: Path) -> dict | None:
    sidecar = pdf.with_suffix(".json")
    if not sidecar.exists():
        return None
    try:
        return json.loads(sidecar.read_text())
    except (OSError, ValueError):
        return None
