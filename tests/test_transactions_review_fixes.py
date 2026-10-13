"""Review findings in redactor/transactions.py, each pinned by the input that
showed it.

The worst outcome is a surviving person or employer; the second worst is a
merchant or a heading proposed as a person, because ticking it renames a
business to an invented human being.
"""

import time

import pytest

from redactor import transactions


def _names(parties):
    return {p.name for p in parties}


# ---------------------------------------------------------------------------
# B1 - an employer the client also shops at is still the employer
# ---------------------------------------------------------------------------


STATEMENT = """10/01 ACH DEPOSIT HOME DEPOT PAYROLL            2,345.67
10/03 THE HOME DEPOT #4401 WEST JORDAN UT        -$33.12
10/05 ACH DEPOSIT SMITHS FOOD PAYROLL             1,100.00
10/06 CHECKCARD 1002 SMITHS FOOD SANDY UT         -$45.00
10/07 Direct deposit - Starbucks                  812.44
10/08 STARBUCKS STORE 1234                        -$5.65"""


def test_a_card_purchase_at_the_employer_does_not_drop_the_employer():
    assert _names(transactions.employers(STATEMENT)) == {"HOME DEPOT", "SMITHS FOOD", "Starbucks"}


@pytest.mark.parametrize("outgoing", [
    "Pingo - ACH  -$139.27",
    "Online Transfer to Pingo  -139.27",
    "PINGO P2P PMT  139.27-",
])
def test_money_sent_back_to_a_payment_app_still_marks_it_a_transfer_partner(outgoing):
    text = f"Direct deposit - Pingo  812.44\n{outgoing}"
    assert transactions.employers(text) == []


# ---------------------------------------------------------------------------
# B2, B3, S2 - a payment app's activity screen
# ---------------------------------------------------------------------------


VENMO = """Tanner Jensen paid You
Rent
+ $500.00
You paid Dallin Cox
pizza
- $19.00
Leilani Fonoti charged You
utilities
- $45.00
Rachel Pratt requested $20.00 from You
movie
- $20.00
You paid Taco Bell
lunch
- $12.34"""


def test_venmo_names_both_ends_on_the_row_itself():
    assert _names(transactions.activity_feed(VENMO)) == {
        "Tanner Jensen", "Dallin Cox", "Leilani Fonoti", "Rachel Pratt"}


def test_an_ocr_sign_glued_to_the_name_still_splits_the_row():
    text = ("Leilani Fonoti+$129.14\ncoffee\n"
            "Tanner Jensen-$92.76\nfantasy league\n"
            "Dallin Cox -$19.00\npizza")
    assert _names(transactions.activity_feed(text)) == {
        "Leilani Fonoti", "Tanner Jensen", "Dallin Cox"}


def test_a_hyphenated_name_is_not_a_sign():
    assert transactions._SIGNED_AMOUNT.findall("Anne-Marie Smith -$12.00") == ["-$12.00"]
    text = "Anne-Marie Smith -$12.00\nbrunch\nDallin Cox -$19.00\npizza"
    assert "Anne-Marie Smith" in _names(transactions.activity_feed(text))


WALLET_MERCHANTS = """Home Depot            -$147.57
Mobile Payment
Taco Bell            -$12.34
Mobile Payment
Panda Express            -$56.00
Express Checkout Payment
Whole Foods            -$88.10
Mobile Payment
Best Buy            -$499.00
General Payment
Panda Express            -$14.50
Mobile Payment
Dollar Tree            -$6.00
Mobile Payment
Costco Wholesale            -$160.00
Mobile Payment
Harmons Grocery            -$60.00
Mobile Payment
Ashley Hansen            -$147.57
Mobile Payment"""


def test_a_merchant_on_a_wallet_screen_is_not_a_person():
    assert _names(transactions.activity_feed(WALLET_MERCHANTS)) == {"Ashley Hansen"}


def test_a_name_that_also_heads_a_purchase_row_is_a_merchant():
    text = ("Jordan Landing -$20.00\nMobile Payment\n"
            "Jordan Landing -$5.00\nPurchase\n"
            "Ashley Hansen -$9.00\nMobile Payment")
    assert _names(transactions.activity_feed(text)) == {"Ashley Hansen"}


def test_a_card_statement_heading_is_not_a_feed_row():
    text = ("Payments            -$500.00\nOther Credits            -$12.00\n"
            "Rewards Summary\nearned this period")
    assert transactions.activity_feed(text) == []


