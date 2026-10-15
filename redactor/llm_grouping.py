"""The local model's say on which names in the review list are one person.

The rules in grouping.py fold a partial name under the one full name it is
part of, and nothing looser: "HARRlNGTON" and "Harringten" are OCR's
Harrington, "Bill" is William, "Kelly Harrington-Moss" is Kelly Harrington -
and each kept a row and an invented name of its own. The attorney read one
person under three names, or under a bracket in one place and a name in
another.

Here the local model (:mod:`redactor.llm`) is shown the people and businesses
found, numbered, and asked which are the same real person or business. What
it answers is checked before anything is folded:

* numbers out of range, and an item claimed by two groups, are ignored;
* two rows registered apart on purpose (both ``distinct``), a child and an
  adult, two people with different first names - a petitioner and a
  respondent who share a surname above all - are never merged, whatever the
  model says;
* a row the operator split out of a group stays out.

A merged form is folded as grouping.py folds one - ``parent`` set to the head,
so it takes the head's invented name in its own shape - and says so on its
row. Split undoes it like any other merge. A business carrying a person's
name is not made a form of the person: it keeps its business words and takes
the person's invented surname in place of the real one.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import grouping, names as _names
from .mapping import Entity, MappingStore

PEOPLE = grouping.PEOPLE
ORGS = grouping.ORGS
CHILD_CATEGORIES = {"minor", "minor_initials"}
# items asked about in one request; a family is never cut in two, and each
# request repeats the families at the end of the one before it
CHUNK = 150
OVERLAP = 25
NOTE = "merged by the local model"

SYSTEM = (
    "You are helping anonymize the documents of one family-law case. Each numbered line "
    "below is one written form of a name that a scan found in the documents: its text, "
    "its type, how many times it was found, and sometimes its role in the case or the "
    "item it is already listed under.\n\n"
    "Group the items that are the same real person, or the same business:\n"
    "- OCR misreadings of one name (HARRlNGTON, Harringten, Srnith for Smith, letters "
    "run together);\n"
    "- nicknames and full first names (Bill and William, Kate and Katherine);\n"
    "- initials (K. Harrington, KDH) and forms with or without a middle name;\n"
    "- a hyphenated, maiden or married surname of the same person;\n"
    "- a business that carries a person's name (Harrington Enterprises) goes in that "
    "person's group.\n\n"
    "Never group two different people. Relatives who share a surname - husband and "
    "wife, parent and child, brothers and sisters - are different people. A "
    "petitioner and a respondent are different people. A child is never the same "
    "person as an adult. Leave out anything you are not sure of, and leave out items "
    "that have no other form.")
SCHEMA = ('{"groups": [{"head": <the item number of the fullest correct form>, '
          '"members": [<item numbers of the other forms>], '
          '"why": "<a few words: OCR misreading, nickname, initials, maiden name, '
          'business named after the person>"}]}')


@dataclass
class Merge:
    head: str                                      # entity keys
    members: list[str] = field(default_factory=list)
    businesses: list[str] = field(default_factory=list)
    why: str = ""
    refused: list[tuple[str, str]] = field(default_factory=list)   # (key, reason)


# --------------------------------------------------------------------------
# how alike two written names are
# --------------------------------------------------------------------------

# the letters OCR confuses, each written one way
_CONFUSED = (("rn", "m"), ("vv", "w"), ("cl", "d"), ("1", "i"), ("l", "i"), ("|", "i"),
             ("!", "i"), ("0", "o"), ("5", "s"), ("8", "b"))


def ocr_key(text: str) -> str:
    folded = text.casefold()
    for wrong, right in _CONFUSED:
        folded = folded.replace(wrong, right)
    return re.sub(r"[^a-z]", "", folded)


def _distance(a: str, b: str, cap: int) -> int:
    """Edit distance, giving up past ``cap``."""
    if abs(len(a) - len(b)) > cap:
        return cap + 1
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1,
                               previous[j - 1] + (x != y)))
        if min(current) > cap:
            return cap + 1
        previous = current
    return previous[-1]


def similar(a: str, b: str) -> bool:
    """One word misread as the other: the same once OCR's usual confusions
    are undone, or a letter off (two in a long word)."""
    x, y = ocr_key(a), ocr_key(b)
    if not x or not y:
        return False
    if x == y:
        return True
    shortest = min(len(x), len(y))
    if shortest >= 9:
        return _distance(x, y, 2) <= 2
    return shortest >= 4 and _distance(x, y, 1) <= 1


def _nickname(a: str, b: str) -> bool:
    a, b = a.strip(".").casefold(), b.strip(".").casefold()
    return a in _names._BY_NICKNAME.get(b, ()) or b in _names._BY_NICKNAME.get(a, ())


def _pieces(word: str) -> list[str]:
    return [p for p in re.split(r"[-\s]", word) if p]


def _words_alike(a: str, b: str) -> bool:
    """One name word another's: equal, its initial, misread, its nickname, or
    one piece of a hyphenated surname."""
    x, y = a.strip(".").casefold(), b.strip(".").casefold()
    if not x or not y:
        return False
    if x == y or ((len(x) == 1 or len(y) == 1) and x[0] == y[0]):
        return True
    if similar(x, y) or _nickname(x, y):
        return True
    return any(similar(p, q) for p in _pieces(x) for q in _pieces(y)
               if len(_pieces(x)) > 1 or len(_pieces(y)) > 1)


def loose_mapping(part: _names.PersonName, whole: _names.PersonName) -> dict[str, str]:
    """Which component of ``whole`` each component of ``part`` is - as
    grouping's mapping, but a misread, a nickname or a hyphenated piece fits
    too. A word that fits nothing takes the component of its own kind."""
    strict = grouping._partial_mapping(part, whole)
    if strict is not None:
        return strict
    theirs = list(whole.ordered)
    keys = {key for key, _value in theirs}
    out: dict[str, str] = {}
    used: set[str] = set()
    for key, value in part.ordered:
        found = next((k for k, v in theirs if k not in used and _words_alike(value, v)), None)
        if found is None and key in keys and key not in used:
            found = key
        if found is None:
            found = "last" if "last" in keys and "last" not in used else \
                next((k for k, _v in theirs if k not in used), "last")
        out[key] = found
        used.add(found)
    return out


def _first(entity: Entity) -> str:
    person = entity.person
    if person is None or not person.first or len(person.first.strip(".")) < 2:
        return ""
    return person.first


def _is_child(entity: Entity) -> bool:
    return entity.category in CHILD_CATEGORIES or (
        entity.distinct and "child" in entity.role.casefold())


def _party(entity: Entity) -> bool:
    role = entity.role.casefold()
    return bool(role) and not role.startswith(("attorney", "counsel")) and "child" not in role


def _surnames_related(a: Entity, b: Entity, why: str) -> bool:
    x = a.person.last if a.person else ""
    y = b.person.last if b.person else ""
    if not x or not y or _words_alike(x, y):
        return True
    # a different surname altogether is one person only as a maiden or
    # married name, and only when the model says that is what it is
    return bool(re.search(r"maiden|married|n[ée]e\b|former|previous", why, re.IGNORECASE))


def refusal(a: Entity, b: Entity, why: str = "") -> str:
    """Why ``a`` and ``b`` may never be merged as one person, or ''."""
    if a.distinct and b.distinct:
        return "both registered apart on purpose"
    if _is_child(a) != _is_child(b):
        return "a child and an adult"
    first_a, first_b = _first(a), _first(b)
    if first_a and first_b and not _words_alike(first_a, first_b):
        if _party(a) and _party(b):
            return "two parties named in the caption"
        return "different first names"
    if not _surnames_related(a, b, why):
        return "different surnames"
    return ""


# --------------------------------------------------------------------------
# asking
# --------------------------------------------------------------------------


def candidates(store: MappingStore) -> list[Entity]:
    """The ticked people and businesses, families together."""
    items = [e for e in store.entities.values()
             if e.enabled and (e.category in PEOPLE or e.category in ORGS)]
    surnames = {ocr_key(e.person.last) for e in items
                if e.category in PEOPLE and e.person is not None and e.person.last}

    def family(entity: Entity) -> str:
        if entity.category in PEOPLE and entity.person is not None:
            return ocr_key(entity.person.last or entity.canonical)
        words = [ocr_key(w) for w in entity.canonical.split()]
        return next((w for w in words if w in surnames), words[0] if words else "")

    return sorted(items, key=lambda e: (family(e), e.category in ORGS, e.canonical.casefold()))


def _families(items: list[Entity]) -> list[list[Entity]]:
    out: list[list[Entity]] = []
    key = None
    for entity in items:
        mine = ocr_key(entity.person.last) if entity.person is not None and entity.person.last \
            else ""
        mine = mine or ocr_key(entity.canonical.split()[0] if entity.canonical.split() else "")
        if out and mine and mine[:4] == (key or "")[:4]:
            out[-1].append(entity)
        else:
            out.append([entity])
            key = mine
    return out


def chunks(items: list[Entity], size: int = CHUNK, overlap: int = OVERLAP) -> list[list[Entity]]:
    """The list in requests of about ``size``, a family never cut in two, each
    request opening with the last families of the one before."""
    out: list[list[Entity]] = []
    current: list[Entity] = []
    fresh = 0
    families = _families(items)
    for family in families:
        if fresh and len(current) + len(family) > size:
            out.append(current)
            tail: list[Entity] = []
            for previous in reversed(_families(current)):
                if len(tail) + len(previous) > overlap:
                    break
                tail = previous + tail
            current, fresh = list(tail), 0
        current.extend(family)
        fresh += len(family)
    if fresh:
        out.append(current)
    return out


def _line(number: int, entity: Entity, numbers: dict[str, int], store: MappingStore) -> str:
    parts = [f"{number}. {entity.canonical}", entity.label, f"{entity.occurrences}x"]
    if entity.role:
        parts.append(f"role: {entity.role}")
    if _is_child(entity):
        parts.append("a child")
    if entity.parent:
        head = grouping.head_of(store, entity)
        parts.append(f"listed under {numbers[head.key]}" if head.key in numbers
                     else f"listed under {head.canonical}")
    return " | ".join(parts)


def _ask(url: str, chunk: list[Entity], store: MappingStore) -> list[tuple[int, list[int], str]]:
    from . import llm

    numbers = {e.key: i for i, e in enumerate(chunk, 1)}
    listing = "\n".join(_line(i, e, numbers, store) for i, e in enumerate(chunk, 1))
    answer = llm.chat(url, SYSTEM, "The items:\n" + listing, SCHEMA,
                      max_tokens=max(1024, 24 * len(chunk)))
    groups = answer.get("groups", []) if isinstance(answer, dict) else answer
    out = []
    for group in groups if isinstance(groups, list) else []:
        if not isinstance(group, dict):
            continue
        head = _number(group.get("head"))
        members = [n for n in map(_number, group.get("members") or []) if n is not None]
        out.append((head, members, str(group.get("why") or "")[:80]))
    return out


def _number(value) -> int | None:
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def propose_merges(store: MappingStore, url: str, progress=lambda message, fraction: None
                   ) -> list[Merge]:
    """What the model says is one person or one business, checked."""
    items = candidates(store)
    if len(items) < 2:
        return []
    found: list[tuple[list[str], str]] = []          # (keys, head first), why
    parts = chunks(items)
    for index, chunk in enumerate(parts):
        progress("Asking the local model which names are the same person",
                 index / max(len(parts), 1))
        used: set[int] = set()
        for head, members, why in _ask(url, chunk, store):
            if head is None or not 1 <= head <= len(chunk) or head in used:
                continue
            numbers = [head] + [n for n in dict.fromkeys(members)
                                if 1 <= n <= len(chunk) and n != head and n not in used]
            if len(numbers) < 2:
                continue
            used.update(numbers)
            found.append(([chunk[n - 1].key for n in numbers], why))
    return [m for m in (_checked(store, keys, why) for keys, why in _joined(found)) if m]


def _joined(found: list[tuple[list[str], str]]) -> list[tuple[list[str], str]]:
    """Groups that share an item - the same family seen in two requests -
    made one, the first one's head leading."""
    out: list[tuple[list[str], str]] = []
    for keys, why in found:
        overlapping = [g for g in out if set(g[0]) & set(keys)]
        if not overlapping:
            out.append((list(keys), why))
            continue
        first = overlapping[0]
        for other in overlapping[1:]:
            first[0].extend(k for k in other[0] if k not in first[0])
            out.remove(other)
        first[0].extend(k for k in keys if k not in first[0])
    return out


