"""Scanned pages read ahead in worker processes: the same words, sooner."""

import io

import pytest

pymupdf = pytest.importorskip("pymupdf")

from redactor import ocr, ocr_pool, pdf_processor  # noqa: E402


@pytest.fixture
def pool(monkeypatch):
    if not ocr.usable_engines():
        pytest.skip("no OCR engine")
    monkeypatch.setenv("REDACTOR_OCR_WORKERS", "2")
    for name, value in (("_pool", None), ("_broken", False), ("_pending", {}), ("_surveys", {})):
        monkeypatch.setattr(ocr_pool, name, value)
    pdf_processor._ocr_cache.clear()
    yield
    if ocr_pool._pool is not None:
        ocr_pool._pool.shutdown(wait=True, cancel_futures=True)
    pdf_processor._ocr_cache.clear()


def _scan(path, pages):
    """A PDF of pictures of text - no text layer, as a scanner makes."""
    text = pymupdf.open()
    for number in range(pages):
        text.new_page().insert_text((72, 100), f"Kyle D Harrington statement page {number + 1}",
                                    fontsize=16)
    scan = pymupdf.open()
    for page in text:
        picture = page.get_pixmap(dpi=150).tobytes("png")
        scan.new_page().insert_image(scan[-1].rect if len(scan) else pymupdf.Rect(0, 0, 612, 792),
                                     stream=io.BytesIO(picture))
    scan.save(path)
    return path


def _in_line(path):
    pdf_processor._ocr_cache.clear()
    with pymupdf.open(path) as doc:
        return [pdf_processor._page_words(page, path, index)[0] for index, page in enumerate(doc)]


def test_pages_read_ahead_are_the_pages_read_in_line(pool, tmp_path):
    path = _scan(tmp_path / "scan.pdf", 3)
    expected = _in_line(path)
    pdf_processor._ocr_cache.clear()
    seen = []
    ocr_pool.read_ahead([path], on_page=lambda p, i: seen.append(i))
    assert sorted(seen) == [0, 1, 2]
    names = [engine.name for engine in ocr.usable_engines()]
    for index in range(3):
        base = pdf_processor._ocr_key(path, index)
        assert pdf_processor.ocr_cached(base, names)
    # what every step then reads comes out of the cache, word for word
    with pymupdf.open(path) as doc:
        got = [pdf_processor._page_words(page, path, index)[0] for index, page in enumerate(doc)]
    assert got == expected
    assert any("HARRINGTON" in w[4].upper() for words in got[0] for w in words)


def test_reading_stops_at_the_pages_a_step_needs(pool, tmp_path):
    path = _scan(tmp_path / "scan.pdf", 4)
    seen = []
    ocr_pool.read_ahead([path], per_file=2, on_page=lambda p, i: seen.append(i))
    assert sorted(seen) == [0, 1]
    names = [engine.name for engine in ocr.usable_engines()]
    assert not pdf_processor.ocr_cached(pdf_processor._ocr_key(path, 3), names)


def test_a_page_read_in_the_background_is_not_read_twice(pool, tmp_path):
    path = _scan(tmp_path / "scan.pdf", 2)
    ocr_pool.read_ahead([path], wait=False)
    in_flight = dict(ocr_pool._pending)
    seen = []
    ocr_pool.read_ahead([path], on_page=lambda p, i: seen.append(i))
    # the step waited on the readings already under way rather than starting its own
    assert set(in_flight) <= {pdf_processor._ocr_key(path, i) for i in range(2)}
    names = [engine.name for engine in ocr.usable_engines()]
    assert all(pdf_processor.ocr_cached(pdf_processor._ocr_key(path, i), names) for i in range(2))


def test_turned_off_it_reads_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("REDACTOR_OCR_WORKERS", "0")
    monkeypatch.setattr(ocr_pool, "_pool", None)
    seen = []
    ocr_pool.read_ahead([tmp_path / "missing.pdf"], on_page=lambda p, i: seen.append(i))
    assert seen == [] and ocr_pool._pool is None


def test_text_pages_are_not_sent_for_reading(pool, tmp_path):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "A typed page with a perfectly good text layer on it.")
    path = tmp_path / "typed.pdf"
    doc.save(path)
    seen = []
    ocr_pool.read_ahead([path], on_page=lambda p, i: seen.append(i))
    assert seen == [] and not ocr_pool._pending
