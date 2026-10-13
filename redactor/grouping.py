"""Gather the written forms of one thing under one row of the review list.

A scan finds the same person, address or account written several ways -
"BRETT H" beside "BRETT H. PRITCHARD", "123 N Main St" beside "123 North Main
Street", "XXXX4417" beside the full card number - and listed separately each
form got its own row, its own tick and its own replacement. Unticking one left
the others; an LLM reading the anonymized file met three addresses where there
was one.

Here each group gets a *head*, the form written most often, and every other
form is attached to it as a variant:

* the variant keeps its own tick and its own count, so a wrong form can be
  left alone without touching the rest;
* the variant takes the head's replacement - the same placeholder, or the
  head's stand-in name in the variant's own shape ("BRETT H" -> "ARLO E");
* a form that could belong to more than one head is not guessed at. It stays
  on its own row with a note naming the candidates.

Numbers are never loosened: a different house number, unit, ZIP, account or
phone digit is a different thing, however close the rest of it is. Letters
are: "N" and "North", "St" and "Street", a dropped or doubled letter in a
street or company name.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Callable, Iterable

from . import names as _names
from .mapping import Entity, MappingStore

PEOPLE = {"person", "minor"}
ORGS = {"organization", "employer"}
ACCOUNTS = {"bank_account", "masked_account", "credit_card", "investment_account",
            "loan_number"}
PHONES = {"phone", "fax"}
EMAILS = {"email"}
ADDRESSES = {"street_address"}

# a typo is one letter dropped, added, changed or swapped - and only in a word
# long enough that one letter does not make it a different word
TYPO_MIN_LENGTH = 4


# --------------------------------------------------------------------------
# the store-level entry points
# --------------------------------------------------------------------------


def group(store: MappingStore) -> None:
    """Recompute every group in the store from scratch. Idempotent."""
    for entity in store.entities.values():
        entity.parent = None
        entity.candidates = ()
        entity.group_note = ""
    _group_people(store)
    _group_values(store, ADDRESSES, _address_groups)
    _group_values(store, ORGS, _same_key_groups(_org_key, typo=True))
    _group_values(store, ACCOUNTS, _account_groups)
    _group_values(store, PHONES, _same_key_groups(_phone_key))
    _group_values(store, EMAILS, _same_key_groups(_email_key))
    _address_notes(store)
    sync(store)
    _one_replacement_per_head(store)


def sync(store: MappingStore) -> None:
    """Give every variant its head's replacement, in the variant's shape."""
    for entity in store.entities.values():
        head = head_of(store, entity)
        if head is entity:
            continue
        if entity.is_person and head.is_person:
            mapping = _partial_mapping(entity.person, head.person)
            if mapping is not None and head.surrogate is not None:
                entity.surrogate = _shaped_surrogate(entity.person, head.surrogate, mapping,
                                                     head.person)
                entity.replacement = entity.surrogate.canonical
                continue
        entity.replacement = head.replacement


def _one_replacement_per_head(store: MappingStore) -> None:
    """No two rows may share a replacement.

    A row split out of its group kept, after a rescan, whatever placeholder
    regrouping had synced onto it - its old head's - whenever the rescan
    numbered one more value and the operator's own was no longer free. Two
    things the operator called different then read as one.
    """
    seen: set[str] = set()
    changed = False
    for entity in sorted((e for e in store.entities.values() if e.parent is None),
                         key=lambda e: (e.key in store.split_out, e.key)):
        if entity.replacement and entity.replacement in seen:
            if entity.is_person and entity.person is not None:
                entity.surrogate = store._unique_person_surrogate(entity.person)
                entity.replacement = entity.surrogate.canonical
            else:
                entity.replacement = store.fresh_placeholder(entity.category)
            changed = True
        seen.add(entity.replacement)
    if changed:
        sync(store)


def attach(store: MappingStore, entity: Entity) -> None:
    """Put a value first seen mid-run into the group it belongs to.

    A run finds forms the review scan did not - the next statement writes the
    street out in full - and a fresh placeholder for each would undo the
    grouping on exactly the pages that differ.
    """
    if entity.is_person or entity.key in store.split_out:
        return
    for family, finder in ((ADDRESSES, _address_groups),
                           (ORGS, _same_key_groups(_org_key, typo=True)),
                           (ACCOUNTS, _account_groups),
                           (PHONES, _same_key_groups(_phone_key)),
                           (EMAILS, _same_key_groups(_email_key))):
        if entity.category not in family:
            continue
        heads = [e for e in store.entities.values()
                 if e.category in family and e.parent is None and e is not entity
                 and _could_join(family, entity, e)]
        for members in finder(heads + [entity]):
            if entity in members and len(members) > 1:
                head = next(e for e in members if e is not entity and e.parent is None)
                # left ticked even under an unticked head: a form nobody has
                # reviewed is redacted rather than shipped
                entity.parent = head.key
                entity.replacement = head.replacement
                return
        return


def _could_join(family: set[str], entity: Entity, head: Entity) -> bool:
    """A cheap first cut before the real comparison, for a value met mid-run.

    Re-reading every row for each new value cost a second apiece at a few
    thousand rows. Only the rows it could possibly join are compared: the same
    house number, the same company-name bucket, the same last four digits.
    """
    if family is ADDRESSES:
        mine = re.match(r"\s*(\d+)", entity.canonical)
        theirs = re.match(r"\s*(\d+)", head.canonical)
        return not mine or (theirs is not None and theirs.group(1) == mine.group(1))
    if family is ORGS:
        a, b = _org_key(entity.canonical), _org_key(head.canonical)
        return bool(a and b) and len(a) == len(b) and a[0][:1] == b[0][:1]
    digits = re.sub(r"\D", "", entity.canonical)[-4:]
    if family is ACCOUNTS or family is PHONES:
        return bool(digits) and re.sub(r"\D", "", head.canonical).endswith(digits)
    return True


def head_of(store: MappingStore, entity: Entity) -> Entity:
    seen = set()
    while entity.parent and entity.parent in store.entities and entity.key not in seen:
        seen.add(entity.key)
        entity = store.entities[entity.parent]
    return entity


def members(store: MappingStore, head: Entity) -> list[Entity]:
    """The head's variants, most frequent first."""
    return sorted((e for e in store.entities.values() if e.parent == head.key),
                  key=lambda e: (-e.occurrences, e.canonical.casefold()))


