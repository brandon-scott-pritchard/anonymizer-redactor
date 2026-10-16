"""Is each thing the run replaced what the run says it is?

The leak score asks whether anything identifying got through; this asks the
other question, item by item. Every hit the final store makes on a typed page
is read back with the words around it and sorted:

``confirmed``
    The labels say it is that kind of thing: the hit's digits are a
    labelled SSN where the run says SSN, its text a labelled person where the
    run says person.
``wrong_kind``
    It is labelled - as something else: a phone taken for an account, a
    reference number for a card.
``third_party``
    A labelled person or value, but not a party's - under parties first,
    replaced by mistake.
``unlabelled``
    Nothing in the labels. These are the ones to judge by their context:
    ``eval.precision_report`` asks the local model, per item, how likely the
    claimed kind is.
"""

from __future__ import annotations

import re

from . import truth

CONTEXT = 70                     # characters either side, as a reader sees them
PER_FILE = 400                   # hits kept per file - enough to sample from

_DIGITS = re.compile(r"\D")
_PERSONISH = {"person", "minor", "party_value"}
# the labels' id kinds, and the run's categories that are each one
_KIND_OF = {
    "ssn": {"ssn"}, "account": {"bank_account", "masked_account", "credit_card",
                                "investment_account", "loan_number", "party_value"},
    "routing": {"routing_number"}, "policy": {"policy_number"}, "vin": {"vin"},
    "plate": {"license_plate"}, "parcel": {"parcel_number"}, "title": {"vehicle_title"},
    "ein": {"ein"}, "order": {"order_number"}, "tracking": {"tracking_number"},
}


def _kind_of_digits(digits: str, secrets: truth.Secrets) -> str | None:
    if not digits:
        return None
    if digits in secrets.ids:
        return secrets.ids[digits]
    if 4 <= len(digits) <= 6:
        # a tail: evidence only against accounts and cards - four digits end
        # some order or tracking number by chance
        if digits[-4:] in secrets.card_tails:
            return "account"
        if any(full.endswith(digits) and kind == "account" for full, kind in secrets.ids.items()):
            return "account"
        return None
    if len(digits) > 6:
        for full, kind in secrets.ids.items():
            if full.endswith(digits) and len(full) > len(digits):
                return kind
    if digits in secrets.phones or (len(digits) == 11 and digits[1:] in secrets.phones):
        return "phone"
    return None


def _person(text: str, secrets: truth.Secrets):
    words = {w.casefold() for w in re.findall(r"[A-Za-z][A-Za-z'’-]+", text)}
    best = None
    for person in secrets.people.values():
        names = {person.first.casefold(), person.last.casefold()} - {""}
        if names and names <= words or (person.last and person.last.casefold() in words
                                        and len(words) <= 2):
            if best is None or (person.household and not best.household):
                best = person
    return best


def classify(category: str, text: str, secrets: truth.Secrets) -> tuple[str, str]:
    """(verdict, what the labels say it is)."""
    digits = _DIGITS.sub("", text)
    if category in ("phone", "fax"):
        kind = _kind_of_digits(digits, secrets)
        if kind == "phone":
            return "confirmed", "phone"
        return ("wrong_kind", kind) if kind else ("unlabelled", "")
    if category == "email":
        return ("confirmed", "email") if text.casefold() in secrets.emails else ("unlabelled", "")
    if category == "street_address":
        number = re.match(r"\s*(\d+)", text)
        for address in secrets.addresses:
            if number and address.startswith(number.group(1) + " "):
                words = set(re.findall(r"[a-z]{3,}", address.casefold()))
                if words & set(re.findall(r"[a-z]{3,}", text.casefold())):
                    return "confirmed", "address"
        return "unlabelled", ""
    if category in _PERSONISH and not digits:
        person = _person(text, secrets)
        if person is None:
            return "unlabelled", ""
        return ("confirmed", "household person") if person.household \
            else ("third_party", "third-party person")
    if category in ("organization", "employer"):
        folded = text.casefold()
        if any(org.casefold() in folded or folded in org.casefold() for org in secrets.orgs):
            return "confirmed", "organization"
        return "unlabelled", ""
    kind = _kind_of_digits(digits, secrets)
    if kind is None:
        return "unlabelled", ""
    for label_kind, categories in _KIND_OF.items():
        if kind == label_kind:
            return ("confirmed", kind) if category in categories else ("wrong_kind", kind)
    return ("confirmed", kind) if kind == category else ("wrong_kind", kind)


def audit(text: str, store, settings, secrets: truth.Secrets) -> list[dict]:
    """Every hit the final store makes on ``text``, with its context and verdict."""
    from redactor import engine

    out = []
    seen: set[tuple[str, str]] = set()
    for hit in engine.scan_text(text, store, settings, register=False):
        entity = store.entities.get(hit.entity_key)
        category = entity.category if entity is not None else hit.category
        key = (category, hit.text.casefold())
        verdict, said = classify(category, hit.text, secrets)
        before = " ".join(text[max(0, hit.start - CONTEXT):hit.start].split())
        after = " ".join(text[hit.end:hit.end + CONTEXT].split())
        out.append({"category": category, "text": hit.text, "replacement": hit.replacement,
                    "verdict": verdict, "labels_say": said, "before": before, "after": after,
                    "repeat": key in seen})
        seen.add(key)
        if len(out) >= PER_FILE:
            break
    return out