# ---------------------------------------------------------------------------
# S3 - a role word in the payee slot
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("line", [
    "Direct deposit from Employer",
    "Payment to Beneficiary",
    "Payment to Provider",
    "Request to Change Address",
    "Transfer to Cardholder",
    "Payment received from Employee",
    "Received from Landlord",
    "Deposits received from Other Financial Institutions",
])
def test_a_role_word_in_the_payee_slot_is_nobody(line):
    assert transactions.harvest(line) == []


# ---------------------------------------------------------------------------
# S4 - a pay stub's earnings rows and a form's empty answers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Regular Salary        2,500.00",
    "Base Salary 2,500.00",
    "Annual Salary 60,000.00",
    "Merit Salary 1,000.00",
    "Employee Net Pay 1,900.00",
    "Hourly Rate 25.00 Salary",
    "Company Retirement Plan",
    "Employer Retirement Plan",
    "Paid by  Check",
    "Paid by: Check",
    "Employer:\n(none)",
    "Employer\nN/A",
    "Employer: Unemployed",
    "Employer contributions & taxes",
    "Employer Contributions\n$482.32",
])
def test_an_earnings_row_or_an_empty_answer_is_not_an_employer(text):
    assert transactions.employers(text) == []


@pytest.mark.parametrize("line, expected", [
    ("10/01 DOMO SALARY  2,345.67", "DOMO"),
    ("ACH DEPOSIT INTEL SALARY", "INTEL"),
    ("Employer: Check Into Cash", "Check Into Cash"),
    ("Direct deposit - Smiths Food & Drug", "Smiths Food & Drug"),
])
def test_a_bank_line_or_a_real_name_is_still_an_employer(line, expected):
    assert expected in _names(transactions.employers(line))


# ---------------------------------------------------------------------------
# S5, S6 and the bare initial - descriptor lines
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("line, expected", [
    ("PAYPAL INST XFER 240315 TANNER JENSEN WEB ID: PAYPALSI77", "TANNER JENSEN"),
    ("PAYPAL INST XFER TANNER JENSEN WEB ID: PAYPALSI77", "TANNER JENSEN"),
    ("VENMO CASHOUT 240315 TANNER JENSEN", "TANNER JENSEN"),
    ("ZELLE TO TANNER  JENSEN", "TANNER JENSEN"),
    ("ZELLE TO TANNER  JENSEN  CONF# 1234", "TANNER JENSEN"),
    ("ZELLE TO TANNER  JENSEN ON 10/03", "TANNER JENSEN"),
    ("ZELLE TO J SMITH", "J SMITH"),
    ("CHECK 1042 TO J SMITH", "J SMITH"),
])
def test_the_payee_is_reached(line, expected):
    assert _names(transactions.harvest(line)) == {expected}


@pytest.mark.parametrize("line, expected", [
    # a column gap after a full name is still a column gap
    ("01/11/2023         Pay Friends to Brynlee Nelson            Pay Friends", {"Brynlee Nelson"}),
    ("Zelle to Kenzie  Pay Friends", {"Kenzie"}),
    ("TRANSFER TO KENZIE  SAVINGS", {"KENZIE"}),
    ("ZELLE TO KENZIE  Completed", {"KENZIE"}),
    ("PAYPAL INST XFER 241005 ETSY WEB ID: PAYPALSI77", set()),
    ("Payment To A Vendor", set()),
])
def test_a_column_gap_is_not_crossed(line, expected):
    assert _names(transactions.harvest(line)) == expected


# ---------------------------------------------------------------------------
# the smaller findings
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Total Fees Account # 1234",
    "Owner Occupied Property",
    "Landlord Tenant Act",
    "Lender Fees Paid",
    "Zions Bancorporation\n1 S Main St\nSalt Lake City UT 84111",
])
def test_a_heading_or_an_institution_is_not_a_person(text):
    assert transactions.account_holders(text) + transactions.labelled_people(text) == []


def test_a_zip_after_a_state_keeps_the_street_an_address():
    text = "TYSON J OKAFOR\n1234 MAPLE GROVE, LOGAN UT 84321"
    assert _names(transactions.account_holders(text)) == {"TYSON J OKAFOR"}
    # and an item's set number is still not a ZIP
    assert transactions.account_holders(
        "Items Ordered\n2 of: LEGO Classic Large Creative Brick Box 10698") == []


def test_a_surname_first_receiver_field_is_read():
    text = "DES:PAYROLL ID:1234 INDN:OKAFOR,TYSON CO ID:9876 PPD"
    assert _names(transactions.receivers(text)) == {"TYSON OKAFOR"}


