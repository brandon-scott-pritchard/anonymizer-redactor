"""Leaks and over-redactions found by running the corpus evaluation.

``eval/corpus_eval.py`` runs every document of a labelled corpus of ~8,200
generated financial PDFs through the app's own four steps and scores what
survives. Each test here encodes an input from that corpus that failed, cut
down to the few lines that make it fail. Every name and number is invented.
"""

import pytest

from redactor import engine, patterns, pdf_processor, pipeline, review, transactions
from redactor.engine import Settings

pymupdf = pytest.importorskip("pymupdf")


def _placed(tmp_path, name, cells, size=7, width=792, height=612):
    """A PDF with every cell written by its own text call, as generators do.

    PyMuPDF then numbers each cell as its own line, which is exactly what hid
    a payment ledger's columns from the table reader.
    """
    path = tmp_path / name
    doc = pymupdf.open()
    page = doc.new_page(width=width, height=height)
    for x, y, text in cells:
        page.insert_text((x, y), text, fontsize=size, fontname="helv")
    doc.save(path)
    doc.close()
    return path


def _run_pdf(path, tmp_path, entries=()):
    settings = Settings(use_ner=False)
    store, _ = review.build_store(list(entries))
    pipeline.prescan([path], store, settings)
    out = tmp_path / ("out-" + path.name)
    pdf_processor.process(path, out, store, settings)
    with pymupdf.open(out) as doc:
        return "\n".join(page.get_text("text") for page in doc), store


# ---------------------------------------------------------------------------
# two names sharing a word
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["redact", "anonymize"])
def test_a_dependent_listed_before_a_sibling_keeps_no_first_name(mode):
    """A 1040's dependents line: "Luke Harrington, Emma Harrington".

    Emma's "Harrington, Emma" out-ranked Luke's "Luke Harrington" by one
    character, and Luke's hit was dropped whole - so "Luke" shipped.
    """
    store, _ = review.build_store([("Luke Harrington", "person"),
                                   ("Emma Harrington", "person"),
                                   ("Kyle D Harrington", "person")])
    settings = Settings(use_ner=False, docx_mode=mode)
    text = "Dependents\nLuke Harrington, Emma Harrington\nIncome"
    out = engine.apply_hits(text, engine.scan_text(text, store, settings, register=False))
    for name in ("Luke", "Emma", "Harrington"):
        assert name not in out, f"{name} survived: {out!r}"


# ---------------------------------------------------------------------------
# a PDF whose cells are separate lines
# ---------------------------------------------------------------------------

LEDGER = [
    (32, 80, "ID"), (112, 80, "Datetime"), (192, 80, "Type"), (293, 80, "Note"),
    (373, 80, "From"), (445, 80, "To"), (540, 80, "Amount"),
    (700, 90, "$49.21"),                       # the opening balance, alone on its row
    (32, 100, "5128176424558106993"), (112, 100, "2020-10-01T16:31:50"),
    (192, 100, "Payment"), (293, 100, "ski pass split"),
    (373, 100, "Jenna Harrington"), (445, 100, "Colton Hughes"), (540, 100, "- $24.13"),
    (32, 110, "3338970279670315574"), (112, 110, "2020-10-02T20:44:49"),
    (192, 110, "Payment"),                     # no memo: one cell short
    (373, 110, "Sarah Hansen"), (445, 110, "Jenna Harrington"), (540, 110, "+ $167.87"),
]


def test_a_payment_ledger_is_read_as_rows_and_columns(tmp_path):
    path = _placed(tmp_path, "ledger.pdf", LEDGER)
    tables = pipeline.tables_in(path)
    assert tables, "no table was reconstructed from one-line-per-cell geometry"
    header = next(t for t in tables if "From" in t[0])[0]
    rows = next(t for t in tables if "From" in t[0])
    frm, to = header.index("From"), header.index("To")
    by_id = {row[0]: row for row in rows[1:] if row[0]}
    # the memo-less row must not slide left: the payer stays under From
    assert by_id["3338970279670315574"][frm] == "Sarah Hansen"
    assert by_id["3338970279670315574"][to] == "Jenna Harrington"


def test_both_ends_of_a_payment_ledger_are_proposed(tmp_path):
    path = _placed(tmp_path, "ledger.pdf", LEDGER)
    names = {p.name for p in transactions.from_tables(pipeline.tables_in(path))}
    assert {"Colton Hughes", "Sarah Hansen", "Jenna Harrington"} <= names


def test_a_recipient_or_sender_column_is_a_party_column():
    table = [["Date", "Type", "Recipient / Sender", "Memo", "Amount"],
             ["01/02/2021", "Sent", "MARISSA LINDQUIST", "household", "-$490.83"]]
    assert [p.name for p in transactions.from_tables([table])] == ["MARISSA LINDQUIST"]


