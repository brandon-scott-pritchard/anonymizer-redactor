"""PDF fixes from code review: sideways text, multi-line text objects, text
over images, OCR name heuristics, per-page counts and accurate warnings."""

import re

import pytest

from redactor import ocr, pdf_processor, pipeline, review
from redactor.engine import Hit, Settings

pymupdf = pytest.importorskip("pymupdf")

HOLDERS = [("Oakley V Ward", "person"), ("3357 3537 2072", "account_number")]
IDENTIFIERS = ("Oakley", "Ward", "3357", "3537", "2072")


def _run(tmp_path, path, mode="anonymize", entries=HOLDERS):
    settings = Settings(use_ner=False, pdf_mode=mode)
    store, _ = review.build_store(list(entries))
    pipeline.prescan([path], store, settings)
    out = tmp_path / ("out-" + mode + "-" + path.name)
    result = pdf_processor.process(path, out, store, settings)
    return out, result, store


def _words(path, page=0):
    with pymupdf.open(path) as doc:
        return doc[page].get_text("words")


def _text(path):
    with pymupdf.open(path) as doc:
        return "\n".join(p.get_text() for p in doc)


def _engine():
    engines = ocr.usable_engines()
    if not engines:
        pytest.skip("no OCR engine")
    return engines[0]


def _ocr(image):
    return " ".join(w.text for w in _engine().read(image))


def _render(path, clip=None, dpi=200):
    from PIL import Image
    with pymupdf.open(path) as doc:
        pm = doc[0].get_pixmap(dpi=dpi, clip=clip, colorspace=pymupdf.csRGB)
        return Image.frombytes("RGB", (pm.width, pm.height), pm.samples)


# ---------------------------------------------------------------------------
# 1 and 3. text set sideways
# ---------------------------------------------------------------------------


def _sideways(tmp_path, rotate):
    path = tmp_path / f"sideways{rotate}.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((60, 80), "Balance 1,234.56 on 01/31/2025", fontsize=12, fontname="helv")
    page.insert_text((300, 400), "Statement for Oakley V Ward acct 3357 3537 2072",
                     fontsize=12, fontname="helv", rotate=rotate)
    doc.save(path)
    doc.close()
    return path


@pytest.mark.parametrize("mode", ["redact", "anonymize"])
@pytest.mark.parametrize("rotate", [90, 270])
def test_sideways_text_leaves_no_fragment_of_an_identifier(tmp_path, rotate, mode):
    out, _result, _store = _run(tmp_path, _sideways(tmp_path, rotate), mode=mode)
    left = {w[4] for w in _words(out)}
    # every word that is not an identifier stays, and nothing else is there:
    # no "O", "y", "d acct 3" or "7 3" off the ends of a trimmed box
    assert left == {"Balance", "1,234.56", "on", "01/31/2025", "Statement", "for", "acct"}


@pytest.mark.parametrize("rotate", [90, 270])
def test_sideways_text_shows_no_identifier_on_the_page(tmp_path, rotate):
    _engine()
    out, _result, _store = _run(tmp_path, _sideways(tmp_path, rotate), mode="redact")
    with pymupdf.open(out) as doc:
        line = [w for w in doc[0].get_text("words") if w[4] == "Statement"][0]
    clip = pymupdf.Rect(line[0] - 10, 60, line[2] + 10, 760)
    image = _render(out, clip).rotate(-rotate, expand=True)
    seen = _ocr(image)
    assert "Statement" in seen                 # the reading works at all
    for value in IDENTIFIERS:
        assert value not in seen


def test_a_sideways_span_carries_its_lines_direction(tmp_path):
    with pymupdf.open(_sideways(tmp_path, 90)) as doc:
        spans = pdf_processor._page_spans(doc[0])
    turned = [s for s in spans if "Statement" in s["text"]]
    level = [s for s in spans if "Balance" in s["text"]]
    assert turned and tuple(turned[0]["dir"]) != (1.0, 0.0)
    assert tuple(level[0]["dir"]) == (1.0, 0.0)