# ---------------------------------------------------------------------------
# S1 - a long run of spaces
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("pattern", [
    transactions.PAY_DEPOSIT_LINES[0],
    transactions.PLAN_SPONSOR,
    transactions.CARDHOLDER,
    transactions.BOOKS_TITLE,
])
def test_a_long_blank_run_does_not_backtrack(pattern):
    """A scanned page's blank row - one letter, 2,000 spaces, one letter - took
    seconds per pattern while two whitespace quantifiers split the run."""
    text = "A" + " " * 2000 + "x\n"
    started = time.perf_counter()
    list(pattern.finditer(text))
    assert time.perf_counter() - started < 0.2


def test_the_rewritten_column_start_still_finds_what_it_found():
    assert _names(transactions.employers(
        "10/01 ACH DEPOSIT QUALTRICS LLC PAYROLL   2,345.67")) == {"QUALTRICS LLC"}
    assert _names(transactions.employers(
        "Summary     NORTHPOINT AEROSPACE SERVICES 401(K) SAVINGS PLAN")) == {
        "NORTHPOINT AEROSPACE SERVICES"}
    assert _names(transactions.account_holders(
        "New Charges  BRIAN ROBERTS Card Ending 2-3164")) == {"BRIAN ROBERTS"}
    assert _names(transactions.businesses(
        "Lindquist Family Dairy & Hay   Profit and Loss")) == {"Lindquist Family Dairy & Hay"}



def test_a_person_whose_surname_is_a_trade_word_is_still_proposed():
    """No surname is on the trade-word list: missing a payee costs more than
    proposing a merchant the operator can untick."""
    screen = "Ashley Garden            -$40.00\nMobile Payment\nKyle Tree            -$12.00\nGeneral Payment"
    found = {p.name for p in transactions.activity_feed(screen)}
    assert {"Ashley Garden", "Kyle Tree"} <= found



def test_topping_up_a_wallet_app_is_money_going_out():
    """The client funds the app and cashes out of it; neither makes it an
    employer."""
    text = ("01/03 DIRECT DEPOSIT ORCHARD CASH 812.44\n"
            "01/09 ORCHARD CASH ADD MONEY   -$50.00")
    assert transactions._paid_by_the_account(text, "ORCHARD CASH")



def test_a_payee_never_comes_from_the_next_line():
    text = "Bank Deposit to\nFUNDING SOURCE ***9268\nGeneral Card Deposit to\nEtsy, Inc."
    assert not {p.name for p in transactions.harvest(text)} & {"FUNDING SOURCE", "Etsy"}
    assert "Tanner Jensen" in {p.name for p in transactions.harvest("ZELLE PAYMENT TO Tanner Jensen")}



def test_a_business_wallet_puts_the_date_before_the_kind():
    """"Jun 8 - Website Payment" under the payer; an amount that wrapped onto
    its own line between them."""
    screen = ("Oakley Morris            +$53.15\nJun 6+:   Website Payment\n"
              "BANK *+#+#7269\n-$1,400.70\nJun 7: General Withdrawal - Bank Ac\n"
              "Andrew Sanders            +$267.28\nJun 8- Website Payment\n"
              "Hailey Johnson\n-$60.67\nJun 10 -    Payment Refund")
    assert {p.name for p in transactions.activity_feed(screen)} == {
        "Oakley Morris", "Andrew Sanders", "Hailey Johnson"}



def test_a_row_never_reads_into_the_next_column():
    """Descriptions read down their column end "Daily Cash"; the type column
    starts "Sent". Joined, the cash-back line read as a payment to a person."""
    text = ("Amazon MKTPL\nTyson Okafor\nDaily Cash\n\nPurchase\nSent\nDaily Cash\n\n"
            "Tyson Okafor            -$14.77\nSent\nDaily Cash            +$17.86\nDaily Cash")
    assert {p.name for p in transactions.activity_feed(text)} == {"Tyson Okafor"}


def test_an_entry_whose_kind_is_its_own_name_is_the_apps_own_line():
    text = ("Malia Fonoti            -$33.80\nSent\n"
            "Daily Cash            +$41.69\nDaily Cash\n"
            "Leilani Fonoti            +$16.39\nReceived")
    assert {p.name for p in transactions.activity_feed(text)} == {"Malia Fonoti", "Leilani Fonoti"}


def test_the_feed_rule_reads_the_page_not_its_columns():
    columns = "Purchase\nDaily Cash\nSent\n\nTyson Okafor            -$14.77\nSent\nAbigail Okafor            +$9.00\nReceived"
    page = "Tyson Okafor            -$14.77\nSent\nAbigail Okafor            +$9.00\nReceived"
    names = {p.name for p in transactions.harvest_documents({"a": columns}, feeds={"a": page})}
    assert "Daily Cash" not in names and {"Tyson Okafor", "Abigail Okafor"} <= names
