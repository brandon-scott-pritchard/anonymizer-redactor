"""The scorer itself: a leak it cannot see hides behind a good number."""

import pytest

from eval import truth


@pytest.fixture
def secrets():
    found = truth.Secrets()
    truth.collect({"holder": {"primary": "KYLE D HARRINGTON"},
                   "txns": [{"type": "Website Payment", "name": "Ashley Hansen"},
                            {"type": "Website Payment", "name": "Spotify USA"},
                            {"type": "General Payment", "name": "Home Depot"}],
                   "card4": "4053", "account_number": "004049659"}, found, set())
    truth.finalize(found, {"Home Depot"})
    return found


def test_every_payment_app_row_names_a_counterparty(secrets):
    """A wallet calls its rows "Website Payment", not "Sent"; their names
    were never collected, so their leaks were never counted."""
    people = {p.full for p in secrets.people.values()}
    assert "Ashley Hansen" in people
    assert not people & {"Spotify USA", "Home Depot"}


@pytest.mark.parametrize("text, category", [
    ("Visa | Last digits: 4053", "card_tail"),
    ("KYLEHARRINGTON paid", "household_name_glued"),
    ("HarringtonStatement_Jan", "household_name_glued"),
    ("Employer EIN004049659Wages", "account"),
])
def test_the_scorer_sees_what_it_used_to_miss(secrets, text, category):
    assert category in {f.category for f in truth.leaks(text, secrets)}


def test_a_word_that_contains_a_surname_is_not_a_leak():
    found = truth.Secrets()
    truth.collect({"holder": {"primary": "MARK E COOPER"}}, found, set())
    truth.finalize(found)
    assert not truth.leaks("Cooperative Extension office", found)


def test_a_suffix_is_not_a_surname():
    found = truth.Secrets()
    truth.collect({"holder": {"primary": "MARCO A RUIZ JR"}}, found, set())
    truth.finalize(found)
    assert {p.last for p in found.people.values()} == {"RUIZ"}


def test_whole_dollars_and_uncommaed_figures_are_money():
    assert truth.money("Total $1,200 due and 1234.56 and $3,357,353.72") == {
        "1,200": 1, "1234.56": 1, "3,357,353.72": 1}



@pytest.mark.parametrize("name", ["Nintendo of America", "To USD", "From EUR",
                                  "Case resolved in seller favor"])
def test_a_wallet_row_that_names_no_person_is_not_a_counterparty(name):
    found = truth.Secrets()
    truth.collect({"txns": [{"type": "General Payment", "name": name}]}, found, set())
    truth.finalize(found)
    assert not found.people


def _scanned_page(cover=None) -> bytes:
    """A page of pixels with a name and a figure on it, optionally boxed."""
    pymupdf = pytest.importorskip("pymupdf")
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=200)
    page.insert_text((60, 80), "Federal Withholding", fontsize=16)
    page.insert_text((360, 80), "-68.67", fontsize=16)
    page.insert_text((60, 140), "KAYLA J RUIZ", fontsize=16)
    if cover is not None:
        page.draw_rect(pymupdf.Rect(cover), color=(0, 0, 0), fill=(0, 0, 0))
    pix = page.get_pixmap(dpi=150)
    scan = pymupdf.open()
    scan.new_page(width=612, height=200).insert_image(scan[0].rect, stream=pix.tobytes("png"))
    return scan.tobytes()


def test_a_figure_painted_over_on_a_scan_is_counted_and_a_name_box_is_not():
    """OCR before and after counted every misread as a lost figure; the
    pixels under the figure cannot be misread."""
    from eval import corpus_eval
    from redactor import ocr
    if not ocr.usable_engines():
        pytest.skip("no OCR engine")
    before = _scanned_page()
    assert not corpus_eval._figures_covered(before, _scanned_page(cover=(55, 120, 200, 148)))
    lost = corpus_eval._figures_covered(before, _scanned_page(cover=(355, 60, 430, 88)))
    assert sum(lost.values()) == 1
