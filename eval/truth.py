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
    "subjects", "recipient", "beneficiary", "borrower", "me", "ship_to_name",
}
# keys that name the *other* end of a transaction on a payment rail
COUNTERPARTY_KEYS = {"frm", "to", "tenant", "gift_recipient"}
# a structured identifier, checked digit-for-digit
ID_KEYS = {
    "account_number": "account", "routing_number_fake": "routing",
    "loan_no": "account", "policy": "policy", "vin": "vin", "plate": "plate",
    "hin": "vin", "parcel": "parcel", "title_no": "title", "ein": "ein",
    "order_id": "order", "tracking": "tracking",
}
HANDLE_KEYS = {"user", "tag"}
ADDRESS_KEYS = {"addr", "property"}
ORG_KEYS = {"business", "entity"}
# things that must survive: the merchant a card line names, the institution
MERCHANT_KEYS = {"merchant", "store", "institution", "creditor", "lien", "seller", "product"}

# payment-rail and payroll kinds from the statement generator
_P2P_KINDS = {"p2p_in", "p2p_out"}
_PAYROLL_SUBS = {"DIR DEP", "PAYROLL", "DIRECT DEP", "DIR DEP PAYROLL"}

_SUFFIXES = {"jr", "sr", "ii", "iii", "iv", "v"}
_SPLIT_PEOPLE = re.compile(r"\s+(?:AND|and|&)\s+")
_MASKED = re.compile(r"[xX*•#]{2,}\s?(\d{4})\b")
_NOT_PERSON = re.compile(
    r"(?i)[*]|\.com\b|\b(?:food|pizza|coffee|cafe|grill|market|store|shop|restaurant|"
    r"bakery|bar|pub|tacos?|burgers?|kitchen|diner|deli|"
    r"inc|llc|pllc|corp|co|company|bank|trust|fund|index|services?|"
    r"group|store|market|insurance|law|office|title|county|state|city|dept|"
    r"department|university|univ|school|district|plan|retirement|brokerage|"
    r"savings|checking|joint|individual|account|payroll|steam|ebay|etsy|"
    r"doordash|uber|amazon|lyft|apple|google|paypal|venmo|zelle|spotify|netflix|"
    r"technologies|technology|entertainment|networks|media|games|studios|wholesale|"
    r"subscription|membership|premium|refund|resolved|favor|"
    # a currency conversion's "To USD", "From EUR"
    r"usd|eur|gbp|cad|mxn|jpy|aud)\b"
    # a name never carries a lowercase joining word: "Nintendo of America",
    # "Case resolved in seller favor"
    r"|\s(?:of|in|to|from|for|at|the|and)\s")

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
    card_tails: set[str] = field(default_factory=set)           # last four of a card
    txn_names: list[str] = field(default_factory=list)          # payment-app rows, unsorted

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
    # "MARCO A RUIZ JR": the surname is RUIZ, not JR
    while len(tokens) > 2 and tokens[-1].strip(".").casefold() in _SUFFIXES:
        tokens.pop()
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
        elif key == "name" and node.get("type"):
            # every payment-app row names its other end, whatever the app calls
            # the row: "Sent", or a wallet's "Website Payment", "General
            # Payment", "Payment Refund". Only Sent/Received/Request were read,
            # and a whole wallet layout's 421 printed names went unscored. A
            # merchant is dropped at finalize, against the case's merchants.
            secrets.txn_names.append(value)
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
        elif key == "account_email" and "@" in value:
            secrets.emails.add(value.casefold())
        elif key == "account_phone" and len(_digits(value)) == 10:
            secrets.phones.add(_digits(value))
        elif key in ("card4", "card4b") and re.fullmatch(r"\d{4}", value):
            secrets.card_tails.add(value)
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


def finalize(secrets: Secrets, merchants: set[str] = frozenset()) -> None:
    known_merchants = {_norm(m).casefold() for m in merchants}
    for value in secrets.txn_names:
        if _norm(value).casefold() in known_merchants:
            continue
        for part in _people(value):
            person = _person(part, household=False)
            if person is not None:
                secrets.people.setdefault(person.full.casefold(), person)
    secrets.surnames = {p.last.casefold() for p in secrets.people.values() if p.household}
    # a counterparty who shares the household surname is family
    for person in secrets.people.values():
        if person.last.casefold() in secrets.surnames:
            person.household = True


# --------------------------------------------------------------------------
# scoring one output against the case
# --------------------------------------------------------------------------