def test_a_label_and_its_value_in_separate_runs_are_still_one_line(tmp_path):
    """The visual-row reading puts "Account number" beside its value."""
    path = _placed(tmp_path, "acct.pdf", [(40, 80, "Account number"), (160, 80, "8812004417"),
                                          (40, 92, "Balance"), (160, 92, "$1,204.50")])
    after, _ = _run_pdf(path, tmp_path)
    assert "8812004417" not in after
    assert "$1,204.50" in after


# ---------------------------------------------------------------------------
# the account holder
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text, expected", [
    # a joint account stacks its holders; only the lower one was ever found
    ("OAKLEY V WARD\nJACOB N WARD\n12469 W CENTER ST\nLAYTON UT 84042-3715",
     {"OAKLEY V WARD", "JACOB N WARD"}),
    ("GERALD W PACKARD AND DIANE M PACKARD\n1488 E DESERT ROSE DR",
     {"GERALD W PACKARD", "DIANE M PACKARD"}),
    ("Colby & Jess Morgan\n12 Main St", {"Colby Morgan", "Jess Morgan"}),
    # every cardholder on a business card gets a heading of their own
    ("New Charges  BRIAN ROBERTS Card Ending 2-3164", {"BRIAN ROBERTS"}),
])
def test_every_account_holder_is_proposed(text, expected):
    assert {p.name for p in transactions.account_holders(text)} == expected


@pytest.mark.parametrize("line, expected", [
    ("UNIV OF UTAH DES:PAYROLL ID:038644597 INDN:OAKLEY V WARD CO ID:5809079224 PPD",
     "OAKLEY V WARD"),
    ("Northpoint Aeros Payroll 200110 7148234928 KYLE D HARRINGTON", "KYLE D HARRINGTON"),
])
def test_the_ach_receiver_is_the_account_holder(line, expected):
    assert [p.name for p in transactions.receivers(line)] == [expected]


# ---------------------------------------------------------------------------
# counterparties
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("line, expected", [
    ("12/30/21 12/30/21 ZELLE CREDIT JESSICA COOK 137.75 4,857.19", "JESSICA COOK"),
    ("Dec 27 Zelle Debit Jennifer Lewis 293.87", "Jennifer Lewis"),
    ("PURCHASE COSTCO GAS #9849 288.45- ZELLE KENZIE LEWIS CHECKS PROCESSED", "KENZIE LEWIS"),
    ("Withdrawal ACH VENMO TYPE: P2P CO: MADISON HARRIS $250.77", "MADISON HARRIS"),
    ("01/11/2023         Pay Friends to Brynlee Nelson            Pay Friends", "Brynlee Nelson"),
    ("12/21/2019 Website Payment - Tyler Foster ID: RCJIMIQYQWR3ZT1PJ USD 221.85", "Tyler Foster"),
    ("1/15   Online Transfer to Cody B Ashworth Ref #Ib01999297 Everyday Savings",
     "Cody B Ashworth"),
])
def test_a_payment_rail_counterparty_is_proposed(line, expected):
    assert [p.name for p in transactions.harvest(line)] == [expected]


@pytest.mark.parametrize("line", [
    # a card processor's star is followed by a merchant, never a person
    "12/19 SQ *BLUE MOON COFFEE OGDEN 78.82",
    "DOORDASH*CHIPOTLE SAN FRANCISCO",
    "Purchase authorized on 01/11 Amazon MKTPL*MMDARCMP0 Amzn.com/bill",
    "CHECKCARD 1216 GOOGLE *YOUTUBEPREMIUM G.CO/HELPPAY# 74913320645",
    "TST* SLAB PIZZA LAYTON",
    "VENMO DES:CASHOUT ID:280016597",
    "ZELLE CONFIRMATION 3031305858",
])
def test_a_merchant_or_a_rail_keyword_is_not_a_person(line):
    assert transactions.harvest(line) == []


# ---------------------------------------------------------------------------
# employers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("line, expected", [
    ("12/23/24   UNIV OF UTAH DES:PAYROLL ID:038644597 INDN:OAKLEY V WARD CO ID:1", "UNIV OF UTAH"),
    ("1/10  Northpoint Aeros Payroll 200110 7148234928 KYLE D HARRINGTON", "Northpoint Aeros"),
    ("Deposit ACH DOMO INC TYPE: PAYROLL ID: 1234567890 CO: DOMO INC", "DOMO INC"),
    ("ACH DEPOSIT QUALTRICS LLC PAYROLL", "QUALTRICS LLC"),
    ("Ach Credit Western Governors Dir Dep", "Western Governors"),
    ("Summitline Softw Payroll PPD ID: 1234567890", "Summitline Softw"),
    ("Direct deposit - Smiths Food & Drug", "Smiths Food & Drug"),
    ("Dec 26 Direct Deposit from Cricut Inc +$993.70", "Cricut Inc"),
    ("12/20/2019 PAYROLL CONTRIBUTION NU SKIN ENTERPRISE $172.41", "NU SKIN ENTERPRISE"),
    ("NORTHPOINT AEROSPACE SERVICES 401(K) SAVINGS PLAN", "NORTHPOINT AEROSPACE SERVICES"),
    ("Personal Information Employer(s)   U.S. Air Force - 75th Air Base Wing   Tradelines",
     "U.S. Air Force - 75th Air Base Wing"),
])
def test_the_employer_is_found_however_the_line_prints_it(line, expected):
    assert expected in [p.name for p in transactions.employers(line)]


