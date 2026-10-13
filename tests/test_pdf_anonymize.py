"""PDF anonymize mode: the replacement written where the original was.

Redaction stays the default. In anonymize mode the original glyphs are still
physically removed - nothing here may weaken that - and the replacement is
drawn in their place, in the page's own text order, so a language model reading
the delivered file sees a statement, not a statement with holes in it.
"""

import pytest

from redactor import docx_processor, pdf_processor, pipeline, review
from redactor.engine import Settings

pymupdf = pytest.importorskip("pymupdf")

HOLDERS = [("Oakley V Ward", "person"), ("Jacob N Ward", "person")]

# every cell its own text call, as statement generators write them
STATEMENT = [
    (60, 80, "OAKLEY V WARD"), (60, 92, "JACOB N WARD"),
    (60, 104, "12469 W CENTER ST"), (60, 116, "LAYTON UT 84042-3715"),
    (360, 140, "Account number:"), (440, 140, "3357 3537 2072"),
    (60, 170, "Date"), (120, 170, "Description"), (520, 170, "Amount"),
    (60, 182, "12/13/24"), (120, 182, "VENMO DES:CASHOUT INDN:OAKLEY V WARD CO ID:5264681992"),
    (520, 182, "248.75"),
    (60, 194, "12/16/24"), (120, 194, "CHECKCARD 1216 SPROUTS FARMERS MKT #6994"),
    (520, 194, "-31.01"),
]


def _pdf(tmp_path, cells, name="statement.pdf", shade=False):
    path = tmp_path / name
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    if shade:
        page.draw_rect(pymupdf.Rect(50, 172, 560, 198), color=None, fill=(0.9, 0.9, 0.9))
    for x, y, text in cells:
        try:
            text.encode("cp1252")
            page.insert_text((x, y), text, fontsize=8, fontname="helv")
        except UnicodeEncodeError:             # "■" needs a Unicode face to exist
            writer = pymupdf.TextWriter(page.rect)
            writer.append((x, y), text, font=pymupdf.Font("cjk"), fontsize=8)
            writer.write_text(page)
    doc.save(path)
    doc.close()
    return path


def _run(tmp_path, path, mode="anonymize", entries=HOLDERS):
    settings = Settings(use_ner=False, pdf_mode=mode)
    store, _ = review.build_store(list(entries))
    pipeline.prescan([path], store, settings)
    out = tmp_path / ("out-" + mode + "-" + path.name)
    result = pdf_processor.process(path, out, store, settings)
    return out, result, store


def _text(path, sort=False):
    with pymupdf.open(path) as doc:
        return "\n".join(page.get_text(sort=sort) for page in doc)


def test_the_originals_are_gone_and_the_replacements_are_there(tmp_path):
    out, result, store = _run(tmp_path, _pdf(tmp_path, STATEMENT))
    text = _text(out)
    for gone in ("OAKLEY", "WARD", "12469", "CENTER", "84042", "3357", "2072"):
        assert gone not in text, f"{gone} survived"
    oakley = next(e for e in store.persons() if e.canonical.lower().startswith("oakley"))
    assert oakley.surrogate.last.upper() in text
    assert "[ACCOUNT-1]" in text
    # figures, dates and merchants untouched
    for kept in ("248.75", "-31.01", "12/13/24", "SPROUTS FARMERS MKT #6994", "CHECKCARD 1216"):
        assert kept in text, f"{kept} was lost"


def test_a_partly_matched_word_keeps_its_unmatched_part(tmp_path):
    """"INDN:OAKLEY" is one word; "INDN:" is the bank's, not the client's."""
    out, _result, store = _run(tmp_path, _pdf(tmp_path, STATEMENT))
    surname = next(e for e in store.persons()
                   if e.canonical.lower().startswith("oakley")).surrogate.last.upper()
    line = next(l for l in _text(out).splitlines() if "CASHOUT" in l)
    assert "INDN:" in line and surname in line and "CO ID:" in line


