"""Dollar figures painted over on scans by boxes meant for something else.

Each case comes from a scanned statement in the corpus run, where the pixels
under a figure were compared before and after. Every name and number here is
invented.
"""

import pytest

pymupdf = pytest.importorskip("pymupdf")

from redactor import patterns, pdf_processor as pp  # noqa: E402
from redactor.engine import Hit  # noqa: E402


def _hit(start, end, text="x"):
    return Hit(start, end, text, "check_number", "[X]", "k", 90)


def test_a_reference_glued_into_a_row_is_boxed_alone():
    """One OCR "word" 320 points wide held the date, a 16-digit reference and
    the balance; the reference's box was the whole run."""
    word = "06/12Deposit9930947247967000Ref37,924.77"
    rect = pymupdf.Rect(100, 200, 420, 210)
    start = word.index("9930")
    box, = pp._rects_for_hit(_hit(start, start + 16), [(0, len(word), rect)],
                             pp.OCR_BOX_PADDING, ocr=True)
    figure_x0 = 100 + 320 * word.index("37,924.77") / len(word)
    assert box.x1 <= figure_x0 + 1, "the balance at the end of the run was covered"
    assert box.width < 200


def test_a_hit_that_is_most_of_a_word_still_takes_the_word():
    word = "4417220193,"
    rect = pymupdf.Rect(100, 200, 160, 210)
    box, = pp._rects_for_hit(_hit(0, 10), [(0, len(word), rect)], pp.OCR_BOX_PADDING, ocr=True)
    assert box.x0 < 100 and box.x1 > 160


@pytest.mark.parametrize("height", [6.0, 9.0, 14.0, 30.0])
def test_small_type_is_padded_less_up_and_down(height):
    """A town on a 6-point merchant line took the tops and bottoms off the
    balances on the rows either side."""
    rect = pymupdf.Rect(100, 200, 140, 200 + height)
    padded = pp._pad_ocr(rect)
    assert padded.x0 == 100 - pp.OCR_BOX_PADDING
    assert rect.y0 - padded.y0 <= max(height * pp.OCR_PAD_SHARE, 0) + 1e-9
    assert rect.y0 - padded.y0 <= pp.OCR_BOX_PADDING
    back = pp._unpad_ocr(padded)
    assert all(abs(a - b) < 1e-6 for a, b in zip(back, rect))


@pytest.mark.parametrize("line", [
    "Savings account S5.816.65",         # "$" read as S, a comma as a period
    "Account number s0.00",
    "Loan Number DueBy02/01/2020",       # a label glued to a date
    "Loan 06/13",
    "Parcel 0.21ac",                     # an area, not the parcel
    "12CarrefourMarketParisFrCard22",    # descriptors run together, not a wallet
    "311AembershipRewardsPoints",
])
def test_a_figure_a_date_or_words_is_not_an_identifier(line):
    assert patterns.scan(line) == []


@pytest.mark.parametrize("line, value", [
    ("BTC 1BoatSLRHtKNngkdXEeobR76b53LETtpyT", "1BoatSLRHtKNngkdXEeobR76b53LETtpyT"),
    ("BTC 1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa", "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"),
    ("Loan number 4417220193", "4417220193"),
    ("Parcel No. 08-123-0045", "08-123-0045"),
    ("Account 12-34-5678", "12-34-5678"),
])
def test_real_identifiers_are_still_taken(line, value):
    assert value in {m.text for m in patterns.scan(line)}


def test_an_address_line_ocr_ran_together_is_still_an_address_block():
    """Boxed only by luck while a hit's box took whole OCR words; once boxes
    shrank to the hit, the employer's street shipped."""
    words = [(35, 40, 150, 50, "RED ROCK DENTAL ARTS", 0, 0, 0),
             (35, 58, 170, 69, "595SBLUFFST.STGEORGEUT84770", 0, 1, 0)]
    blocks = pp.address_blocks(words, ocr=True)
    assert blocks and blocks[0].contains(pymupdf.Rect(35, 58, 170, 69))
    # a text layer keeps its spaces; an ID at a line's end is not a ZIP there
    assert not pp.address_blocks([(35, 58, 170, 69, "REF CA84770", 0, 0, 0)], ocr=False)


def test_a_town_glued_to_its_state_still_ends_an_address():
    words = [(43, 72, 136, 83, "DANIELRHOLBROOK", 0, 0, 0),
             (43, 95, 74, 107, "1184N", 0, 1, 0), (80, 95, 159, 107, "CANYONCRESTDR", 0, 1, 1),
             (43, 107, 75, 119, "LEHIUT", 0, 2, 0), (80, 107, 132, 119, "84043-3317", 0, 2, 1)]
    blocks = pp.address_blocks(words, ocr=True)
    assert blocks and blocks[0].contains(pymupdf.Rect(43, 72, 159, 119))


@pytest.mark.parametrize("line, value", [
    ("Cardaccountxxoxxxxxxxxxx3957", "3957"),          # mask glued to its label
    ("Subtotalforcardaccountxxxxxxxxxxxx3957", "3957"),
    ("Visa*.8779", "8779"),                            # stars read as a period
    ("Visa·.8779", "8779"),
])
def test_a_card_tail_ocr_misread_on_a_photo_is_taken(line, value):
    assert value in {m.text for m in patterns.scan(line)}


@pytest.mark.parametrize("line", ["EXXON 1234", "boxxxxx12", "Visa 1.25"])
def test_ordinary_text_is_not_a_mask(line):
    assert patterns.scan(line) == []
