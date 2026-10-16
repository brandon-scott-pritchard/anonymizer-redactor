"""The party sheet an attorney would type, read off a case's labels.

The parties-first redesign has the operator fill in, per set, who the
petitioner, the respondent and the children are and what identifies them -
names, SSNs, addresses, phones, accounts - before anything is scanned (the
shape is the one ``GET /api/parties`` serves). The harness has to fill that
sheet the way an attorney would, from the synthetic labels, and then score a
run against *it*: only the parties' and children's secrets and the family
surname must go; everyone else - payees, payers, merchants, the people on the
other end of a Venmo row - must survive, and replacing them is over-redaction.

The labels carry no roles, so they are inferred:

- **Adults** are the people a document belongs to: a statement's holders, the
  employee on a stub, a credit report's subjects, a vehicle's owner, the
  "me" of a payment app. The first two seen are petitioner and respondent;
  any more are "other".
- **Children** are people of the household who are never an adult in any
  document: the patient on an EOB, a ship-to name in an order history, a
  student, a dependent on a 1040.
- **Relatives** share the family surname but are neither (a brother repaid on
  a promissory note): the surname goes, the first name stays.
- Everyone else in the labels is a **third party**.

Each identifier goes to the person the label ties it to (a Zelle token to the
name on its row, a phone line to the first name it is labelled with, a VIN to
the vehicle's owner) and otherwise to the person whose document it is in - the
statement holder. An SSN only appears on the page, never in a label, so it
goes to the nearest party name written above it on the page ("Spouse's social
security number" sits under the spouse's name).

Loan and mortgage numbers are left off the sheet: the specification lists them
among what is left as written. They are kept apart (``other_numbers``) so a
run that takes them anyway shows up as numbers replaced that are not accounts.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

from . import truth

ROLES = ("petitioner", "respondent", "child", "other")

# keys whose person is the one the document belongs to - an adult of the case
ADULT_KEYS = ("primary", "secondary", "employee", "owner", "me", "subjects", "recipient", "person")
# keys whose person is one of the household but could be a child
CHILD_KEYS = ("patient", "ship_to_name", "student")
# the document's owner, in the order a label is asked
OWNER_KEYS = ("owner", "employee", "person", "me", "recipient", "subjects", "student", "patient")

# Statements whose number is a loan's: left as written (see the module note).
_LOAN_TYPES = {"auto", "mortgage", "student", "heloc", "loan", "personal", "personal_loan",
               "installment", "boat", "rv"}
_CARD_TYPES = {"credit", "card", "store", "cu", "charge"}
# label keys whose value is a number that is not anyone's identity and must
# survive: what the specification lists as left as written
OTHER_NUMBER_KEYS = {
    "loan_no": "loan", "policy": "policy", "order_id": "order", "tracking": "tracking",
    "title_no": "title", "routing_number_fake": "routing", "id": "transaction",
    "conf": "confirmation", "ppd": "ach_id", "check_no": "check", "ref": "reference",
    "advice_no": "advice", "claim": "claim", "member_id": "member", "group": "group",
    "invoice": "invoice",
}
# A masked account or card tail inside any label string: "xxxxxx8731",
# "Bank ***3618", "Debit Card ••9107", "Transfer to Savings ..8731".
_TAIL = re.compile(r"(?:[xX*•#]{2,}|\.{2,})\s?(\d{4})\b")
_PHONE_TEXT = re.compile(r"\(?\b(\d{3})\)?[\s.-]*(\d{3})[\s.-]*(\d{4})\b")
_DOB_TEXT = re.compile(
    r"(?i)(?:date\s+of\s+birth|birth\s*date|\bDOB)\W{0,4}"
    r"(\d{1,2}/\d{1,2}/\d{2,4}|\d{4}-\d{2}-\d{2}|[A-Z][a-z]{2,8}\.? \d{1,2},? \d{4})")
_DL_TEXT = re.compile(
    r"(?i)(?:driver'?s?\s+licen[cs]e|\bDL)\s*(?:#|no\.?|number)?\s*[:\-]?\s*([A-Z]?\d[A-Z0-9]{4,12})\b")
# a document of the household's own business: its EIN is the party's TIN
_BUSINESS_DOCS = re.compile(r"(?i)profit|pnl|schedule_c|1120|1065|k-?1|business|balance_sheet")
# first names the corpus prints on a child's line: "Gymnastics - Aiden (monthly)"
_FIRST_ON_LINE = re.compile(r"\b([A-Z][a-z]{2,})\b")


def _blank_party(role: str) -> dict:
    """One party in exactly the sheet's shape (see the specification)."""
    return {"role": role, "names": [], "dob": [], "ssn": [], "tin": [], "dl": [],
            "passport": [], "addresses": [], "phones": [], "emails": [], "employers": [],
            "businesses": [], "accounts": [], "cards": [], "handles": [], "vehicles": [],
            "case_numbers": [], "parcels": [], "other": []}