@pytest.mark.parametrize("line", [
    "For Direct Deposit use, give your employer the routing number below",
    "Online Banking transfer from SAV 4378 Confirmation# 3031305858",
])
def test_instructions_are_not_an_employer(line):
    assert transactions.employers(line) == []


@pytest.mark.parametrize("stub, expected", [
    ("RED ROCK DENTAL ARTS\n595 S BLUFF ST, ST GEORGE UT 84770\nEARNINGS STATEMENT\n"
     "Gross Pay\nNet Pay\nPay Date", "RED ROCK DENTAL ARTS"),
    ("MCKAY VALLEY CARE CENTER\nPAY STATEMENT\nEmployee:\nJENNA R HARRINGTON\n"
     "Gross Pay\nNet Pay\nPay Date: 01/17/2020", "MCKAY VALLEY CARE CENTER"),
    ("ABIGAIL N OKAFOR\n435 N 900 E Apt 12\nProvo, UT 84604\nABIGAIL N OKAFOR\n"
     "Employee Pay Stub\nGross Pay\nNet Pay\nPay Period\nBrightline Tutoring LLC\n"
     "Employee Pay Stub", "Brightline Tutoring LLC"),
])
def test_a_pay_stub_letterhead_names_the_employer(stub, expected):
    found = transactions.pay_stub_employers(stub)
    assert expected in found
    assert not any("OKAFOR" in name or "HARRINGTON" in name for name in found)


def test_a_statement_letterhead_is_not_an_employer():
    """A bank prints its name over its address too; it is not a pay stub."""
    text = "Continental Crest Bank\n100 Main St, Tampa FL 33622\nAccount summary\nNet change"
    assert transactions.pay_stub_employers(text) == []


# ---------------------------------------------------------------------------
# the client's own business
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text, expected", [
    ("Continental Crest Bank\nP.O. Box 25118, Tampa, FL 33622-5118\n"
     "HARRINGTON ENTERPRISES LLC\n2279 E 5950 S\nOGDEN UT 84403-7079", ["HARRINGTON ENTERPRISES LLC"]),
    ("Harrington Enterprises Llc\nbilling@harringtonco.example.com\n2279 E 5950 S, Ogden, UT",
     ["Harrington Enterprises Llc"]),
    ("Redstone Auto Repair LLC\nProfit and Loss\nJanuary 2025", ["Redstone Auto Repair LLC"]),
    ("X DES:TRANSFER ID:819175091 INDN:HIGH DESERT DESIGN CO CO ID:1111128324 PPD",
     ["HIGH DESERT DESIGN CO"]),
    # a servicer's return address has the same shape and must stay
    ("Wasatch Front Mortgage Co.\n123 Main St\nSalt Lake City UT 84101", []),
])
def test_the_clients_business_is_proposed_and_an_institution_is_not(text, expected):
    assert [p.name for p in transactions.businesses(text)] == expected


# ---------------------------------------------------------------------------
# detector shapes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text", [
    "Online Transfer to Cody Ref #Ib01999297 Everyday Savings\nxxxxxxxxxx\n150.00\n4,053.29",
    "4,053.29   xxxxxxxxxx",
])
def test_a_mask_never_borrows_digits_from_a_dollar_figure(text):
    """"xxxxxxxxxx 150" and "29 xxxxxxxxxx" were registered as accounts, and the
    literal pass then boxed those digits out of every figure containing them."""
    assert not [m for m in patterns.scan(text) if m.category in ("masked_account", "bank_account")]


@pytest.mark.parametrize("text, value, category", [
    ("Monthly Statement Jan 1 - Jan 31, 2021 $TysonOka273 Starting balance $60.58",
     "$TysonOka273", "payment_handle"),
    ("LNOTICE EXPIRES11302020 PLATE Y777MX VIN/HIN", "Y777MX", "license_plate"),
    ("TITLE NUMBER 44945830 ISSUE DATE 06122019", "44945830", "vehicle_title"),
    ("VIN/HIN CXLACBFJEXBDAUBVY VEHICLE 2020 GMC", "CXLACBFJEXBDAUBVY", "vin"),
    ("Account Summary INDIVIDUAL - TOD 059-600037", "059-600037", "investment_account"),
    ("ACCOUNT SUMMARY JOINT WROS 215995662", "215995662", "investment_account"),
])
def test_an_unlabelled_identifier_the_corpus_prints(text, value, category):
    assert (value, category) in [(m.text, m.category) for m in patterns.scan(text)]


@pytest.mark.parametrize("text", ["paid $AAPL and $USD 45.00", "plate glass window",
                                  "Roth IRA 2024 contribution limit", "Individual 1,234,567.00"])
