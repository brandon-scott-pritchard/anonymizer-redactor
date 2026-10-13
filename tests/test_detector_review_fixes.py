"""Detector fixes from code review: catastrophic backtracking, and values the
pattern rules dropped or mangled."""

import time

import pytest

from redactor import patterns
from redactor.engine import _company_any_form


def _found(text):
    return [(m.category, m.text) for m in patterns.scan(text)]


def _timed(text):
    t = time.perf_counter()
    patterns.scan(text)
    return time.perf_counter() - t


# ---------------------------------------------------------------------------
# backtracking
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", [
    "PAY " + "*" * 60 + "832.81 TO THE ORDER OF",       # took ten seconds at 36
    "Account " + "X" * 60 + "!",
    "Card ending in " + "* " * 30 + "x 1",
    "1234" + "*" * 60 + "a",
    "Card ROTH IRA account " + " " * 1000 + "! 1",      # a padded column after a label
    "facebook " + " " * 1000 + "!",
    "1.23456" + " " * 1000 + "x",
])
def test_a_long_run_cannot_stall_the_scan(text):
    assert _timed(text) < 0.5


@pytest.mark.parametrize("text, value", [
    ("Acct XXX-XX-6789", "XXX-XX-6789"), ("****-****-1234", "****-****-1234"),
    ("Acct #: ...3907", "...3907"), ("****3907", "****3907"),
    ("XXXX-XXXX-XXXX-4417", "XXXX-XXXX-XXXX-4417"), ("XXXX XXXX XXXX 4417", "XXXX XXXX XXXX 4417"),
    ("Account 4417****", "4417****"), ("Payment method: Visa •••• 4053", "•••• 4053"),
])
def test_every_mask_shape_still_matches(text, value):
    assert value in [m.text for m in patterns.scan(text)]


def test_a_check_protected_amount_is_still_not_a_mask():
    assert not _found("PAY " + "*" * 36 + "832.81 TO THE ORDER OF")


# ---------------------------------------------------------------------------
# values that were dropped
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text, category, value", [
    ("Checking account ending in 2024", "bank_account", "2024"),
    ("Savings ending in 1987", "bank_account", "1987"),
    ("Acct No. 2015", "bank_account", "2015"),
    ("Loan ending in 2019", "loan_number", "2019"),
    ("Account last 4 digits 2019", "bank_account", "2019"),
])
def test_a_last_four_that_looks_like_a_year_is_kept_when_the_label_says_so(text, category, value):
    assert (category, value) in _found(text)


@pytest.mark.parametrize("text", ["Roth IRA 2024 contribution", "401(k) 2023 limit",
                                  "Account number for 2024", "the last 2024 filing"])
def test_a_year_after_a_bare_label_is_still_a_year(text):
    assert _found(text) == []


@pytest.mark.parametrize("text", [
    "Card ending in 4417 N Ogden", "VISA 4417 E ZPASS",
    "DEBIT CARD 4417 WEST ELM 801-555-1234", "Card #4417 paid to Dr. Smith",
])
def test_a_card_tail_the_street_rule_does_not_take_is_a_card_tail(text):
    assert ("credit_card", "4417") in _found(text)


def test_a_card_tail_the_street_rule_does_take_is_redacted_as_the_street():
    found = _found("Card:4417 at St. Marks")
    assert found and found[0][1].startswith("4417")


@pytest.mark.parametrize("text, value", [
    ("Visa | Last digits: 4053", "4053"), ("Mastercard | Last digits: 6256", "6256"),
    ("Amex | Last digits: 1001", "1001"), ("Card ending -4053", "4053"),
    ("Visa last 4 digits 4053", "4053"), ("Card last four 4053", "4053"),
])
def test_a_card_tail_after_last_digits_is_caught(text, value):
    assert ("credit_card", value) in _found(text)


def test_a_grouped_account_does_not_give_digits_back_to_dodge_the_money_guard():
    assert _found("Checking 4417 1500.00") == [("bank_account", "4417")]