def _ident(person: truth.Person) -> tuple[str, str]:
    """One person however written: "BRITTANY S NAKAMURA" is "Brittany Nakamura"."""
    return person.first.casefold().strip("."), person.last.casefold()


def _fmt_phone(digits: str) -> str:
    return f"({digits[:3]}) {digits[3:6]}-{digits[6:]}"


def _digits(text) -> str:
    return re.sub(r"\D", "", str(text))


@dataclass
class Party:
    role: str                                   # petitioner | respondent | child | other
    key: str                                    # petitioner, respondent, child-1, other-1
    people: list[truth.Person] = field(default_factory=list)
    sheet: dict = field(default_factory=dict)

    @property
    def first(self) -> str:
        return self.people[0].first if self.people else ""

    @property
    def last(self) -> str:
        return self.people[0].last if self.people else ""


@dataclass
class PartyTruth:
    """A case's parties, and what the labels say about everyone else."""

    parties: list[Party] = field(default_factory=list)
    family_surnames: list[str] = field(default_factory=list)
    relatives: list[truth.Person] = field(default_factory=list)
    third_people: list[truth.Person] = field(default_factory=list)
    # what third parties are known by, which must survive: phones (ten
    # digits), emails, handles, street lines
    third_values: dict[str, set[str]] = field(default_factory=lambda: {
        "phones": set(), "emails": set(), "handles": set(), "addresses": set()})
    # numbers the labels name that are nobody's identity: alnum -> what they are
    other_numbers: dict[str, str] = field(default_factory=dict)
    # ZIP+4 of the parties' addresses (the street line alone is on the sheet)
    zip4: set[str] = field(default_factory=set)
    # Masked tails found only in transaction text ("Bank ***3618", "Transfer
    # to Savings ..8731") that match no account on the sheet: the parties'
    # all the same - the specification replaces them as [PARTIES-ACCOUNT-n] -
    # but nothing an attorney types. A payment app's export names a fresh one
    # on almost every row (176 in one household). Party key -> entries.
    found: dict[str, list[dict]] = field(default_factory=dict)

    # ------------------------------------------------------------------ out

    def sheet(self) -> dict:
        """The sheet, exactly as ``PUT /api/parties`` takes it."""
        return {"parties": [p.sheet for p in self.parties],
                "family_surnames": list(self.family_surnames)}

    def party(self, key: str) -> Party | None:
        return next((p for p in self.parties if p.key == key), None)

    def party_accounts(self, party: Party) -> list[tuple[str, dict]]:
        """(kind, entry) for each account and card of one party, found tails too."""
        return ([("account", a) for a in party.sheet["accounts"]]
                + [("card", c) for c in party.sheet["cards"]]
                + [("found", f) for f in self.found.get(party.key, [])])

    def accounts(self) -> list[dict]:
        """Every account and card number of the parties: full digits and last four."""
        out = []
        for party in self.parties:
            for kind, item in self.party_accounts(party):
                digits = _digits(item.get("number", ""))
                if len(digits) < 4:
                    continue
                out.append({"party": party.key, "kind": kind,
                            "number": digits if not re.search(r"[*xX•]", item["number"]) else "",
                            "last4": digits[-4:], "institution": item.get("institution", ""),
                            "written": item["number"]})
        return out

    def secrets(self) -> truth.Secrets:
        """What must not survive a parties-first run, as :func:`truth.leaks` reads it."""
        s = truth.Secrets()
        for party in self.parties:
            for person in party.people:
                s.people[person.full.casefold()] = truth.Person(person.full, person.first,
                                                                person.last, True)
            sheet = party.sheet
            for ssn in sheet["ssn"]:
                s.ids[_digits(ssn)] = "ssn"
            for tin in sheet["tin"]:
                s.ids[_digits(tin)] = "ein"
            for dl in sheet["dl"]:
                s.ids[truth._alnum(dl["number"])] = "drivers_license"
            for passport in sheet["passport"]:
                s.ids[truth._alnum(passport)] = "passport"
            for item in sheet["accounts"]:
                digits = _digits(item["number"])
                if len(digits) >= 6:
                    s.ids[digits] = "account"
            for item in sheet["cards"]:
                digits = _digits(item["number"])
                if len(digits) >= 6:
                    s.ids[digits] = "card"
                if len(digits) >= 4:
                    s.card_tails.add(digits[-4:])
            for vehicle in sheet["vehicles"]:
                for part, category in (("vin", "vin"), ("plate", "plate")):
                    if len(truth._alnum(vehicle.get(part, ""))) >= 5:
                        s.ids[truth._alnum(vehicle[part])] = category
            for parcel in sheet["parcels"]:
                s.ids[truth._alnum(parcel)] = "parcel"
            for case in sheet["case_numbers"]:
                s.ids[truth._alnum(case)] = "case_number"
            s.phones.update(_digits(p)[-10:] for p in sheet["phones"])
            s.emails.update(e.casefold() for e in sheet["emails"])
            s.handles.update(sheet["handles"])
            s.addresses.update(a.split(",")[0].strip() for a in sheet["addresses"])
            for org in sheet["employers"]:
                s.orgs[org] = "employer"
            for org in sheet["businesses"]:
                s.orgs[org] = "business"
        for person in self.relatives:
            s.people[person.full.casefold()] = Relative(person.full, person.first,
                                                        person.last, True)
        s.zip4 = set(self.zip4)
        s.surnames = {n.casefold() for n in self.family_surnames}
        return s