def test_tickers_prose_years_and_money_are_left_alone(text):
    assert patterns.scan(text) == []


def test_a_number_is_found_however_it_is_punctuated():
    """A W-2 prints the EIN as 00-8749107 in box b and 008749107 in box 15."""
    store, _ = review.build_store([])
    settings = Settings(use_ner=False, docx_mode="redact")
    engine.scan_text("b Employer ID number (EIN) 00-8749107", store, settings)
    engine.scan_text("Account number: 3357 3537 2072", store, settings)
    for text, value in (("15 State / Employer's state ID\nUT  008749107", "008749107"),
                        ("coupon scan 335735372072 due", "335735372072")):
        hits = engine.scan_text(text, store, settings, register=False)
        assert value in [h.text for h in hits]
    # never inside money
    assert engine.scan_text("total 3,357,353.72", store, settings, register=False) == []


# ---------------------------------------------------------------------------
# the remittance address, by geometry
# ---------------------------------------------------------------------------


def test_the_address_block_stays_in_its_own_column(tmp_path):
    """A utility bill prints "Amount due $116.31" on the address's own lines.

    Taking whole visual lines boxed the amount due off every bill in the
    corpus - 528 dollar figures on utility bills alone.
    """
    cells = [(50, 117, "MARCUS A VALDEZ"), (340, 117, "Amount due"), (543, 117, "$116.31"),
             (50, 128, "ALYSSA M VALDEZ"), (340, 131, "Due"), (532, 131, "02/26/2025"),
             (50, 138, "4417 W SAGEBRUSH LN"),
             (50, 149, "SOUTH JORDAN UT 84009-2210")]
    path = _placed(tmp_path, "bill.pdf", cells, size=8, width=612, height=792)
    after, _ = _run_pdf(path, tmp_path)
    for kept in ("$116.31", "02/26/2025", "Amount due"):
        assert kept in after, f"{kept} was destroyed"
    for gone in ("VALDEZ", "SAGEBRUSH", "84009"):
        assert gone not in after, f"{gone} survived"


# ---------------------------------------------------------------------------
# scans are read at review time, not only during the run
# ---------------------------------------------------------------------------


def test_a_scanned_page_is_read_by_the_review_steps(tmp_path):
    from redactor import ocr
    ok, _note = ocr.describe()
    if not ok:
        pytest.skip("no OCR engine")
    source = pymupdf.open()
    page = source.new_page()
    for index, line in enumerate(["Zelle to Marisol Quintanilla on 02/01", "Ref #Rp066382387"]):
        page.insert_text((60, 90 + index * 30), line, fontsize=14, fontname="helv")
    pixmap = page.get_pixmap(dpi=200)
    doc = pymupdf.open()
    doc.new_page().insert_image(doc[0].rect, pixmap=pixmap)
    path = tmp_path / "scan.pdf"
    doc.save(path)

    text = "\n".join(pdf_processor.extract_text(path).values())
    assert "Quintanilla" in text
    names = [p.name for p in transactions.harvest(text)]
    assert any("Quintanilla" in name for name in names)


# ---------------------------------------------------------------------------
# money in every sign convention, and digits that only look like a card
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("value", ["636.09-", "-$1,204.50", "(412.55)", "412.55CR", "12.34 DR"])
def test_a_signed_amount_is_money_for_every_identifier(value):
    """One layout prints every debit as "636.09-"; each one registered as an
    account number and was boxed out of the column."""
    assert patterns._wrong_shape("bank_account", value)
    assert patterns._wrong_shape("loan_number", value)


def test_a_debit_column_with_trailing_minus_survives(tmp_path):
    cells = [(40, 80, "LOAN PMT"), (40, 92, "12/26"), (100, 92, "636.09-"),
             (150, 92, "ACH DEBIT CANYON STATE CREDIT UN LOAN PMT"),
             (40, 104, "12/26"), (100, 104, "916.12-"),
             (150, 104, "ACH DEBIT CLEARPATH LENDING LOAN PMT")]
    path = _placed(tmp_path, "debits.pdf", cells, size=7, width=612, height=792)
    after, _ = _run_pdf(path, tmp_path)
    assert "636.09-" in after and "916.12-" in after


@pytest.mark.parametrize("line", [
    "CHECKCARD 1212 SWIG 363 LAYTON 32902688744",          # a posting date, not a card
    "Northpoint Aeros Payroll 200110 7148234928",          # a trace date and a company ID
])
def test_digits_that_only_look_like_a_card_are_left_alone(line):
    assert not [m for m in patterns.scan(line) if m.category == "credit_card"]


@pytest.mark.parametrize("line, value", [
    ("S851943881268519 Card 9031", "9031"),
    ("Visa card 4111 1111 1111 1111", "4111 1111 1111 1111"),
    ("3782 822463 10005", "3782 822463 10005"),
    ("card ending in 9876", "9876"),
])
def test_a_card_as_cards_are_printed_is_still_caught(line, value):
    assert (value, "credit_card") in [(m.text, m.category) for m in patterns.scan(line)]


