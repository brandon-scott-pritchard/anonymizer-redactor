"""Photographed pay stubs and scanned statements: the employer in a letterhead
OCR read without its spaces, the stub's own row labels never taken for one, and
household names OCR glued into a run of other text."""

from pathlib import Path

import pytest

from redactor import engine, pdf_processor, review, transactions
from redactor.engine import Settings

pymupdf = pytest.importorskip("pymupdf")


# ---------------------------------------------------------------------------
# the employer on a photographed pay stub
# ---------------------------------------------------------------------------

def test_a_stub_read_without_its_spaces_still_gives_its_letterhead_employer():
    """OCR of a photo drops the space in small caps: "NETPAY", "CHECKDATE",
    "GROSSEARNINGS". The stub was not recognised as one and its letterhead
    employer shipped."""
    text = ("PINECREST DENTAL GROUP            PAY STATEMENT\n"
            "Employee:    DANARWHITLOCK\n"
            "Pay Date:      12/08/2023\n"
            "GROSSEARNINGS            1,643.87\n"
            "NETPAY            788.85\n")
    assert transactions.pay_stub_employers(text) == ["PINECREST DENTAL GROUP"]
    glued = ("ORCHARDVALEMOTORS            EARNINGSSTATEMENT\n"
             "CHECK   PERIODBEGINPERIOD END   CHECKDATE   NETPAY\n")
    assert transactions.pay_stub_employers(glued) == ["ORCHARDVALEMOTORS"]


def test_the_employees_name_spaced_like_a_column_is_not_the_employer():
    """"DANA R   WHITLOCK" over the employee's street: the surname alone was a
    cell of its own, and was proposed as the employer."""
    text = ("PINECREST DENTAL GROUP\n"
            "DANA R   WHITLOCK\n"
            "1184 N CANYON CREST DR\n"
            "LEHI UT 84043-3317\n"
            "EARNINGS STATEMENT\n"
            "GROSS PAY 1,643.87  NET PAY 1,035.33  CHECK DATE 03/28/25\n")
    assert "WHITLOCK" not in transactions.pay_stub_employers(text)


def test_stub_row_labels_and_the_employees_town_are_never_the_employer():
    """A photographed stub read down its columns put "Social Security Tax" and
    "Sick/PTO" straight over the title, and a column gap cut the town out of
    the employee's street line - each proposed as the employer and boxed."""
    text = ("Pinecrest Dental Group\n"
            "1321 S 1100 E Unit 4,   Salt Lake City,   UT 84105            Employee Pay Stub\n"
            "Social Security Tax\n"
            "Employee Pay Stub\n"
            "-56.63       -172.34\n"
            "Sick/PTO\n"
            "\n"
            "Employee Pay Stub\n"
            "SocialSecurityTax\n"
            "Pay Stub\n"
            "Net Pay 1,086.04   Pay Period: 06/01/2024 - 06/15/2024\n")
    found = transactions.pay_stub_employers(text)
    assert not {"Salt Lake City", "Social Security Tax", "Sick/PTO", "SocialSecurityTax"} & set(found)
    assert transactions._letterhead_name("Salt Lake City") == ""
    assert transactions._letterhead_name("Pinecrest Dental Group") == "Pinecrest Dental Group"
    # a business that says it is one keeps its name, tax word and all
    assert transactions._letterhead_name("Liberty Tax Service LLC") == "Liberty Tax Service LLC"


def test_a_name_never_runs_on_across_a_column_gap():
    """"KAYLA J RUIZ            Current" over the street was proposed as one
    person, boxed across the row and through the figure under it."""
    text = ("DANA R WHITLOCK            Current\n"
            "1321 S 1100 E Unit 4, Salt Lake City, UT 84105\n")
    names = {p.name for p in transactions.account_holders(text)}
    assert names == {"DANA R WHITLOCK"}


# ---------------------------------------------------------------------------
# household names glued into an OCR run
# ---------------------------------------------------------------------------

def _anon():
    return Settings(use_ner=False, pdf_mode="anonymize")