class Relative(truth.Person):
    """One of the family who is not a party: only the surname must go."""

    @property
    def forms(self) -> list[tuple[str, str]]:
        return [(self.full, "full"), (f"{self.first} {self.last}", "first+last"),
                (self.last, "surname")]


# --------------------------------------------------------------------------
# building it
# --------------------------------------------------------------------------


def _people_in(value) -> list[truth.Person]:
    values = value if isinstance(value, list) else [value]
    out = []
    for item in values:
        if not isinstance(item, str):
            continue
        for part in truth._people(item):
            person = truth._person(part, household=True)
            if person is not None:
                out.append(person)
    return out


def _walk_keys(node, sink, parent=None) -> None:
    """``sink(key, value, node)`` for every key, whatever its value."""
    if isinstance(node, dict):
        for key, value in node.items():
            sink(key, value, node)
            _walk_keys(value, sink, node)
    elif isinstance(node, list):
        for item in node:
            _walk_keys(item, sink, parent)


def _strings(node):
    if isinstance(node, str):
        yield node
    elif isinstance(node, dict):
        for value in node.values():
            yield from _strings(value)
    elif isinstance(node, list):
        for item in node:
            yield from _strings(item)


class _Builder:
    def __init__(self, labels: list[tuple[Path, dict]], texts: dict[Path, str],
                 secrets: truth.Secrets | None, typed_tails: bool = False):
        self.typed_tails = typed_tails
        self.labels = labels
        self.texts = texts
        self.secrets = secrets or truth.Secrets()
        self.forms: dict[tuple[str, str], list[truth.Person]] = {}
        self.adults: list[tuple[str, str]] = []
        self.children: list[tuple[str, str]] = []
        self.parties: dict[tuple[str, str], Party] = {}
        self.out = PartyTruth()

    # ---------------------------------------------------------------- people

    def _see(self, person: truth.Person) -> tuple[str, str]:
        ident = _ident(person)
        known = self.forms.setdefault(ident, [])
        if person.full.casefold() not in {p.full.casefold() for p in known}:
            known.append(person)
        return ident

    def people(self) -> None:
        for _path, label in self.labels:
            def sink(key, value, node):
                if key in ADULT_KEYS:
                    for person in _people_in(value):
                        ident = self._see(person)
                        if ident not in self.adults:
                            self.adults.append(ident)
                elif key in CHILD_KEYS:
                    for person in _people_in(value):
                        ident = self._see(person)
                        if ident not in self.children:
                            self.children.append(ident)
            _walk_keys(label, sink)
        # a dependent on a return is only on the page
        for text in self.texts.values():
            for match in truth.DEPENDENTS.finditer(text):
                for name in re.split(r",\s*", match.group(1)):
                    person = truth._person(name, household=True)
                    if person is not None:
                        ident = self._see(person)
                        if ident not in self.children:
                            self.children.append(ident)
        self.children = [c for c in self.children if c not in self.adults]
        surnames = list(dict.fromkeys(self.forms[i][0].last for i in self.adults[:2]))
        # a "child" slot of another surname that is no one's dependent is a
        # guest on an order, not a child of the marriage
        self.children = [c for c in self.children
                         if c[1] in {s.casefold() for s in surnames} or len(self.adults) < 2]
        roles = [("petitioner", "petitioner"), ("respondent", "respondent")]
        others = 0
        for index, ident in enumerate(self.adults):
            if index < 2:
                role, key = roles[index]
            else:
                others += 1
                role, key = "other", f"other-{others}"
            self._party(ident, role, key)
        for index, ident in enumerate(self.children, 1):
            self._party(ident, "child", f"child-{index}")
        for party in self.parties.values():
            if party.last and party.last not in surnames:
                surnames.append(party.last)
        self.out.family_surnames = list(dict.fromkeys(s.title() for s in surnames))
        family = {s.casefold() for s in surnames}
        # everyone the existing truth knows, sorted into relative or third party
        named = set(self.parties)
        seen_relatives: set[tuple[str, str]] = set()
        for person in self.secrets.people.values():
            ident = _ident(person)
            if ident in named:
                continue
            if person.last.casefold() in family:
                if ident not in seen_relatives:
                    seen_relatives.add(ident)
                    self.out.relatives.append(person)
            else:
                self.out.third_people.append(person)
        for ident, forms in self.forms.items():
            if ident in named or ident in seen_relatives or ident[1] not in family:
                continue
            seen_relatives.add(ident)
            self.out.relatives.extend(forms[:1])

    def _party(self, ident, role, key) -> None:
        forms = self.forms.get(ident, [])
        # the forms the existing truth gathered too: "Brittany Nakamura" off a
        # Zelle row is the respondent written another way
        for person in self.secrets.people.values():
            if _ident(person) == ident and person.full.casefold() not in {
                    p.full.casefold() for p in forms}:
                forms.append(person)
        party = Party(role, key, list(forms), _blank_party(role))
        names = []
        for person in forms:
            if person.full.casefold() not in {n.casefold() for n in names}:
                names.append(person.full)
        party.sheet["names"] = names
        self.parties[ident] = party
        self.out.parties.append(party)

    def whose(self, value) -> Party | None:
        for person in _people_in(value):
            party = self.parties.get(_ident(person))
            if party is not None:
                return party
        return None

    def by_first(self, text: str) -> Party | None:
        for word in _FIRST_ON_LINE.findall(text):
            for party in self.parties.values():
                if any(word.casefold() == p.first.casefold() for p in party.people):
                    return party
        return None

    def owner(self, label: dict) -> list[Party]:
        """Whose document this is: the holders, else the person it names."""
        holder = label.get("holder") if isinstance(label.get("holder"), dict) else {}
        found = []
        for who in ("primary", "secondary"):
            party = self.whose(holder.get(who)) if holder.get(who) else None
            if party is not None and party not in found:
                found.append(party)
        if not found:
            for key in OWNER_KEYS:
                for node in (label, label.get("data") if isinstance(label.get("data"), dict) else {}):
                    party = self.whose(node.get(key)) if node.get(key) else None
                    if party is not None:
                        return [party]
        return found

    # ----------------------------------------------------------- identifiers

    def _add(self, party: Party, kind: str, value) -> None:
        bucket = party.sheet[kind]
        if isinstance(value, dict):
            key = _digits(value.get("number", "")) or truth._alnum(value.get("vin", "")
                                                                 + value.get("plate", ""))
            if any((_digits(v.get("number", "")) or truth._alnum(v.get("vin", "") + v.get("plate", "")))
                   == key for v in bucket):
                return
        elif kind == "addresses":
            street = _street_key(value)
            if any(_street_key(v) == street for p in self.out.parties for v in p.sheet[kind]):
                return
        elif kind in ("employers", "businesses"):
            # an employer is the household's once, under whoever earns there
            if any(value.casefold() == v.casefold() for p in self.out.parties for v in p.sheet[kind]):
                return
        elif any(str(v).casefold() == str(value).casefold() for v in bucket):
            return
        bucket.append(value)

    def _first_party(self) -> Party | None:
        return self.out.parties[0] if self.out.parties else None

    def identifiers(self) -> None:
        # (party, last4, institution, kind, from a card field rather than free text)
        tails: list[tuple[Party, str, str, str, bool]] = []
        for path, label in self.labels:
            owners = self.owner(label) or ([self._first_party()] if self._first_party() else [])
            if not owners:
                continue
            owner = owners[0]
            institution = str(label.get("institution") or label.get("app") or label.get("bank") or "")
            account_type = str(label.get("account_type") or "").casefold()
            layout = str(label.get("layout") or "")
            number = label.get("account_number")
            if number:
                digits = _digits(number)
                if layout[:1] in ("L", "M") or account_type in _LOAN_TYPES:
                    self.out.other_numbers[digits] = "loan"
                elif layout[:1] == "C" or account_type in _CARD_TYPES:
                    self._add(owner, "cards", {"number": digits, "institution": institution})
                elif len(digits) >= 4:
                    self._add(owner, "accounts", {"number": digits, "institution": institution,
                                                  "kind": account_type or "account"})
            holder = label.get("holder") if isinstance(label.get("holder"), dict) else {}
            address = holder.get("address") if isinstance(holder.get("address"), dict) else None
            if address and address.get("line1"):
                for party in owners:
                    self._add(party, "addresses", _address_line(address))
                if re.fullmatch(r"\d{5}-\d{4}", str(address.get("zip4") or "")):
                    self.out.zip4.add(address["zip4"])
            if holder.get("business"):
                self._add(owner, "businesses", str(holder["business"]))
            if label.get("employer") and isinstance(label["employer"], str):
                self._add(owner, "employers", label["employer"])
            if label.get("business") and isinstance(label["business"], str):
                self._add(owner, "businesses", label["business"])
            if label.get("account_email"):
                self._add(owner, "emails", str(label["account_email"]).casefold())
            if label.get("account_phone") and len(_digits(label["account_phone"])) == 10:
                self._add(owner, "phones", _fmt_phone(_digits(label["account_phone"])))
            self._walk_label(label, owner, institution, tails)
            for text in _strings(label):
                for match in _TAIL.finditer(text):
                    kind = "cards" if re.search(r"(?i)card|visa|mastercard|amex|discover", text) \
                        else "accounts"
                    tails.append((owner, match.group(1), institution if kind == "cards" else "",
                                  kind, False))
            self._from_text(path, label, owners)
        # a tail of an account already on the sheet is that account
        for party, last4, institution, kind, typed in tails:
            if any(_digits(item["number"]).endswith(last4)
                   for p in self.out.parties for _k, item in self.out.party_accounts(p)):
                continue
            if any(number.endswith(last4) and what == "loan"
                   for number, what in self.out.other_numbers.items()):
                continue
            # as the sheet writes them: a card by its last four, an account
            # known only by its tail as the tail
            entry = {"number": last4 if kind == "cards" else f"****{last4}",
                     "institution": institution}
            if kind == "accounts":
                entry["kind"] = "linked"
            if typed or self.typed_tails:
                self._add(party, kind, entry)
            else:
                self.out.found.setdefault(party.key, []).append(entry)

    def _walk_label(self, label: dict, owner: Party, institution: str, tails: list) -> None:
        def sink(key, value, node):
            if isinstance(value, bool) or value is None:
                return
            if key in OTHER_NUMBER_KEYS and isinstance(value, (str, int)):
                normal = truth._alnum(value)
                if len(normal) >= 4 and not truth._filler(normal):
                    self.out.other_numbers.setdefault(normal, OTHER_NUMBER_KEYS[key])
                return
            if key in ("card", "card4", "card4b") and isinstance(value, (str, int)):
                digits = _digits(value)
                if len(digits) == 4 and digits != "0000" and not re.search(r"[*•xX]", str(value)):
                    brand = node.get("payment_brand") or institution
                    tails.append((owner, digits, str(brand or ""), "cards", True))
                return
            if key == "vehicle" and isinstance(value, dict):
                who = self.whose(value.get("owner")) if value.get("owner") else None
                vehicle = {"vin": str(value.get("vin") or value.get("hin") or ""),
                           "plate": str(value.get("plate") or "")}
                if vehicle["vin"] or vehicle["plate"]:
                    self._add(who or owner, "vehicles", vehicle)
                return
            if key == "property" and isinstance(value, (dict, str)):
                addr = value.get("addr") if isinstance(value, dict) else value
                if addr and re.match(r"\s*\d", str(addr)):
                    self._add(owner, "addresses", _norm_address(str(addr)))
                if isinstance(value, dict) and value.get("parcel"):
                    self._add(owner, "parcels", str(value["parcel"]))
                if isinstance(value, dict) and value.get("tenant"):
                    pass                                  # a tenant is a third party
                return
            if key == "parcel" and isinstance(value, str):
                self._add(owner, "parcels", value)
                return
            if key == "comps" and isinstance(value, list):
                for comp in value:
                    if isinstance(comp, dict) and comp.get("addr"):
                        self.out.third_values["addresses"].add(str(comp["addr"]).split(",")[0].strip())
                return
            if key == "addr" and isinstance(value, str) and node is label \
                    and str(label.get("doc_type", "")) in ("w2", "w-2"):
                # the employer's address on a W-2 is the employer's, not the employee's
                self.out.third_values["addresses"].add(value.split(",")[0].strip())
                return
            if key == "ship_to" and isinstance(value, dict) and value.get("line1"):
                who = self.whose(node.get("ship_to_name")) if node.get("ship_to_name") else None
                if who is None and node.get("gift_recipient"):
                    self.out.third_values["addresses"].add(" ".join(str(value["line1"]).split()))
                return
            if key == "token" and isinstance(value, str):
                who = self.whose(node.get("name")) if node.get("name") else None
                if "@" in value:
                    if who is not None:
                        self._add(who, "emails", value.casefold())
                    else:
                        self.out.third_values["emails"].add(value.casefold())
                elif len(_digits(value)) == 10:
                    if who is not None:
                        self._add(who, "phones", _fmt_phone(_digits(value)))
                    else:
                        self.out.third_values["phones"].add(_digits(value))
                return
            if key in ("user", "tag") and isinstance(value, str) and value[:1] in "@$":
                self._add(owner, "handles", value)
                return
            if key == "lines" and isinstance(value, list):
                for line in value:
                    text = line[0] if isinstance(line, list) and line else line
                    if not isinstance(text, str):
                        continue
                    for match in _PHONE_TEXT.finditer(text):
                        digits = "".join(match.groups())
                        self._add(self.by_first(text) or owner, "phones", _fmt_phone(digits))
                return
            if key == "party" and isinstance(value, str) and node.get("kind") == "ach_credit" \
                    and str(node.get("sub", "")).upper() in truth._PAYROLL_SUBS:
                self._add(owner, "employers", " ".join(value.split()))
        _walk_keys(label, sink)

    def _from_text(self, path: Path, label: dict, owners: list[Party]) -> None:
        """SSNs, EINs, dates of birth and licences: on the page only."""
        pdf = path.with_suffix(".pdf")
        text = self.texts.get(pdf)
        if not text:
            return
        business_doc = bool(_BUSINESS_DOCS.search(str(label.get("doc_type", "")))
                            or (isinstance(label.get("holder"), dict) and label["holder"].get("business")))
        for match in truth.CORPUS_SSN.finditer(text):
            party = self.near(text, match.start()) or owners[0]
            self._add(party, "ssn", match.group())
        for match in truth.CORPUS_EIN.finditer(text):
            normal = truth._alnum(match.group())
            if truth._filler(normal):
                continue
            if business_doc:
                self._add(owners[0], "tin", match.group())
            else:
                self.out.other_numbers.setdefault(normal, "ein")
        for match in _DOB_TEXT.finditer(text):
            iso = _iso_date(match.group(1))
            if iso:
                self._add(self.near(text, match.start()) or owners[0], "dob", iso)
        for match in _DL_TEXT.finditer(text):
            party = self.near(text, match.start()) or owners[0]
            self._add(party, "dl", {"number": match.group(1).upper(), "state": ""})

    def near(self, text: str, position: int) -> Party | None:
        """The party whose name is written closest above ``position`` (four lines)."""
        start = text.rfind("\n", 0, position) + 1
        lines_before = text[:start].split("\n")[-5:-1] if start else []
        same = text[start:position]
        candidates = [same, *reversed(lines_before)]
        for chunk in candidates:
            best = None
            for party in self.parties.values():
                for person in party.people:
                    for form in (person.full, f"{person.first} {person.last}",
                                 f"{person.last}, {person.first}"):
                        for found in truth._word(form).finditer(chunk):
                            if best is None or found.end() > best[0]:
                                best = (found.end(), party)
            if best is not None:
                return best[1]
        return None

    def third_names(self) -> None:
        """Third parties a Zelle row or payment app names with a phone or email."""
        named = set(self.parties)
        keep = []
        seen = set()
        for person in self.out.third_people:
            ident = _ident(person)
            if ident in named or ident in seen:
                continue
            seen.add(ident)
            keep.append(person)
        self.out.third_people = keep


