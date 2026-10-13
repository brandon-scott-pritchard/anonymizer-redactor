"""Photos and screenshots of documents: made into PDFs, then read as scans."""

import io
from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf")
np = pytest.importorskip("numpy")
cv2 = pytest.importorskip("cv2")
from PIL import Image  # noqa: E402

from redactor import ocr, pdf_processor, photo, pipeline, review  # noqa: E402
from redactor.engine import Settings  # noqa: E402

LINES = ["WELLSPRING HSA CUSTODIAL BANK", "KYLE D HARRINGTON", "2279 E 5950 S",
         "OGDEN UT 84403", "Account number 4417220193", "Ending balance $1,204.50"]


def _page(lines=LINES, dark=False) -> Image.Image:
    """A letter page with the lines on it, as pixels."""
    doc = pymupdf.open()
    page = doc.new_page(width=612, height=792)
    for i, line in enumerate(lines):
        page.insert_text((60, 90 + 28 * i), line, fontsize=14)
    pix = page.get_pixmap(dpi=150)
    image = Image.frombytes("RGB", (pix.width, pix.height), pix.samples)
    return Image.fromarray(255 - np.asarray(image)) if dark else image


def _photographed(page: Image.Image) -> Image.Image:
    """The page lying on a brown desk, shot at an angle."""
    src = np.asarray(page)
    h, w = src.shape[:2]
    canvas_w, canvas_h = int(w * 1.5), int(h * 1.4)
    corners = np.float32([[0, 0], [w, 0], [w, h], [0, h]])
    placed = np.float32([[canvas_w * 0.16, canvas_h * 0.10], [canvas_w * 0.86, canvas_h * 0.06],
                         [canvas_w * 0.90, canvas_h * 0.92], [canvas_w * 0.12, canvas_h * 0.95]])
    matrix = cv2.getPerspectiveTransform(corners, placed)
    desk = np.full((canvas_h, canvas_w, 3), (110, 74, 45), np.uint8)
    warped = cv2.warpPerspective(src, matrix, (canvas_w, canvas_h))
    mask = cv2.warpPerspective(np.full((h, w), 255, np.uint8), matrix, (canvas_w, canvas_h))
    desk[mask > 0] = warped[mask > 0]
    return Image.fromarray(desk)


def _with_exif(image: Image.Image, path: Path, orientation: int = 1) -> Path:
    exif = Image.Exif()
    exif[0x0112] = orientation                         # Orientation
    exif[0x010F] = "Phone Maker"                       # Make
    gps = exif.get_ifd(0x8825)
    gps[2] = (40.0, 44.0, 12.0)                        # GPSLatitude
    gps[1] = "N"
    image.save(path, format="JPEG", exif=exif, quality=92)
    return path


def _embedded(pdf: Path) -> Image.Image:
    with pymupdf.open(pdf) as doc:
        xref = doc[0].get_images()[0][0]
        return Image.open(io.BytesIO(doc.extract_image(xref)["image"]))


def test_a_photo_on_a_desk_is_cropped_to_the_page_and_squared_up(tmp_path):
    shot = _with_exif(_photographed(_page()), tmp_path / "statement.jpg")
    pdf = photo.to_pdf(shot, tmp_path / "statement.pdf")
    with pymupdf.open(pdf) as doc:
        width, height = doc[0].rect.width, doc[0].rect.height
    assert len(pymupdf.open(pdf)) == 1
    assert abs(width / height - 612 / 792) < 0.06, "not squared up to the page's shape"
    # the desk is gone: the border of the result is page-white, not brown
    pixels = np.asarray(_embedded(pdf).convert("RGB")).astype(int)
    border = np.concatenate([pixels[:4].reshape(-1, 3), pixels[-4:].reshape(-1, 3)])
    assert border.mean() > 200


def test_nothing_from_the_camera_reaches_the_pdf(tmp_path):
    """The EXIF block carries GPS, the phone's make and when it was taken."""
    shot = _with_exif(_photographed(_page()), tmp_path / "statement.jpg")
    assert Image.open(shot).getexif().get_ifd(0x8825), "the fixture must carry GPS"
    pdf = photo.to_pdf(shot, tmp_path / "statement.pdf")
    raw = pdf.read_bytes()
    assert b"Phone Maker" not in raw and b"Exif" not in raw
    assert not dict(_embedded(pdf).getexif())
    assert not any(pymupdf.open(pdf).metadata.get(k) for k in ("author", "creator", "producer", "title"))


def test_a_photo_saved_sideways_is_turned_upright(tmp_path):
    """Orientation 6: the camera was held sideways, the pixels lie on their side."""
    upright = _page()
    sideways = upright.rotate(90, expand=True)          # what the sensor stored
    shot = _with_exif(sideways, tmp_path / "side.jpg", orientation=6)
    pdf = photo.to_pdf(shot, tmp_path / "side.pdf")
    with pymupdf.open(pdf) as doc:
        assert doc[0].rect.height > doc[0].rect.width


def test_a_dark_mode_screenshot_is_turned_light(tmp_path):
    shot = tmp_path / "app.png"
    _page(dark=True).save(shot)
    pdf = photo.to_pdf(shot, tmp_path / "app.pdf")
    assert np.asarray(_embedded(pdf).convert("L")).mean() > 180