def _checked(store: MappingStore, keys: list[str], why: str) -> Merge | None:
    """One group the model proposed, as far as it may be applied."""
    heads: list[Entity] = []
    refused: list[tuple[str, str]] = []
    for key in keys:
        entity = store.entities.get(key)
        if entity is None:
            continue
        if key in store.split_out:
            refused.append((key, "split out by hand"))
            continue
        head = grouping.head_of(store, entity)
        if head.key in store.split_out and head is not entity:
            refused.append((key, "split out by hand"))
            continue
        if head not in heads:
            heads.append(head)
    people = [h for h in heads if h.category in PEOPLE and h.is_person and h.person]
    businesses = [h for h in heads if h.category in ORGS]
    if people:
        # the head is a person: the model's, or the most written
        head = people[0] if heads and heads[0] is people[0] else \
            max(people, key=lambda e: grouping.group_total(store, e))
    elif businesses:
        head = businesses[0]
    else:
        return None
    merge = Merge(head=head.key, why=why or "the same name", refused=refused)
    accepted = [head]
    for person in people:
        if person is head:
            continue
        reason = next((r for r in (refusal(person, other, why) for other in accepted) if r), "")
        if reason:
            merge.refused.append((person.key, reason))
        else:
            accepted.append(person)
            merge.members.append(person.key)
    for business in businesses:
        if business is head:
            continue
        if head.category in ORGS:
            merge.members.append(business.key)
        else:
            merge.businesses.append(business.key)
    return merge if merge.members or merge.businesses else None


