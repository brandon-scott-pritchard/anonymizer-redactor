"""Identifiers that shipped because OCR or a form wrote them in a shape the
detectors did not know.

Each case comes from a photographed or scanned document in the corpus run,
cut down to the line that failed. Every name and number is invented.
"""

import pytest

from redactor import patterns


def _found(text):
    return [(m.category, m.text) for m in patterns.scan(text)]


# ------------------------------------------------------------------ ITIN ---

@pytest.mark.parametrize("line, value", [
    ("Heber Valley Regional Hospital            922-74-1631            00-9537472",
     "922-74-1631"),
    ("ITIN: 912701234", "912701234"),
    ("Your ITIN 978 93 4410", "978 93 4410"),
])
def test_an_itin_is_caught_like_an_ssn(line, value):
    assert ("ssn", value) in _found(line)


@pytest.mark.parametrize("value", ["900-12-3456", "912-93-1234", "978-70-0000"])
def test_a_nine_hundred_number_outside_the_itin_groups_is_not_one(value):
    assert patterns.valid_ssn(value) is False


# ----------------------------------------------------- masked card tails ---

@pytest.mark.parametrize("line, tail", [
    ('DEBIT ***"2628   69.97', "2628"),
    ('EBIT **""7710   67.00', "7710"),
    ("EBIT *7710 67.00", "7710"),
    ("#*3233", None),          # the plain mask rule already takes it whole
])
def test_an_ocr_mangled_mask_needs_no_label(line, tail):
    found = _found(line)
    assert found, line
    if tail:
        assert ("masked_account", tail) in found


@pytest.mark.parametrize("line", [
    "*2024 figures are estimates",   # a footnote on a year
    '*"2019 restated',
    "PAY ****832.81",                # check-protected amount
    "Total $*1234.56",
    "*1234.56",
    "Sprouts Farmers Mkt #1236 -$40.34",   # a store number
    '"2024"',
    "Form *1099-MISC",
])
def test_a_star_beside_money_a_year_or_a_store_is_left_alone(line):
    assert not [f for f in _found(line) if f[0] == "masked_account"], _found(line)


def test_a_mask_glued_to_the_amount_does_not_take_the_amount():
    assert _found("DEBIT****262869.97") == []


# ----------------------------------------------- tail after "to/from bank" ---

@pytest.mark.parametrize("line, tail", [
    ("Withdrawal to BANK 3233 2025-06-09 07:52:44", "3233"),
    ("Added cash from Bank 8697", "8697"),
    ("Transfer to Zions Bank 4417", "4417"),
])
def test_a_transfer_to_a_bank_takes_its_tail(line, tail):
    assert ("bank_account", tail) in _found(line)


@pytest.mark.parametrize("line", [
    "Transfer to bank 2024 bonus",
    "Withdrawal from bank 1500.00",
    "Withdrawal from bank 2025-06-09",
])
def test_a_year_a_figure_or_a_date_after_bank_is_not_a_tail(line):
    assert not [f for f in _found(line) if f[0] == "bank_account"], _found(line)


# ------------------------------------------------- card tail glued by OCR ---

@pytest.mark.parametrize("line", [
    "S420126815924201Card1848",
    "Purchase authorized on 03/26 MICROSOFT S420126815924201Card 1848  45.50",
])
def test_a_card_glued_to_the_transaction_reference_is_caught(line):
    assert ("credit_card", "1848") in _found(line)


def test_a_store_number_on_a_card_purchase_is_not_the_card():
    """A payment app's "Cash Card - <merchant> NNNN" ends in the merchant's
    store number, not the client's card - the card is the app's own."""
    for line in ("Cash Card - Shell Oil 3138 -$49.21", "Cash Card - Harmons #7628"):
        assert not [f for f in _found(line) if f[0] in ("credit_card", "masked_account")]


# --------------------------------------------- routing behind a mangled label

@pytest.mark.parametrize("line", [
    "Routing Number (RTNj:229240574",
    "ROUTINGMUMIA (RTM) 229240574 OVERDRAFTPROTECTION",
])
def test_a_routing_number_behind_a_mangled_label_is_caught(line):
    assert ("routing_number", "229240574") in _found(line)