def test_a_sideways_name_is_boxed_not_given_a_level_replacement(tmp_path):
    out, result, store = _run(tmp_path, _sideways(tmp_path, 90), mode="anonymize")
    stand_in = next(e for e in store.persons()).surrogate
    text = _text(out)
    # a level replacement would be drawn across the page from the sideways
    # line, over whatever lies beside it
    assert stand_in.last not in text and stand_in.first not in text
    assert not any("added on top" in w for w in result.warnings)
    with pymupdf.open(out) as doc:
        page = doc[0]
        line = [w for w in page.get_text("words") if w[4] == "Statement"][0]
        # the name and number up the margin are boxed (across the middle of the
        # line, as level text is boxed across the middle of its height)
        pm = page.get_pixmap(dpi=72, clip=pymupdf.Rect(line[0], 150, line[2], line[1]),
                             colorspace=pymupdf.csGRAY)
        assert sum(b < 64 for b in pm.samples) / len(pm.samples) > 0.3


def test_upright_text_keeps_its_middle_band_box():
    rect = pymupdf.Rect(10, 10, 60, 20)
    assert pdf_processor._box(rect, 1.0) == pdf_processor._inset(rect, 1.0)
    down = pdf_processor._box(pymupdf.Rect(10, 10, 20, 60), 1.0, (0.0, -1.0))
    assert down.y0 < 10 and down.y1 > 60 and down.x0 > 10 and down.x1 < 20
    slanted = pdf_processor._box(rect, 1.0, (0.707, -0.707))
    assert slanted.contains(rect)


# ---------------------------------------------------------------------------
# 2. one text object, several lines or cells
# ---------------------------------------------------------------------------


def _one_text_object(tmp_path, stream: bytes, name: str):
    path = tmp_path / name
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_font(fontname="helv")
    xref = doc.get_new_xref()
    doc.update_object(xref, "<<>>")
    doc.update_stream(xref, stream)
    doc.xref_set_key(page.xref, "Contents", f"{xref} 0 R")
    doc.save(path)
    doc.close()
    return path


def _positions(path):
    return {w[4]: (w[0], w[1]) for w in _words(path)}


@pytest.mark.parametrize("stream", [
    # lines moved with T*
    b"BT /helv 10 Tf 12 TL 60 700 Td (Statement period 01/01/2025 to 01/31/2025) Tj T* "
    b"(Account holder: Oakley V Ward) Tj T* (Beginning balance 1,234.56) Tj T* "
    b"(Ending balance 2,345.67) Tj T* (Thank you for banking with us) Tj ET",
    # table cells placed with Td, all in one text object
    b"BT /helv 10 Tf 60 700 Td (12/13/24) Tj 80 0 Td (Zelle to Oakley V Ward) Tj "
    b"260 0 Td (248.75) Tj -340 -14 Td (12/16/24) Tj 80 0 Td (SPROUTS FARMERS MKT) Tj "
    b"260 0 Td (-31.01) Tj ET",
])
def test_a_text_object_of_several_lines_keeps_every_line_in_place(tmp_path, stream):
    path = _one_text_object(tmp_path, stream, "one-object.pdf")
    before = _positions(path)
    out, result, _store = _run(tmp_path, path)
    after = _positions(out)
    assert not result.warnings
    for word, (x, y) in before.items():
        if word in ("Oakley", "V", "Ward"):
            assert word not in after
            continue
        assert word in after, word
        assert abs(after[word][0] - x) < 1.0 and abs(after[word][1] - y) < 1.0, word


# ---------------------------------------------------------------------------
# 4. a page's own text over an image is not anonymized twice
# ---------------------------------------------------------------------------

LINES = ["Zelle to Oakley V Ward on 02/01", "Monthly rent 1,250.00", "Water base rate 22.50"]


def _picture_of(lines):
    src = pymupdf.open()
    page = src.new_page()
    for i, line in enumerate(lines):
        page.insert_text((60, 90 + i * 30), line, fontsize=14, fontname="helv")
    return page.get_pixmap(dpi=200)


