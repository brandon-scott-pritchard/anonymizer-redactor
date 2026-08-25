"""OCR: engine selection, and that a scanned page really does get redacted."""

import pymupdf
import pytest

from redactor import ocr, pdf_processor
from redactor.engine import Settings
from redactor.mapping import MappingStore


SCAN_LINES = [
    "IN THE THIRD JUDICIAL DISTRICT COURT",
    "Petitioner Jane Elizabeth Smith, DOB 04/17/1985.",
    "SSN 528-41-9963, phone (801) 555-0184.",
    "Respondent John Michael Smith, Case No. 224900871.",
    "Pursuant to Rule 26, Judge Amber M. Cordova presided.",
]

SCAN_SECRETS = ["528-41-9963", "555-0184", "224900871", "Smith", "1985"]


@pytest.fixture
def scanned_page(tmp_path):
    """A page that is nothing but a picture of text."""
    source = pymupdf.open()
    page = source.new_page()
    for index, line in enumerate(SCAN_LINES):
        page.insert_text((60, 90 + index * 30), line, fontsize=13, fontname="helv")
    pixmap = page.get_pixmap(dpi=200)
    source.close()

    doc = pymupdf.open()
    target = doc.new_page()
    target.insert_image(target.rect, pixmap=pixmap)
    path = tmp_path / "scanned.pdf"
    doc.save(path)
    doc.close()
    return path


def loaded_store():
    store = MappingStore()
    store.add_person("Jane Elizabeth Smith")
    store.add_person("John Michael Smith")
    return store


def test_an_engine_is_always_available():
    """RapidOCR is a pip dependency, so OCR should never be entirely missing."""
    ok, note = ocr.describe()
    assert ok, note


def test_the_bundled_tesseract_is_preferred_when_present():
    bundled = ocr.bundled_tesseract()
    if bundled is None:
        pytest.skip("Tesseract has not been vendored on this machine")
    binary, tessdata = bundled
    assert binary.exists()
    assert (tessdata / "eng.traineddata").exists()
    engine, note = ocr.select_engine()
    assert engine.name == "Tesseract"
    assert "bundled" in note


@pytest.mark.parametrize("engine_name", ["Tesseract", "RapidOCR"])
def test_each_engine_redacts_a_scanned_page(scanned_page, tmp_path, monkeypatch, engine_name):
    candidates = [ocr.TesseractEngine(), ocr.RapidOcrEngine()]
    chosen = [e for e in candidates if e.name == engine_name]
    monkeypatch.setattr(ocr, "_ENGINES", chosen)

    available, note = ocr.describe()
    if not available:
        pytest.skip(f"{engine_name} unavailable: {note}")

    out = tmp_path / f"out-{engine_name}.pdf"
    result = pdf_processor.process(scanned_page, out, loaded_store(),
                                   Settings(ocr_scanned_pdfs=True))
    assert not result.refused
    assert result.pages[0].ocr_used
    assert result.pages[0].boxes > 0

    # read the finished page back through OCR - a black box must leave nothing
    with pymupdf.open(out) as doc:
        words = pdf_processor._ocr_words(doc[0])
    recovered = " ".join(w[4] for w in words)
    leaked = [s for s in SCAN_SECRETS if s in recovered]
    assert leaked == [], f"{engine_name} left: {leaked}"


def test_a_scan_is_refused_when_every_engine_is_disabled(scanned_page, tmp_path, monkeypatch):
    monkeypatch.setattr(ocr, "_ENGINES", [])
    out = tmp_path / "out.pdf"
    result = pdf_processor.process(scanned_page, out, loaded_store(),
                                   Settings(ocr_scanned_pdfs=True))
    assert result.refused
    assert not out.exists()


def test_ocr_boxes_get_extra_padding():
    """OCR boxes are approximate; too wide costs nothing, too narrow leaks."""
    assert pdf_processor.OCR_BOX_PADDING > pdf_processor.BOX_PADDING
