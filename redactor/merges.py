"""Rows of the review list kept together: merges made by hand, and kept so.

"Merge into..." on the review list puts one row under another - the same
thing written another way - and :func:`merge_by_hand` folds it as grouping.py
folds a form: ``parent`` set to the head, so it takes the head's replacement,
a person's invented name in its own shape. A regrouping starts from scratch
and would drop it; :func:`settle` puts it back after anything changes the
list. "Split out" undoes it, and a row merged by hand comes back out as it
was before - its party, its kind, its own replacement (grouping.split).

(The same keeping-together once carried merges proposed by a local language
model, taken out on 2026-10-16; a store saved with such merges still settles
them, marked with ``NOTE``.)
"""

from __future__ import annotations

import re

from . import grouping, names as _names
from .mapping import Entity, MappingStore

NOTE = "merged by the local model"
# a merge made on the review list
MANUAL = "merged by you"


def _is_merge_note(note: str) -> bool:
    return note.startswith(NOTE) or note == MANUAL


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



# --------------------------------------------------------------------------
# folding a row under another, and keeping it there
# --------------------------------------------------------------------------

def _fold(store: MappingStore, entity: Entity, head: Entity, why: str, via: str) -> None:
    """``entity`` made a form of ``head``, and its own forms with it."""
    for form in grouping.members(store, entity):
        _fold(store, form, head, why, via)
    entity.parent = head.key
    entity.candidates = ()
    entity.group_note = MANUAL if why == MANUAL else f"{NOTE}: {why}"
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
            if _is_merge_note(entity.group_note):
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
        if _is_merge_note(form.group_note):
            form.group_note = ""
        if form is not owner and owner is not None and form.key not in store.split_out:
            form.parent = owner.key                # back under the row it came with
    # a party's row merged by hand and split back has its own invented name
    # again (grouping.split restored it): it is not to be given a new one
    if owner is not None and owner.is_person and owner.person is not None \
            and not owner.fixed:
        # its invented name was the head's, in its shape - and an invented
        # surname follows the real one, so it would have kept the head's
        owner.surrogate = None
        owner.surrogate = store._unique_person_surrogate(owner.person, misread=False)
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
# a merge made on the review list
# --------------------------------------------------------------------------

# kinds that can be one thing: a person and a child, the account kinds, and
# so on; anything else only with its own kind
_FAMILIES = (grouping.PEOPLE, grouping.ACCOUNTS, grouping.ORGS, grouping.PHONES,
             grouping.ADDRESSES)


def _same_family(a: Entity, b: Entity) -> bool:
    if a.category == b.category:
        return True
    return any(a.category in f and b.category in f for f in _FAMILIES)


def merge_by_hand(store: MappingStore, entity: Entity, target: Entity) -> str:
    """Put ``entity``'s row - it and every form under it - under ``target``'s
    row: one thing written several ways. The forms take the target's
    replacement (a person's in their own shape) and keep it through a
    regrouping; "Split out" takes a form back out. Returns why it could not
    be done, or "" when it was."""
    own, head = grouping.head_of(store, entity), grouping.head_of(store, target)
    if own is head:
        return "That row is already part of that group."
    if not _same_family(own, head):
        return "Only rows of the same kind can be merged."
    for form in [own, *grouping.members(store, own)]:
        store.split_out.discard(form.key)        # merged now, not kept apart
    store.merged_by_hand[own.key] = (own.fixed, own.party, own.kind, own.replacement,
                                     own.surrogate)
    _fold(store, own, head, MANUAL, own.key)
    settle(store)
    return ""



# --------------------------------------------------------------------------
# a written form chosen at the final check
# --------------------------------------------------------------------------

FINAL_CHECK = "added at the final check"


def stand_in(store: MappingStore, head: Entity, text: str) -> str:
    """What ``text``, a written form of ``head``, is to read as."""
    parsed = _names.parse(text)
    if not parsed.canonical or head.person is None or head.surrogate is None:
        return head.replacement
    shaped = grouping._shaped_surrogate(parsed, head.surrogate,
                                        loose_mapping(parsed, head.person), head.person)
    return _names.match_case(text, shaped.canonical)


def add_form(store: MappingStore, head: Entity, text: str, written: str = "") -> Entity | None:
    """``text`` registered as one more written form of ``head``, taking its
    invented name in its own shape. ``written``, when given, is what the form
    is to read as instead - a glued word with only the name in it swapped.
    None when it is already a form, or was unticked by hand."""
    parsed = _names.parse(text)
    if not parsed.canonical:
        return None
    key = f"{head.category}:{parsed.canonical.casefold()}"
    if key in store.entities:
        return None
    entity = Entity(key=key, category=head.category, canonical=parsed.canonical,
                    replacement="", source=FINAL_CHECK, person=parsed,
                    variants=_names.variants(parsed, include_single_tokens=True),
                    parent=head.key, group_note=FINAL_CHECK)
    store.entities[key] = entity
    shape(store, entity)
    if written:
        entity.surrogate = _names.PersonName(raw=written, last=written)
        entity.replacement = written
    return entity