MONEY = re.compile(
    r"(?<![\d,.])-?\$?\s?-?(?:\d{1,3}(?:,\d{3})*|\d{4,})\.\d{2}(?![\d])"
    # whole dollars, which a figure with a $ and thousands commas still is
    r"|(?<![\d,.])-?\$\s?\d{1,3}(?:,\d{3})+(?![\d.,])")
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

    reported_surnames: set[str] = set()
    for person in secrets.people.values():
        category = "household_name" if person.household else "counterparty"
        for form, label in person.forms:
            if label == "first" and (len(form) < 3 or form.casefold() in _COMMON_WORDS):
                continue
            if label == "surname" and form.casefold() in reported_surnames:
                break               # one surviving PACKARD is one leak, not one per Packard
            match = _word(form).search(output)
            if match:
                sub = category if label != "first" else "household_first_name"
                add(sub, person.full, output, match, label)
                if label == "surname":
                    reported_surnames.add(form.casefold())
                break
        else:
            glued = _glued(person.last, output) if person.household else None
            if glued:
                add("household_name_glued", person.full, output, glued, "glued")

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
    for tail in secrets.card_tails:
        # "Visa ending in 5546", "Mastercard - 5546": four digits are only a
        # card when the line says so
        match = re.search(rf"(?i)\b(?:visa|mastercard|master card|american express|amex|"
                          rf"discover|card|ending(?: in)?|last (?:4 |four )?digits)\b"
                          rf"[^\n\d]{{0,24}}{tail}\b", output)
        if match:
            add("card_tail", tail, output, match)
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


def _glued(surname: str, output: str) -> re.Match | None:
    """A household surname run into its neighbours: "KYLEHARRINGTON",
    "HarringtonStatement_Jan", "2403289000BLACKBURNLAW". Word boundaries never
    see these, and a glued name is the leak class this project was built
    around. Only where the gluing is plain from the case - capitals running
    into capitals, a capital starting a new word, a digit - so "Cooperative"
    is not a Cooper."""
    if len(surname) < 5:
        return None
    for match in re.finditer(re.escape(surname), output, re.IGNORECASE):
        written = match.group(0)
        before = output[match.start() - 1] if match.start() else " "
        after = output[match.end()] if match.end() < len(output) else " "
        if not before.isalnum() and not after.isalnum():
            continue                                   # a plain word: _word's business
        caps = written.isupper()
        before_glued = before.isdigit() or (caps and before.isupper()) or (
            before.islower() and written[:1].isupper())
        after_glued = after.isdigit() or (caps and after.isupper()) or (
            not caps and after.isupper())
        if (before_glued or not before.isalnum()) and (after_glued or not after.isalnum()):
            return match
    return None


def _bounded(text: str, index: int, length: int) -> bool:
    """Whether the value stands on its own: its neighbours are not of its kind.

    A digit run wedged between letters - "EIN004049659WAGES" once the line is
    collapsed - is a whole number; it was taken for part of a longer token.
    """
    before = text[index - 1] if index else " "
    after = text[index + length] if index + length < len(text) else " "
    first, last = text[index], text[index + length - 1]
    before_ok = not before.isalnum() or before.isdigit() != first.isdigit()
    after_ok = not after.isalnum() or after.isdigit() != last.isdigit()
    return before_ok and after_ok


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


# --------------------------------------------------------------------------
# scoring against the party sheet (see eval.sheet)
# --------------------------------------------------------------------------

# A phone as printed: parentheses or a separator after the area code, a
# separator before the last four, a valid NANP area code and exchange.
PHONE_SHAPE = re.compile(
    r"(?<![\w(])(?:\+?1[\s.-]?)?(?:\(([2-9]\d{2})\)\s?|([2-9]\d{2})[\s.-])([2-9]\d{2})[\s.-](\d{4})(?!\d)")
# what a number token is when it is a date or a time: scored by lost_dates
_DATE_SPAN = re.compile(
    r"\b\d{4}-\d{2}-\d{2}(?:T\d{2}:\d{2}(?::\d{2})?)?\b|\b\d{1,2}[/-]\d{1,2}(?:[/-]\d{2,4})?\b"
    r"|\b\d{1,2}/\d{4}\b|\b\d{1,2}:\d{2}(?::\d{2})?\b"
    r"|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]*\.? \d{1,2},? \d{4}\b", re.I)
NUMBER_TOKEN = re.compile(r"\d+(?:[-./]\d+)*")
# Before a bare last four: the words that make it an account's tail.
_TAIL_MASK = re.compile(r"(?:[xX*•#]{2,}|\.{2,})[\s-]*$")
_TAIL_CUE = re.compile(
    r"(?i)(?:ending(?:\s+in)?|last\s+(?:4|four)(?:\s+digits)?|acct|account|card|visa|mastercard|"
    r"amex|discover|debit|savings|checking|bank)\b\W{0,6}[xX*•#.\s-]{0,12}$")


