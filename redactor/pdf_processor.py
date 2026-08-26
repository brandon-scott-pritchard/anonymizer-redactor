"""PDF redaction.

PDFs are always redacted, never anonymized: the sensitive glyphs are physically
removed from the content stream with PyMuPDF's redaction annotations and an
opaque black box is drawn in their place.  Copy/paste and text extraction find
nothing, because there is nothing left to find.

Pages with no text layer are scanned images.  With OCR enabled they are read
with Tesseract and redacted by pixel coordinates; without it they are refused
outright rather than passed through looking processed.
"""

from __future__ import annotations

import re

from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from . import categories, engine, ocr
from .engine import Hit, PersonMatcher, Settings
from .mapping import MappingStore

OCR_DPI = 300
MIN_TEXT_CHARS = 12          # below this a page is treated as image-only
BOX_PADDING = 1.0            # points of slack around an exact text box
OCR_BOX_PADDING = 3.0        # OCR boxes are approximate, so they get more


@dataclass
class PageReport:
    number: int
    hits: int = 0
    boxes: int = 0
    ocr_used: bool = False
    image_only: bool = False
    text_chars: int = 0
    images_redacted: int = 0


@dataclass
class PdfResult:
    source: Path
    output: Path | None
    hits: list[Hit] = field(default_factory=list)
    pages: list[PageReport] = field(default_factory=list)
    annotations_removed: int = 0
    links_removed: int = 0
    images_redacted: int = 0
    attachments_removed: int = 0
    bookmarks_removed: int = 0
    metadata_scrubbed: bool = False
    refused: bool = False
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# OCR availability
# --------------------------------------------------------------------------

def ocr_available() -> tuple[bool, str]:
    """(can we OCR, which engine)."""
    return ocr.describe()


# --------------------------------------------------------------------------
# words -> text, keeping a map back to rectangles
# --------------------------------------------------------------------------


# The widest run of spaces a column gap is rendered as. Enough to break any
# digit-run detector, capped so a mostly-empty line cannot produce a huge
# string.
MAX_GAP_SPACES = 12


def _words_to_text(words) -> tuple[str, list[tuple[int, int, pymupdf.Rect]]]:
    """Join extracted words into text, remembering each word's span and box.

    Line breaks are preserved so the detectors and the allowlist see the same
    shape of text they would see in a DOCX.

    Column gaps are preserved too, and that is not cosmetic. Joining every
    same-line word with a single space destroys the column structure of a bank
    statement, and a statement is nothing but columns. On a declaration's asset
    schedule it put the account number one space from the money beside it:

        Certificate of deposit 44-8821-0033 950.00 Joint

    and the bare-digits card detector ran straight from one into the other,
    matching "44-8821-0033 950" - which redacted most of a dollar figure.
    Rendering a wide gap as a wide gap stops any digit-run detector crossing a
    column, and has the side benefit that extracted text keeps the shape of the
    table it came from.
    """
    pieces: list[str] = []
    spans: list[tuple[int, int, pymupdf.Rect]] = []
    cursor = 0
    previous_line = None
    previous_x1 = 0.0
    previous_width = 0.0
    previous_len = 1
    for x0, y0, x1, y1, word, block, line, _no in words:
        if not word:
            continue
        key = (block, line)
        if previous_line is not None:
            if key != previous_line:
                separator = "\n"
            else:
                # one character's width on the previous word, as the yardstick
                char = max(previous_width / max(previous_len, 1), 1.0)
                gap = x0 - previous_x1
                if gap < char * 1.4:
                    separator = " "
                else:
                    separator = " " * min(MAX_GAP_SPACES,
                                          max(2, int(round(gap / char))))
            pieces.append(separator)
            cursor += len(separator)
        previous_line = key
        previous_x1, previous_width, previous_len = x1, x1 - x0, len(word)
        start = cursor
        pieces.append(word)
        cursor += len(word)
        spans.append((start, cursor, pymupdf.Rect(x0, y0, x1, y1)))
    return "".join(pieces), spans


