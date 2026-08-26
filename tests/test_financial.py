"""Financial exhibits: statements, pay stubs, tax forms, the declaration itself.

The tool was built against pleadings. Run against the other half of a family law
file - the documents a Financial Declaration is built from - it leaked 28% of
the identifiers planted in a generated corpus and destroyed 23 dollar figures.

Both halves of that matter, and the second one more. A declaration whose numbers
have been altered is not a redacted document; it is a false statement filed
under penalty. So every test here that asserts an identifier is gone also
asserts the money beside it is untouched.

Every document in this file is invented.
"""

import re

import pytest

from redactor import (categories, docx_processor, engine, mapping, patterns,
                      pipeline, review, transactions)

docx = pytest.importorskip("docx")


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def build(tmp_path, name, paragraphs=(), tables=()):
    """A DOCX with the given paragraphs and tables (each a list of rows)."""
    path = tmp_path / name
    document = docx.Document()
    for line in paragraphs:
        document.add_paragraph(line)
    for rows in tables:
        table = document.add_table(rows=len(rows), cols=max(len(r) for r in rows))
        for r, row in enumerate(rows):
            for c, cell in enumerate(row):
                table.cell(r, c).text = cell
    document.save(path)
    return path


def run(path, out, entries=()):
    """Prescan and process, the way the real run does."""
    settings = engine.Settings()
    store, _ = review.build_store(list(entries))
    pipeline.prescan([path], store, settings)
    docx_processor.process(path, out, store, settings)
    return "\n".join(docx_processor.extract_text(out).values()), store


def scanned(text):
    return [(m.text, m.category) for m in patterns.scan(text)]


# ---------------------------------------------------------------------------
# money is never an identifier
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("line", [
    "Savings account 842.16 was moved to the vacation sub-account.",
    "Certificate of deposit 950.00 penalty applies.",
    "Mortgage 985.00 late fee applies.",
    "Escrow 412.55 collected monthly.",
    "Line of credit 985.00 minimum payment.",
    "Pension 985.00 monthly if retiring at 62.",
    "Roth IRA 640.00",
    "Retirement account 640.00 minimum distribution.",
    # the label that broke the first attempt: "visa" is also an immigration
    # document, so a debt schedule's "Summit Ridge Visa … 212.00" was read as
    # an alien registration number
    "Summit Ridge Visa: 212.00",
    "Monthly payment: 212.00",
])
def test_a_dollar_figure_after_a_financial_label_is_left_alone(line):
    assert scanned(line) == []


@pytest.mark.parametrize("line, survives", [
    # the bare 457 and 529 labels carried no word boundary, so they matched
    # inside an amount and sliced digits out of the middle of it
    ("Check 2203 03/24 4571.30", "4571.30"),
    ("Total 5290.44 collected", "5290.44"),
    ("Balance 84579.20", "84579.20"),
])
def test_a_numeric_plan_label_never_slices_an_amount(line, survives):
    for text, _ in scanned(line):
        assert text not in survives, f"{text!r} was cut out of {survives!r}"


def test_a_wrong_money_registration_cannot_spread_to_other_documents(tmp_path):
    """One bad registration used to destroy that figure in every later file.

    ``find_values`` matches a registered literal everywhere, so "Escrow 412.55"
    in a mortgage statement took out the pay stub's federal withholding and
    three insurance rows in a profit and loss - documents where no detector
    fired at all. This is the regression, and no single-document test can catch
    it.
    """
    one = build(tmp_path, "mortgage.docx", ["Escrow 412.55 collected monthly."])
    two = build(tmp_path, "paystub.docx",
                tables=[[["Federal income tax", "412.55"], ["Medicare", "88.10"]]])
    settings = engine.Settings()
    store = mapping.MappingStore()
    pipeline.prescan([one, two], store, settings)
    for source, name in ((one, "out1.docx"), (two, "out2.docx")):
        docx_processor.process(source, tmp_path / name, store, settings)
        text = "\n".join(docx_processor.extract_text(tmp_path / name).values())
        assert "412.55" in text, f"{name} lost the figure"


# ---------------------------------------------------------------------------
# a table splits the label from the value
# ---------------------------------------------------------------------------


ASSET_TABLE = [
    ["Description", "Account / identifying number", "Value", "Titled to"],
    ["Checking - Summit Ridge Bank", "000488213907", "2,884.71", "Petitioner"],
    ["Savings account", "000488227145", "842.16", "Joint"],
    ["Certificate of deposit", "44-8821-0033", "950.00", "Joint"],
]