def party_leaks(output: str, party_secrets: Secrets, *, ocr: bool = False,
                anonymized: bool = False) -> list[Finding]:
    """:func:`leaks`, against only what the party sheet says must go.

    ``party_secrets`` is :meth:`eval.sheet.PartyTruth.secrets`. A third
    party's email now survives on purpose, so the sweep for any email shape
    is dropped; an SSN shape or a masked tail is still the parties' (the
    specification replaces both wherever they are).
    """
    return [f for f in leaks(output, party_secrets, ocr=ocr, anonymized=anonymized)
            if f.category != "email_shape"]


def account_forms_left(output: str, accounts: list[dict]) -> list[dict]:
    """The parties' account and card numbers still readable, in any form.

    ``accounts`` is :meth:`eval.sheet.PartyTruth.accounts`. Full digits
    (spaced or dashed - the lines are collapsed), two-thirds of a long one,
    or the last four behind a mask or a word that makes them a tail:
    "****1234", "xxxx-1234", "ending in 1234", "Card 1234".
    """
    collapsed = _collapsed(output)
    found = []
    for account in accounts:
        forms = []
        context = ""
        number = account.get("number", "")
        if len(number) >= 6:
            index = collapsed.find(number)
            if index >= 0 and _bounded(collapsed, index, len(number)):
                forms.append("full")
                context = collapsed[max(0, index - 30):index + len(number) + 30]
            elif len(number) >= 10:
                part = _partial(collapsed, number)
                if part:
                    forms.append("partial")
                    context = part
        last4 = account["last4"]
        for match in re.finditer(rf"(?<!\d){last4}(?!\d)", output):
            before = output[max(0, match.start() - 28):match.start()]
            if _TAIL_MASK.search(before):
                form = "masked"
            elif _TAIL_CUE.search(before):
                form = "labelled tail"
            else:
                continue
            if form not in forms:
                forms.append(form)
                context = context or _context(output, match.start(), match.end())
        if forms:
            found.append({**{k: account.get(k, "") for k in ("party", "kind", "institution", "written")},
                          "forms": forms, "context": " ".join(context.split())[:120]})
    return found


def _spans(pattern: re.Pattern, text: str) -> list[tuple[int, int]]:
    return [m.span() for m in pattern.finditer(text)]


def _inside(spans: list[tuple[int, int]], start: int, end: int) -> bool:
    return any(a <= start and end <= b for a, b in spans)


def number_tokens(text: str) -> list[tuple[str, str, int]]:
    """(written, digits, start) for each number of four digits or more that is
    not money, a date, a time or a phone - those are scored apart."""
    skip = _spans(MONEY, text) + _spans(_DATE_SPAN, text) + _spans(PHONE_SHAPE, text)
    out = []
    for match in NUMBER_TOKEN.finditer(text):
        digits = re.sub(r"\D", "", match.group())
        if len(digits) < 4 or _inside(skip, *match.span()):
            continue
        out.append((match.group(), digits, match.start()))
    return out


def numbers_replaced(source: str, output: str, protected: set[str], account_digits: list[str],
                     other_numbers: dict[str, str], took=None, examples: int = 30) -> dict:
    """Numbers on the page that are none of the parties' and did not survive.

    The attorney's complaint: transaction, reference and check numbers, ZIPs
    and years taken as accounts. ``protected`` holds every digit string the
    parties own (accounts, their last fours, SSNs, ZIPs and house numbers of
    their addresses...), ``account_digits`` their full account numbers (a
    number ending one of them is that account). ``took(digits)`` names the
    category of the entity that took a number - ``""`` when none did - and
    ``other_numbers`` what the labels call it (order, tracking, check...).
    """
    def theirs(digits: str) -> bool:
        if digits in protected:
            return True
        return any(len(digits) >= 4 and full.endswith(digits) for full in account_digits)

    before = [t for t in number_tokens(source) if not theirs(t[1])]
    after = Counter(t[1] for t in number_tokens(output))
    counted = Counter(t[1] for t in before)
    lost = counted - after
    by_took: Counter = Counter()
    by_known: Counter = Counter()
    shown: list[dict] = []
    first = {}
    for written, digits, start in before:
        first.setdefault(digits, (written, start))
    for digits, count in sorted(lost.items(), key=lambda kv: -kv[1]):
        written, start = first[digits]
        category = (took(digits) if took else "") or "(not traced)"
        known = _known(written, digits, source, start, other_numbers)
        by_took[category] += count
        by_known[known] += count
        if len(shown) < examples:
            shown.append({"written": written, "count": count, "took": category, "known": known,
                          "context": _context(source, start, start + len(written), 30)})
    return {"count": sum(lost.values()), "of": len(before), "by_took": dict(by_took),
            "by_known": dict(by_known), "examples": shown}


_CHECK_CUE = re.compile(r"(?i)\b(?:check|chk|ck)\b\W{0,3}(?:no\.?|#)?\s*$")