def test_a_labelled_street_keeps_its_town():
    text = "Address: 1734 W Gentile St\nLayton, UT 84041"
    assert ("street_address", "1734 W Gentile St\nLayton, UT 84041") in _found(text)
    # the street mid-line over a city line of its own is a wrapped block too
    assert ("street_address", "1734 W Gentile St\nLayton, UT 84041") in _found(
        "Home   1734 W Gentile St\nLayton, UT 84041")


def test_a_table_row_still_does_not_take_the_next_columns_town():
    text = "Bath Set   2750 ASPEN SPRINGS DR\nTWIN FALLS ID 83301   PARK CITY UT 84060"
    assert ("street_address", "2750 ASPEN SPRINGS DR") in _found(text)


def test_identifiers_in_a_comma_list_are_not_a_figure():
    assert _found("SSN 123-45-6789,234-56-7890") == [("ssn", "123-45-6789"), ("ssn", "234-56-7890")]
    assert ("bank_account", "44821903") in _found("Account No. 44821903,44821904")
    # and a real figure still is one
    assert _found("PRIMARY SAVINGS 111,274.97") == []
    assert _found("Savings account 1,500.00 was moved") == []


@pytest.mark.parametrize("text, category, value", [
    ("PatientDOB: 01/02/1990", "dob", "01/02/1990"),
    ("ChkAcct 44821903", "bank_account", "44821903"),
    ("CustomerAcct# 44821903", "bank_account", "44821903"),
])
def test_a_camel_cased_label_is_a_label(text, category, value):
    assert (category, value) in _found(text)


@pytest.mark.parametrize("text", ["GPBSDLHG5", "1Z59T2L3YUS96VY4RX\n2022-05-20"])
def test_capitals_inside_a_code_are_still_not_a_label(text):
    assert not {c for c, _ in _found(text)} & {"drivers_license", "prescription"}


def test_an_address_glued_to_its_label_is_an_address():
    assert ("street_address", "1734 W Gentile St") in _found("Address:1734 W Gentile St")
    # a time's colon still blocks it
    assert not [c for c, _ in _found("Meeting 10:30 Park Ave") if c == "street_address"]


# ---------------------------------------------------------------------------
# things that were taken and should not be
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("text", ["$million in losses", "the $Total column", "$Balance due"])
def test_a_dollar_sign_before_a_word_is_not_a_cashtag(text):
    assert not [c for c, _ in _found(text) if c == "payment_handle"]


@pytest.mark.parametrize("text, value", [("$JennaHar727 sent", "$JennaHar727"),
                                         ("from $jenna_h", "$jenna_h"), ("$JennaHar", "$JennaHar")])
def test_a_cashtag_that_looks_like_a_handle_is_still_one(text, value):
    assert ("payment_handle", value) in _found(text)


@pytest.mark.parametrize("text", ["Plate 1234 ST GEORGE", "Plate 12 OZ", "plate glass window"])
def test_a_number_after_plate_is_not_a_plate(text):
    assert not [c for c, _ in _found(text) if c == "license_plate"]


@pytest.mark.parametrize("text, value", [("PLATE Y777MX VIN", "Y777MX"), ("Plate ABC 123", "ABC 123"),
                                         ("Plate: 7ABC123", "7ABC123")])
def test_a_plate_is_still_a_plate(text, value):
    assert ("license_plate", value) in _found(text)


def test_a_month_and_year_after_card_is_an_expiry_not_a_tail():
    assert not [c for c, _ in _found("Card 12-2024") if c == "credit_card"]
    assert ("credit_card", "2-5666") in _found("Card Ending 2-5666")
    assert ("credit_card", "1-2019") in _found("Card Ending 1-2019")


def test_a_two_word_employer_is_not_found_in_lower_case_prose():
    rule = _company_any_form("Best Buy Co., Inc.")
    assert not rule.search("the best buy of the year")
    for text in ("BEST BUY CO", "Best Buy #123", "Paid Best Buy, Inc."):
        assert rule.search(text), text


def test_an_employer_is_found_without_its_apostrophe():
    rule = _company_any_form("Smith's Food & Drug")
    for text in ("SMITHS FOOD AND DRUG", "Smith’s Food & Drug Centers", "SMITH'S FOOD & DRUG"):
        assert rule.search(text), text