# --------------------------------------------------------------------------
# folding
# --------------------------------------------------------------------------


def _fold(store: MappingStore, entity: Entity, head: Entity, why: str, via: str) -> None:
    """``entity`` made a form of ``head``, and its own forms with it."""
    for form in grouping.members(store, entity):
        _fold(store, form, head, why, via)
    entity.parent = head.key
    entity.candidates = ()
    entity.group_note = f"{NOTE}: {why}"
    store.merged[entity.key] = (head.key, why, via)


def shape(store: MappingStore, entity: Entity) -> None:
    """A merged person form written as its head's invented name, in its own
    shape - grouping's way, through a looser mapping of the words."""
    head = grouping.head_of(store, entity)
    if head is entity or not (entity.is_person and head.is_person):
        return
    if entity.person is None or head.person is None or head.surrogate is None:
        entity.replacement = head.replacement
        return
    mapping = loose_mapping(entity.person, head.person)
    entity.surrogate = grouping._shaped_surrogate(entity.person, head.surrogate, mapping,
                                                  head.person)
    entity.replacement = entity.surrogate.canonical


def named_after(business: str, person: Entity) -> str:
    """The business's name with the person's real name words swapped for
    their invented ones: "Harrington Enterprises" -> "Tolk Enterprises"."""
    real, fake = person.person, person.surrogate
    if real is None or fake is None:
        return ""
    swaps = [(r, f) for r, f in ((real.last, fake.last), (real.first, fake.first),
                                 *zip(real.middles, fake.middles))
             if r and f and len(r.strip(".")) > 1]
    changed = False

    def swap(m: re.Match) -> str:
        nonlocal changed
        word = m.group(0)
        for r, f in swaps:
            if _words_alike(word, r) and len(word) > 1:
                changed = True
                return _names.match_case(word, f)
        return word
    out = re.sub(r"[A-Za-z][A-Za-z'’\-]*", swap, business)
    return out if changed else ""