def test_an_asset_schedule_loses_its_account_numbers_and_keeps_its_figures(tmp_path):
    """The single highest-value test here.

    A table cell is its own paragraph, so the scanner is handed 'Savings
    account' and '000488227145' as two separate strings and no labelled
    detector can see a pair. Every account number in a real declaration's asset
    and debt tables survived, while the dollar column was redacted instead -
    delivered, verbatim, from the audit:

        ['Certificate of deposit', '44-8821-0033', '[ACCOUNT-8]', 'Joint']
    """
    path = build(tmp_path, "declaration.docx", ["FINANCIAL DECLARATION"],
                 tables=[ASSET_TABLE])
    text, _ = run(path, tmp_path / "out.docx")
    for account in ("000488213907", "000488227145", "44-8821-0033"):
        assert account not in text, f"{account} shipped"
    for money in ("2,884.71", "842.16", "950.00"):
        assert money in text, f"{money} was destroyed"


def test_a_form_grid_labels_the_cell_above_not_the_first_row(tmp_path):
    """An IRS form alternates label rows and value rows.

    Row 0 of a 1099 says "RECIPIENT'S TIN"; the label for the account number is
    in the row immediately above it, not in the header. Reading only row 0
    shipped the account number.
    """
    grid = [
        ["PAYER'S name", "PAYER'S TIN", "RECIPIENT'S TIN", "1 Nonemployee compensation"],
        ["Harrowgate Cabinet Supply", "84-7712204", "602-38-1147", "12,450.00"],
        ["RECIPIENT'S name", "", "Account number (see instructions)", "4 Federal tax withheld"],
        ["Dorian Whitlock", "", "HCS-0044-2211", "0.00"],
    ]
    path = build(tmp_path, "1099.docx", tables=[grid])
    text, _ = run(path, tmp_path / "out.docx")
    assert "HCS-0044-2211" not in text
    assert "12,450.00" in text and "0.00" in text


def test_a_w2_box_grid_loses_the_social_security_number(tmp_path):
    """A W-2 is a table, and nobody thinks of it as one."""
    grid = [["a Employee's social security number", "b Employer ID number"],
            ["514-72-8830", "84-7712204"]]
    path = build(tmp_path, "w2.docx", tables=[grid])
    text, _ = run(path, tmp_path / "out.docx")
    assert "514-72-8830" not in text and "84-7712204" not in text


def test_a_reconstructed_row_never_becomes_a_hit_of_its_own(tmp_path):
    """Table pairs register; they never apply.

    The strings the table pass builds - a cell joined to its column heading -
    appear nowhere on the page. Joining a whole row with spaces let the
    bare-digits card detector run from the account column into the money
    column and register "44-8821-0033 950" as a credit card, which is why the
    pairs are joined with a colon instead.
    """
    path = build(tmp_path, "assets.docx", tables=[ASSET_TABLE])
    store = mapping.MappingStore()
    pipeline.register_table_context([path], store, engine.Settings())
    for entity in store.entities.values():
        assert not re.search(r"\d\s+\d", entity.canonical), \
            f"{entity.canonical!r} spans two cells"
        assert entity.category != "credit_card" or "-" in entity.canonical


# ---------------------------------------------------------------------------
# the forms an account number is written in
# ---------------------------------------------------------------------------


def test_a_space_grouped_account_number_is_taken_whole(tmp_path):
    """Only the first group used to be captured.

    "Account Number: [ACCOUNT-14] 4477 9301" ships two thirds of the number
    while reading as redacted, which is the worst possible failure mode: it
    will not be caught on review.
    """
    path = build(tmp_path, "utility.docx", ["Account Number: 8102 4477 9301"])
    text, _ = run(path, tmp_path / "out.docx")
    assert "4477" not in text and "9301" not in text


@pytest.mark.parametrize("line, tail", [
    ("Acct #: ...3907", "3907"),
    ("The statement prints ****3907 for the same account.", "3907"),
    ("Charged to XXXX-XXXX-XXXX-4417 last month.", "4417"),
    ("CARD PURCHASE XXXX9302 HOME SUPPLY", "9302"),
    ("Card ending in 4417", "4417"),
    ("card ending in 9302", "9302"),
])
def test_every_masked_form_of_an_account_is_caught(tmp_path, line, tail):
    path = build(tmp_path, f"masked-{tail}-{abs(hash(line)) % 999}.docx", [line])
    text, _ = run(path, tmp_path / f"out-{tail}-{abs(hash(line)) % 999}.docx")
    assert tail not in text, f"{line!r} shipped its tail"