def test_routing_instructions_without_a_number_take_nothing():
    assert _found("For Direct Deposit use, give your employer the routing number below") == []


# ---------------------------------------------------- misread street suffix

@pytest.mark.parametrize("line, address", [
    ("Orla Quast 4417 W Sagebrush in, South Jordan, UT 84009 SSN",
     "4417 W Sagebrush in, South Jordan, UT 84009"),
    ("4417WSagebrushn,Southordan,UT84009", "4417WSagebrushn,Southordan,UT84009"),
    ("1734 W Gentile 5t, Layton, UT 84041", "1734 W Gentile 5t, Layton, UT 84041"),
])
def test_a_street_with_a_misread_suffix_is_held_by_its_city_line(line, address):
    assert ("street_address", address) in _found(line)


@pytest.mark.parametrize("line", [
    "Total 1,234 Items, Provo, UT 84604",
    "Due 02/26/2025 Payment, Provo, UT 84604",
    "Store #4417 Main, Provo, UT 84604",
])
def test_a_figure_a_date_or_a_store_before_a_city_line_is_not_a_street(line):
    assert not [f for f in _found(line) if f[0] == "street_address"], _found(line)


def test_an_itin_found_hyphenated_is_also_taken_bare():
    """One engine reads the W-2's box as "922-74-1631", the other glues it
    beside a garbled label as "922741631". Once the first registers it, the
    literal pass takes the second."""
    from redactor import engine
    from redactor.mapping import MappingStore
    store = MappingStore()
    out, _hits = engine.scan_and_apply(
        "Heber Valley Regional Hospital   922-74-1631\nEMPTOYER1DEURE1PI) 922741631", store,
        engine.Settings(use_ner=False), "doc")
    assert "922-74-1631" not in out and "922741631" not in out, out


@pytest.mark.parametrize("line, expected", [
    ("Cad9916", [("credit_card", "9916")]),
    ("Crd9916", [("credit_card", "9916")]),
    ("CAD 1234", []),          # Canadian dollars
    ("Arcad1234", []),         # inside a longer word
])
def test_a_card_label_missing_a_letter_still_introduces_its_tail(line, expected):
    assert [f for f in _found(line) if f[0] == "credit_card"] == expected


def test_a_balance_ocr_wrote_with_periods_is_money_not_a_handle():
    """A scanned loan ledger's balances came back "13.861.19" beside its
    Method column's "Zelle", and eight balances were boxed as handles."""
    text = ("12/01/2024   276.25   14,090.47 Zelle\n"
            "01/01/2025   276.25   13.861.19 Zelle\n"
            "02/01/2025   276.25   1.483.01 Zelie\n")
    assert not [m for m in patterns.scan(text) if m.category == "payment_handle"]


@pytest.mark.parametrize("text", ["Zelle\nPage 1 of 1", "Paid by Zelle\nSee reverse", "Venmo\nwins #"])
def test_the_word_on_the_next_row_is_not_a_handle(text):
    assert not [m for m in patterns.scan(text) if m.category == "payment_handle"]


@pytest.mark.parametrize("text, handle", [
    ("Venmo: @jane-doe-7", "@jane-doe-7"),
    ("Venmo\n@jane-doe-7", "@jane-doe-7"),
    ("Cash App\n$JaneD", "$JaneD"),
    ("Paid via Zelle janedoe22", "janedoe22"),
])
def test_a_handle_is_still_a_handle(text, handle):
    assert handle in {m.text for m in patterns.scan(text) if m.category == "payment_handle"}


@pytest.mark.parametrize("text,handle", [
    ("Venmo:\njohn-smith-12", "john-smith-12"),
    ("Cash App\n  kaylaruiz88", "kaylaruiz88"),
    ("PayPal\n  kayla.ruiz88", "kayla.ruiz88"),
    ("Zelle\nPage 2 of 4", None),
    ("Zelle\nSee reverse", None),
])
def test_a_handle_printed_under_its_rail_is_still_a_handle(text, handle):
    """A form prints "Venmo:" over the handle, mostly without its "@";
    a ledger's next row ("Page", "See") under "Zelle" is not one."""
    found = [m.text for m in patterns.scan(text) if m.category == "payment_handle"]
    assert found == ([handle] if handle else [])