def _name_business(store: MappingStore, business: Entity, person: Entity, why: str) -> bool:
    target = grouping.head_of(store, business)
    replacement = named_after(target.canonical, person)
    if not replacement:
        return False
    taken = {e.replacement.casefold() for e in store.entities.values()
             if e.parent is None and e is not target}
    if replacement.casefold() in taken:
        return False
    target.replacement = replacement
    target.group_note = f"named after {person.canonical} - {NOTE}"
    store.named_after[target.key] = (person.key, why, replacement)
    return True


def apply_merges(store: MappingStore, merges: list[Merge]) -> list[dict]:
    """Fold each merge into the store; what was done, for the review page."""
    done: list[dict] = []
    for merge in merges:
        head = store.entities.get(merge.head)
        if head is None:
            continue
        head = grouping.head_of(store, head)
        for key in merge.members:
            entity = store.entities.get(key)
            if entity is None or entity is head or grouping.head_of(store, entity) is head:
                continue
            _fold(store, entity, head, merge.why, key)
            done.append({"form": entity.canonical, "head": head.canonical, "why": merge.why})
        for key in merge.businesses:
            business = store.entities.get(key)
            if business is not None and _name_business(store, business, head, merge.why):
                done.append({"form": business.canonical, "head": head.canonical,
                             "why": merge.why, "business": True})
    settle(store)
    return done


