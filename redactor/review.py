"""Name-list and review-screen decisions, independent of any front end.

Both the Tk GUI and the web app let the operator untick rows, change a
row's type, and edit replacements - and both must survive a rescan without
losing those decisions. The store-level logic lives here once.
"""

from __future__ import annotations

from typing import Iterable, Sequence

from . import categories, grouping, names as _names
from .mapping import Entity, MappingStore

# --------------------------------------------------------------------------
# the name list (step 2)
# --------------------------------------------------------------------------

_PERSONAL = {"person", "minor"}


def resolve_overlaps(
    entries: Sequence[tuple[str, str]],
) -> tuple[list[tuple[str, str]], list[_names.Overlap]]:
    """Fold ticked names that a longer ticked name already covers.

    Second pass over what step 2 hands back: ``Smith`` sitting beside
    ``Jane Elizabeth Smith`` is not a second person, it is one of the written
    forms the matcher generates for the first.  Left in, it becomes a second
    entity with its own surrogate and the surname comes out inconsistent.

    Only unambiguous folds are applied - one longer name claiming the short
    form, both of them people.  ``Smith`` with two Smiths on the list stays put
    and is reported instead, because guessing which one it belongs to would be
    the wrong kind of clever.

    Returns the entries to actually register, plus every overlap found, folded
    or not, so the operator can be told what happened.
    """
    overlaps = _names.overlapping_names([name for name, _category in entries])
    if not overlaps:
        return list(entries), []

    category_of = {name.casefold(): category for name, category in entries}
    folded = {
        overlap.inner.casefold()
        for overlap in overlaps
        if overlap.merge
        and category_of.get(overlap.inner.casefold()) in _PERSONAL
        and category_of.get(overlap.outer.casefold()) in _PERSONAL
    }
    kept = [(name, category) for name, category in entries
            if name.casefold() not in folded]
    return kept, overlaps


def overlap_notes(overlaps: Iterable[_names.Overlap]) -> list[str]:
    """One line per overlap, ready for a status bar or a warning strip."""
    seen: set[str] = set()
    out: list[str] = []
    for overlap in overlaps:
        note = overlap.note
        if note not in seen:
            seen.add(note)
            out.append(note)
    return out


def build_store(
    entries: Sequence[tuple[str, str]],
    roles: dict[str, str] | None = None,
) -> tuple[MappingStore, list[_names.Overlap]]:
    """A store built from the step 2 name list, overlaps resolved first."""
    # a payment line's or an account's words off the end of a person's name
    # first, so "Rebecca L Blackburn Ret" does not swallow Rebecca L Blackburn
    entries = [(_names.without_tail(name) if categories.style_for(category) == "person" else name,
                category) for name, category in entries]
    # and a "person" that is a business, a business
    entries = [(name, "organization" if category == "person" and _names.is_business(name)
                else category) for name, category in entries]
    seen: set[tuple[str, str]] = set()
    entries = [e for e in entries if not ((e[0].casefold(), e[1]) in seen
                                          or seen.add((e[0].casefold(), e[1])))]
    kept, overlaps = resolve_overlaps(entries)
    roles = roles or {}
    store = MappingStore()
    for name, category in kept:
        # route on the category's own style rather than a hardcoded pair, so a
        # new placeholder category (a child's initials, a birth date carried
        # over from a roster) lands in the right half of the store
        role = roles.get(name.casefold(), "")
        if categories.style_for(category) == "person":
            store.add_person(name, category=category, role=role,
                             source="name-list", distinct=_is_child(role, category))
        else:
            store.add_value(category, name, source="name-list")
    return store, overlaps


_CHILD_CATEGORIES = {"minor", "minor_initials"}


def _is_child(role: str, category: str) -> bool:
    """Whether this entry came off a children roster.

    A child keeps their own identity even when typed as an ordinary person: it
    is the only signal that separates a son from the father he is named after.
    """
    return category in _CHILD_CATEGORIES or "child" in role.casefold()


# --------------------------------------------------------------------------
# the review screen (step 3)
# --------------------------------------------------------------------------