def test_a_screenshot_with_a_grey_margin_is_not_cropped_to_its_card(tmp_path):
    """A light screen's grey surround is not a desk: cropping to the white card
    would drop the header above it - the account holder's name."""
    screen = np.full((1400, 640, 3), 236, np.uint8)
    screen[300:1100, 40:600] = 255
    cv2.putText(screen, "Kyle Harrington", (40, 120), cv2.FONT_HERSHEY_SIMPLEX, 1.2, (0, 0, 0), 2)
    shot = tmp_path / "screen.png"
    Image.fromarray(screen).save(shot)
    pdf = photo.to_pdf(shot, tmp_path / "screen.pdf")
    assert _embedded(pdf).size == (640, 1400)


def test_every_page_of_a_tiff_becomes_its_own_page(tmp_path):
    """Pillow's frame iterator hands back one image object: collected before
    copying, a fax came back as every page a copy of its last."""
    greys = [(250, 250, 250), (200, 200, 200), (150, 150, 150)]
    frames = [Image.new("RGB", (400, 500), grey) for grey in greys]
    tiff = tmp_path / "fax.tif"
    frames[0].save(tiff, save_all=True, append_images=frames[1:])
    assert [int(np.asarray(p).mean()) for p in photo.open_pages(tiff)] == [250, 200, 150]
    assert len(pymupdf.open(photo.to_pdf(tiff, tmp_path / "fax.pdf"))) == 3


def test_an_iphone_heic_is_read(tmp_path):
    pytest.importorskip("pillow_heif")
    photo._register_heif()
    heic = tmp_path / "IMG_0042.HEIC"
    _photographed(_page()).save(heic, format="HEIF")
    assert pipeline.classify(heic) == "image"
    pdf = photo.to_pdf(heic, tmp_path / "IMG_0042.pdf")
    assert len(pymupdf.open(pdf)) == 1


def test_a_folder_gathers_photos_as_documents(tmp_path):
    root = tmp_path / "Case"
    root.mkdir()
    _page().save(root / "receipt.jpg")
    (root / "notes.csv").write_text("a,b")
    gathered = pipeline.gather([root])
    assert [rel for _p, rel in gathered.files] == ["Case/receipt.jpg"]
    assert gathered.skipped == ["Case/notes.csv"]


@pytest.mark.parametrize("mode", ["redact", "anonymize"])
def test_a_photographed_statement_loses_its_identifiers(tmp_path, mode):
    if not ocr.usable_engines():
        pytest.skip("no OCR engine")
    shot = _with_exif(_photographed(_page()), tmp_path / "Harrington_statement.jpg")
    pdf = pipeline.as_document(shot, tmp_path / "converted")
    settings = Settings(use_ner=False, pdf_mode=mode)
    store, _ = review.build_store([("Kyle D Harrington", "person")])
    pipeline.prescan([pdf], store, settings)
    out = tmp_path / f"out-{mode}.pdf"
    pdf_processor.process(pdf, out, store, settings)
    with pymupdf.open(out) as doc:
        page = doc[0]
        pix = page.get_pixmap(dpi=200)
    seen = " ".join(w.text for w in ocr.usable_engines()[0].read(
        Image.frombytes("RGB", (pix.width, pix.height), pix.samples))).upper()
    layer = pymupdf.open(out)[0].get_text().upper()
    for gone in ("HARRINGTON", "4417220193", "5950"):
        assert gone not in seen, f"{gone} still readable on the page"
        assert gone not in layer, f"{gone} in the text layer"
    assert "1,204.50" in seen.replace(" ", "") or "1,204.50" in layer
    assert "WELLSPRING" in seen or "WELLSPRING" in layer


@pytest.mark.parametrize("header", [(30, 80, 200), (25, 25, 25)])
def test_a_coloured_header_band_is_never_cropped_away(tmp_path, header):
    """A banking app's blue header, or a statement's dark banner, holds the
    account holder's name; it is not the desk the page lies on."""
    screen = np.full((1400, 640, 3), 255, np.uint8)
    screen[:180] = header
    cv2.putText(screen, "Kyle Harrington", (30, 110), cv2.FONT_HERSHEY_SIMPLEX, 1.4,
                (255, 255, 255), 3)
    assert photo.prepare(Image.fromarray(screen)).size == (640, 1400)


def test_a_statement_with_a_dark_banner_on_a_desk_keeps_its_banner(tmp_path):
    page = np.asarray(_page()).copy()
    page[:120] = (20, 40, 90)                     # the bank's banner, name and account in it
    shot = _photographed(Image.fromarray(page))
    out = photo.prepare(shot)
    width, height = out.size
    assert abs(width / height - 612 / 792) < 0.06, "the page was not found whole"
    # the banner is still the top of the page, and still dark - neither cropped
    # off as background nor lightened away as a shadow
    rows = np.asarray(out.convert("L"))[: height // 6, width // 4: 3 * width // 4].mean(axis=1)
    assert rows.min() < 110


def test_a_page_running_off_two_edges_of_the_photo_is_still_squared_up(tmp_path):
    """Hand-held, the page runs off the right and bottom of the frame; refused
    as a "screen", it stayed skewed and OCR missed its slanted lines."""
    shot = np.asarray(_photographed(_page())).copy()
    h, w = shot.shape[:2]
    cut = shot[: int(h * 0.93), : int(w * 0.88)]          # page now meets two edges
    out = photo.prepare(Image.fromarray(cut))
    assert out.size != (cut.shape[1], cut.shape[0]), "the page was not cropped and squared"


def test_slanted_text_with_no_page_edge_is_levelled():
    page = _page()
    tilted = page.rotate(6, expand=False, fillcolor="white")    # white on white: no edge
    out = photo.prepare(tilted)
    ink = (np.asarray(out.convert("L")) < 128).sum(axis=1)
    before = (np.asarray(tilted.convert("L")) < 128).sum(axis=1)
    assert np.var(ink) > 1.3 * np.var(before)
