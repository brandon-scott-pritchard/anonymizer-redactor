"""Suggestions that are probably not names are listed apart, not dropped.

On a household's statements the model proposed headings, merchants, places
and payment services as people; "Tick all" sent every one through the run.
Every name here is invented.
"""

import pytest

from redactor import ner


@pytest.mark.parametrize("text,category", [
    ("Statement", "organization"), ("Interest Rate", "person"), ("Maturity Date", "person"),
    ("Zelle", "person"), ("Los Angeles", "person"), ("SAN JOSE", "person"), ("FDIC", "person"),
    ("Spotify Usa", "person"), ("Add Money", "person"), ("Taxes", "organization"),
])
def test_headings_places_and_rails_are_probably_not_names(text, category):
    assert not ner.likely_name(text, category)


@pytest.mark.parametrize("text,category", [
    ("Thomas Larsen", "person"), ("Brittany Okonkwo", "person"), ("Mark Miller", "person"),
    ("Red Canyon Lands", "employer"),
])
def test_names_stay_likely(text, category):
    assert ner.likely_name(text, category)


def test_a_business_carrying_a_listed_name_is_likely():
    known = frozenset({"okonkwo", "brittany"})
    assert ner.likely_name("Okonkwo Landscaping", "organization", known)
    assert not ner.likely_name("Cascade Home Loan Servicing", "organization", known)