def _known(written: str, digits: str, source: str, start: int, other_numbers: dict) -> str:
    """What the labels call a number. Four or five digits recur by chance - a
    store's "Swig 1156" is not check 1156 - so a short one is a check only
    when the page says so in front of it."""
    if len(digits) >= 6:
        found = other_numbers.get(_alnum(written)) or other_numbers.get(digits)
        if found:
            return found
    elif _CHECK_CUE.search(source[max(0, start - 14):start]):
        return "check"
    return _shape(written)


def _shape(written: str) -> str:
    digits = re.sub(r"\D", "", written)
    if re.fullmatch(r"(?:19|20)\d{2}", written):
        return "year-shaped"
    if re.fullmatch(r"\d{5}(?:-\d{4})?", written):
        return "zip-shaped"
    return f"{len(digits)}-digit number"


PHONE_CATEGORIES = {"phone", "fax"}


def phones_mistyped(source: str, output: str, party_phones: set[str], took=None,
                    examples: int = 20) -> dict:
    """Phone-shaped strings that did not survive, by what took them.

    A phone must lose to nothing: taken as an account, an ID or a check
    number it reads as one. Counted: the parties' phones replaced as phones
    (right), anyone else's replaced as phones (over-redaction under parties
    first), and any replaced as something else (``mistyped``).
    """
    def phones(text):
        out = Counter()
        where = {}
        for match in PHONE_SHAPE.finditer(text):
            digits = "".join(g for g in match.groups() if g)
            out[digits] += 1
            where.setdefault(digits, match)
        return out, where

    src, where = phones(source)
    out, _ = phones(output)
    lost = src - out
    result = {"party_as_phone": 0, "other_as_phone": 0, "mistyped": 0, "of": sum(src.values()),
              "by_took": {}, "examples": []}
    by_took: Counter = Counter()
    for digits, count in lost.items():
        category = (took(digits) if took else "") or "(not traced)"
        if category in PHONE_CATEGORIES:
            result["party_as_phone" if digits in party_phones else "other_as_phone"] += count
            continue
        result["mistyped"] += count
        by_took[category] += count
        if len(result["examples"]) < examples:
            match = where[digits]
            result["examples"].append({"written": match.group(), "took": category,
                                       "party": digits in party_phones,
                                       "context": _context(source, match.start(), match.end(), 30)})
    result["by_took"] = dict(by_took)
    return result


def third_replaced(source: str, output: str, people: list[Person],
                   values: dict[str, set[str]], examples: int = 20) -> dict:
    """Third parties' names and values a page had and its delivered copy does not.

    Under parties first they should all survive: payees, payers, the other
    end of a Venmo row, a comparable sale's address.
    """
    result = {"names": 0, "phones": 0, "emails": 0, "handles": 0, "addresses": 0,
              "examples": []}

    def note(kind, value, lost):
        result[kind] += lost
        if lost and len(result["examples"]) < examples:
            result["examples"].append({"kind": kind, "value": value, "lost": lost})

    for person in people:
        forms = {person.full, f"{person.first} {person.last}"}
        pattern = re.compile("|".join(_word(f).pattern for f in sorted(forms, key=len, reverse=True)),
                             re.IGNORECASE)
        lost = len(pattern.findall(source)) - len(pattern.findall(output))
        if lost > 0:
            note("names", person.full, lost)
    src_lines = Counter(d for line in source.splitlines() for d in _ten_digit(line))
    out_lines = Counter(d for line in output.splitlines() for d in _ten_digit(line))
    for phone in values.get("phones", ()):
        lost = src_lines[phone] - out_lines[phone]
        if lost > 0:
            note("phones", phone, lost)
    low_src, low_out = source.casefold(), output.casefold()
    for email in values.get("emails", ()):
        lost = low_src.count(email) - low_out.count(email)
        if lost > 0:
            note("emails", email, lost)
    for handle in values.get("handles", ()):
        lost = low_src.count(handle.casefold()) - low_out.count(handle.casefold())
        if lost > 0:
            note("handles", handle, lost)
    for street in values.get("addresses", ()):
        pattern = _word(street)
        lost = len(pattern.findall(source)) - len(pattern.findall(output))
        if lost > 0:
            note("addresses", street, lost)
    return result


def _ten_digit(line: str) -> list[str]:
    return ["".join(m.groups()) for m in PHONE.finditer(line)]


def load_label(pdf: Path) -> dict | None:
    sidecar = pdf.with_suffix(".json")
    if not sidecar.exists():
        # a photographed document's pages share one label: "…_p1.jpg"
        sidecar = pdf.with_name(re.sub(r"_p\d+$", "", pdf.stem) + ".json")
    if not sidecar.exists():
        return None
    try:
        return json.loads(sidecar.read_text())
    except (OSError, ValueError):
        return None