@pytest.mark.parametrize("sort", [False, True])
def test_a_replacement_reads_in_its_own_row_in_either_order(tmp_path, sort):
    """A parser that reads in file order - PyPDFLoader, pypdf, PyMuPDF's default -
    must find the replacement in its transaction row, not after the footer."""
    out, _result, _store = _run(tmp_path, _pdf(tmp_path, STATEMENT))
    lines = _text(out, sort=sort).splitlines()
    venmo = next(i for i, l in enumerate(lines) if "VENMO" in l)
    nearby = " ".join(lines[max(0, venmo - 2):venmo + 3])
    assert "INDN:" in nearby and "248.75" in nearby
    checkcard = next(i for i, l in enumerate(lines) if "CHECKCARD" in l)
    assert venmo < checkcard, "the rows came out of order"


def test_redaction_is_still_the_default(tmp_path):
    path = _pdf(tmp_path, STATEMENT)
    settings = Settings(use_ner=False)
    assert settings.pdf_mode == "redact"
    out, _result, _store = _run(tmp_path, path, mode="redact")
    text = _text(out)
    assert "OAKLEY" not in text and "[ACCOUNT-1]" not in text


def test_the_file_says_it_is_anonymized_and_the_page_does_not(tmp_path):
    out, _result, _store = _run(tmp_path, _pdf(tmp_path, STATEMENT))
    with pymupdf.open(out) as doc:
        assert "Anonymized copy" in doc.metadata["subject"]
        assert "anonymized" in doc.metadata["keywords"]
    assert "Anonymized" not in _text(out)


def test_the_shading_and_rules_behind_the_text_survive(tmp_path):
    path = _pdf(tmp_path, STATEMENT, shade=True)
    with pymupdf.open(path) as doc:
        before = len(doc[0].get_drawings())
    out, _result, _store = _run(tmp_path, path)
    with pymupdf.open(out) as doc:
        assert len(doc[0].get_drawings()) == before


def test_a_character_the_standard_faces_lack_does_not_stop_the_rebuild(tmp_path):
    """A bank prints a "■" in its date line; it used to send the whole page to
    the fallback, where replacements land at the bottom of the page."""
    cells = [(60, 64, "December 15, 2020 ■ Page 1 of 4")] + STATEMENT
    out, result, _store = _run(tmp_path, _pdf(tmp_path, cells))
    assert not [w for w in result.warnings if "added on top" in w]
    text = _text(out)
    assert "December 15, 2020 ■ Page 1 of 4" in text and "OAKLEY" not in text


def _scribble(page, x, y, width=110.0, height=12.0, colour=(0.05, 0.1, 0.45)):
    """Pen strokes the size of a handwritten word: no font, nothing OCR can read."""
    shape = page.new_shape()
    step = 7.0
    cx = x
    while cx < x + width:
        shape.draw_bezier((cx, y), (cx + 2, y - height), (cx + 5, y + height * 0.4),
                          (cx + step, y - height * 0.2))
        cx += step
    shape.finish(color=colour, width=1.4, closePath=False)
    shape.commit()


def _picture(lines, scribble_at=None, size=(612, 792), dpi=200):
    """A pixmap of printed lines, optionally with handwriting, as a scanner makes."""
    source = pymupdf.open()
    page = source.new_page(width=size[0], height=size[1])
    for index, line in enumerate(lines):
        page.insert_text((60, 90 + index * 30), line, fontsize=14, fontname="helv")
    if scribble_at:
        _scribble(page, *scribble_at)
    return page.get_pixmap(dpi=dpi)


def _ocr_or_skip():
    from redactor import ocr
    if not ocr.describe()[0]:
        pytest.skip("no OCR engine")


