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


def test_the_local_model_sorts_the_suggestions_and_only_sorts(monkeypatch):
    """A store with a town in it is no name; a person whose surname is a town
    is one - which no word list can tell apart."""
    from redactor import llm, llm_names

    asked = []

    def chat(url, system, user, schema, max_tokens=0):
        asked.append(user)
        return {"labels": {"1": "other", "2": "person", "3": "family_business", "9": "person",
                           "x": "person", "4": "nonsense"}}

    monkeypatch.setattr(llm, "chat", chat)
    labels = llm_names.classify(["Dollar Tree Layton", "Megan Layton", "Okonkwo Landscaping",
                                 "Interest Rate"], ["Brittany Okonkwo"], "http://127.0.0.1:1")
    assert labels == {"Dollar Tree Layton": "other", "Megan Layton": "person",
                      "Okonkwo Landscaping": "family_business"}
    assert "Brittany Okonkwo" in asked[0]


def test_the_model_may_not_drop_an_employer_or_a_first_and_last_name():
    """Given the parties, the model once called every other real person
    "other" - 412 of 421 suggestions - and 217 names shipped."""
    from redactor import llm_names

    assert llm_names.keeps("Brightline Tutoring LLC", "employer", "other")
    assert llm_names.keeps("WILLIAM SMITH", "person", "other")
    assert not llm_names.keeps("Dollar Tree Layton", "person", "other")
    assert llm_names.keeps("Megan Layton", "person", "person")
    assert llm_names.keeps("Mark Store", "person", None) is None
    assert "not only" in llm_names.SYSTEM and "never mark" in llm_names.SYSTEM


@pytest.mark.parametrize("text", ["Tran Date", "Date Ps", "Posting Amount"])
def test_a_name_ending_in_a_column_heading_is_a_table_read_across(text):
    """Ticked, "Tran Date" took every "Date" heading of a household: 532."""
    from redactor import llm_names

    assert not ner.likely_name(text, "person")
    assert llm_names.keeps(text, "person", "person") is False


def test_an_everyday_word_is_never_a_payment_handle():
    from redactor import patterns

    assert not [m for m in patterns.scan("Venmo:\nActivity") if m.category == "payment_handle"]
    assert [m.text for m in patterns.scan("Venmo\nkaylaruiz88")
            if m.category == "payment_handle"] == ["kaylaruiz88"]
