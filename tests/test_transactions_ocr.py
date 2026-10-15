"""Counterparties and employers on scanned and photographed statements.

Every input here is a reading OCR made of a page whose text-layer original the
rules already handled: the spaces lost, a label misread, an avatar's initial or
a middle dot in the row. The name shipped each time because the cue around it
no longer looked like the cue the rule was written for.
"""

import pytest

from redactor import transactions


def _names(parties):
    return {p.name for p in parties}


# ---------------------------------------------------------------------------
# a processor's "<type> Payment - <customer> ID:" line
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("line, expected", [
    ("09/05/2025     WebsitePayment-Madison Long ID:            USD", "Madison Long"),
    ("09/03/2025    “Website Paymént- Kevin Anderson ID:     ~—=~SCS        211,60", "Kevin Anderson"),
    ("09/03/2025    WebsitePayment-KevinAnderson ID:            USD", "Kevin Anderson"),
    ("09/20/2025     Website Payment-RachelBell ID;   =   USD", "Rachel Bell"),
    ("WebsitePayment-Rachel BellID:", "Rachel Bell"),
    ("06/14/2024    PaymentRelund-Tyler Pratt ID:            USD          -177.02", "Tyler Pratt"),
    ("06/14/2024 Payment Refund - Tyler Pratt ID USD 177.02", "Tyler Pratt"),
    ("02/26/2020 Website Payment : Kimberly Price ID USD 375.62", "Kimberly Price"),
    ("03/07/2020 Website Payment - Tyler Brooks !D AB0027WDKNVZB082V", "Tyler Brooks"),
    ("12/22/2023 Website Payment Joshua Turner iD uso 287.47", "Joshua Turner"),
    ("12/22/2023     Website Payment -JoshuaTurner ID:            USD", "Joshua Turner"),
    ("09/04/2025 Website Payment - Whitney MorganID:      US", "Whitney Morgan"),
    ("02/16/2020      Website Payment - Aubrey GarcialD:   = USD", "Aubrey Garcia"),
    ("09/18/2025    WebsitePayment-ColtonRichardsonID:            USD", "Colton Richardson"),
])
def test_an_ocr_reading_of_a_processor_line_still_names_the_customer(line, expected):
    assert _names(transactions.harvest(line)) == {expected}


@pytest.mark.parametrize("line, expected", [
    # a surname's own capital is not a join
    ("WebsitePayment-KayleeMcDonald ID:", "Kaylee McDonald"),
    ("WebsitePayment-Kaylee MacKenzie ID:", "Kaylee MacKenzie"),
    ("WebsitePayment-DeShawnLewis ID:", "DeShawn Lewis"),
    # a name ending in "ld" is not a name glued to the ID field
    ("User Payment - Jane Harold USD 5.00", "Jane Harold"),
])
def test_a_join_is_only_opened_where_ocr_made_it(line, expected):
    assert _names(transactions.harvest(line)) == {expected}


@pytest.mark.parametrize("line", [
    "WebsitePayment-UberTechnologies ID: 4KD9",
    "User Payment - DoorDash Inc USD -10.00",
])
def test_a_processor_merchant_glued_together_is_still_a_merchant(line):
    assert transactions.harvest(line) == []


def test_a_brand_glued_in_a_transfer_line_is_not_opened_into_a_person():
    # only the processor's customer slot opens a join; a brand elsewhere is
    # the brand - "PayPal", "DoorDash" - not a first and last name
    assert "Door Dash" not in _names(transactions.harvest("ZELLE TO DoorDash"))


# ---------------------------------------------------------------------------
# a wallet screenshot
# ---------------------------------------------------------------------------


WALLET = """Recent activity
Uber Technologies            -$204.81
May 3:      Express Checkout Payment
FUNDING SOURCE ***8623       +$204.81
May 3 - General Card Deposit
Brynlee Long            +$226.00
May & - General Payment
eBay Inc.            -$188.25
May 12: PreApproved Payment Bill   Use"""

WALLET_WITH_AVATARS = """UberTechnologies            -$204.81
May 3 · Express Checkout Payment
FUNDINGSOURCE***8623      +$204.81
May 3 · General Card Deposit
B   Brynlee Long            +$226.00
May 8·General Payment
S   Spotify USA Inc            -$139.43
May 19· General Payment"""


@pytest.mark.parametrize("screen", [WALLET, WALLET_WITH_AVATARS])
def test_a_wallet_row_read_with_an_ocr_date_or_an_avatar_names_the_payer(screen):
    assert _names(transactions.activity_feed(screen)) == {"Brynlee Long"}


# ---------------------------------------------------------------------------
# a payment app's From / To ledger, read across its rows
# ---------------------------------------------------------------------------


LEDGER = """ID            Datetime            Type         Status    Note            From           To            (total)       Fee Funding Source
9008223054395262090            Leilani Fonoti         Anthony Garcia            +$138.50
Payment       Complete   utilities split            Leilani Fonoti         Jennifer Hernandez
2121431043070464   Merchant Transact Complete   Leilani Fonoti        Etsy.Com- Vintagefinds     + $55.20
Standard Transfer            Emily Thompson          -$133.43        Bank***9323"""