def set_enabled(store: MappingStore, entity: Entity, enabled: bool) -> list[Entity]:
    """Tick or untick a row; a head carries its variants with it."""
    changed = [entity]
    entity.enabled = enabled
    if entity.parent is None:
        for member in members(store, entity):
            member.enabled = enabled
            changed.append(member)
    return changed


def split(store: MappingStore, entity: Entity) -> None:
    """Take a wrongly grouped variant out of its group, for good."""
    if entity.parent is None:
        return
    store.split_out.add(entity.key)
    entity.parent = None
    if entity.is_person:
        entity.surrogate = store._unique_person_surrogate(entity.person)
        entity.replacement = entity.surrogate.canonical
    else:
        entity.replacement = store.fresh_placeholder(entity.category)


def group_total(store: MappingStore, head: Entity) -> int:
    return head.occurrences + sum(m.occurrences for m in members(store, head))


# --------------------------------------------------------------------------
# people: a partial name under the one full name it is part of
# --------------------------------------------------------------------------


def _tokens(person: _names.PersonName) -> list[tuple[str, str]]:
    return [(key, value.strip(".").casefold()) for key, value in person.ordered]


def _fits(part: str, whole: str) -> bool:
    if part == whole:
        return True
    return len(part) == 1 and whole[:1] == part


def _partial_mapping(part: _names.PersonName | None,
                     whole: _names.PersonName | None) -> dict[str, str] | None:
    """Which component of ``whole`` each component of ``part`` is, or None.

    "BRETT H" -> {first: first, last: middle0} of "BRETT H. PRITCHARD";
    "PRITCHARD BRETT" (surname first, no comma) maps too. Every word of the
    part must be a word of the whole or its initial, and at least one must be
    spelled out: "B. H." alone is two letters, not a name.
    """
    if part is None or whole is None:
        return None
    if part.suffix and whole.suffix and part.suffix.casefold() != whole.suffix.casefold():
        return None
    mine, theirs = _tokens(part), _tokens(whole)
    if not mine or len(mine) > len(theirs) or mine == theirs:
        return None
    if not any(len(v) > 1 for _k, v in mine):
        return None

    def assign(order: list[int]) -> dict[str, str] | None:
        # each part word, in turn, takes the next whole word it fits
        out: dict[str, str] = {}
        used: set[int] = set()
        position = 0
        for index in order:
            key, value = mine[index]
            for j in range(position, len(theirs)):
                if j not in used and _fits(value, theirs[j][1]):
                    out[key] = theirs[j][0]
                    used.add(j)
                    position = j + 1
                    break
            else:
                return None
        return out

    in_order = assign(list(range(len(mine))))
    if in_order is not None:
        return in_order
    # surname first: the last word of the part leads
    if len(mine) >= 2:
        return assign([len(mine) - 1, *range(len(mine) - 1)])
    return None