def test_a_scan_is_anonymized_with_a_text_layer_and_its_handwriting_blacked(tmp_path):
    _ocr_or_skip()
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)        # the picture's own size
    page.insert_image(page.rect, pixmap=_picture(
        ["Zelle to Oakley V Ward on 02/01", "Monthly rent 1,250.00"], scribble_at=(60, 200)))
    path = tmp_path / "scan.pdf"
    doc.save(path)
    out, result, store = _run(tmp_path, path)
    text = _text(out)
    # the page now has text a parser can read, with the replacement in it
    surname = next(e for e in store.persons()
                   if e.canonical.lower().startswith("oakley")).surrogate.last
    assert surname.upper() in text.upper() and "1,250.00" in text
    assert "Oakley" not in text and "Ward" not in text
    # the handwriting is under a black box, and said to be
    assert "[HANDWRITING]" in text
    assert any("handwriting" in w for w in result.warnings)
    with pymupdf.open(out) as doc:
        dark = doc[0].get_pixmap(dpi=72, clip=pymupdf.Rect(64, 192, 160, 202))
        samples = dark.samples
        assert sum(samples[i] < 40 for i in range(0, len(samples), dark.n)) > 0.5 * dark.width * dark.height


def test_a_clean_scan_gets_no_handwriting_marks(tmp_path):
    """Table rules, a scan's skew and its noise are not handwriting."""
    _ocr_or_skip()
    doc = pymupdf.open()
    source = pymupdf.open()
    sp = source.new_page()
    for index, line in enumerate(["CHARGES", "Water base rate   $22.50", "Sewer   $38.40"]):
        sp.insert_text((60, 90 + index * 24), line, fontsize=12, fontname="helv")
    sp.draw_line((55, 95), (560, 99), width=1.2)            # a rule, skewed
    sp.draw_line((55, 150), (560, 152), width=0.8)
    page = doc.new_page()
    page.insert_image(page.rect, pixmap=sp.get_pixmap(dpi=200))
    path = tmp_path / "clean.pdf"
    doc.save(path)
    out, result, _store = _run(tmp_path, path)
    assert "[HANDWRITING]" not in _text(out)
    assert "$22.50" in _text(out)


def test_an_image_of_text_on_a_text_page_is_anonymized_not_blacked(tmp_path):
    _ocr_or_skip()
    path = tmp_path / "exhibit.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((60, 50), "Exhibit 4 - statement excerpt", fontsize=11, fontname="helv")
    picture = _picture(["Payment from Oakley V Ward", "Amount 512.40"], size=(400, 160))
    page.insert_image(pymupdf.Rect(60, 70, 460, 230), pixmap=picture)
    doc.save(path)
    out, _result, store = _run(tmp_path, path)
    text = _text(out)
    surname = next(e for e in store.persons()
                   if e.canonical.lower().startswith("oakley")).surrogate.last
    assert surname.upper() in text.upper() and "512.40" in text
    assert "Oakley" not in text and "Exhibit 4" in text


def test_an_image_with_no_text_is_replaced_and_marked(tmp_path):
    """A photo, a logo, a signature: nothing to read, so nothing of it stays."""
    _ocr_or_skip()
    path = tmp_path / "photo.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((60, 50), "Account holder: Oakley V Ward", fontsize=11, fontname="helv")
    source = pymupdf.open()
    sp = source.new_page(width=300, height=120)
    _scribble(sp, 20, 70, width=240, height=30)               # a signature
    page.insert_image(pymupdf.Rect(60, 80, 360, 200), pixmap=sp.get_pixmap(dpi=150))
    doc.save(path)
    out, _result, _store = _run(tmp_path, path)
    assert "[IMAGE]" in _text(out)
    with pymupdf.open(out) as doc:
        # right of the "[IMAGE]" label, where the signature's strokes were
        region = doc[0].get_pixmap(dpi=72, clip=pymupdf.Rect(130, 85, 355, 195))
        samples = region.samples
        # no ink left where the signature was
        assert sum(samples[i] < 100 for i in range(0, len(samples), region.n)) < 20