def _rects_for_hit(hit: Hit, spans, padding: float = BOX_PADDING) -> list[pymupdf.Rect]:
    """Boxes covering a hit, one per line the hit crosses."""
    lines: list[pymupdf.Rect] = []
    for start, end, rect in spans:
        if end <= hit.start or start >= hit.end:
            continue
        placed = False
        for i, existing in enumerate(lines):
            # same visual line if the vertical ranges substantially overlap
            overlap = min(existing.y1, rect.y1) - max(existing.y0, rect.y0)
            if overlap > 0.5 * min(existing.height, rect.height):
                # Rect union returns a new object, so assign it back into the
                # list - an in-place |= would only rebind the loop variable and
                # silently leave every word but the first uncovered.
                lines[i] = existing | rect
                placed = True
                break
        if not placed:
            lines.append(pymupdf.Rect(rect))
    return [r + (-padding, -padding, padding, padding) for r in lines]


# --------------------------------------------------------------------------
# OCR
# --------------------------------------------------------------------------


def _ocr_words(page: pymupdf.Page, ocr_engine=None):
    """OCR one page, returning words in PDF coordinates.

    Returns ``None`` - not ``[]`` - when the engine cannot run: an empty list
    means "a page with nothing on it", while None means "we could not look",
    and the caller must refuse the page rather than deliver it as clean.
    """
    from PIL import Image

    if ocr_engine is None:
        ocr_engine, _note = ocr.select_engine()
    if ocr_engine is None:
        return None

    scale = OCR_DPI / 72.0
    pixmap = page.get_pixmap(dpi=OCR_DPI, colorspace=pymupdf.csRGB)
    image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)

    return [
        (w.x0 / scale, w.y0 / scale, w.x1 / scale, w.y1 / scale,
         w.text, w.block, w.line, w.word)
        for w in ocr_engine.read(image)
    ]


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------


def extract_text(path: Path, max_pages: int | None = None) -> dict[str, str]:
    """Text per page, for scanning and caption harvesting."""
    out: dict[str, str] = {}
    with pymupdf.open(path) as doc:
        for index, page in enumerate(doc):
            if max_pages is not None and index >= max_pages:
                break
            out[f"page {index + 1}"] = page.get_text("text")
    return out


def iter_text_units(path: Path):
    """The exact per-page text :func:`process` will scan, in the same order."""
    with pymupdf.open(path) as doc:
        for page in doc:
            words = page.get_text("words")
            if len(page.get_text("text").strip()) < MIN_TEXT_CHARS:
                continue          # image-only; OCR happens only during the run
            text, _spans = _words_to_text(words)
            if text.strip():
                yield text


# A line that ends in "UT 84606" or "IA 50306-0522" - a state and a ZIP, which
# is how the last line of a mailing address always ends and how nothing else on
# a statement ends. A merchant line says "ASIAN MARKET PROVO UT 13.99": a state,
# then money, and no ZIP.
_ZIP_LINE = re.compile(r"\b[A-Z]{2}\.?[ \t]+\d{5}(?:-\d{4})?[ \t]*$")
_MONEY_ON_LINE = re.compile(r"\d[\d,]*\.\d{2}")
# the lines above the ZIP: a street number, or a person's or company's name
_ADDRESS_LINE = re.compile(r"^\s*(?:\d{1,6}[A-Za-z]?\s+\S|P\.?\s?O\.?\s+Box\b|[A-Za-z])")

ADDRESS_BLOCK_LINES = 4          # a name, two street lines and the city line
ADDRESS_LINE_CHARS = 48          # an address line is short; prose is not
ADDRESS_BLOCK_PAD = 2.0


def _visual_lines(words):
    """[(text, rect, y0)] grouped by visual line, top to bottom."""
    rows: dict[int, list] = {}
    for x0, y0, x1, y1, word, *_ in words:
        if word:
            rows.setdefault(round(y0 / 2.0), []).append((x0, y0, x1, y1, word))
    lines = []
    for key in sorted(rows):
        items = sorted(rows[key], key=lambda w: w[0])
        rect = pymupdf.Rect(items[0][0], items[0][1], items[0][2], items[0][3])
        for x0, y0, x1, y1, _w in items[1:]:
            rect |= pymupdf.Rect(x0, y0, x1, y1)
        lines.append((" ".join(i[4] for i in items), rect, rect.y0))
    return lines


