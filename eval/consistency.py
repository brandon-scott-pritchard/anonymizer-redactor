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
        glued = [t for t in tokens if len(t) > len(last) + 2 and _fold(last) in _fold(t)]
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