@pytest.mark.parametrize("kind", ["searchable scan", "tinted background"])
def test_text_over_an_image_is_anonymized_once(tmp_path, kind):
    _engine()
    if kind == "searchable scan":
        picture, render = _picture_of(LINES), 3      # the picture, and hidden text
    else:
        blank = pymupdf.open().new_page()
        blank.draw_rect(blank.rect, color=None, fill=(0.97, 0.97, 0.9))
        picture, render = blank.get_pixmap(dpi=72), 0
    path = tmp_path / "over-image.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(page.rect, pixmap=picture)
    for i, line in enumerate(LINES):
        page.insert_text((60, 90 + i * 30), line, fontsize=14, fontname="helv",
                         render_mode=render)
    doc.save(path)
    doc.close()
    out, _result, store = _run(tmp_path, path)
    text = _text(out)
    stand_in = next(e for e in store.persons()).surrogate
    for word in ("Zelle", "Monthly", "1,250.00", "22.50"):
        assert text.count(word) == 1, (word, text)
    assert text.count(stand_in.last) == 1
    seen = _ocr(_render(out))
    assert "Oakley" not in seen and "Ward" not in seen
    assert seen.count(stand_in.last) == 1, seen


# ---------------------------------------------------------------------------
# 5. OCR's name heuristics leave other words alone
# ---------------------------------------------------------------------------

PEOPLE = [("Brandon White", "person"), ("Steven Grant", "person"),
          ("Nathan C Blackburn", "person"), ("Anthony Garcia", "person"),
          ("Jill Smith", "person")]


def _ocr_words(tokens):
    return [(10.0 + i * 60, 10.0, 60.0 + i * 60, 20.0, t, 0, 0, i, 95.0)
            for i, t in enumerate(tokens)]


def _found(tokens):
    settings = Settings(use_ner=False, pdf_mode="anonymize")
    store, _ = review.build_store(PEOPLE)
    words = _ocr_words(tokens)
    return {hit.text for hit, _r in [*pdf_processor._glued_names(words, store, settings),
                                     *pdf_processor._ocr_near_misses(words, store, settings)]}


def test_ordinary_words_holding_a_name_are_not_taken_on_a_scan():
    ordinary = ["Write:", "Grand", "Stevenson", "Whitehall", "Granted", "STEVENSON",
                "WHITEHALL", "granted", "Seven", "Smithy", "Garden"]
    assert _found(ordinary) == set()


def test_names_glued_or_misread_on_a_scan_are_still_taken():
    names = ["NATHANCBLACKBURN", "StevenGrant", "fromBlackburn", "BLACKBURNLAW",
             "BLACKBURN.LAW", "Garcias", "Garcla", "BLACKBUM", "Smlth", "Srnith"]
    assert _found(names) == set(names)


def test_a_rewritten_ocr_line_keeps_ordinary_words_beside_a_name():
    settings = Settings(use_ner=False, pdf_mode="anonymize")
    store, _ = review.build_store(PEOPLE)
    said = pdf_processor._no_glued_names("Paid WHITEHALL LLC for WHITE", store, settings)
    assert "WHITEHALL" in said and not said.endswith("WHITE")


# ---------------------------------------------------------------------------
# the rest of a name beside the part OCR read
# ---------------------------------------------------------------------------


def _plan(tokens, entries):
    settings = Settings(use_ner=False, pdf_mode="anonymize")
    anon = Settings(**{**settings.__dict__, "docx_mode": "anonymize"})
    store, _ = review.build_store(entries)
    words = _ocr_words(tokens)
    result = pdf_processor.PdfResult(source=None, output=None)
    edits = pdf_processor._plan_edits([words], store, anon, None, settings, result,
                                      "test", ocr=True)
    taken = {i for i, w in enumerate(words) for e in edits
             for r in e.words if pymupdf.Rect(w[:4]).intersects(r)}
    return [w[4] for i, w in enumerate(words) if i in taken], store


def test_a_misread_surname_beside_a_found_given_name_goes_with_it():
    entries = [("KYLE D HARRINGTON", "person"), ("JENNA R HARRINGTON", "person")]
    taken, _store = _plan(["Online", "Transfer", "to", "Kyle", "D", "Haningion", "Ref",
                           "#ib07920443", "Everyday", "Checking"], entries)
    assert {"Kyle", "D", "Haningion"} <= set(taken)
    assert not {"Online", "Transfer", "Ref", "Everyday", "Checking"} & set(taken)