def address_blocks(words) -> list[pymupdf.Rect]:
    """Rectangles covering every mailing-address block on a page.

    Matching the address as *text* keeps going wrong on a real statement,
    because a coupon's address block is not laid out as text so much as
    positioned: the name wraps, a word gets split across two runs, and a box
    drawn from whichever span happened to match covers "BRANDON PR" and leaves
    "ITCHARD" sitting on the page. Reading the geometry instead sidesteps all
    of it - find the line that ends in a state and a ZIP, walk up while the
    lines above are short and carry no money, and black the whole rectangle.

    Deliberately blunt. A mailing address is the one thing on a statement where
    covering a little too much costs nothing: there is no transaction data in
    it, and the alternative is a name that ships in pieces.
    """
    lines = _visual_lines(words)
    blocks: list[pymupdf.Rect] = []
    for index, (text, rect, _y) in enumerate(lines):
        if not _ZIP_LINE.search(text) or _MONEY_ON_LINE.search(text):
            continue
        block = pymupdf.Rect(rect)
        height = max(rect.height, 1.0)
        top = index
        for above in range(index - 1, max(index - ADDRESS_BLOCK_LINES - 1, -1), -1):
            line, line_rect, _ = lines[above]
            if (len(line) > ADDRESS_LINE_CHARS
                    or _MONEY_ON_LINE.search(line)
                    or not _ADDRESS_LINE.match(line)
                    # must be the line directly above, not across a gap
                    or lines[top][1].y0 - line_rect.y0 > height * 2.2):
                break
            block |= line_rect
            top = above
        blocks.append(block + (-ADDRESS_BLOCK_PAD, -ADDRESS_BLOCK_PAD,
                               ADDRESS_BLOCK_PAD, ADDRESS_BLOCK_PAD))
    return blocks


def iter_tables(path: Path):
    """Rows of cells reconstructed from word geometry, one list per page.

    A PDF has no table elements at all; it has words with coordinates. But a
    statement, a pay stub and a tax form are all columns, and the column a
    value sits under is its label just as surely as a ``<w:tc>`` header would
    be. Without this the PDF path has no way to pair "Account / identifying
    number" with the bare number three inches to its right, and every account
    number on a declaration's asset schedule ships.

    Cells are split where the horizontal gap between two words is wide enough
    to be a column break rather than a word space - the same measurement
    :func:`_words_to_text` uses. The shape it returns matches
    ``docx_processor.iter_tables`` so the same pairing code reads both.
    """
    with pymupdf.open(path) as doc:
        for page in doc:
            words = page.get_text("words")
            if len(page.get_text("text").strip()) < MIN_TEXT_CHARS:
                continue                     # image-only; nothing to lay out
            lines: dict[tuple[int, int], list] = {}
            for x0, y0, x1, y1, word, block, line, no in words:
                if word:
                    lines.setdefault((block, line), []).append((x0, x1, word, no))
            rows: list[list[str]] = []
            for key in sorted(lines, key=lambda k: (k[0], k[1])):
                items = sorted(lines[key], key=lambda w: w[0])
                cells: list[list[str]] = []
                previous_x1 = None
                previous_char = 1.0
                for x0, x1, word, _no in items:
                    if previous_x1 is None or (x0 - previous_x1) < previous_char * 1.4:
                        if not cells:
                            cells.append([word])
                        else:
                            cells[-1].append(word)
                    else:
                        cells.append([word])
                    previous_x1 = x1
                    previous_char = max((x1 - x0) / max(len(word), 1), 1.0)
                joined = [" ".join(cell) for cell in cells]
                if any(joined):
                    rows.append(joined)
            # A page is not one table. Yield each run of consecutive lines that
            # actually have columns, so the first line of a run is its heading
            # row - which is what the caller treats row 0 as. A single-cell
            # line is prose and ends the run; pairing prose lines with each
            # other would invent labels that were never on the page.
            # A single-cell line ends the run. Letting one stray line through -
            # an address cell wraps onto its own line inside a 1099's grid, and
            # merging across it would give that grid's account number a label -
            # was measured and made things worse: it also merged the blocks
            # either side of a heading on the asset schedule, so row 0 of the
            # merged block was no longer the heading and three more account
            # numbers shipped. One leak beats four.
            block: list[list[str]] = []
            for row in rows:
                if len(row) >= 2:
                    block.append(row)
                    continue
                if len(block) >= 2:
                    yield block
                block = []
            if len(block) >= 2:
                yield block