@pytest.mark.parametrize("line", ["card 1 of 2", "card holder name", "State ID card 12345678"])
def test_the_bare_card_label_does_not_fire_on_prose(line):
    assert not any(c == "credit_card" for _, c in scanned(line))


# ---------------------------------------------------------------------------
# labels the vocabulary was missing
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("line, value, category", [
    ("Employee No.: PN-2299018", "PN-2299018", "employee_id"),
    ("Share Certificate (CD) number 44-8821-0033", "44-8821-0033", "bank_account"),
    ("Account number (see instructions)  HCS-0044-2211", "HCS-0044-2211", "bank_account"),
    ("Plan number: 004-88213", "004-88213", "investment_account"),
    ("Meter number 44719023", "44719023", "bank_account"),
    ("PTIN P00448219", "P00448219", "ein"),
    ("CRD 4471902", "4471902", "professional_license"),
    ("Employer's state ID number: UT-4410932-001", "UT-4410932-001", "ein"),
    ("Sales tax license 12447190-002-STC.", "12447190-002-STC", "professional_license"),
    ("Student ID at school of record: SU-4471902", "SU-4471902", "student_id"),
    ("VENMO PAYMENT 3948217364", "3948217364", "check_number"),
    # the ABA checksum function existed and was referenced nowhere, so a MICR
    # line and a bare wire instruction both shipped
    ("MICR |:124302150|: 000771226613", "124302150", "routing_number"),
])
def test_the_label_vocabulary_covers_the_financial_forms(line, value, category):
    assert (value, category) in scanned(line)


def test_a_state_id_label_no_longer_requires_the_letters_car():
    """"card?" parsed as "car" plus an optional "d"."""
    assert ("12345678", "drivers_license") in scanned("State ID card 12345678")
    assert ("UT-4410932-001", "ein") in scanned("State ID number: UT-4410932-001")


@pytest.mark.parametrize("line", [
    "Transaction 04/12/2026 posted",
    "Invoice 03/2026 for services",
    "Reference number 03/15/2026",
    # "action" matched inside Trans*action*
    "Satisfaction 04-2026 recorded",
])
def test_a_date_is_never_a_cheque_or_case_number(line):
    assert not any(c in {"check_number", "case_number", "case_designator"}
                   for _, c in scanned(line))


def test_the_word_payment_is_not_a_venmo_handle():
    assert not any(t.lower() == "payment" for t, _ in scanned("VENMO PAYMENT 3948217364"))


# ---------------------------------------------------------------------------
# people named in transaction lines
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("line, expected", [
    ("ZELLE TO SHAWNA KIRKENDALL", ["SHAWNA KIRKENDALL"]),
    ("CHECK 1042 TO D. TORVIK", ["D. TORVIK"]),
    ("ACH PMT FROM DOUGLAS VANDENBROOK", ["DOUGLAS VANDENBROOK"]),
    ("PAYPAL *HOLLENBECK", ["HOLLENBECK"]),
    ("Venmo payment to Carmen Lattimore", ["Carmen Lattimore"]),
])
def test_a_counterparty_is_proposed_from_a_transaction_line(line, expected):
    assert sorted(p.name for p in transactions.harvest(line)) == sorted(expected)


@pytest.mark.parametrize("line", [
    # every one of these is a real false positive the patterns produce without
    # their guards, and the guards matter more than the patterns do
    "TRANSFER TO SAVINGS XXXXXXXX7145",
    "WIRE TO BANK OF THE WEST",
    "PAYMENT TO CREDIT UNION",
    "TRANSFER TO CHECKING 000488213907",
    "AMAZON MKTPL",
    "DIRECT DEP CASCADIA FREIGHT LOGISTICS PAYROLL",
    "CHECK 1042 TO ATM",
])
def test_an_account_or_an_institution_is_not_proposed_as_a_person(line):
    assert transactions.harvest(line) == []