def test_a_word_beside_a_name_that_is_not_the_name_stays():
    entries = [("KYLE D HARRINGTON", "person")]
    taken, _store = _plan(["Paid", "Kyle", "D", "Hardware", "Supply", "42.10"], entries)
    assert "Hardware" not in taken and "Supply" not in taken


def test_a_misread_surname_on_a_scan_is_gone_from_the_page(tmp_path):
    _engine()
    lines = ["9/8 Online Transfer From Kyle D Haningion Ref 300.00",
             "9/14 Water base rate 22.50"]
    path = tmp_path / "scan.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(page.rect, pixmap=_picture_of(lines))
    doc.save(path)
    doc.close()
    out, _result, _store = _run(tmp_path, path,
                                entries=[("KYLE D HARRINGTON", "person")])
    seen = _ocr(_render(out))
    assert "Kyle" not in seen and "Haning" not in seen
    assert "300.00" in seen and "22.50" in seen


# ---------------------------------------------------------------------------
# 6 and 7. per-page counts and an accurate warning
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("mode", ["redact", "anonymize"])
def test_each_page_counts_its_own_hits(tmp_path, mode):
    path = tmp_path / "two.pdf"
    doc = pymupdf.open()
    for _ in range(2):
        doc.new_page().insert_text((60, 80), "Holder: Oakley V Ward", fontsize=12)
    doc.save(path)
    doc.close()
    _out, result, _store = _run(tmp_path, path, mode=mode)
    assert [page.hits for page in result.pages] == [1, 1]


def test_a_rotated_scan_is_not_reported_as_anonymized(tmp_path):
    _engine()
    path = tmp_path / "rotated-scan.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_image(page.rect, pixmap=_picture_of(LINES))
    page.set_rotation(90)
    doc.save(path)
    doc.close()
    _out, result, _store = _run(tmp_path, path)
    assert not any("anonymized from their OCR" in w for w in result.warnings)
    assert any("rotated scan redacted rather than anonymized" in w for w in result.warnings)


def test_name_tails_ignore_a_hit_that_is_not_a_person():
    store, _ = review.build_store([("3357 3537 2072", "account_number")])
    entity = next(iter(store.entities.values()))
    hit = Hit(0, 4, "3357", entity.category, "[ACCOUNT-1]", entity.key, 50)
    spans = [(0, 4, pymupdf.Rect(0, 0, 20, 10)), (5, 9, pymupdf.Rect(22, 0, 40, 10))]
    assert pdf_processor._name_tails("3357 Ward", spans, [hit], store, Settings()) == []


# ---------------------------------------------------------------------------
# text that runs past the edge of the page
# ---------------------------------------------------------------------------


def _margin_line(tmp_path, rotate):
    # PyMuPDF's default page; turned 90 degrees this line runs off the top
    path = tmp_path / f"margin{rotate}.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((300, 200), "Statement for Oakley V Ward acct 3357 3537 2072",
                     fontsize=11, rotate=rotate)
    doc.save(path)
    doc.close()
    return path


@pytest.mark.parametrize("mode", ["redact", "anonymize"])
@pytest.mark.parametrize("rotate", [90, 270])
def test_no_digit_of_an_account_number_survives_at_the_page_edge(tmp_path, rotate, mode):
    out, _result, _store = _run(tmp_path, _margin_line(tmp_path, rotate), mode=mode,
                                entries=[("Oakley V Ward", "person")])
    with pymupdf.open(out) as doc:
        page = doc[0]
        shown = page.get_text()
        # what a parser that does not clip to the page reads
        drawn = "".join(chr(c[0]) for span in page.get_texttrace() for c in span["chars"])
    for text in (shown, drawn):
        assert not any(ch.isdigit() for ch in text), (rotate, mode, text)
        assert "Oakley" not in text and "Ward" not in text
    assert "Statement for" in shown