def caption_sources(path: Path) -> dict[str, str]:
    """Page one's caption block, plus the running header strip of later pages."""
    sources: dict[str, str] = {}
    with pymupdf.open(path) as doc:
        if doc.page_count == 0:
            return sources
        first = doc[0]
        sources[f"{path.name} [caption]"] = first.get_text("text")
        header_text: list[str] = []
        for index in range(1, min(doc.page_count, 6)):
            page = doc[index]
            strip = pymupdf.Rect(0, 0, page.rect.width, page.rect.height * 0.15)
            header_text.append(page.get_text("text", clip=strip))
        joined = "\n".join(t for t in header_text if t.strip())
        if joined.strip():
            sources[f"{path.name} [running header]"] = joined
    return sources


def is_image_only(path: Path) -> bool:
    with pymupdf.open(path) as doc:
        for page in doc:
            if len(page.get_text("text").strip()) >= MIN_TEXT_CHARS:
                return False
    return True


# --------------------------------------------------------------------------
# rewriting
# --------------------------------------------------------------------------


def _strip_page_extras(page: pymupdf.Page) -> tuple[int, int]:
    """Remove annotations, form widgets, and links.

    Annotations include FreeText, sticky notes and drawn Ink (a handwritten
    signature made in a PDF viewer). Links are NOT in ``page.annots()`` - they
    have their own accessor - and a mailto: or file:// target under redacted
    text would otherwise survive into the delivered file.
    """
    removed = links = 0
    for annot in list(page.annots() or []):
        try:
            page.delete_annot(annot)
            removed += 1
        except Exception:                          # pragma: no cover - defensive
            continue
    for widget in list(page.widgets() or []):
        try:
            page.delete_widget(widget)
            removed += 1
        except Exception:                          # pragma: no cover - defensive
            continue
    for link in list(page.get_links()):
        try:
            page.delete_link(link)
            links += 1
        except Exception:                          # pragma: no cover - defensive
            continue
    return removed, links


