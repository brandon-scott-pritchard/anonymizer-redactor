"""Review-screen decisions, independent of any front end.

Both the Tk GUI and the web app let the operator untick rows, change a
row's type, and edit replacements - and both must survive a rescan without
losing those decisions. The store-level logic lives here once.
"""

from __future__ import annotations

from . import categories, names as _names
from .mapping import Entity, MappingStore


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
    return fresh


def snapshot_decisions(store: MappingStore) -> dict[str, tuple[bool, str, str]]:
    """What the operator decided, keyed by the found text."""
    return {
        entity.canonical.casefold():
            (entity.enabled, entity.category, entity.replacement)
        for entity in store.entities.values()
    }


def carry_decisions(store: MappingStore,
                    carryover: dict[str, tuple[bool, str, str]]) -> None:
    """Re-apply earlier review decisions to a freshly scanned store."""
    valid = {c.key for c in categories.CATEGORIES}
    taken = {e.replacement for e in store.entities.values()}
    for entity in list(store.entities.values()):
        previous = carryover.get(entity.canonical.casefold())
        if previous is None:
            continue
        enabled, category, replacement = previous
        if category != entity.category and category in valid:
            moved = retype_entity(store, entity, category)
            if moved is not None:
                entity = moved
        # restore an edited replacement, but never mint a duplicate
        # placeholder if numbering shifted between scans
        if (replacement and replacement != entity.replacement
                and category == entity.category
                and replacement not in taken):
            entity.replacement = replacement
            if entity.is_person and entity.surrogate is not None:
                entity.surrogate = _names.parse(replacement)
        entity.enabled = enabled


def set_replacement(entity: Entity, replacement: str) -> None:
    """Apply an operator-edited replacement, keeping the surrogate in step."""
    entity.replacement = replacement
    if entity.is_person and entity.surrogate is not None:
        entity.surrogate = _names.parse(replacement)