def test_the_same_person_has_the_same_name_in_the_pdf_and_the_word_file(tmp_path):
    docx = pytest.importorskip("docx")
    word = tmp_path / "letter.docx"
    document = docx.Document()
    document.add_paragraph("Account holder: Oakley V Ward")
    document.save(word)
    pdf = _pdf(tmp_path, STATEMENT)
    settings = Settings(use_ner=False, pdf_mode="anonymize", docx_mode="anonymize")
    store, _ = review.build_store(list(HOLDERS))
    pipeline.prescan([word, pdf], store, settings)
    docx_processor.process(word, tmp_path / "w.docx", store, settings)
    pdf_processor.process(pdf, tmp_path / "p.pdf", store, settings)
    surrogate = next(e for e in store.persons()
                     if e.canonical.lower().startswith("oakley")).replacement
    word_text = "\n".join(docx_processor.extract_text(tmp_path / "w.docx").values())
    assert surrogate in word_text
    assert surrogate.split()[0].upper() in _text(tmp_path / "p.pdf")


def test_the_output_is_deterministic(tmp_path):
    path = _pdf(tmp_path, STATEMENT)
    first, _r, _s = _run(tmp_path, path)
    first_text = _text(first)
    (tmp_path / "again").mkdir()
    second, _r, _s = _run(tmp_path / "again", path)
    assert _text(second) == first_text


def test_the_page_header_above_the_holder_is_not_part_of_the_address(tmp_path):
    """"December 15, 2020 · Page 1 of 4" sat close enough above the holder's
    name to be taken into the mailing block, and was deleted with it."""
    cells = [(60, 64, "December 15, 2020 - Page 1 of 4")] + STATEMENT
    for mode in ("anonymize", "redact"):
        out, _result, _store = _run(tmp_path, _pdf(tmp_path, cells, name=f"{mode}.pdf"), mode=mode)
        assert "December 15, 2020 - Page 1 of 4" in _text(out), mode


# ---------------------------------------------------------------------------
# OCR's predictable misreads
# ---------------------------------------------------------------------------


def test_a_misread_name_or_number_on_a_scan_is_still_found():
    store, _ = review.build_store([("Anthony Garcia", "person"), ("Nathan C Blackburn", "person")])
    store.add_value("bank_account", "1027-4419-3301")
    store.add_value("employee_id", "124735")
    words = [(0, 0, 10, 10, t, 0, 0, i) for i, t in enumerate(
        ["Garcla", "BLACKBUM", "1O27-4419-33O1", "Garden", "Statement", "1,247.35", "12/47/35"])]
    found = {hit.text: hit.replacement
             for hit, _rect in pdf_processor._ocr_near_misses(words, store, Settings())}
    garcia = next(e for e in store.persons() if e.canonical.startswith("Anthony"))
    assert found["Garcla"] == garcia.surrogate.last
    assert found["BLACKBUM"].isupper()
    assert found["1O27-4419-33O1"].startswith("[ACCOUNT")
    for left in ("Garden", "Statement", "1,247.35", "12/47/35"):
        assert left not in found, f"{left} was taken"


def test_ocr_reads_a_run_of_asterisks_as_other_marks():
    from redactor import patterns
    found = [(m.text, m.category) for m in
             patterns.scan("Payment method Visa +*8779 Mastercard «*1741 Status")]
    assert ("8779", "masked_account") in found and ("1741", "masked_account") in found


def test_an_anonymized_text_page_is_clean_to_the_eye_too(tmp_path):
    """The text layer is not the whole page: a change that drew replacements
    over glyphs it failed to remove would pass every get_text() check. OCR
    reads what a person - or a vision model - would see."""
    from PIL import Image
    from redactor import ocr
    engines = ocr.usable_engines()
    if not engines:
        pytest.skip("no OCR engine")
    out, _result, _store = _run(tmp_path, _pdf(tmp_path, STATEMENT))
    with pymupdf.open(out) as doc:
        pix = doc[0].get_pixmap(dpi=200)
    seen = " ".join(w.text for w in engines[0].read(
        Image.frombytes("RGB", (pix.width, pix.height), pix.samples))).upper()
    for gone in ("OAKLEY", "12469", "84042"):
        assert gone not in seen, f"{gone} still visible on the page"