def process(
    source: Path,
    output: Path,
    store: MappingStore,
    settings: Settings,
    matcher: PersonMatcher | None = None,
) -> PdfResult:
    """Write a truly redacted copy of ``source`` to ``output``."""
    source, output = Path(source), Path(output)
    result = PdfResult(source=source, output=output)
    matcher = matcher or PersonMatcher(store, settings)
    document_label = source.name

    # PDFs are always redacted regardless of the DOCX mode the operator chose
    redact_settings = Settings(**{**settings.__dict__, "docx_mode": "redact"})

    ocr_ok, ocr_note = ocr_available()
    # probe once per document, not once per scanned page
    ocr_engine = ocr.select_engine()[0] if (settings.ocr_scanned_pdfs and ocr_ok) else None
    doc = pymupdf.open(source)

    try:
        image_only_pages: list[int] = []
        ocr_failed_pages: list[int] = []
        ocr_pages_delivered = False
        for index, page in enumerate(doc):
            report = PageReport(number=index + 1)

            if settings.scrub_embedded:
                removed, links = _strip_page_extras(page)
                result.annotations_removed += removed
                result.links_removed += links

            words = page.get_text("words")
            page_text = page.get_text("text")
            report.text_chars = len(page_text.strip())

            if report.text_chars < MIN_TEXT_CHARS:
                report.image_only = True
                if settings.ocr_scanned_pdfs and ocr_ok:
                    words = _ocr_words(page, ocr_engine)
                    if words is None:
                        # the engine passed the probe but failed mid-run;
                        # delivering this page as "clean" would be a leak
                        ocr_failed_pages.append(index + 1)
                        result.pages.append(report)
                        continue
                    report.ocr_used = True
                    ocr_pages_delivered = True
                else:
                    image_only_pages.append(index + 1)
                    result.pages.append(report)
                    continue

            text, spans = _words_to_text(words)
            hits = engine.scan_text(text, store, redact_settings, matcher)
            report.hits = len(hits)

            # OCR boxes come back in rendered (rotated) space; map them onto
            # the page's own coordinates or a rotated scan gets boxes in the
            # wrong place while the real values stay readable
            derotate = page.derotation_matrix if report.ocr_used else None

            for hit in hits:
                entity = store.entities.get(hit.entity_key)
                if entity is not None:
                    store.record_hit(entity, document_label)
                padding = OCR_BOX_PADDING if report.ocr_used else BOX_PADDING
                for rect in _rects_for_hit(hit, spans, padding):
                    if derotate is not None:
                        rect = pymupdf.Rect(rect) * derotate
                    if rect.is_empty or rect.is_infinite:
                        continue
                    annot_text = ""
                    if settings.label_redaction_boxes:
                        annot_text = f"[{categories.tag_for(hit.category)}]"
                    page.add_redact_annot(
                        rect,
                        text=annot_text,
                        fontsize=6,
                        fill=(0, 0, 0),
                        text_color=(1, 1, 1),
                        align=pymupdf.TEXT_ALIGN_CENTER,
                    )
                    report.boxes += 1

            # The mailing address, covered as a block rather than word by word.
            # A coupon positions its address rather than laying it out as text,
            # so a box drawn from whichever span matched covered "BRANDON PR"
            # and left "ITCHARD" on the page. Gated on street_address, so an
            # operator who turns addresses off still gets what they asked for.
            if settings.category_enabled("street_address"):
                for rect in address_blocks(words):
                    if derotate is not None:
                        rect = pymupdf.Rect(rect) * derotate
                    if rect.is_empty or rect.is_infinite:
                        continue
                    page.add_redact_annot(rect, fill=(0, 0, 0))
                    report.boxes += 1

            # Every embedded image on a text page is blacked out whole: a
            # photo, an exhibit scan beside a typed header, or a signature
            # image is confidential and no text scan can read into it. A
            # full-page scan being OCR'd is the page itself and is exempt -
            # its recognised text was boxed above.
            if settings.redact_images and not report.image_only:
                for image_info in page.get_images(full=True):
                    xref = image_info[0]
                    try:
                        rects = page.get_image_rects(xref)
                    except Exception:              # pragma: no cover - defensive
                        continue
                    for rect in rects:
                        if rect.is_empty or rect.is_infinite:
                            continue
                        page.add_redact_annot(rect, fill=(0, 0, 0))
                        report.boxes += 1
                        report.images_redacted += 1
                result.images_redacted += report.images_redacted

            result.hits.extend(hits)
            if report.boxes:
                # PDF_REDACT_IMAGE_PIXELS also clears image content under a box,
                # which is what makes OCR'd scans genuinely redacted
                page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS)
            result.pages.append(report)

        if image_only_pages or ocr_failed_pages:
            if ocr_failed_pages:
                reason = "the OCR engine stopped working part-way through"
                pages = ocr_failed_pages + image_only_pages
            else:
                reason = ocr_note if not ocr_ok else "OCR was turned off"
                pages = image_only_pages
            result.refused = True
            result.warnings.append(
                f"pages {', '.join(str(p) for p in sorted(pages))} carry no text layer "
                f"and could not be read ({reason}). The file was NOT written - a scanned "
                "page passed through untouched would look redacted without being redacted."
            )
            result.output = None
            return result

        if ocr_pages_delivered:
            result.warnings.append(
                "Some pages are scans read by OCR. Machine-printed text on them was "
                "redacted; handwriting, stamps and margin notes on a scan cannot be "
                "recognised and may still be readable. Review scanned pages by hand."
            )

        if settings.scrub_metadata:
            doc.set_metadata({})
            try:
                doc.del_xml_metadata()
            except Exception:                      # pragma: no cover - defensive
                pass
            result.metadata_scrubbed = True

        if settings.scrub_embedded:
            toc = doc.get_toc()
            if toc:
                doc.set_toc([])
                result.bookmarks_removed = len(toc)
            for name in list(doc.embfile_names()):
                try:
                    doc.embfile_del(name)
                    result.attachments_removed += 1
                except Exception:                  # pragma: no cover - defensive
                    continue
            try:
                for xref in range(1, doc.xref_length()):
                    if doc.xref_get_key(xref, "JS")[0] != "null":
                        doc.xref_set_key(xref, "JS", "null")
            except Exception:                      # pragma: no cover - defensive
                pass

        output.parent.mkdir(parents=True, exist_ok=True)
        doc.save(output, garbage=4, deflate=True, clean=True)
    finally:
        doc.close()

    return result