def test_a_lockbox_address_takes_no_bank_name_with_it(tmp_path):
    # spaced as the corpus statement prints it: 7pt type on 10pt leading
    cells = [(40, 80, "Write: Pioneer Trail Bank, N.A. (114)"), (40, 90, "P.O. Box 6995"),
             (40, 100, "Portland,  OR 97228-6995")]
    path = _placed(tmp_path, "lockbox.pdf", cells, size=7, width=612, height=792)
    after, _ = _run_pdf(path, tmp_path)
    assert "Pioneer Trail Bank" in after


def test_a_date_on_the_line_above_is_not_a_house_number():
    """"Due 02/26/2025" over "4417 W SAGEBRUSH LN" opened an address at "2025"."""
    matches = patterns.scan("Due 02/26/2025\n4417 W SAGEBRUSH LN\nSOUTH JORDAN UT 84009-2210")
    assert not any("2025" in m.text for m in matches if m.category == "street_address")
    assert any(m.text.startswith("4417 W SAGEBRUSH LN") for m in matches)


def test_a_box_never_takes_glyphs_off_the_neighbouring_lines(tmp_path):
    """Character boxes are taller than the line pitch in a tight table.

    PyMuPDF deletes any character whose box touches a redaction rectangle, so
    a box padded outward reached into the rows above and below it. Lines here
    are set solid - no leading at all - which is the worst case.
    """
    cells = [(40, 80, "Opening balance 4,812.07"),
             (40, 89, "Zelle to Marisol Quintanilla 212.00"),
             (40, 98, "Closing balance 4,600.07")]
    path = _placed(tmp_path, "solid.pdf", cells, size=8, width=612, height=792)
    after, _ = _run_pdf(path, tmp_path, [("Marisol Quintanilla", "person")])
    assert "Quintanilla" not in after
    for kept in ("Opening balance 4,812.07", "Closing balance 4,600.07", "212.00"):
        assert kept in after, f"{kept!r} lost glyphs"


# ---------------------------------------------------------------------------
# round three: scans, employers in grids, and the address block as text
# ---------------------------------------------------------------------------


def test_ocr_that_reads_the_mask_as_a_quote_still_loses_the_digits():
    """Tesseract returned "***8697" as '"8697'."""
    assert ("8697", "masked_account") in [(m.text, m.category) for m in
                                          patterns.scan('Added cash trom Bank  "8697')]
    assert not patterns.scan('In 1999 the "2019" plan was quoted')


def test_an_initial_glued_to_a_hyphenated_surname_goes_with_it():
    """OCR merged "A BARRETT-WHITFIELD" into one word; the exact "WHITFIELD"
    inside it out-ranked the typo match on the whole word."""
    store, _ = review.build_store([("Jonathan E Whitfield", "person"),
                                   ("Chloe A Barrett-Whitfield", "person")])
    settings = Settings(use_ner=False, docx_mode="redact")
    text = "New Charges  CHLOE ABARRETT-WHITFIELD Card Ending 3-6815"
    out = engine.apply_hits(text, engine.scan_text(text, store, settings))
    for gone in ("CHLOE", "BARRETT", "WHITFIELD", "6815"):
        assert gone not in out, f"{gone} survived: {out!r}"


@pytest.mark.parametrize("line", [
    "Feb 27       Direct deposit - Venmo            ACH            +$539.13",
    "Direct deposit - Coinbase",
])
def test_money_from_a_payment_app_is_not_wages(line):
    assert transactions.employers(line) == []


def test_a_processor_line_names_the_person_before_its_transaction_id():
    text = ("PreApproved Payment Bill User Payment - Kenzie Cook ID: FDALS2I4D3FTCQP9O USD -125.86\n"
            "PreApproved Payment Bill User Payment - eBay Inc. ID: X1\n"
            "PAYMENT - THANK YOU")
    assert [p.name for p in transactions.harvest(text)] == ["Kenzie Cook"]


def test_a_heading_over_a_numbered_line_is_not_an_account_holder():
    """A tax form's "Part II Adjustments" over "10 Adjustments to income"."""
    assert transactions.account_holders("Part II Adjustments\n10 Adjustments to income\n") == []


@pytest.mark.parametrize("table, expected", [
    ([["PAYER'S / EMPLOYER'S name, street address, city, state, ZIP", "a Employee's SSN"],
      ["Red Rock Dental Arts", "902-98-8919"]], ["Red Rock Dental Arts"]),
    ([["Base Period Wages", "Employer", "Q1"], ["", "McKay Valley Care Center", "$7,800.00"]],
     ["McKay Valley Care Center"]),
])
def test_the_employer_in_a_form_grid_is_proposed(table, expected):
    assert [p.name for p in transactions.employers_from_tables([table])] == expected