def _shaped_surrogate(part: _names.PersonName, stand_in: _names.PersonName,
                      mapping: dict[str, str],
                      whole: _names.PersonName | None = None) -> _names.PersonName:
    """The head's stand-in written in the part's shape.

    An initial stays an initial - unless the head's own component is one too,
    because the head's stand-in spells that component out, and "BRETT H"
    must read the same as the "Brett H." inside "Brett H. Pritchard".
    """
    lookup = dict(stand_in.ordered)
    real = dict(whole.ordered) if whole is not None else {}
    fallback = stand_in.last or stand_in.first

    def value(key: str, original: str) -> str:
        target_key = mapping.get(key, "")
        target = lookup.get(target_key, "") or fallback
        head_initial = len(real.get(target_key, "xx").strip(".")) == 1
        if len(original.strip(".")) == 1 and not head_initial:
            return target[:1].upper() + ("." if original.endswith(".") else "")
        return target

    first = value("first", part.first) if part.first else ""
    middles = tuple(value(f"middle{i}", m) for i, m in enumerate(part.middles))
    last = value("last", part.last) if part.last else ""
    raw = " ".join(p for p in (first, *middles, last) if p)
    return _names.PersonName(raw=raw, first=first, middles=middles, last=last,
                             suffix=part.suffix)


def _group_people(store: MappingStore) -> None:
    people = [e for e in store.entities.values()
              if e.category in PEOPLE and e.person is not None]
    # richest first: a head is decided before anything that could hang off it
    people.sort(key=lambda e: (-len(e.person.ordered), -len(e.canonical)))
    heads: list[Entity] = []
    for entity in people:
        if entity.key in store.split_out or entity.distinct:
            heads.append(entity)
            continue
        candidates = [h for h in heads
                      if h.category == entity.category and not h.candidates
                      and _partial_mapping(entity.person, h.person) is not None]
        if len(candidates) == 1:
            entity.parent = candidates[0].key
        elif len(candidates) > 1:
            entity.candidates = tuple(h.canonical for h in candidates)
        else:
            heads.append(entity)


# --------------------------------------------------------------------------
# values
# --------------------------------------------------------------------------


def _group_values(store: MappingStore, family: set[str],
                  finder: Callable[[list[Entity]], Iterable[list[Entity]]]) -> None:
    pool = [e for e in store.entities.values()
            if e.category in family and e.key not in store.split_out]
    for found in finder(pool):
        if len(found) < 2:
            continue
        head = max(found, key=_prevalence)
        for entity in found:
            if entity is not head:
                entity.parent = head.key


def _prevalence(entity: Entity) -> tuple:
    """The head is the form written most; a tie goes to the fuller form."""
    return (entity.occurrences, len(entity.canonical), entity.canonical)


def _typo(a: str, b: str) -> bool:
    """One letter dropped, added, changed, or two neighbours swapped."""
    if a == b:
        return True
    if not (a.isalpha() and b.isalpha()) or min(len(a), len(b)) < TYPO_MIN_LENGTH:
        return False
    if abs(len(a) - len(b)) > 1:
        return False
    if len(a) == len(b):
        diff = [i for i in range(len(a)) if a[i] != b[i]]
        if len(diff) == 1:
            return True
        return (len(diff) == 2 and diff[1] == diff[0] + 1
                and a[diff[0]] == b[diff[1]] and a[diff[1]] == b[diff[0]])
    short, long_ = (a, b) if len(a) < len(b) else (b, a)
    return any(long_[:i] + long_[i + 1:] == short for i in range(len(long_)))


def _words_alike(a: tuple[str, ...], b: tuple[str, ...]) -> bool:
    return len(a) == len(b) and all(_typo(x, y) for x, y in zip(a, b))


