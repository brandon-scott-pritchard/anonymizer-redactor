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


def test_kev_and_probablepeople_sort_the_suggestions_and_only_sort(monkeypatch):
    """A suggestion is set apart only when both readers call it no person: a
    store Kev doubts but the library reads as a person stays a person."""
    from redactor import name_clusters, name_screen

    kev = {"Dollar Tree Layton": 0.05, "Megan Layton": 0.97, "Okonkwo Landscaping": 0.10,
           "Interest Rate": 0.02}
    monkeypatch.setattr(name_clusters, "kev_is_name", lambda url: lambda text: kev[text])
    labels = name_screen.classify(list(kev), "http://127.0.0.1:1")
    assert labels["Megan Layton"] == "person"
    assert labels["Okonkwo Landscaping"] == "other"      # Kev no, probablepeople a business
    assert set(labels) == set(kev)


def test_a_suggestion_kev_does_not_answer_is_not_set_apart(monkeypatch):
    from redactor import name_clusters, name_screen

    def silent(url):
        def ask(text):
            raise OSError("no answer")
        return ask
    monkeypatch.setattr(name_clusters, "kev_is_name", silent)
    assert name_screen.classify(["Okonkwo Landscaping LLC"], "http://127.0.0.1:1") == {
        "Okonkwo Landscaping LLC": "person"}


def test_the_sort_may_not_drop_an_employer_or_a_first_and_last_name():
    """Given the parties, a sorter once called every other real person
    "other" - 412 of 421 suggestions - and 217 names shipped."""
    from redactor import name_screen

    assert name_screen.keeps("Brightline Tutoring LLC", "employer", "other")
    assert name_screen.keeps("WILLIAM SMITH", "person", "other")
    assert not name_screen.keeps("Dollar Tree Layton", "person", "other")
    assert name_screen.keeps("Megan Layton", "person", "person")
    assert name_screen.keeps("Mark Store", "person", None) is None


@pytest.mark.parametrize("text", ["Tran Date", "Date Ps", "Posting Amount"])
def test_a_name_ending_in_a_column_heading_is_a_table_read_across(text):
    """Ticked, "Tran Date" took every "Date" heading of a household: 532."""
    from redactor import name_screen

    assert not ner.likely_name(text, "person")
    assert name_screen.keeps(text, "person", "person") is False


def test_an_everyday_word_is_never_a_payment_handle():
    from redactor import patterns

    assert not [m for m in patterns.scan("Venmo:\nActivity") if m.category == "payment_handle"]
    assert [m.text for m in patterns.scan("Venmo\nkaylaruiz88")
            if m.category == "payment_handle"] == ["kaylaruiz88"]