def test_a_financial_statement_names_the_business_it_owns():
    text = "Other assets (100% interest Redstone Auto Repair LLC) $285,000.00\nBusiness Platinum Card"
    assert [p.name for p in transactions.businesses(text)] == ["Redstone Auto Repair LLC"]


def test_a_lockbox_line_is_not_boxed_as_a_home_address(tmp_path):
    cells = [(40, 760, "Redfield Retail Bank, PO Box 669804, Dallas, TX 75266-0781")]
    path = _placed(tmp_path, "remit.pdf", cells, size=7, width=612, height=792)
    after, _ = _run_pdf(path, tmp_path)
    assert "Redfield Retail Bank" in after


def test_a_zip_plus_four_is_an_address_wherever_it_stands():
    assert ("84403-7079", "street_address") in [(m.text, m.category) for m in
                                                patterns.scan("Checking Routing it 84403-7079 Beginning")]


def test_a_word_document_loses_its_whole_mailing_block(tmp_path):
    """The PDF path covers an address block by geometry; a DOCX has none, and
    the city, state and ZIP+4 shipped under a redacted street."""
    docx = pytest.importorskip("docx")
    from redactor import docx_processor
    path = tmp_path / "statement.docx"
    document = docx.Document()
    for line in ["Continental Crest Bank", "P.O. Box 25118, Tampa, FL 33622-5118",
                 "OAKLEY V WARD", "12469 W CENTER ST", "LAYTON UT 84042",
                 "Beginning balance $1,246.48"]:
        document.add_paragraph(line)
    document.save(path)
    settings = Settings(use_ner=False, docx_mode="anonymize")
    store, _ = review.build_store([("Oakley V Ward", "person")])
    pipeline.prescan([path], store, settings)
    out = tmp_path / "out.docx"
    docx_processor.process(path, out, store, settings)
    after = "\n".join(docx_processor.extract_text(out).values())
    for gone in ("OAKLEY", "12469", "LAYTON UT 84042"):
        assert gone not in after, f"{gone} survived"
    for kept in ("Continental Crest Bank", "$1,246.48"):
        assert kept in after


# ---------------------------------------------------------------------------
# people named beside a label, which is most of how forms name anybody
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("text, expected", [
    ("PAY STATEMENT\nEmployee:\nJENNA R HARRINGTON\nSSN:", ["JENNA R HARRINGTON"]),
    ("2024 Corporation EIN Shareholder CODY B ASHWORTH Ownership % 100%", ["CODY B ASHWORTH"]),
    ("2020 Names KYLE D HARRINGTON & JENNA R HARRINGTON SSN",
     ["KYLE D HARRINGTON", "JENNA R HARRINGTON"]),
    ("NAME (LAST, FIRST, MI)\nNAKAMURA, DEREK L\nSOC. SEC. NO.", ["DEREK L NAKAMURA"]),
    ("Your first name and middle initial / Last name\nGERALD W PACKARD\n", ["GERALD W PACKARD"]),
    ("promises to pay to the order of Cody B Ashworth and Shayla R Ashworth",
     ["Cody B Ashworth", "Shayla R Ashworth"]),
    ("Trust Account Account Number MARCO A RUIZ TTEE RUIZ FAMILY TRUST", ["MARCO A RUIZ"]),
    ("Transactions  Jonathan E Whitfield #3813 Trans Date", ["Jonathan E Whitfield"]),
    ("From account Checking for Jonathan E Whitfield. Zelle payments", ["Jonathan E Whitfield"]),
])
def test_a_person_beside_a_label_is_proposed(text, expected):
    assert [p.name for p in transactions.labelled_people(text)] == expected


@pytest.mark.parametrize("text", [
    "Name Residence Business", "Name\nEmployee ID\nPay Period Begin", "Owner Distribution",
    "Primary Savings Account", "Lender Name Clearpath Lending", "Payee Status Active",
    "Pay to the order of ROCKY MOUNTAIN POWER",
])
def test_a_form_heading_beside_a_label_is_not_a_person(text):
    assert transactions.labelled_people(text) == []


def test_a_set_of_books_is_headed_with_the_business():
    assert [p.name for p in transactions.businesses(
        "Lindquist Family Dairy & Hay\nProfit and Loss\nJanuary 2021")] == ["Lindquist Family Dairy & Hay"]
    assert transactions.businesses("Monthly Report\nProfit and Loss") == []


# ---------------------------------------------------------------------------
# round four: the Word renditions, and what reading them showed
# ---------------------------------------------------------------------------


def test_a_store_number_is_not_a_card_tail():
    text = "SUMMIT HOME #0845 PURCHASE\nTransactions  Jonathan E Whitfield #3813\nWALGREENS #3805 LAYTON"
    assert [p.name for p in transactions.labelled_people(text)] == ["Jonathan E Whitfield"]


def test_a_stub_with_the_employee_between_employer_and_title():
    stub = ("Net Pay   2,667.28     YTD Net 2,667.28\nNew Hope Pacific Islander Church\n"
            "SAMUEL T FONOTI\nEmployee Pay Stub\nPay Period: 01/01/2022 - 01/15/2022")
    assert transactions.pay_stub_employers(stub) == ["New Hope Pacific Islander Church"]


