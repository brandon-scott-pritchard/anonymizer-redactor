"""Is each real person, and each business, given one stand-in everywhere?

The leak and figure scores say nothing about it: a run can replace every
name and still turn one person into three invented people - the full name
into one, an OCR misreading of the surname into another, the surname alone
into a third - or into an invented name on one page and "[PERSON-4]" on the
next. Read as a case file, that is three people where there was one, and a
reader cannot follow who paid whom.

Scored from the run's own mapping store against the labelled people of the
case: every entity whose written form is a labelled person's (allowing for
OCR misreadings and nicknames) is tied to that person, and the stand-ins
those entities were given are counted.

Also here: what an anonymized run takes that it should not - ordinary words
and dates - counted from the text a typed page keeps.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from . import truth

# OCR's usual confusions, folded before two spellings are compared
_OCR_FOLD = str.maketrans({"0": "o", "1": "l", "i": "l", "|": "l", "!": "l", "5": "s",
                           "8": "b", "6": "g", "$": "s", "@": "a"})


def _fold(word: str) -> str:
    word = re.sub(r"[^a-z0-9|!$@]", "", word.casefold()).replace("rn", "m").replace("vv", "w")
    return word.translate(_OCR_FOLD)


def _close(a: str, b: str) -> bool:
    """The same word once OCR's misreadings are allowed for."""
    a, b = _fold(a), _fold(b)
    if not a or not b:
        return False
    if a == b:
        return True
    if min(len(a), len(b)) < 5 or abs(len(a) - len(b)) > 1:
        return False
    return _distance(a, b) <= 1