def _clusters(items: list, alike: Callable) -> list[list]:
    """Connected groups under ``alike`` - small n, so pairwise is fine."""
    parent = list(range(len(items)))

    def root(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i

    for i in range(len(items)):
        for j in range(i + 1, len(items)):
            if alike(items[i], items[j]):
                parent[root(i)] = root(j)
    out: dict[int, list] = defaultdict(list)
    for i, item in enumerate(items):
        out[root(i)].append(item)
    return list(out.values())


def _same_key_groups(key_of: Callable[[str], object], typo: bool = False):
    def finder(pool: list[Entity]) -> list[list[Entity]]:
        keyed = [(key_of(e.canonical), e) for e in pool]
        keyed = [(k, e) for k, e in keyed if k]
        if typo:
            # a slipped letter only in a name of two words or more: "Vans"
            # and "Vons" are two companies. Compared within buckets of one
            # length and first letter, not every name against every other.
            buckets: dict = defaultdict(list)
            for k, e in keyed:
                buckets[(len(k), k[0][:1])].append((k, e))
            found = []
            for bucket in buckets.values():
                found.extend(_clusters(bucket, lambda x, y: x[0] == y[0] or (
                    len(x[0]) >= 2 and _words_alike(x[0], y[0]))))
        else:
            by: dict = defaultdict(list)
            for k, e in keyed:
                by[k].append((k, e))
            found = list(by.values())
        return [[e for _k, e in members_] for members_ in found]
    return finder


# ---------------------------------------------------------------- orgs --

_ORG_SUFFIXES = {"inc", "incorporated", "llc", "l", "c", "corp", "corporation", "co",
                 "company", "ltd", "limited", "lp", "llp", "pllc", "pc", "na", "the"}


def _org_key(text: str) -> tuple[str, ...]:
    words = re.findall(r"[a-z0-9]+", text.casefold().replace("&", " and "))
    core = tuple(w for w in words if w not in _ORG_SUFFIXES)
    return core


# ------------------------------------------------------- phones, emails --

def _phone_key(text: str) -> str:
    digits = re.sub(r"\D", "", text.split("x")[0].split("ext")[0])
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    return digits if len(digits) == 10 else ""


def _email_key(text: str) -> str:
    text = text.strip().casefold()
    return text[7:] if text.startswith("mailto:") else text


# ------------------------------------------------------------ accounts --

def _account_groups(pool: list[Entity]) -> list[list[Entity]]:
    """A number however it is punctuated, and its masked tail under it.

    A tail ("XXXX4417", "ending in 4417") joins the one full number that ends
    in it; with two such numbers it is left on its own. Tails with no full
    number to join are grouped with each other.
    """
    full: dict[str, list[Entity]] = defaultdict(list)
    tails: dict[str, list[Entity]] = defaultdict(list)
    for entity in pool:
        digits = re.sub(r"\D", "", entity.canonical)
        if len(digits) < 4:
            continue
        masked = re.search(r"[xX*•#]", entity.canonical) is not None
        if masked or len(digits) == 4:
            tails[digits[-4:]].append(entity)
        else:
            full[digits].append(entity)
    out = [list(v) for v in full.values()]
    for tail, found in tails.items():
        owners = [digits for digits in full if digits.endswith(tail)]
        if len(owners) == 1:
            index = list(full).index(owners[0])
            out[index].extend(found)
        elif not owners:
            out.append(found)
        else:
            for entity in found:
                entity.candidates = tuple(full[d][0].canonical for d in owners)
    return out


# ----------------------------------------------------------- addresses --

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


class _Address:
    __slots__ = ("number", "street", "unit", "city", "zip5", "entity", "box")

    def __init__(self, entity: Entity):
        self.entity = entity
        self.number = self.unit = self.zip5 = self.box = ""
        self.street: tuple[str, ...] = ()
        self.city: tuple[str, ...] = ()


def parse_address(text: str) -> _Address | None:
    """House number, street, unit, city and ZIP, each written one way."""
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


def _same_street(a: _Address, b: _Address) -> bool:
    if a.number != b.number:
        return False
    x, y = a.street, b.street
    # a dropped leading direction: "1734 GENTILE ST" and "1734 W GENTILE ST"
    if len(x) == len(y) + 1 and x[0] in _DIRECTIONS.values() and x[1:] == y:
        return True
    if len(y) == len(x) + 1 and y[0] in _DIRECTIONS.values() and y[1:] == x:
        return True
    return _words_alike(x, y)


def _has_direction(a: _Address) -> bool:
    return bool(a.street) and a.street[0] in _DIRECTIONS.values()


def _same_place(a: _Address, b: _Address) -> bool:
    """Not contradicted by the ZIP or the city, when both carry them."""
    if a.zip5 and b.zip5 and a.zip5 != b.zip5:
        return False
    if a.city and b.city and not _words_alike(a.city, b.city):
        return False
    return True


def _by_place(cluster: list[_Address]) -> list[list[_Address]]:
    """One group per town; a form that names no town joins the only one."""
    placed = [a for a in cluster if a.zip5 or a.city]
    loose = [a for a in cluster if not (a.zip5 or a.city)]
    towns = _clusters(placed, _same_place) if placed else []
    # a town that conflicts with another inside one cluster stays apart
    if len(towns) <= 1:
        return [(towns[0] if towns else []) + loose]
    out = [list(t) for t in towns]
    for a in loose:
        a.entity.candidates = tuple(t[0].entity.canonical for t in towns)
        out.append([a])
    return out


def _address_groups(pool: list[Entity]) -> list[list[Entity]]:
    parsed = []
    for entity in pool:
        address = parse_address(entity.canonical)
        if address is not None:
            address.entity = entity
            parsed.append(address)
    streets = [a for a in parsed if a.number]
    boxes = [a for a in parsed if a.box]
    lines = [a for a in parsed if not a.number and not a.box]   # city lines, ZIP+4s

    groups: list[list[_Address]] = []
    # streets: same number, same street (letters may slip), same unit - then
    # split by place, so a form with no city cannot chain "2750 Aspen Springs
    # Dr, Park City" to "2750 Aspen Springs Dr, Twin Falls"
    by_number: dict[str, list[_Address]] = defaultdict(list)
    for a in streets:
        by_number[a.number].append(a)
    for bucket in by_number.values():
        for cluster in _clusters(bucket, lambda a, b: _words_alike(a.street, b.street)
                                 and a.unit == b.unit):
            groups.extend(_by_place(cluster))
    # A form that dropped its direction joins the one direction it could be.
    # Matched loosely before, "620 1200 S" chained "620 E 1200 S" to
    # "620 W 1200 S" - two houses on a Utah grid, one placeholder.
    for loose in [g for g in groups if all(not _has_direction(a) for a in g)]:
        homes = [g for g in groups if g is not loose and any(_has_direction(a) for a in g)
                 and any(_same_street(a, b) and _same_place(a, b) and a.unit == b.unit
                         for a in g for b in loose)]
        if len(homes) == 1:
            homes[0].extend(loose)
            loose.clear()
        elif len(homes) > 1:
            for a in loose:
                a.entity.candidates = tuple(g[0].entity.canonical for g in homes)
    groups = [g for g in groups if g]
    # a form with no unit joins the one unit it could be, never one of two
    for loose in [g for g in groups if all(not a.unit for a in g)]:
        homes = [g for g in groups if g is not loose and any(a.unit for a in g)
                 and any(_same_street(a, b) and _same_place(a, b) for a in g for b in loose)]
        if len(homes) == 1:
            homes[0].extend(loose)
            loose.clear()
        elif len(homes) > 1:
            for a in loose:
                a.entity.candidates = tuple(g[0].entity.canonical for g in homes)
    groups = [g for g in groups if g]
    for cluster in _clusters(boxes, lambda a, b: a.box == b.box and _same_place(a, b)):
        groups.append(cluster)
    # a city line or a ZIP+4 joins the one street group with its ZIP
    for line in lines:
        homes = [g for g in groups if any(a.zip5 and a.zip5 == line.zip5
                                          and _same_place(a, line) for a in g)]
        if len(homes) == 1:
            homes[0].append(line)
        elif len(homes) > 1:
            line.entity.candidates = tuple(g[0].entity.canonical for g in homes)
    return [[a.entity for a in g] for g in groups]


def _address_notes(store: MappingStore) -> None:
    """A near miss on the digits is reported on the row, never merged."""
    by_text = {e.canonical: e for e in store.entities.values()
               if e.category in ADDRESSES and e.parent is None}
    heads = []
    for text, entity in by_text.items():
        address = parse_address(text)
        if address is not None and address.number:
            address.entity = entity
            heads.append(address)
    for a in heads:
        for b in heads:
            if (a is not b and a.number != b.number and a.street == b.street
                    and _same_place(a, b) and _typo_digits(a.number, b.number)):
                a.entity.group_note = (f"similar to {b.entity.canonical} - "
                                       "a different house number, so not merged")


def _typo_digits(a: str, b: str) -> bool:
    if len(a) != len(b):
        return False
    diff = [i for i in range(len(a)) if a[i] != b[i]]
    return len(diff) == 1 or (len(diff) == 2 and diff[1] == diff[0] + 1
                              and a[diff[0]] == b[diff[1]])