def _street_key(address: str) -> str:
    return " ".join(address.split(",")[0].casefold().split())


def _address_line(address: dict) -> str:
    line1 = " ".join(str(address.get("line1", "")).split())
    city = " ".join(str(address.get("city", "")).split())
    state = str(address.get("state", "") or "")
    zip5 = str(address.get("zip", "") or "")
    tail = " ".join(x for x in (state, zip5) if x)
    return ", ".join(x for x in (line1, city, tail) if x)


def _norm_address(text: str) -> str:
    """ "1734 W Gentile St, Layton UT 84041" -> "1734 W Gentile St, Layton, UT 84041"."""
    parts = [p.strip() for p in text.split(",")]
    if len(parts) == 2:
        match = re.match(r"(.*?)\s+([A-Z]{2})\s+(\d{5}(?:-\d{4})?)$", parts[1])
        if match:
            return f"{parts[0]}, {match.group(1)}, {match.group(2)} {match.group(3)}"
    return ", ".join(parts)


_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), 1)}


def _iso_date(text: str) -> str:
    text = text.strip()
    match = re.fullmatch(r"(\d{4})-(\d{2})-(\d{2})", text)
    if match:
        return text
    match = re.fullmatch(r"(\d{1,2})/(\d{1,2})/(\d{2,4})", text)
    if match:
        month, day, year = (int(g) for g in match.groups())
        year += 1900 if year < 100 and year > 30 else (2000 if year < 100 else 0)
        return f"{year:04d}-{month:02d}-{day:02d}" if 1 <= month <= 12 and 1 <= day <= 31 else ""
    match = re.fullmatch(r"([A-Za-z]{3})[a-z]*\.? (\d{1,2}),? (\d{4})", text)
    if match and match.group(1).casefold() in _MONTHS:
        return f"{int(match.group(3)):04d}-{_MONTHS[match.group(1).casefold()]:02d}-{int(match.group(2)):02d}"
    return ""