def _distance(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1,
                               previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def _first_names_match(written: str, real: str) -> bool:
    from redactor import names as _names

    if _close(written, real):
        return True
    w, r = written.casefold(), real.casefold()
    for formal, short in _names.NICKNAMES.items():
        forms = {formal.casefold(), *(s.casefold() for s in short)}
        if w in forms and r in forms:
            return True
    return False


def _tokens(text: str) -> list[str]:
    return [t for t in re.split(r"[^\w|!$@']+", text) if t]


def _whose(canonical: str, people: list[truth.Person]) -> list[truth.Person]:
    """The labelled people this written form could be."""
    tokens = _tokens(canonical)
    if not tokens:
        return []
    found = []
    for person in people:
        last = person.last
        surname = [t for t in tokens if _close(t, last)]
        # glued: the word holds the first name too - "toABIGAILOKAFOR" is
        # Abigail; "CRUMBLCOOKIES" and a tracking number holding "COX" are not
        glued = [t for t in tokens if len(t) > len(last) + 2 and _fold(last) in _fold(t)
                 and person.first and _fold(person.first) in _fold(t)]
        if not surname and not glued:
            continue
        others = [t for t in tokens if t not in surname and t not in glued]
        if glued and any(_fold(person.first) in _fold(t) for t in glued):
            found.append(person)                       # "ThomasLarsen": this one person
            continue
        if not others:
            found.append(person)                       # the surname alone, or glued
            continue
        firsts = [t for t in others if len(t) > 1]
        if any(_first_names_match(t, person.first) for t in firsts):
            found.append(person)
        elif all(len(t.strip(".")) == 1 for t in others) and \
                others[0].strip(".").casefold() == person.first[:1].casefold():
            found.append(person)                       # "K. Harrington"
    return found


def _stand_in(entity, store) -> tuple[str, str]:
    """(kind, stand-in) the run gave an entity: its group head's, if it has one."""
    head = store.entities.get(entity.parent) if entity.parent else None
    head = head or entity
    replacement = head.replacement or ""
    if replacement.startswith("[") or not head.is_person:
        return "bracket", re.sub(r"\d+", "#", replacement) if replacement.startswith("[") \
            else replacement
    return "name", replacement


def score(store, secrets: truth.Secrets) -> dict:
    """Stand-ins per labelled person, surname and business, from a run's store."""
    people = [p for p in secrets.people.values() if p.last]
    per_person: dict[str, dict] = {}
    per_surname: dict[str, Counter] = defaultdict(Counter)
    for entity in store.entities.values():
        if not entity.enabled:
            continue
        owners = _whose(entity.canonical, people)
        if not owners:
            continue
        kind, stand_in = _stand_in(entity, store)
        surname_only = len(owners) > 1 or all(
            _close(t, owners[0].last) for t in _tokens(entity.canonical))
        for person in owners[:1] if not surname_only else owners:
            slot = per_person.setdefault(person.full, {
                "household": person.household, "names": Counter(), "brackets": Counter(),
                "forms": []})
            if kind == "name":
                full = " ".join(stand_in.split())
                if not surname_only:
                    slot["names"][full.casefold()] += 1
                per_surname[person.last.casefold()][full.split()[-1].casefold()
                                                    if full else ""] += 1
            else:
                slot["brackets"][stand_in] += 1
            slot["forms"].append(f"{entity.canonical} -> {stand_in} ({entity.category})")
    out_people = {}
    for full, slot in per_person.items():
        out_people[full] = {
            "household": slot["household"],
            "stand_ins": len(slot["names"]),
            "brackets": sum(slot["brackets"].values()),
            "mixed": bool(slot["names"]) and bool(slot["brackets"]),
            "forms": slot["forms"][:40],
        }
    businesses = {}
    for org in secrets.orgs:
        stand_ins = Counter()
        forms = []
        for entity in store.entities.values():
            if entity.enabled and _same_business(entity.canonical, org):
                kind, stand_in = _stand_in(entity, store)
                stand_ins[re.sub(r"\d+", "#", stand_in) if kind == "bracket" else stand_in] += 1
                forms.append(f"{entity.canonical} -> {stand_in} ({entity.category})")
        if forms:
            businesses[org] = {"stand_ins": len(stand_ins), "forms": forms[:20]}
    addresses = {}
    for address in secrets.addresses:
        number = re.match(r"\s*(\d+)", address)
        words = [t for t in _tokens(address)[1:] if len(t) > 2 and not t.isdigit()]
        if not number or not words:
            continue
        stand_ins = Counter()
        forms = []
        for entity in store.entities.values():
            if not entity.enabled or entity.category != "street_address":
                continue
            mine = re.match(r"\s*(\d+)", entity.canonical)
            if not mine or mine.group(1) != number.group(1):
                continue
            theirs = [t for t in _tokens(entity.canonical)[1:] if len(t) > 2]
            if not any(_close(a, b) for a in words for b in theirs):
                continue
            kind, stand_in = _stand_in(entity, store)
            stand_ins[re.sub(r"\d+", "#", stand_in) if kind == "bracket" else stand_in] += 1
            forms.append(f"{entity.canonical} -> {stand_in}")
        if forms:
            addresses[address] = {"stand_ins": len(_head_replacements(store, address, number.group(1), words)),
                                  "forms": forms[:20]}
    return {
        "addresses": addresses,
        "people": out_people,
        "surnames": {s: dict(c) for s, c in per_surname.items()},
        "businesses": businesses,
    }


def _head_replacements(store, address: str, number: str, words: list[str]) -> set[str]:
    """The distinct placeholders one labelled address was given."""
    out = set()
    for entity in store.entities.values():
        if not entity.enabled or entity.category != "street_address":
            continue
        mine = re.match(r"\s*(\d+)", entity.canonical)
        if not mine or mine.group(1) != number:
            continue
        theirs = [t for t in _tokens(entity.canonical)[1:] if len(t) > 2]
        if any(_close(a, b) for a in words for b in theirs):
            head = store.entities.get(entity.parent) if entity.parent else entity
            out.add((head or entity).replacement)
    return out


def _same_business(written: str, org: str) -> bool:
    a = [t for t in _tokens(written) if len(t) > 2 and t.casefold() not in _ORG_FILLER]
    b = [t for t in _tokens(org) if len(t) > 2 and t.casefold() not in _ORG_FILLER]
    if not a or not b:
        return False
    hits = sum(any(_close(x, y) for y in b) for x in a)
    return hits >= 1 and hits / max(len(a), len(b)) >= 0.6


_ORG_FILLER = {"llc", "inc", "co", "corp", "company", "the", "and", "ltd", "pllc", "llp"}


# --------------------------------------------------------------------------
# one stand-in per party per kind (see eval.sheet)
# --------------------------------------------------------------------------

# entities whose last four make them one account: the masked tail and the full
# number are one account and must read as one
_ACCOUNTISH = {"bank_account", "masked_account", "credit_card", "investment_account",
               "loan_number", "iban"}
_MASK = re.compile(r"[xX*•#]|\.\.|ending|last", re.IGNORECASE)


def _head(entity, store):
    head = store.entities.get(entity.parent) if entity.parent else None
    return head or entity


def _digits(text: str) -> str:
    return re.sub(r"\D", "", text)


def _phone10(text: str) -> str:
    digits = _digits(text)
    return digits[1:] if len(digits) == 11 and digits[0] == "1" else digits


def _same_address(a: str, b: str, zip4=frozenset()) -> bool:
    from redactor import grouping

    x, y = grouping.parse_address(a), grouping.parse_address(b)
    if x is None or y is None or not x.number:
        return False
    if not y.number and not y.box:
        # the city line or the ZIP+4 under the street line is the same house:
        # "LAYTON UT 84041-4986" given a placeholder of its own is the split
        # the attorney's run was full of
        zip_only = b.strip() in zip4
        return bool(x.zip5) and y.zip5 == x.zip5 and (
            zip_only or (bool(y.city) and set(y.city) <= set(x.city)))
    if x.number != y.number:
        return False
    if x.street == y.street:
        return True
    words = [w for w in x.street if len(w) > 2 and not w.isdigit()]
    theirs = [w for w in y.street if len(w) > 2 and not w.isdigit()]
    return any(_close(p, q) for p in words for q in theirs)


def party_consistency(store, parties) -> dict:
    """For each party and kind, how many stand-ins each of its values got.

    ``parties`` is :class:`eval.sheet.PartyTruth`. One respondent address
    written five ways must come out as one placeholder; "respondent address:
    3 placeholders" is the failure the attorney's overnight run was full of.
    Accounts are one value per last four. A form that matches two parties
    (the family surname alone) is left to the family-surname row.
    """
    everyone = [(p, person) for p in parties.parties for person in p.people]
    people = [person for _p, person in everyone]
    owner_of = {id(person): p for p, person in everyone}
    out: dict[str, dict] = {}

    def slot(party, kind, value):
        values = out.setdefault(party.key, {}).setdefault(kind, {})
        return values.setdefault(value, {"value": value, "replacements": Counter(), "forms": []})

    def hit(party, kind, value, entity):
        head = _head(entity, store)
        entry = slot(party, kind, value)
        entry["replacements"][" ".join((head.replacement or "").split())] += 1
        if len(entry["forms"]) < 20:
            entry["forms"].append(f"{entity.canonical} -> {head.replacement} ({entity.category})")

    # what reached a page: a sheet value's generated printed forms that no
    # document uses ("1734 West GENTILE Street") cannot read as two people
    active = [e for e in store.entities.values() if e.enabled and e.occurrences > 0]
    for entity in active:
        text = entity.canonical
        digits = _digits(text)
        if entity.is_person:
            owners = {id(owner_of[id(p)]): owner_of[id(p)] for p in _whose(text, people)}
            surname_only = all(any(_close(t, p.last) for p in people) for t in _tokens(text))
            if len(owners) == 1 and not surname_only:
                hit(next(iter(owners.values())), "name", "names", entity)
            continue
        for party in parties.parties:
            sheet = party.sheet
            if entity.category == "street_address" or re.match(r"\s*\d+\s+\w", text):
                for address in sheet["addresses"]:
                    if _same_address(address, text, parties.zip4):
                        hit(party, "address", address, entity)
            if digits:
                for phone in sheet["phones"]:
                    if _phone10(text) == _digits(phone)[-10:]:
                        hit(party, "phone", phone, entity)
                for ssn in sheet["ssn"]:
                    real = _digits(ssn)
                    if digits == real or (entity.category == "ssn" and len(digits) == 4
                                          and real.endswith(digits)) \
                            or (_MASK.search(text) and len(digits) == 4 and real.endswith(digits)
                                and entity.category == "ssn"):
                        hit(party, "ssn", ssn, entity)
                for _kind, item in parties.party_accounts(party):
                    real = _digits(item["number"])
                    if len(real) < 4:
                        continue
                    last4 = real[-4:]
                    full = len(real) >= 6 and digits == real
                    tail = digits.endswith(last4) and len(digits) <= len(real) and (
                        entity.category in _ACCOUNTISH or _MASK.search(text))
                    if full or tail:
                        label = f"{item.get('institution') or '?'} ****{last4}"
                        hit(party, "account", label, entity)
                for vehicle in sheet["vehicles"]:
                    alnum = re.sub(r"[^0-9A-Za-z]", "", text).upper()
                    if alnum and alnum in {re.sub(r"[^0-9A-Za-z]", "", vehicle.get(k, "")).upper()
                                           for k in ("vin", "plate")}:
                        hit(party, "vehicle", vehicle.get("vin") or vehicle.get("plate"), entity)
                for parcel in sheet["parcels"]:
                    if digits == _digits(parcel):
                        hit(party, "parcel", parcel, entity)
                if entity.category == "dob":
                    from .sheet import _iso_date
                    for dob in sheet["dob"]:
                        if _iso_date(text) == dob:
                            hit(party, "dob", dob, entity)
            low = text.casefold().strip()
            for email in sheet["emails"]:
                if low == email.casefold():
                    hit(party, "email", email, entity)
            for handle in sheet["handles"]:
                if low.lstrip("@$") == handle.casefold().lstrip("@$"):
                    hit(party, "handle", handle, entity)
            for kind, key in (("employer", "employers"), ("business", "businesses")):
                for org in sheet[key]:
                    if _same_business(text, org):
                        hit(party, kind, org, entity)
    result = {}
    for key, kinds in out.items():
        result[key] = {}
        for kind, values in kinds.items():
            result[key][kind] = [{"value": v["value"], "stand_ins": len(v["replacements"]),
                                  "replacements": dict(v["replacements"]), "forms": v["forms"]}
                                 for v in values.values()]
    return result


def party_splits(consistency: dict) -> list[str]:
    """ "respondent address: 3 placeholders" - each value given more than one stand-in."""
    out = []
    for party, kinds in (consistency or {}).items():
        for kind, values in kinds.items():
            for value in values:
                if value["stand_ins"] > 1:
                    what = kind if kind == "name" else f"{kind} {value['value']}"
                    out.append(f"{party} {what}: {value['stand_ins']} stand-ins")
    return out


def review_rows(store, groups: list[dict] | None = None) -> dict:
    """How many rows the operator would see on the review screen.

    The parties-first server returns ``groups`` with the review result, one per
    party per kind; where it is given it is counted as is. Otherwise each head
    entity is a row (its other written forms fold under it), as the review
    screen lists them today; ``rows`` counts the heads left ticked.
    """
    if groups is not None:
        kinds = Counter(str(g.get("kind", "")) for g in groups)
        return {"rows": len(groups), "listed": len(groups),
                "found": sum(1 for g in groups if g.get("count")),
                "by_kind": dict(kinds), "source": "groups"}
    heads = [e for e in store.entities.values()
             if not e.parent or e.parent not in store.entities]
    enabled = [e for e in heads if e.enabled]
    places = Counter()
    for entity in store.entities.values():
        head = _head(entity, store)
        places[head.key] += entity.occurrences
    return {"rows": len(enabled), "listed": len(heads),
            "found": sum(1 for e in enabled if places[e.key]),
            "by_kind": dict(Counter(e.category for e in enabled)), "source": "heads"}


def taker(store):
    """``took(digits)``: the category of the entity a number was taken by.

    The entity whose digits are exactly the number wins, then the shortest
    whose digits hold it - "1734" was taken by the address it starts.
    """
    index = []
    for entity in store.entities.values():
        if not entity.enabled:
            continue
        digits = _digits(entity.canonical)
        if len(digits) >= 4:
            index.append((digits, entity))

    def took(number: str) -> str:
        best = None
        for digits, entity in index:
            if number not in digits:
                continue
            rank = (digits != number, len(entity.canonical))
            if best is None or rank < best[0]:
                best = (rank, entity.category)
        return best[1] if best else ""
    return took


# --------------------------------------------------------------------------
# what an anonymized page should have kept
# --------------------------------------------------------------------------

_WORD = re.compile(r"[A-Za-z][a-z]{2,}")
_DATE = re.compile(r"\b\d{1,2}/\d{1,2}(?:/\d{2,4})?\b|\b(?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|"
                   r"Nov|Dec)[a-z]*\.? \d{1,2},? \d{4}\b")


def _secret_words(secrets: truth.Secrets) -> set[str]:
    words = set()
    for person in secrets.people.values():
        words.update(t.casefold() for t in _tokens(person.full))
    for org in secrets.orgs:
        words.update(t.casefold() for t in _tokens(org))
    for address in secrets.addresses:
        words.update(t.casefold() for t in _tokens(address))
    return words


def lost_words(source: str, output: str, secrets: truth.Secrets) -> Counter:
    """Ordinary words a typed page had and the delivered copy does not."""
    keep_out = _secret_words(secrets)
    before = Counter(w.casefold() for w in _WORD.findall(source))
    after = Counter(w.casefold() for w in _WORD.findall(output))
    lost = Counter()
    for word, count in before.items():
        if word in keep_out:
            continue
        gone = count - after.get(word, 0)
        if gone > 0:
            lost[word] = gone
    return lost


def word_count(source: str) -> int:
    return len(_WORD.findall(source))


def lost_dates(source: str, output: str) -> Counter:
    return Counter(_DATE.findall(source)) - Counter(_DATE.findall(output))
