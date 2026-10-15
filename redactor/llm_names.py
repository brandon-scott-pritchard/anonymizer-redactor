"""The local model's say on which of the name screen's suggestions are names.

The scan proposes a merchant ("Dollar Tree Layton"), a heading ("Promo
Expiration Date") or a town ("Layton UT") as a person as readily as a person,
and "Tick all" sent every one of them through the run - each a stand-in where
a store's or a column's name had been, 29% of the ordinary words on one
household's typed pages. The word lists in :func:`ner.likely_name` catch the
headings made of everyday words, but not a store or a town, and they cannot
tell "Megan Layton" from "Dawgs Layton". The model can.

It only sorts: a suggestion it calls no name is listed apart, unticked, as
before, and the operator can tick it. Nothing is dropped.
"""

from __future__ import annotations

from . import llm

PER_REQUEST = 150

SYSTEM = (
    "You sort a list of strings that a scanner proposed as names in a family-law case's "
    "financial documents: bank and card statements, pay stubs, receipts, court filings. "
    "Label each one:\n"
    '- "person": ANY real individual human being, whoever they are - not only the case\'s '
    "parties: people paid or paying, senders and recipients of transfers, employees, "
    "landlords, tenants, children, relatives, lawyers, doctors, anyone - written in any form "
    "(full name, surname, first name, initials, misspelled or in capitals);\n"
    '- "family_business": a business, trust or account named after one of the case\'s parties;\n'
    '- "other": not a person at all - a store, merchant, restaurant, bank, company, government '
    "agency, school, city, town, place, product, payment service, column heading, form label "
    "or ordinary words.\n"
    "The parties are listed only so you can recognise a business named after them; never mark "
    "someone \"other\" because they are not a party. When a string could be a person's name, "
    "say person: a missed name is worse than an extra one.")

SCHEMA = '{"labels": {"<number>": "person" | "family_business" | "other"}}'


def classify(texts: list[str], parties: list[str], url: str, progress=None) -> dict[str, str]:
    """{text: label} for each suggestion the model labelled."""
    out: dict[str, str] = {}
    for start in range(0, len(texts), PER_REQUEST):
        part = texts[start:start + PER_REQUEST]
        if progress is not None:
            progress("Asking the local model which suggestions are names",
                     start / max(len(texts), 1))
        user = ("The case's parties (for spotting a business named after them): "
                + ("; ".join(parties) if parties else "(none listed yet)")
                + "\n\nStrings:\n" + "\n".join(f"{i}. {t}" for i, t in enumerate(part, 1)))
        answer = llm.chat(url, SYSTEM, user, SCHEMA, max_tokens=12 * len(part) + 64)
        labels = answer.get("labels", {}) if isinstance(answer, dict) else {}
        for key, label in labels.items():
            try:
                index = int(str(key).strip().rstrip(".")) - 1
            except ValueError:
                continue
            if 0 <= index < len(part) and label in ("person", "family_business", "other"):
                out[part[index]] = label
    return out


def describe(labels: dict[str, str]) -> str:
    others = sum(1 for v in labels.values() if v == "other")
    return (f"The local model sorted the suggestions: {others} that are not names "
            "(stores, places, headings) are listed apart." if labels else "")


def keeps(text: str, category: str, label: str | None) -> bool | None:
    """The model's sort, with the two things it may not overrule: an employer
    the scan found is the household's own, and a common first name before a
    surname is a person. None when the model did not label the suggestion."""
    from . import ner

    if category == "employer":
        return True
    if category in ("person", "minor") and ner.heading_ended(text):
        return False                   # "Tran Date": a table read across, whoever says so
    if label is None:
        return None
    if label != "other":
        return True
    words = [ner._plain(w) for w in text.split()]
    _common, given = ner._lists()
    return len(words) >= 2 and words[0] in given and words[-1] not in ner._RAILS_AND_BRANDS