@pytest.mark.parametrize("mode", ["redact", "anonymize"])
@pytest.mark.parametrize("rotate", [90, 270])
def test_no_digit_of_an_account_number_is_visible_at_the_page_edge(tmp_path, rotate, mode):
    _engine()
    out, _result, _store = _run(tmp_path, _margin_line(tmp_path, rotate), mode=mode,
                                entries=[("Oakley V Ward", "person")])
    with pymupdf.open(out) as doc:
        line = [w for w in doc[0].get_text("words") if w[4] == "Statement"][0]
        clip = pymupdf.Rect(line[0] - 10, 0, line[2] + 10, doc[0].rect.y1)
    seen = _ocr(_render(out, clip).rotate(-rotate, expand=True))
    assert "Statement" in seen
    # OCR reads the edge of a black box as a stray "2" or "i" now and then; a
    # digit of the number shows as a run of them, or as the half-cut "3"
    assert not re.search(r"\d{2}|\b3\b", seen), seen


@pytest.mark.parametrize("rotation", [0, 90, 180, 270])
def test_text_inside_the_page_is_left_alone_by_the_edge_rule(tmp_path, rotation):
    path = tmp_path / "edge.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((60, 838), "Page 1 of 4  Balance 1,234.56", fontsize=8)
    # "Overhang " ends inside the page; "98765" runs past its right edge
    page.insert_text((595 - 60, 80), "Overhang 98765", fontsize=12)
    page.set_rotation(rotation)            # a turned page keeps the same edges
    doc.save(path)
    doc.close()
    with pymupdf.open(path) as doc:
        page = doc[0]
        assert pdf_processor._remove_off_page_text(page) == len("98765")
        text = page.get_text()
        drawn = "".join(chr(c[0]) for span in page.get_texttrace() for c in span["chars"])
    assert "Page 1 of 4  Balance 1,234.56" in drawn and "Overhang" in text
    assert "98765" not in drawn and "9" not in text.split("Overhang")[1]


def test_a_word_read_wider_by_the_other_engine_is_still_covered():
    """One engine read "Harrington" and a garbled "**+#7039"; the other read
    "Harrington ***7039" as one wide word. The first's edit to the name counted
    the wide word as taken, and the masked digits in it were never covered."""
    from pathlib import Path
    from redactor import engine, review
    from redactor.engine import Settings
    s = Settings(use_ner=False, pdf_mode="anonymize")
    store, _ = review.build_store([("Kyle Harrington", "person")])
    matcher = engine.EntityMatcher(store, s)
    first = [(10, 10, 60, 20, "Paid", 1, 1, 1, 95.0), (64, 10, 140, 20, "Harrington", 1, 1, 2, 95.0),
             (146, 10, 200, 20, "**+#7039", 1, 1, 3, 40.0)]
    second = [(10, 10, 60, 20, "Paid", 1, 1, 1, 95.0), (64, 10, 200, 20, "Harrington ***7039", 1, 1, 2, 95.0)]
    edits = pdf_processor._plan_edits([first, second], store, s, matcher, s,
                                      pdf_processor.PdfResult(Path("a"), Path("b")), "x", ocr=True)
    digits = pymupdf.Rect(146, 10, 200, 20)
    covered = sum((digits & r).get_area() for e in edits for r in e.words if r.intersects(digits))
    assert covered >= digits.get_area() * 0.9


def test_a_surname_glued_after_a_lowercase_word_is_replaced_and_covered():
    """"TransferHARRINGTONENTERPRISES": letters on both sides, but the change
    of case is the join."""
    from redactor import review
    from redactor.engine import Settings
    s = Settings(use_ner=False, pdf_mode="anonymize")
    store, _ = review.build_store([("Kyle D Harrington", "person")])
    said = pdf_processor._no_glued_names("TransferHARRINGTONENTERPRISES", store, s)
    assert "HARRINGTON" not in said and said.startswith("Transfer")
    people = pdf_processor._people_parts(store, s)
    found = pdf_processor._name_inside(10, 10, 300, 20, "TransferHARRINGTONENTERPRISES", people)
    assert found and found[0][0].text == "HARRINGTON"