def test_a_payee_column_yields_people_and_not_companies(tmp_path):
    """A cheque register puts the payee in a column, not in a descriptor."""
    register = [
        ["No.", "Date", "Payee / Description", "Payment"],
        ["1040", "03/01/2026", "Cascade Valley Power", "214.88"],
        ["1041", "03/03/2026", "Brookhollow School District - lunch", "60.00"],
        ["1042", "03/07/2026", "D. Torvik - March rent", "1,850.00"],
        ["1043", "03/12/2026", "Shawna Kirkendall - childcare", "450.00"],
        ["1044", "03/19/2026", "Dr. Prentice Ambrose DDS", "180.00"],
    ]
    path = build(tmp_path, "register.docx", tables=[register])
    tables = list(docx_processor.iter_tables(path))
    found = sorted(p.name for p in transactions.from_tables(tables))
    assert found == ["D. Torvik", "Prentice Ambrose", "Shawna Kirkendall"]


def test_a_counterparty_is_proposed_as_a_person_not_a_placeholder(tmp_path):
    """"ZELLE TO [NAME-4]" reads as a redaction; a name reads as a statement."""
    path = build(tmp_path, "stmt.docx", ["03/11  ZELLE TO SHAWNA KIRKENDALL  250.00"])
    found, _ = pipeline.collect_suggestions(
        [path], mapping.MappingStore(), engine.Settings(use_ner=False))
    match = [s for s in found if s.text == "SHAWNA KIRKENDALL"]
    assert match, f"not proposed; got {[s.text for s in found]}"
    assert match[0].category == "person"
    assert categories.style_for(match[0].category) == "person"


# ---------------------------------------------------------------------------
# table structure
# ---------------------------------------------------------------------------


def test_a_nested_table_is_read_once_and_rows_keep_their_width(tmp_path):
    """``iter`` is a descendant axis, so a nested table was read three times.

    A two-column table came back with a six-cell row, and two paragraphs in one
    cell were concatenated with no separator at all - the same fusing bug the
    tab handling exists to prevent, one level up.
    """
    path = tmp_path / "nested.docx"
    document = docx.Document()
    outer = document.add_table(rows=2, cols=2)
    outer.cell(0, 0).text = "Institution"
    outer.cell(0, 1).text = "Detail"
    outer.cell(1, 0).text = "Summit Ridge Bank"
    outer.cell(1, 0).add_paragraph("Member since 2019")
    inner = outer.cell(1, 1).add_table(rows=2, cols=2)
    inner.cell(0, 0).text = "Account No."
    inner.cell(0, 1).text = "000488213907"
    inner.cell(1, 0).text = "Routing"
    inner.cell(1, 1).text = "124302150"
    document.save(path)

    tables = list(docx_processor.iter_tables(path))
    assert len(tables) == 2, "the nested table was read more than once"
    assert all(len(row) == 2 for rows in tables for row in rows)
    assert tables[0][1][0] == "Summit Ridge Bank Member since 2019"
    assert ["Account No.", "000488213907"] in tables[1]


def test_the_bench_and_the_figures_both_survive(tmp_path):
    """The two things that must never change, in one document."""
    path = build(
        tmp_path, "decree.docx",
        ["IN THE FOURTH JUDICIAL DISTRICT COURT, UTAH COUNTY, STATE OF UTAH",
         "Judge Harriet L. Ostrowski",
         "Commissioner Blaine T. Sudweeks",
         "Petitioner Dorian Whitlock resides at 812 Kestrel Bend, Orem, UT 84057."],
        tables=[[["Income source", "Monthly"],
                 ["Wages", "6,412.88"],
                 ["Rental income", "985.00"],
                 ["Interest", "12.40"]]])
    settings = engine.Settings()
    officials = pipeline.collect_officials([path])
    settings.protected_names = [o.name for o in officials]
    store, _ = review.build_store([("Dorian Whitlock", "person")])
    pipeline.prescan([path], store, settings)
    docx_processor.process(path, tmp_path / "out.docx", store, settings)
    text = "\n".join(docx_processor.extract_text(tmp_path / "out.docx").values())

    assert "Harriet L. Ostrowski" in text
    assert "Blaine T. Sudweeks" in text
    for money in ("6,412.88", "985.00", "12.40"):
        assert money in text, f"{money} was destroyed"
    assert "Dorian Whitlock" not in text
    assert "812 Kestrel Bend" not in text


@pytest.mark.parametrize("line", ["812 KESTREL BEND", "2200 Foundry Row",
                                  "44 Copper Creek Crossing", "9 Marsh Point"])
def test_a_subdivision_street_suffix_is_still_a_street(line):
    """Bend, Row, Crossing and Point name streets as readily as Street does."""
    assert any(c == "street_address" for _, c in scanned(line))