def test_a_letterhead_sharing_its_line_with_the_title():
    stub = ("RED ROCK DENTAL ARTS    EARNINGS STATEMENT\n595 S BLUFF ST, ST GEORGE UT 84770\n"
            "Gross Pay\nNet Pay\nCheck Date")
    assert "RED ROCK DENTAL ARTS" in transactions.pay_stub_employers(stub)


@pytest.mark.parametrize("line", [
    "FEDERAL TAXABLE WAGES: 876.23 YTD NET PAY: 742.03",
    "Jan 03  Ach Debit Verizon Wireless Payments  142.30",
    "*****DIRECT DEPOSIT - VOID - NON-NEGOTIABLE*****",
])
def test_a_stub_heading_or_a_bill_is_not_an_employer(line):
    assert transactions.employers(line) == []


def test_grouped_digits_never_cross_a_line_into_a_figure():
    """"Checking 6323" over "100.00" registered "6323\\n100" as the account."""
    values = [m.text for m in patterns.scan("Transfer From Checking 6323\n100.00")]
    assert values == ["6323"]


def test_a_registered_number_is_never_found_inside_a_figure():
    store, _ = review.build_store([])
    store.add_value("bank_account", "6323 100")
    store.add_value("check_number", "100")
    settings = Settings(use_ner=False, docx_mode="redact")
    text = "Transfer From Checking 6323            100.00\nFee 100.00"
    hits = engine.scan_text(text, store, settings, register=False)
    assert not [h for h in hits if "100" in h.text], hits


def test_a_two_column_letterhead_is_read_down_its_columns(tmp_path):
    """A Word letterhead table reads across: name, INQUIRIES, street, Call..."""
    docx = pytest.importorskip("docx")
    path = tmp_path / "business.docx"
    document = docx.Document()
    table = document.add_table(rows=3, cols=2)
    for r, (left, right) in enumerate([("RED CANYON LANDSCAPING INC", "INQUIRIES"),
                                       ("890 W ROYAL HUNTE DR", "Call: 800-555-0247"),
                                       ("CEDAR CITY UT 84720-7327", "(24 Hours, 7 days a week)")]):
        table.cell(r, 0).text, table.cell(r, 1).text = left, right
    document.save(path)
    found, _ = pipeline.collect_suggestions([path], review.build_store([])[0], Settings(use_ner=False))
    assert ("RED CANYON LANDSCAPING INC", "organization") in [(s.text, s.category) for s in found]


def test_a_heading_over_the_holder_is_not_a_holder():
    assert transactions.account_holders("Monthly Statement\nTyson J Okafor\n435 N 900 E Apt 12") == [
        transactions.Party("Tyson J Okafor", "", "person")]


def test_account_details_between_a_business_and_its_street():
    text = ("Redstone Auto Repair Llc\nAccount ID: SNBKK3GRG1IDY\n"
            "billing@redstoneautorepair.example.com\nBusiness account\n"
            "4417 W Sagebrush Ln, South Jordan, UT 84009")
    assert [p.name for p in transactions.businesses(text)] == ["Redstone Auto Repair Llc"]


def test_no_surrogate_borrows_a_real_first_name():
    """"Marcus Inglewood" standing in for someone else reads as the client
    Marcus Valdez wherever the surname is out of sight."""
    people = ["Marcus A Valdez", "Alyssa M Valdez", "Linda Mitchell", "Jessica Cook",
              "Tamsin Grey", "Quentin Blake", "Celeste Moreau", "Everett Lang"]
    store, _ = review.build_store([(n, "person") for n in people])
    real_first = {n.split()[0].casefold() for n in people}
    for entity in store.entities.values():
        given = {entity.surrogate.first.casefold(), *(m.casefold() for m in entity.surrogate.middles)}
        assert not given & real_first, f"{entity.canonical} -> {entity.replacement}"


# ---------------------------------------------------------------------------
# round five
# ---------------------------------------------------------------------------


def test_a_check_protected_amount_is_money():
    """A pay stub prints its net pay "**832.81"; the mask shape took "**83"."""
    assert patterns.scan("ABIGAIL N OKAFOR  **832.81") == []


@pytest.mark.parametrize("text", [
    "PreApproved Payment Bill User Payment - Kenzie Cook\nID: FDALS2I4D3FTCQP9O",
    "06/16/2021      PreApproved Payment Bill User Payment - Kenzie Cook USD            -125.86",
])
def test_a_wrapped_processor_line_still_names_the_person(text):
    assert [p.name for p in transactions.harvest(text)] == ["Kenzie Cook"]


def test_a_processor_merchant_is_not_a_person():
    text = "User Payment - Uber Technologies USD -13.00\nUser Payment - DoorDash Inc USD -10.00"
    assert transactions.harvest(text) == []