def test_both_ends_of_a_scanned_ledger_row_are_proposed():
    assert _names(transactions.ledger_parties(LEDGER)) == {
        "Leilani Fonoti", "Anthony Garcia", "Jennifer Hernandez"}


def test_a_holder_glued_shut_in_one_cell_still_pairs_with_the_payee():
    """OCR read "Anthony Walker   AlyssaValdez"; with the holder's cell one
    word, the row had one person-shaped cell and the payee shipped."""
    text = ("Statement for Alyssa Valdez\n"
            "Status    Note            From          To\n"
            "movie            Anthony Walker      AlyssaValdez            +$79.33\n"
            "groceries        AlyssaValdez        DoorDash                -$41.10\n")
    assert _names(transactions.ledger_parties(text)) == {"Anthony Walker", "Alyssa Valdez"}


def test_a_page_without_a_from_to_heading_is_not_a_ledger():
    text = "\n".join(LEDGER.splitlines()[1:])
    assert transactions.ledger_parties(text) == []


# ---------------------------------------------------------------------------
# a Zelle activity row: the direction, then the party
# ---------------------------------------------------------------------------


ZELLE = """Date       Type      Recipient/ Sender            Email orU.S.Mobile          Memo          Status            Amount
06/03/2025    Sent        MEGANHOLBROOK       (801)555-0167            kids/groceries        Completed         -$712.05
06/19/2025            CANYONKEYSPM            Completed          -$134.02
06/20/2025    Received     KELSEYAOKI            (801)555-0110            babysitting            Completed           $58.54
06/21/2025    Recewed     DALLIN COX            (801)555-0111            pizza            Completed           $8.54
06/22/2025    Sent     Tanner Jensen            tj@example.com            rent            Completed           -$800.00
06/23/2025    Sent     CANYON KEYS PM            (801)555-0112            rent            Completed           -$900.00"""


def test_the_party_after_a_direction_cell_is_proposed():
    names = _names(transactions.direction_rows(ZELLE))
    assert {"KELSEYAOKI", "DALLIN COX", "Tanner Jensen"} <= names
    # no direction on its row, and a three-word business where it has one
    assert not names & {"CANYONKEYSPM", "CANYON KEYS PM", "Completed"}


def test_the_direction_rule_reads_the_page():
    names = _names(transactions.harvest_documents({"a": ""}, feeds={"a": ZELLE}))
    assert "KELSEYAOKI" in names


# ---------------------------------------------------------------------------
# the employer on a scanned or photographed deposit line
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("line, expected", [
    ("11/28/2024     DePOSACHOVERSTOCLCOMTIPE:DiRDOPD:2214822131CO:OVERSTOCK.COM    $1,409.39",
     "OVERSTOCK.COM"),
    ("5/31            RedCanyonLandsPayroll2305319913065380CODYBASHWORTH            1.789.16",
     "Red Canyon Lands"),
    ("5/15           Red CanyonLandsPayroll2305159913065380CODYBASHWORTH", "Red Canyon Lands"),
    ("8/16            IronCountyMediPayroll2408169085711120CODYBASHWORTH", "Iron County Medi"),
])
def test_the_employer_on_a_glued_or_misread_pay_line_is_found(line, expected):
    assert expected in _names(transactions.employers(line))


@pytest.mark.parametrize("line", [
    "12/05/2024      WhWAOHVERIZON WIRELESSTYPEPATMENTSID:0048941603CO:VERI2ON   $148.78",
    "12/04/2024      WINSWAAOHZELLETYPE:P2P CO:HALEY COX            $290.31",
    "Withdrawal ACH ROCKY MOUNTAIN POWER TYPE: UTILITY ID: 75 CO: ROCKY MOUNTAIN POWER",
    "12/09 CHECKCARD 1208 DoorDashDasher 4412 -$22.10",
])
def test_a_bill_a_transfer_or_a_purchase_is_not_an_employer(line):
    assert transactions.employers(line) == []


@pytest.mark.parametrize("line,expected", [
    ("MEGAN L   HOLBROOK", ["MEGAN L HOLBROOK"]),
    ("TYSON   OKAFOR", ["TYSON OKAFOR"]),
    ("Megan L   Holbrook", ["Megan L Holbrook"]),
    ("KAYLA J RUIZ   Current", ["KAYLA J RUIZ"]),
    ("KAYLA J RUIZ   CURRENT", ["KAYLA J RUIZ"]),
    ("MEGAN L HOLBROOK   YTD", ["MEGAN L HOLBROOK"]),
])
def test_a_holder_spaced_like_a_column_gap_is_one_name(line, expected):
    """OCR spaces a typed name as widely as a column gap: cut at the gap, the
    holder "MEGAN L   HOLBROOK" was lost whole; the next column's heading
    still stays out."""
    assert transactions._names_on_line(line) == expected


def test_a_bill_payment_row_names_a_merchant_not_a_person():
    text = ("06/21/2025   Paid   COSTCO WHOLESALE   groceries   $45.00\n"
            "07/02/2025   Sent   KAYLA RUIZ   $40.00\n")
    assert [p.name for p in transactions.direction_rows(text)] == ["KAYLA RUIZ"]