def test_one_small_letter_in_a_line_of_capitals_does_not_make_it_cased():
    """OCR read an I as "i" - "HARRiNGTONENTERPRISES" - and the one small letter
    made the word count as mixed case, so the surname glued to the business
    stayed in the rewritten line."""
    store, _ = review.build_store([("Owen D Whitlock", "person")])
    said = pdf_processor._no_glued_names("IntuPymdSonTrarsler[CARD-2]WHiTLOCKENTERPRISES",
                                         store, _anon())
    assert "whitlock" not in said.casefold() and "ENTERPRISES" in said
    # ordinary mixed-case words that begin with a name stay what they are
    store, _ = review.build_store([("Steven Grant", "person")])
    assert pdf_processor._no_glued_names("Paid Stevenson", store, _anon()) == "Paid Stevenson"


def test_a_whole_name_run_together_at_the_end_of_an_ocr_word_is_covered():
    """A scanned ACH line read as one word ends in the account holder's name,
    initial and all: no boundary after the surname, a letter before it."""
    store, _ = review.build_store([("Owen D Whitlock", "person")])
    word = (100.0, 20.0, 310.0, 30.0, "CardSvcsEpay2408296158391052OWENDWHITLOCK", 1, 1, 1, 90.0)
    found = pdf_processor._glued_names([word], store, _anon())
    assert found
    hit, rect = found[0]
    assert hit.text == "OWENDWHITLOCK"
    assert rect.x1 >= 310.0 and rect.x0 > 100.0 + 210.0 * 0.5
    # a first name and surname that merely appear inside an ordinary word are
    # not a run-together name: too short to count
    store, _ = review.build_store([("Al Ott", "person")])
    word = (100.0, 20.0, 310.0, 30.0, "Ballotted", 1, 1, 1, 90.0)
    assert pdf_processor._glued_names([word], store, _anon()) == []


def test_a_glued_name_beside_a_narrow_box_from_the_other_engine_is_still_covered():
    """One engine boxed "OWENDWHITLOCK" a third of its real width; the other
    read the whole line as one word. The first engine's edit claimed the start
    of the name, the second's cover was dropped as already taken, and the
    surname past the narrow box stayed on the page."""
    s = _anon()
    store, _ = review.build_store([("Owen D Whitlock", "person")])
    matcher = engine.EntityMatcher(store, s)
    first = [(270, 335, 300, 340, "OWENDWHITLOCK", 1, 1, 1, 90.0)]
    second = [(164, 333, 345, 343, "CardSvcsEpay2408296158391052OWENDWHITLOCK", 1, 1, 1, 90.0)]
    edits = pdf_processor._plan_edits([first, second], store, s, matcher, s,
                                      pdf_processor.PdfResult(Path("a"), Path("b")), "x", ocr=True)
    tail = pymupdf.Rect(305, 334, 345, 342)
    covered = sum((tail & r).get_area() for e in edits for r in e.words if r.intersects(tail))
    assert covered >= tail.get_area() * 0.9


def test_a_name_glued_after_a_merchants_digits_takes_only_its_share_of_the_word():
    """A run-together name the household was also proposed as - one token,
    "OwenDWhitlock" - is glued after the descriptor's digits: a join at the
    word's edge. The whole word was covered, and with it every merchant
    description on a scanned statement."""
    s = _anon()
    store, _ = review.build_store([("Owen D Whitlock", "person"), ("OwenDWhitlock", "person")])
    word = (116.0, 619.0, 357.0, 627.0,
            "LittleSproutsAcademyTuition2409059000000000OWENDWHITLOCK", 1, 1, 1, 90.0)
    found = pdf_processor._glued_names([word], store, s)
    assert [hit.text for hit, _r in found] == ["OWENDWHITLOCK"]
    assert found[0][1].x0 > 116.0 + 241.0 * 0.6
    matcher = engine.EntityMatcher(store, s)
    narrow = [(270, 619, 300, 627, "OWENDWHITLOCK", 1, 1, 1, 90.0)]
    edits = pdf_processor._plan_edits([narrow, [word]], store, s, matcher, s,
                                      pdf_processor.PdfResult(Path("a"), Path("b")), "x", ocr=True)
    merchant = pymupdf.Rect(116, 620, 230, 626)
    assert not any(r.intersects(merchant) for e in edits for r in e.words)
    # a short glued word is still taken whole: "NATHANCBLACKBURN", "BLACKBURNLAW"
    store, _ = review.build_store([("Nathan C Blackburn", "person")])
    word = (10.0, 20.0, 110.0, 32.0, "BLACKBURNLAW", 1, 1, 1, 90.0)
    assert [h.text for h, _r in pdf_processor._glued_names([word], store, s)] == ["BLACKBURNLAW"]