def test_an_iolta_holder_line_names_the_firm():
    text = ("SALT FLATS BANK & TRUST\nRIDGEVIEW FAMILY LAW PLLC IOLTA TRUST ACCOUNT\n"
            "1520 E 3300 S STE 210\nSALT LAKE CITY UT 84106")
    assert [p.name for p in transactions.businesses(text)] == ["RIDGEVIEW FAMILY LAW PLLC"]


def test_a_tenant_pair_sharing_a_surname():
    text = "Monthly rent $2,250.00 Tenant Colby & Jess Morgan Lease end 07/31/2024"
    assert [p.name for p in transactions.labelled_people(text)] == ["Colby Morgan", "Jess Morgan"]


def test_an_issuer_letterhead_over_the_holder_is_not_a_holder():
    """"OQUIRRH MOUNTAIN FCU" became a person, and its middle name "Mountain"
    was then blacked out of "SUNDANCE MOUNTAIN RSRT"."""
    found = transactions.account_holders("OQUIRRH MOUNTAIN FCU\nKYLE D HARRINGTON\n2279 E 5950 S")
    assert [p.name for p in found] == ["KYLE D HARRINGTON"]


@pytest.mark.parametrize("text", [
    "Summit Home #0845 St George $17.37",
    "8307456089557259286   2022-11-22T12:47:13    Merchant Transacti Complete   Sq *Blue Moon Coffee",
])
def test_a_store_number_or_a_time_is_not_a_house_number(text):
    assert not [m for m in patterns.scan(text) if m.category == "street_address"]


def test_a_name_glued_together_by_ocr_is_still_boxed():
    """RapidOCR returned "NATHAN C BLACKBURN" as one token, NATHANCBLACKBURN."""
    store, _ = review.build_store([("Nathan C Blackburn", "person")])
    words = [(50.0, 88.0, 149.0, 97.0, "NATHANCBLACKBURN", 0, 0, 0),
             (160.0, 88.0, 200.0, 97.0, "ACROSS", 0, 0, 1),
             (210.0, 88.0, 260.0, 97.0, "Statement", 0, 0, 2)]
    found = pdf_processor._glued_names(words, store, Settings(use_ner=False))
    assert [hit.text for hit, _rect in found] == ["NATHANCBLACKBURN"]


# ---------------------------------------------------------------------------
# the language model's proposals on a statement
# ---------------------------------------------------------------------------


def test_the_model_proposes_no_merchant_and_no_merged_cells():
    from redactor import ner
    ok, _note = ner.available()
    if not ok:
        pytest.skip("no language model")
    ledger = ("Payment\nComplete\nski pass split\nElizabeth Kimball\nJenna Harrington\n- $24.13\n"
              "Merchant Transacti Complete\nUber *Eats\nJenna Harrington\n- $36.77\n"
              + "\n".join(f"Costco {n}\n${n}.{n:02d}" for n in range(10, 24)))
    found = ner.suggest({"ledger": ledger})
    texts = {s.text for s in found}
    assert "Elizabeth Kimball Jenna Harrington" not in texts
    assert not any("*" in t or "Transacti" in t for t in texts)
    assert pipeline._is_financial(ledger)


def test_a_name_wrapped_inside_its_cell_before_the_transaction_id():
    text = "PreApproved Payment Bill User Payment - Brian\nGomez ID: OYLV0ZP6T2COYZ3M4\nUSD"
    assert [p.name for p in transactions.harvest(text)] == ["Brian Gomez"]


def test_a_payment_apps_name_column_holds_merchants_too():
    table = [["Date", "Type", "Name", "Amount"],
             ["03/01", "PreApproved Payment", "Spotify USA", "-10.99"],
             ["03/02", "PreApproved Payment", "Uber Technologies", "-13.00"],
             ["03/03", "Received", "Allison Edwards", "50.00"]]
    assert [p.name for p in transactions.from_tables([table])] == ["Allison Edwards"]


def test_a_letterhead_typed_on_one_line_with_the_period_label():
    stub = ("Earnings Statement\nCache County School District Period Beginning: 01/01/2021\n"
            "Period Ending: 01/31/2021\n2063 N 1200 E, Logan UT 84341\nGross Pay\nNet Pay\nPay Date")
    assert "Cache County School District" in transactions.pay_stub_employers(stub)


def test_a_title_before_a_payee_is_not_part_of_the_name():
    table = [["ID", "From", "To", "Amount"], ["1", "Coach Tanner Reed", "Leilani Fonoti", "+ $14.76"]]
    assert "Tanner Reed" in [p.name for p in transactions.from_tables([table])]


def test_a_plan_sponsor_whose_name_carries_a_comma():
    line = "Arbor Retirement Services\nSilicon Slopes Analytics, Inc. Profit Sharing Plan Quarterly Participant"
    # proposed without the trailing period, as every employer is; the literal
    # pass still finds it in "…Analytics, Inc. Profit Sharing Plan"
    assert "Silicon Slopes Analytics, Inc" in [p.name for p in transactions.employers(line)]