def retype_entity(store: MappingStore, entity: Entity, new_category: str) -> Entity | None:
    """Re-register ``entity`` under ``new_category``.

    Returns the resulting entity, or ``None`` when the change is refused
    (a digit string cannot become a person). If the same text already
    exists under the target type, the two are merged and the survivor's
    tick is kept.
    """
    if (categories.style_for(new_category) == "person"
            and not any(ch.isalpha() for ch in entity.canonical)):
        # "528-41-9963" parses as a "name" and would be replaced by an
        # invented human name; refuse rather than invent
        return None
    existing = store.get(new_category, entity.canonical)
    if existing is not None and existing is not entity:
        existing.occurrences += entity.occurrences
        existing.documents |= entity.documents
        del store.entities[entity.key]
        if store.grouped:
            # its forms pointed at the deleted row: regroup, or they vanish
            # from both review lists while still being applied
            grouping.group(store)
        return existing
    del store.entities[entity.key]
    if categories.style_for(new_category) == "person":
        fresh = store.add_person(entity.canonical, category=new_category,
                                 role=entity.role, source=entity.source)
    else:
        fresh = store.add_value(new_category, entity.canonical,
                                source=entity.source)
    if fresh is None:
        store.entities[entity.key] = entity
        return None
    fresh.enabled = entity.enabled
    fresh.occurrences = entity.occurrences
    fresh.documents = entity.documents
    fresh.contexts = list(entity.contexts)
    if store.grouped:
        grouping.group(store)
    return fresh


def snapshot_decisions(store: MappingStore) -> dict[str, tuple[bool, str, str, bool]]:
    """What the operator decided, keyed by the found text.

    The fourth field is whether the operator split the row out of its group.
    A variant's replacement is its head's, so only a head's is carried.
    """
    return {
        entity.canonical.casefold():
            (entity.enabled, entity.category,
             "" if entity.parent else entity.replacement,
             entity.key in store.split_out)
        for entity in store.entities.values()
    }


def carry_decisions(store: MappingStore,
                    carryover: dict[str, tuple]) -> None:
    """Re-apply earlier review decisions to a freshly scanned store."""
    valid = {c.key for c in categories.CATEGORIES}
    split = [key for key, decided in carryover.items() if len(decided) > 3 and decided[3]]
    if split:
        for entity in store.entities.values():
            if entity.canonical.casefold() in split:
                store.split_out.add(entity.key)
        grouping.group(store)
    taken = {e.replacement for e in store.entities.values()}
    for entity in list(store.entities.values()):
        previous = carryover.get(entity.canonical.casefold())
        if previous is None:
            continue
        enabled, category, replacement = previous[:3]
        if category != entity.category and category in valid:
            moved = retype_entity(store, entity, category)
            if moved is not None:
                entity = moved
        # restore an edited replacement, but never mint a duplicate
        # placeholder if numbering shifted between scans. It belongs to the
        # group: if a rescan made another form its head, the edit goes there.
        target = grouping.head_of(store, entity) if entity.parent else entity
        if (replacement and replacement != target.replacement
                and category == entity.category
                and replacement not in taken):
            target.replacement = replacement
            if target.is_person and target.surrogate is not None:
                target.surrogate = _names.parse(replacement)
        entity.enabled = enabled
    grouping.sync(store)


def set_replacement(entity: Entity, replacement: str,
                    store: MappingStore | None = None) -> None:
    """Apply an operator-edited replacement, keeping the surrogate in step.

    Editing a variant's replacement takes it out of its group: it no longer
    reads as the same thing. Editing a head's carries to its variants.
    """
    if store is not None and entity.parent:
        grouping.split(store, entity)
    entity.replacement = replacement
    if entity.is_person and entity.surrogate is not None:
        entity.surrogate = _names.parse(replacement)
    if store is not None:
        grouping.sync(store)


# --------------------------------------------------------------------------
# what the review list says about itself - one wording for both front ends
# --------------------------------------------------------------------------

REVIEW_INSTRUCTIONS = (
    "☑ Ticked = this WILL BE CHANGED (redacted or anonymized) everywhere it appears.   "
    "☐ Unticked = left exactly as written.\n"
    "A row with ▸ gathers every form of one person, address or number: ticking it ticks "
    "them all. Open it to untick a single form, or to split out a form that is not the "
    "same thing. Edit a row to change its type or replacement. Nothing has been written yet."
)


def review_note(entity: Entity) -> str:
    """Why a row deserves a second look - shown beside it."""
    if entity.candidates:
        return "Could be " + " or ".join(entity.candidates) + " - kept on its own row"
    return entity.group_note