def build(labels: list[tuple[Path, dict]], texts: dict[Path, str] | None = None,
          secrets: truth.Secrets | None = None, typed_tails: bool = False) -> PartyTruth:
    """The case's party sheet and the truth to score it by.

    ``labels`` are (path, JSON) for every label of the case, in a stable order
    - the first two adults seen are petitioner and respondent. ``texts`` maps
    a PDF to its text, for what only the page carries (SSNs, dependents).
    ``secrets`` is the case's :class:`truth.Secrets`, whose people are sorted
    into relatives and third parties. ``typed_tails`` puts the masked tails
    found only in transaction text on the sheet too, as if the attorney had
    typed every one; by default they are party truth but not typed.
    """
    builder = _Builder(labels, texts or {}, secrets, typed_tails)
    builder.people()
    builder.identifiers()
    builder.third_names()
    out = builder.out
    # a third party's phone or email the parties also use is theirs
    party_phones = {_digits(p)[-10:] for party in out.parties for p in party.sheet["phones"]}
    party_emails = {e.casefold() for party in out.parties for e in party.sheet["emails"]}
    party_streets = {_street_key(a) for party in out.parties for a in party.sheet["addresses"]}
    out.third_values["phones"] -= party_phones
    out.third_values["emails"] -= party_emails
    out.third_values["addresses"] = {a for a in out.third_values["addresses"]
                                     if _street_key(a) not in party_streets}
    return out