def settle(store: MappingStore) -> None:
    """Keep the model's merges standing after anything else changed the list.

    A regrouping starts from scratch and would drop them; a row split out
    leaves them, with whatever came in with it; an edited head's invented
    name is carried to its merged forms in their shape.
    """
    for key, (head_key, why, via) in list(store.merged.items()):
        entity = store.entities.get(key)
        head = store.entities.get(head_key)
        if entity is None or head is None:
            store.merged.pop(key, None)
            continue
        if key in store.split_out and key != via:
            # one form split out of what came in with a merged row
            store.merged.pop(key, None)
            if entity.group_note.startswith(NOTE):
                entity.group_note = ""
            continue
        if via in store.split_out:
            _unmerge(store, via)
            continue
        target = grouping.head_of(store, head)
        if target is not entity and grouping.head_of(store, entity) is not target:
            _fold(store, entity, target, why, via)       # a regrouping undid it
    grouping.sync(store)
    for key in store.merged:
        shape(store, store.entities[key])
    for key, (person_key, why, given) in list(store.named_after.items()):
        business, person = store.entities.get(key), store.entities.get(person_key)
        if business is None or person is None:
            store.named_after.pop(key, None)
            continue
        if business.replacement == given:          # not edited by hand since
            person = grouping.head_of(store, person)
            fresh = named_after(business.canonical, person)
            if fresh and fresh != given:
                business.replacement = fresh
                store.named_after[key] = (person_key, why, fresh)
    grouping.sync(store)
    for key in store.merged:
        shape(store, store.entities[key])


def _unmerge(store: MappingStore, via: str) -> None:
    """A merged row split out: it, and the forms that came in with it, are
    the model's no longer."""
    owner = store.entities.get(via)
    for key, (_head, _why, came_with) in list(store.merged.items()):
        if came_with != via:
            continue
        form = store.entities.get(key)
        store.merged.pop(key, None)
        if form is None:
            continue
        if form.group_note.startswith(NOTE):
            form.group_note = ""
        if form is not owner and owner is not None and form.key not in store.split_out:
            form.parent = owner.key                # back under the row it came with
    if owner is not None and owner.is_person and owner.person is not None:
        # its invented name was the head's, in its shape - and an invented
        # surname follows the real one, so it would have kept the head's
        owner.surrogate = None
        owner.surrogate = store._unique_person_surrogate(owner.person)
        owner.replacement = owner.surrogate.canonical


def merged_rows(store: MappingStore) -> list[dict]:
    """What the model merged, for the review page."""
    out = []
    for key, (head_key, why, via) in store.merged.items():
        entity, head = store.entities.get(key), store.entities.get(head_key)
        if entity is not None and head is not None and key == via:
            out.append({"form": entity.canonical,
                        "head": grouping.head_of(store, head).canonical, "why": why})
    for key, (person_key, why, _given) in store.named_after.items():
        business, person = store.entities.get(key), store.entities.get(person_key)
        if business is not None and person is not None:
            out.append({"form": business.canonical, "head": person.canonical, "why": why,
                        "business": True})
    return out


# --------------------------------------------------------------------------
# a written form the final check found
# --------------------------------------------------------------------------


def stand_in(store: MappingStore, head: Entity, text: str) -> str:
    """What ``text``, a written form of ``head``, is to read as."""
    parsed = _names.parse(text)
    if not parsed.canonical or head.person is None or head.surrogate is None:
        return head.replacement
    shaped = grouping._shaped_surrogate(parsed, head.surrogate,
                                        loose_mapping(parsed, head.person), head.person)
    return _names.match_case(text, shaped.canonical)


def add_form(store: MappingStore, head: Entity, text: str, why: str,
             written: str = "") -> Entity | None:
    """``text`` registered as one more written form of ``head``, taking its
    invented name. ``written``, when given, is what the form is to read as
    instead - a glued word with only the name in it swapped."""
    parsed = _names.parse(text)
    if not parsed.canonical:
        return None
    key = f"{head.category}:{parsed.canonical.casefold()}"
    existing = store.entities.get(key)
    if existing is not None:
        return None                                  # already a form, or unticked by hand
    entity = Entity(key=key, category=head.category, canonical=parsed.canonical,
                    replacement="", source="local model", person=parsed,
                    variants=_names.variants(parsed, include_single_tokens=True),
                    parent=head.key, group_note=f"{NOTE}: {why}")
    store.entities[key] = entity
    shape(store, entity)
    if written:
        entity.surrogate = _names.PersonName(raw=written, last=written)
        entity.replacement = written
    return entity
