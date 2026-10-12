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
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from . import categories, engine, names as _names, ocr
from .engine import Hit, PersonMatcher, Settings
from .mapping import MappingStore

OCR_DPI = 300
MIN_TEXT_CHARS = 12          # below this a page is treated as image-only
BOX_PADDING = 1.0            # points of slack around an exact text box
# How far a text-layer box is pulled in from the top and bottom of its line, as
# a fraction of the line's height. PyMuPDF deletes every character whose box
# touches a redaction rectangle, and a character's box is the font's full
# ascender-to-descender height - taller than the line pitch in a tight table.
# A box padded outward therefore took glyphs off the line above and the line
# below: "Pioneer Trail Bank, N" vanished over a remittance address, and ".A.
# (114)" survived only because the box ended there. The middle half of a line
# still intersects every character on it, and nothing on its neighbours.
TEXT_BOX_INSET = 0.25
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
MIN_GAP_SPACES = 3


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
    for x0, y0, x1, y1, word, block, line, *_rest in words:
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
                    # never fewer than three, so a pattern that allows two
                    # spaces inside a value can never read across a column
                    separator = " " * min(MAX_GAP_SPACES,
                                          max(MIN_GAP_SPACES, int(round(gap / char))))
            pieces.append(separator)
            cursor += len(separator)
        previous_line = key
        previous_x1, previous_width, previous_len = x1, x1 - x0, len(word)
        start = cursor
        pieces.append(word)
        cursor += len(word)
        spans.append((start, cursor, pymupdf.Rect(x0, y0, x1, y1)))
    return "".join(pieces), spans


# Two words share a visual row when their vertical centres are this close,
# as a fraction of the taller one's height.
ROW_TOLERANCE = 0.45


def _by_rows(words) -> list:
    """The same words renumbered so each visual row is one line.

    PyMuPDF's (block, line) numbering follows how the file was *written*, and a
    generator that places every table cell with its own text call gives every
    cell its own line. A Venmo ledger then reads as a column of isolated
    fragments - ``Jenna Harrington`` on one line, ``Colton Hughes`` on the next,
    the amount on a third - and nothing that needs a label beside its value,
    or a payee column under its heading, ever fires. Grouping by where the
    words actually sit on the page gives back the row a person reads.
    """
    items = sorted((w for w in words if w[4]), key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    rows: list[list] = []
    centre = height = 0.0
    for word in items:
        mid = (word[1] + word[3]) / 2
        tall = max(word[3] - word[1], 1.0)
        if rows and abs(mid - centre) <= max(tall, height) * ROW_TOLERANCE:
            rows[-1].append(word)
            continue
        rows.append([word])
        centre, height = mid, tall
    out = []
    for number, row in enumerate(rows):
        for index, w in enumerate(sorted(row, key=lambda w: w[0])):
            out.append((w[0], w[1], w[2], w[3], w[4], 0, number, index))
    return out


def _readings(words) -> list[tuple[str, list]]:
    """The page as written, and the page as laid out - both are scanned.

    Neither reading is enough alone. The file's own order keeps a mailing
    address's lines apart from the "Customer service" panel printed beside it;
    the visual order puts a label next to its value and a counterparty in the
    same line as the transfer it received. A redaction box is a rectangle, so
    the union of what either reading finds costs nothing in legibility.
    """
    first = _words_to_text(words)
    second = _words_to_text(_by_rows(words))
    if second[0] == first[0]:
        return [first]
    return [first, second]


def _inset(rect: pymupdf.Rect, padding: float) -> pymupdf.Rect:
    """Horizontal slack, and the middle band of the line vertically."""
    trim = rect.height * TEXT_BOX_INSET
    return pymupdf.Rect(rect.x0 - padding, rect.y0 + trim, rect.x1 + padding, rect.y1 - trim)


def _rects_for_hit(hit: Hit, spans, padding: float = BOX_PADDING,
                   ocr: bool = False) -> list[pymupdf.Rect]:
    """Boxes covering a hit, one per line the hit crosses.

    On a scan the box is padded outward on every side, because there it is the
    pixels under it that get removed and OCR boxes are approximate. On a text
    layer it is padded sideways and trimmed vertically - see TEXT_BOX_INSET.
    """
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
    if ocr:
        return [r + (-padding, -padding, padding, padding) for r in lines]
    return [_inset(r, padding) for r in lines]


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
         w.text, w.block, w.line, w.word, w.confidence)
        for w in ocr_engine.read(image)
    ]


# Recognised words per scanned page, so the review steps and the run read a
# scan once between them. Keyed on the file's identity as well as its path, so
# a file replaced under the same name is read afresh.
OCR_CACHE_PAGES = 4000
_ocr_cache: "OrderedDict[tuple, list]" = OrderedDict()
_ocr_cache_lock = threading.Lock()


def _ocr_key(path: Path, index: int) -> tuple | None:
    try:
        stat = Path(path).stat()
    except OSError:
        return None
    return (str(Path(path).resolve()), stat.st_size, stat.st_mtime_ns, index)


def _page_words(page: pymupdf.Page, path: Path, index: int, engines=None):
    """``(word_sets, scanned)`` - the text layer, or every engine's OCR of a scan.

    A text page gives one word list. A scanned page gives one per OCR engine
    that could read it, preferred engine first, and every caller reads all of
    them: see :func:`ocr.usable_engines` for why one engine is not enough.

    Scanned pages used to be read only during the run. Every review step -
    the caption, the children roster, the counterparties, the prescan that
    fills the review screen - saw nothing on them, so a batch of scans came to
    the review screen empty and the run redacted only what the patterns alone
    could recognise: 268 scanned pages in the test corpus leaked names at three
    times the rate of their text-layer originals. ``word_sets`` is None when
    the page is a scan and no engine can read it.
    """
    if len(page.get_text("text").strip()) >= MIN_TEXT_CHARS:
        return [page.get_text("words")], False
    if engines is None:
        engines = ocr.usable_engines()
    sets: list[list] = []
    for engine_ in engines:
        key = _ocr_key(path, index)
        key = key + (engine_.name,) if key is not None else None
        words = None
        if key is not None:
            with _ocr_cache_lock:
                words = _ocr_cache.get(key)
                if words is not None:
                    _ocr_cache.move_to_end(key)
        if words is None:
            words = _ocr_words(page, engine_)
            if words is None:
                if not sets:
                    return None, True        # the preferred engine failed: refuse
                continue
            if key is not None:
                with _ocr_cache_lock:
                    _ocr_cache[key] = words
                    while len(_ocr_cache) > OCR_CACHE_PAGES:
                        _ocr_cache.popitem(last=False)
        sets.append(words)
    return (sets or None), True


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
            sets, scanned = _page_words(page, path, index)
            for number, words in enumerate(sets or [[]]):
                label = f"page {index + 1}" + (f" (ocr {number + 1})" if number else "")
                if scanned:
                    text = _words_to_text(words)[0] if words else ""
                else:
                    text = page.get_text("text")
                out[label] = text
                rows, _spans = _words_to_text(_by_rows(words))
                if rows.strip():
                    out[f"{label} (rows)"] = rows
    return out


def iter_text_units(path: Path):
    """The exact per-page text :func:`process` will scan, in the same order."""
    with pymupdf.open(path) as doc:
        for index, page in enumerate(doc):
            sets, _scanned = _page_words(page, path, index)
            for words in sets or []:      # none: a scan nothing can read; refused
                for text, _spans in _readings(words):
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

# A lockbox is the institution's address, not anybody's home, and the line
# above it is the institution's name - "Write: Pioneer Trail Bank, N.A. /
# P.O. Box 6995 / Portland, OR 97228-6995". Walking up through it blacked the
# bank's name off every statement it printed on.
_LOCKBOX = re.compile(r"(?i)\b(?:P\.?\s?O\.?\s+box|post\s+office\s+box|lockbox)\b")
_INSTITUTION_LINE = re.compile(
    r"(?i)\b(?:bank|bancorp|credit\s+union|federal|n\.a\.|mortgage|servicing|lending"
    r"|insurance|financial|card\s+services|remit|payments?\s+to)\b")
ADDRESS_BLOCK_LINES = 4          # a name, two street lines and the city line
_UNIT_LINE = re.compile(r"(?i)^\s*(?:apt\.?|apartment|unit|suite|ste\.?|bldg\.?|#|p\.?\s?o\.?\s+box)\b")


def _numbered_but_not_an_address(line: str) -> bool:
    """A line with figures in it that is not a street, a unit or a box.

    Names carry no digits; a street opens with its house number. Anything else
    numbered - "December 15, 2020 · Page 1 of 4" set just above the holder - is
    the page's own header, and taking it into the block deleted it.
    """
    if not any(ch.isdigit() for ch in line):
        return False
    return not (re.match(r"^\s*\d", line) or _UNIT_LINE.match(line))
ADDRESS_LINE_CHARS = 48          # an address line is short; prose is not
ADDRESS_BLOCK_PAD = 2.0


def _segments(words) -> list[tuple[str, pymupdf.Rect]]:
    """Visual lines split at column gaps: one entry per cell of each line."""
    out: list[tuple[str, pymupdf.Rect]] = []
    for row in _row_groups(words):
        for text, x0, x1 in _cells(row):
            members = [w for w in row if w[0] >= x0 - 0.01 and w[2] <= x1 + 0.01]
            rect = pymupdf.Rect(members[0][:4])
            for w in members[1:]:
                rect |= pymupdf.Rect(w[:4])
            out.append((text, rect))
    return out


def address_blocks(words, ocr: bool = False) -> list[pymupdf.Rect]:
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

    Blunt about height, not about width. The walk follows the address's own
    column: a utility bill prints "Amount due $116.31" and "Due 02/26/2025" on
    the same visual lines as the remittance address, two inches to the right,
    and taking whole lines boxed the amount due off every bill in the corpus.
    """
    segments = _segments(words)
    blocks: list[pymupdf.Rect] = []
    for text, rect in segments:
        if not _ZIP_LINE.search(text) or _MONEY_ON_LINE.search(text):
            continue
        # "Redfield Retail Bank, PO Box 669804, Dallas, TX 75266-0781" is where
        # the payment goes, on one line with the issuer's name; the PO box
        # pattern still takes the box number, and the issuer stays readable
        if _LOCKBOX.search(text):
            continue
        block = pymupdf.Rect(rect)
        height = max(rect.height, 1.0)
        top = rect
        for _step in range(ADDRESS_BLOCK_LINES):
            above = [(t, r) for t, r in segments
                     if r.y1 <= top.y0 + height * 0.5 and r.y0 < top.y0
                     and min(r.x1, block.x1) - max(r.x0, block.x0) > 0]
            if not above:
                break
            line, line_rect = max(above, key=lambda item: item[1].y0)
            if (len(line) > ADDRESS_LINE_CHARS
                    or _MONEY_ON_LINE.search(line)
                    or not _ADDRESS_LINE.match(line)
                    or _numbered_but_not_an_address(line)
                    # must be the line directly above, not across a gap
                    or top.y0 - line_rect.y0 > height * 2.2
                    or _INSTITUTION_LINE.search(line)):
                break
            block |= line_rect
            top = line_rect
            if _LOCKBOX.search(line):
                break
        if ocr:
            blocks.append(block + (-ADDRESS_BLOCK_PAD, -ADDRESS_BLOCK_PAD,
                                   ADDRESS_BLOCK_PAD, ADDRESS_BLOCK_PAD))
        else:
            trim = height * TEXT_BOX_INSET
            blocks.append(pymupdf.Rect(block.x0 - ADDRESS_BLOCK_PAD, block.y0 + trim,
                                       block.x1 + ADDRESS_BLOCK_PAD, block.y1 - trim))
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
        for index, page in enumerate(doc):
            sets, _scanned = _page_words(page, path, index)
            for words in sets or []:         # none: an unreadable scan
                yield from _tables_on(words)


def _tables_on(words):
    """The column runs on one page's words - see :func:`iter_tables`."""
    rows = [_cells(row) for row in _row_groups(words)]
    rows = [row for row in rows if any(text for text, _a, _b in row)]
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
    #
    # Two refinements, both about where a table starts. A lone figure -
    # the opening balance printed under the Balance column, between the
    # heading and the first transaction - is a value in one column, not
    # prose, so it does not end the run; ending it there separated a
    # Venmo ledger from its From / To heading. And a heading row part
    # way down a run starts a new table, so the heading is row 0 of
    # the rows it heads rather than of the letterhead above it.
    block: list = []
    for row in rows:
        if len(row) >= 2 or (block and _is_figure(row)):
            if _is_heading(row) and block and not _is_heading(block[-1]):
                if len(block) >= 2:
                    yield _align(block)
                block = []
            block.append(row)
            continue
        if len(block) >= 2:
            yield _align(block)
        block = []
    if len(block) >= 2:
        yield _align(block)


def _row_groups(words) -> list[list]:
    """Words grouped into visual rows, each sorted left to right."""
    rows: dict[int, list] = {}
    for word in _by_rows(words):
        rows.setdefault(word[6], []).append(word)
    return [rows[key] for key in sorted(rows)]


def _cells(row) -> list[tuple[str, float, float]]:
    """One visual row split into cells where the gap is a column break."""
    cells: list[list] = []
    previous_x1 = None
    previous_char = 1.0
    for x0, _y0, x1, _y1, word, *_rest in row:
        if previous_x1 is not None and (x0 - previous_x1) < previous_char * 1.4:
            cells[-1][0].append(word)
            cells[-1][2] = x1
        else:
            cells.append([[word], x0, x1])
        previous_x1 = x1
        previous_char = max((x1 - x0) / max(len(word), 1), 1.0)
    return [(" ".join(words), x0, x1) for words, x0, x1 in cells]


_FIGURE = re.compile(r"^[\s$()+\-.,%\d]+$")


def _is_figure(row) -> bool:
    return len(row) == 1 and bool(_FIGURE.match(row[0][0]))


def _is_heading(row) -> bool:
    """Three or more cells and not a digit among them: a column heading row."""
    texts = [text for text, _a, _b in row if text]
    return len(texts) >= 3 and not any(ch.isdigit() for t in texts for ch in t)


def _align(block: list) -> list[list[str]]:
    """Cells placed under the column they sit in, blanks where one is empty.

    A Venmo row with no memo has one cell fewer than its heading, and read by
    position every cell after the gap slides one column left - the payee lands
    under "Note" and the amount under "To". The columns are taken from the row
    with the most cells (the heading, when there is one) and every cell goes to
    the column it overlaps most, or the nearest one. Merging every row's
    extents instead let one overflowing cell - "Merchant Transacti" running
    into the Status column - fuse two columns for the whole table.
    """
    reference = max(block, key=len)
    columns = [(x0, x1) for _text, x0, x1 in reference]
    if len(columns) < 2:
        return [[text for text, _a, _b in row] for row in block]

    def place(x0: float, x1: float) -> int:
        overlaps = [min(x1, b) - max(x0, a) for a, b in columns]
        best = max(range(len(columns)), key=lambda k: overlaps[k])
        if overlaps[best] > 0:
            return best
        middle = (x0 + x1) / 2
        return min(range(len(columns)),
                   key=lambda k: abs((columns[k][0] + columns[k][1]) / 2 - middle))

    out: list[list[str]] = []
    for row in block:
        cells = [""] * len(columns)
        for text, x0, x1 in row:
            column = place(x0, x1)
            cells[column] = f"{cells[column]} {text}".strip()
        out.append(cells)
    return out


def caption_sources(path: Path) -> dict[str, str]:
    """Page one's caption block, plus the running header strip of later pages."""
    sources: dict[str, str] = {}
    with pymupdf.open(path) as doc:
        if doc.page_count == 0:
            return sources
        first = doc[0]
        sets, scanned = _page_words(first, path, 0)
        sources[f"{path.name} [caption]"] = (
            "\n".join(_words_to_text(words)[0] for words in sets or []) if scanned
            else first.get_text("text"))
        header_text: list[str] = []
        for index in range(1, min(doc.page_count, 6)):
            page = doc[index]
            strip = pymupdf.Rect(0, 0, page.rect.width, page.rect.height * 0.15)
            sets, scanned = _page_words(page, path, index)
            if scanned:
                for words in sets or []:
                    inside = [w for w in words if w[3] <= strip.y1]
                    header_text.append(_words_to_text(inside)[0])
            else:
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


# --------------------------------------------------------------------------
# anonymizing: the replacement written where the original was
# --------------------------------------------------------------------------

# What the delivered file says about itself, in its document properties rather
# than on the page.
ANONYMIZED_NOTE = ("Anonymized copy: names, account numbers and other identifiers "
                   "have been replaced. Figures, dates and merchants are as issued.")
# A replacement wider than the space it replaces is squeezed sideways, never
# below this fraction of its natural width; past that the type gets smaller.
MIN_SQUEEZE = 0.55


@dataclass
class _Edit:
    words: list                      # the original word rectangles, one line
    text: str                        # what goes there; "" removes only
    hit: Hit


def _runs(hits: list[Hit], spans) -> list[tuple[list, list[Hit]]]:
    """The words the hits touch, as runs of whole words on one line each.

    A hit rarely lines up with word boundaries - "INDN:OAKLEY V WARD" is three
    words, the first only half a name - so the unit that is removed and
    rewritten is the run of whole words, and its new text keeps whatever part
    of them no hit covered. Hits touching the same words share a run, so two
    names inside one word are both replaced, never one of them left standing
    in the other's leftovers. A hit that wraps onto the next line writes its
    replacement on the first line and clears the rest.
    """
    touched: dict[int, list[Hit]] = {}
    for hit in hits:
        for index, (start, end, _rect) in enumerate(spans):
            if start < hit.end and hit.start < end:
                touched.setdefault(index, []).append(hit)
    runs: list[tuple[list, list[Hit]]] = []
    current: list[int] = []
    for index in sorted(touched):
        if current:
            last = spans[current[-1]][2]
            this = spans[index][2]
            same_line = min(last.y1, this.y1) - max(last.y0, this.y0) > 0.5 * min(last.height, this.height)
            shares_hit = any(h in touched[current[-1]] for h in touched[index])
            if index == current[-1] + 1 and same_line and shares_hit:
                current.append(index)
                continue
            runs.append(current)
        current = [index]
    if current:
        runs.append(current)
    out = []
    for run in runs:
        run_hits: list[Hit] = []
        for index in run:
            for hit in touched[index]:
                if hit not in run_hits:
                    run_hits.append(hit)
        out.append(([spans[i] for i in run], sorted(run_hits, key=lambda h: h.start)))
    return out


def _rewrite(text: str, run: list, hits: list[Hit]) -> str:
    """The run's own text with every hit in it replaced."""
    start, end = run[0][0], run[-1][1]
    pieces, cursor = [], start
    for hit in hits:
        if hit.end <= cursor:
            continue
        lo, hi = max(hit.start, start), min(hit.end, end)
        pieces.append(text[cursor:lo])
        # the replacement goes where the hit starts; a wrapped tail clears
        if hit.start >= start:
            pieces.append(hit.replacement)
        cursor = hi
    pieces.append(text[cursor:end])
    return " ".join("".join(pieces).split())


def _same_word(a: pymupdf.Rect, b: pymupdf.Rect, share: float = 0.5) -> bool:
    overlap = a & b
    return not overlap.is_empty and overlap.get_area() > share * min(a.get_area(), b.get_area())


def _union(rects) -> pymupdf.Rect:
    out = pymupdf.Rect(rects[0])
    for r in rects[1:]:
        out |= r
    return out


_BASE14 = {  # (serif, mono, bold, italic) -> PyMuPDF's built-in font
    (False, False, False, False): "helv", (False, False, False, True): "heit",
    (False, False, True, False): "hebo", (False, False, True, True): "hebi",
    (True, False, False, False): "tiro", (True, False, False, True): "tiit",
    (True, False, True, False): "tibo", (True, False, True, True): "tibi",
}
_MONO = {False: {False: "cour", True: "coit"}, True: {False: "cobo", True: "cobi"}}


def _style(span: dict) -> tuple[str, float, tuple, tuple, tuple]:
    """(built-in font, size, colour, baseline origin, direction) for a span.

    The original font is usually a subset - only the letters the issuer used
    are in the file - so the replacement is set in the nearest of the fourteen
    standard faces, at the original size, weight, slant and colour.
    """
    name = span.get("font", "").casefold()
    flags = span.get("flags", 0)
    bold = bool(flags & 16) or any(k in name for k in ("bold", "black", "heavy", "semibold"))
    italic = bool(flags & 2) or "italic" in name or "oblique" in name
    mono = bool(flags & 8) or "courier" in name or "mono" in name
    serif = (bool(flags & 4) or "times" in name or "serif" in name
             or "garamond" in name or "georgia" in name) and "sans" not in name
    font = _MONO[bold][italic] if mono else _BASE14[(serif, False, bold, italic)]
    colour = span.get("color", 0)
    rgb = (((colour >> 16) & 255) / 255, ((colour >> 8) & 255) / 255, (colour & 255) / 255)
    return font, float(span.get("size", 9)), rgb, tuple(span.get("origin", (0, 0))), \
        tuple(span.get("dir", (1, 0)))


def _span_at(spans: list[dict], rect: pymupdf.Rect) -> dict | None:
    best, area = None, 0.0
    for span in spans:
        overlap = pymupdf.Rect(span["bbox"]) & rect
        if not overlap.is_empty and overlap.get_area() > area:
            best, area = span, overlap.get_area()
    return best


def _write(page: pymupdf.Page, edit: _Edit, span: dict, right_limit: float) -> bool:
    """Set ``edit.text`` on the original baseline. False if it cannot be."""
    font, size, rgb, origin, direction = _style(span)
    if abs(direction[0] - 1) > 0.01 or abs(direction[1]) > 0.01:
        return False                    # rotated text: the caller boxes it instead
    start = pymupdf.Point(edit.words[0].x0, origin[1])
    natural = pymupdf.get_text_length(edit.text, fontname=font, fontsize=size)
    room = max(right_limit - start.x, 1.0)
    squeeze = min(1.0, room / natural) if natural else 1.0
    if squeeze < MIN_SQUEEZE:
        size *= squeeze / MIN_SQUEEZE
        squeeze = MIN_SQUEEZE
    morph = (start, pymupdf.Matrix(squeeze, 1)) if squeeze < 0.999 else None
    page.insert_text(start, edit.text, fontname=font, fontsize=size, color=rgb, morph=morph)
    return True


# A recognised word that begins or ends with a registered name is that name
# with its neighbours glued on. RapidOCR returns a line with its spaces lost
# as often as not - "NATHANCBLACKBURN", "fromBank***1711" - and no pattern
# with word boundaries can find a name inside one token. Scans only, and only
# names of five letters or more, so "Ross" is never found inside "ACROSS".
GLUED_NAME_MIN = 5


def _glued_names(words, store: MappingStore, settings: Settings):
    """(hit, rect) for every OCR token that carries a person's name glued in."""
    people = []
    for entity in store.persons():
        if not entity.enabled or not settings.category_enabled(entity.category):
            continue
        person = entity.person
        if person is None:
            continue
        bits = [re.sub(r"[^a-z]", "", bit.casefold())
                for piece in (person.first, *person.middles, person.last)
                for bit in re.split(r"[-\s]", piece or "")]
        people.append((entity, [b for b in bits if b]))
    if not people:
        return []
    out = []
    for x0, y0, x1, y1, text, *_rest in words:
        letters = re.sub(r"[^a-z]", "", text.casefold())
        # "REBECCALBLACKBURN" carries two Blackburns' surname; the one whose
        # other names are in it too is the one it is
        best, score = None, 0
        for entity, bits in people:
            if not any(len(b) >= GLUED_NAME_MIN and len(letters) > len(b)
                       and (letters.startswith(b) or letters.endswith(b)) for b in bits):
                continue
            explained = sum(len(b) for b in bits if b in letters)
            if explained > score:
                best, score = entity, explained
        if best is None:
            continue
        rect = pymupdf.Rect(x0, y0, x1, y1) + (-OCR_BOX_PADDING, -OCR_BOX_PADDING,
                                                OCR_BOX_PADDING, OCR_BOX_PADDING)
        replacement = _names.match_case(text, best.replacement) if best.replacement else ""
        out.append((Hit(0, len(text), text, best.category, replacement or engine.REDACTION_TEXT,
                        best.key, engine.FUZZY_PRIORITY), rect))
    return out


def _plan_edits(word_sets: list[list], store, anon_settings, matcher, settings,
                result: PdfResult, document_label: str, ocr: bool = False) -> list[_Edit]:
    """Every replacement on one page's words, each word claimed once.

    Each word list is read both ways, as on the redact path, and a word is
    claimed by the first hit that reaches it: a later hit over words already
    taken writes nothing more, and only removes whatever it covers that nothing
    else did. No word is ever written twice, and none is ever left. On a scan
    every OCR engine's reading is planned, and names OCR glued to their
    neighbours are taken whole.
    """
    claimed: list[pymupdf.Rect] = []
    edits: list[_Edit] = []
    counted: list[Hit] = []

    # two OCR engines box the same word differently - one a word, the other a
    # slice of a whole line - so on a scan a smaller overlap is the same word
    share = 0.25 if ocr else 0.5

    def claim(line: list[pymupdf.Rect]) -> list[pymupdf.Rect]:
        fresh = [w for w in line if not any(_same_word(w, c, share) for c in claimed)]
        claimed.extend(fresh)
        return fresh

    for words in word_sets:
        for text, spans in _readings(words):
            hits = engine.scan_text(text, store, anon_settings, matcher)
            for run, run_hits in _runs(hits, spans):
                words_in = [rect for _s, _e, rect in run]
                fresh = claim(words_in)
                if not fresh:
                    continue
                counted.extend(h for h in run_hits if h not in counted)
                if len(fresh) == len(words_in):
                    say = _rewrite(text, run, run_hits)
                else:
                    say, words_in = "", fresh
                edits.append(_Edit(words_in, say, run_hits[0]))
        if ocr:
            for hit, rect in _glued_names(words, store, settings):
                rect = rect + (OCR_BOX_PADDING, OCR_BOX_PADDING, -OCR_BOX_PADDING, -OCR_BOX_PADDING)
                if claim([rect]):
                    edits.append(_Edit([rect], hit.replacement, hit))
                    counted.append(hit)
        # the mailing block: whatever in it no hit took is removed, so a name
        # that slipped every pattern still does not ship
        if settings.category_enabled("street_address"):
            for block in address_blocks(words, ocr=True):
                for x0, y0, x1, y1, word, *_rest in words:
                    rect = pymupdf.Rect(x0, y0, x1, y1)
                    centre = pymupdf.Point((x0 + x1) / 2, (y0 + y1) / 2)
                    if word and block.contains(centre) and claim([rect]):
                        edits.append(_Edit([rect], "", Hit(0, 0, word, "street_address",
                                                           "", "", 0)))
    for hit in counted:
        result.hits.append(hit)
        entity = store.entities.get(hit.entity_key)
        if entity is not None:
            store.record_hit(entity, document_label)
    return edits


# How an image with no text in it is marked, once it has been whited out
IMAGE_PLACEHOLDER = "[IMAGE]"
# An image whose OCR finds fewer words than this is a photo, a logo or a
# signature, not a picture of text, and is replaced whole.
MIN_IMAGE_WORDS = 3
_BLACK = ("0 0 0 rg", (0.0, 0.0, 0.0))


# A word OCR read at or above this confidence (0-100) is print. Anything below
# is a guess - which is what OCR makes of handwriting - and is treated as unread.
INK_CONFIDENCE = 60.0
INK_DPI = 100
INK_THRESHOLD = 165          # grey level below which a pixel is ink
SOLID_INK = 0.6              # a component more filled than this is a bar or a box
RULE_ASPECT = 15             # longer than this many times its thickness: a rule
THIN_INK = 3.0               # points; ink thinner than this either way is a rule
HANDWRITING_MARK = "[HANDWRITING]"
_WHITE = ("1 1 1 rg", (1.0, 1.0, 1.0))


def _confident(word) -> bool:
    return (word[8] if len(word) > 8 else 100.0) >= INK_CONFIDENCE


def _unread_ink(page: pymupdf.Page, region: pymupdf.Rect, word_sets: list[list],
                cleared: list[pymupdf.Rect]) -> list[pymupdf.Rect]:
    """Rectangles over ink in ``region`` that no confident OCR word accounts for.

    OCR reads machine print. Handwriting, a signature, a stamp or a margin note
    comes back as nothing, or as a low-confidence guess, and so the text pass
    can neither find a name in it nor replace one - while a person, or a
    language model looking at the page, can read it perfectly well. So the ink
    itself is measured: every dark pixel inside a confidently read word, or
    under an area already being replaced, is set aside, straight table rules
    are set aside, and whatever is left - strokes nobody read - is boxed.
    Solid shapes (a header bar, a filled box, a logo) are left; strokes are not.
    """
    import numpy as np
    import cv2

    pixmap = page.get_pixmap(dpi=INK_DPI, clip=region, colorspace=pymupdf.csGRAY)
    grey = np.frombuffer(pixmap.samples, dtype=np.uint8)
    grey = grey.reshape(pixmap.height, pixmap.stride)[:, :pixmap.width]
    ink = (grey < INK_THRESHOLD).astype(np.uint8)
    scale = INK_DPI / 72.0

    def blank(rect: pymupdf.Rect, pad: float = 1.5) -> None:
        x0 = max(int((rect.x0 - pad - region.x0) * scale), 0)
        y0 = max(int((rect.y0 - pad - region.y0) * scale), 0)
        x1 = min(int((rect.x1 + pad - region.x0) * scale) + 1, ink.shape[1])
        y1 = min(int((rect.y1 + pad - region.y0) * scale) + 1, ink.shape[0])
        if x1 > x0 and y1 > y0:
            ink[y0:y1, x0:x1] = 0

    for words in word_sets:
        for word in words:
            if word[4].strip() and _confident(word):
                blank(pymupdf.Rect(word[:4]), pad=OCR_BOX_PADDING)
    for rect in cleared:
        blank(rect)
    reach = max(15, int(30 * scale))
    rules = (cv2.morphologyEx(ink, cv2.MORPH_OPEN,
                              cv2.getStructuringElement(cv2.MORPH_RECT, (reach, 1)))
             | cv2.morphologyEx(ink, cv2.MORPH_OPEN,
                                cv2.getStructuringElement(cv2.MORPH_RECT, (1, reach))))
    ink = ink & (1 - rules)
    join = int(4 * scale) + 1
    grown = cv2.dilate(ink, cv2.getStructuringElement(cv2.MORPH_RECT, (join, join)))
    count, _labels, stats, _centres = cv2.connectedComponentsWithStats(grown, connectivity=8)
    boxes: list[pymupdf.Rect] = []
    for index in range(1, count):
        x, y, w, h, _area = (int(v) for v in stats[index])
        strokes = int(ink[y:y + h, x:x + w].sum())
        if strokes < 15 or w < 3 or h < 3:
            continue                        # specks and scanner noise
        if strokes / float(w * h) > SOLID_INK:
            continue                        # a bar, a filled box, a logo
        if max(w, h) > RULE_ASPECT * min(w, h):
            continue                        # a rule a scan has skewed off straight
        rows, cols = np.nonzero(ink[y:y + h, x:x + w])
        if (rows.max() - rows.min() + 1) < THIN_INK * scale or \
                (cols.max() - cols.min() + 1) < THIN_INK * scale:
            continue                        # the stub of a rule; writing is taller
        boxes.append(pymupdf.Rect(region.x0 + x / scale - 1, region.y0 + y / scale - 1,
                                  region.x0 + (x + w) / scale + 1,
                                  region.y0 + (y + h) / scale + 1))
    return boxes


def _ocr_layer(word_sets: list[list], edits: list[_Edit],
               marks: list[pymupdf.Rect] = ()) -> list[tuple]:
    """A scan's text layer, anonymized, in reading order.

    The words OCR read are laid back over the image as invisible text, so a
    parser - and a language model behind it - reads the page; a scanned page
    left as a picture gives a text pipeline nothing at all. Every replacement
    is drawn visibly, on the white patch where the original was. A word any
    edit touched is never laid down, whichever engine read it.
    """
    pad = (-OCR_BOX_PADDING, -OCR_BOX_PADDING, OCR_BOX_PADDING, OCR_BOX_PADDING)
    covers = [_union(edit.words) + pad for edit in edits]
    pieces: list[tuple] = []          # (rect, text, visible)
    for x0, y0, x1, y1, word, *rest in (word_sets[0] if word_sets else []):
        rect = pymupdf.Rect(x0, y0, x1, y1)
        # a low-confidence word is OCR's guess at handwriting: never laid down
        if not word.strip() or not _confident((x0, y0, x1, y1, word, *rest)):
            continue
        if any(_same_word(rect, c) or c.intersects(rect) for c in [*covers, *marks]):
            continue
        pieces.append((rect, word, False))
    for edit in edits:
        if edit.text:
            pieces.append((_union(edit.words), edit.text, True))
    for mark in marks:
        pieces.append((mark, HANDWRITING_MARK, None))
    # order them as the page reads, row by row
    rows = _by_rows([(r.x0, r.y0, r.x1, r.y1, str(i), 0, 0, 0)
                     for i, (r, _t, _v) in enumerate(pieces)])
    plan = []
    for row in rows:
        rect, text, visible = pieces[int(row[4])]
        if visible is None:
            # the black box over unread ink, labelled so a reader knows what is gone
            size = max(3.0, min(8.0, rect.height * 0.6))
            plan.append(("helv", size, _WHITE, 0, rect.x0 + 1.0,
                         rect.y0 + (rect.height + size * 0.7) / 2, text, -(rect.width - 2.0)))
            continue
        height = max(rect.height, 4.0)
        size = height * (0.8 if visible else 0.85)
        baseline = rect.y1 - height * 0.2
        plan.append(("helv", size, _BLACK, 0 if visible else 3, rect.x0, baseline, text,
                     -(rect.width + 2.0) if visible else rect.width))
    return plan


def _anonymize_scan(page, word_sets, store, anon_settings, matcher, settings,
                    report: PageReport, result: PdfResult, document_label: str) -> None:
    """A scanned page: white out each hit, write its replacement, add a text layer."""
    edits = _plan_edits(word_sets, store, anon_settings, matcher, settings, result,
                        document_label, ocr=True)
    report.hits = len(result.hits)
    marks = _white_out(page, page.rect, word_sets, edits, report)
    if report.boxes:
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
    _draw_plan(page, _ocr_layer(word_sets, edits, marks))
    if marks:
        result.warnings.append(
            f"page {report.number}: {len(marks)} area(s) of handwriting or unread ink "
            "blacked out.")


def _white_out(page, region, word_sets, edits, report: PageReport) -> list[pymupdf.Rect]:
    """Queue the redactions for one OCR'd region: white under each replacement,
    black over every patch of unread ink. Returns the black boxes."""
    pad = (-OCR_BOX_PADDING, -OCR_BOX_PADDING, OCR_BOX_PADDING, OCR_BOX_PADDING)
    cleared = [_union(edit.words) + pad for edit in edits]
    marks = _unread_ink(page, region, word_sets, cleared)
    for rect in cleared:
        page.add_redact_annot(rect, fill=(1, 1, 1), cross_out=False)
        report.boxes += 1
    for rect in marks:
        page.add_redact_annot(rect, fill=(0, 0, 0), cross_out=False)
        report.boxes += 1
    return marks


def _ocr_region(page: pymupdf.Page, rect: pymupdf.Rect, engines) -> list[list]:
    """Every engine's words for one region of a page, in page coordinates."""
    from PIL import Image

    pixmap = page.get_pixmap(dpi=OCR_DPI, clip=rect, colorspace=pymupdf.csRGB)
    image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    scale = OCR_DPI / 72.0
    sets = []
    for engine_ in engines:
        try:
            read = engine_.read(image)
        except Exception:
            continue
        sets.append([(rect.x0 + w.x0 / scale, rect.y0 + w.y0 / scale,
                      rect.x0 + w.x1 / scale, rect.y0 + w.y1 / scale,
                      w.text, w.block, w.line, w.word, w.confidence) for w in read])
    return sets


def _anonymize_images(page, store, anon_settings, matcher, settings, report: PageReport,
                      result: PdfResult, document_label: str) -> list[tuple]:
    """Images on a text page: their text anonymized like a scan's, or replaced.

    Each image is read by OCR. One that holds text - a pasted statement, a
    check image - has its hits whited out and replaced, and its words added to
    the page's text layer. One with almost no text - a photo, a logo, a
    signature - is whited out whole and marked [IMAGE]. With no OCR engine an
    image is blacked out, as when redacting. Returns the pieces to draw once
    the redactions are applied.
    """
    engines = ocr.usable_engines() if settings.ocr_scanned_pdfs else []
    plan: list[tuple] = []
    read_any = False
    inked = 0
    for image_info in page.get_images(full=True):
        try:
            rects = page.get_image_rects(image_info[0])
        except Exception:                          # pragma: no cover - defensive
            continue
        for rect in rects:
            if rect.is_empty or rect.is_infinite:
                continue
            report.images_redacted += 1
            sets = _ocr_region(page, rect, engines) if engines else []
            if not sets:
                page.add_redact_annot(rect, fill=(0, 0, 0))
                report.boxes += 1
                continue
            read_any = True
            if max(len(words) for words in sets) < MIN_IMAGE_WORDS:
                page.add_redact_annot(rect, fill=(1, 1, 1), cross_out=False)
                report.boxes += 1
                size = max(4.0, min(10.0, rect.height * 0.3))
                plan.append(("helv", size, _BLACK, 0, rect.x0 + 2.0,
                             rect.y0 + (rect.height + size) / 2, IMAGE_PLACEHOLDER,
                             -(rect.width - 4.0)))
                continue
            edits = _plan_edits(sets, store, anon_settings, matcher, settings, result,
                                document_label, ocr=True)
            marks = _white_out(page, rect, sets, edits, report)
            plan.extend(_ocr_layer(sets, edits, marks))
            inked += len(marks)
    result.images_redacted += report.images_redacted
    if read_any:
        result.warnings.append(
            f"page {report.number}: images were read by OCR and anonymized; "
            f"{inked} area(s) of handwriting or unread ink in them blacked out.")
    return plan


def _anonymize_page(page, words, store, anon_settings, matcher, settings,
                    report: PageReport, result: PdfResult, document_label: str) -> None:
    """Replace every hit on a text page with its replacement, in place.

    Both readings of the page are scanned, as on the redact path, and a word is
    claimed by the first hit that reaches it: a later hit over words already
    taken writes nothing more, and only removes whatever it covers that nothing
    else did. No word is ever written twice, and none is ever left.
    """
    trace = page.get_texttrace()             # the page's text as drawn, before any edit
    edits = _plan_edits([words], store, anon_settings, matcher, settings, result,
                        document_label)
    report.hits = len(result.hits)

    spans = [span for block in page.get_text("dict")["blocks"]
             for line in block.get("lines", []) for span in line["spans"]]
    rows = _row_groups(words)
    planned: list[tuple[_Edit, dict, float]] = []
    for edit in edits:
        line = _union(edit.words)
        span = _span_at(spans, edit.words[0])
        # the room a replacement may take: up to the next word on its row
        row = next((r for r in rows if any(_same_word(pymupdf.Rect(w[:4]), edit.words[0])
                                           for w in r)), [])
        after = [w[0] for w in row if w[0] >= line.x1 - 0.5
                 and not any(_same_word(pymupdf.Rect(w[:4]), e) for e in edit.words)]
        limit = (min(after) - 2.0) if after else max(line.x1, page.rect.x1 - 18.0)
        limit = max(limit, line.x1)
        rotated = span is not None and tuple(span.get("dir", (1, 0))) != (1.0, 0.0)
        page.add_redact_annot(_inset(line, BOX_PADDING),
                              fill=(0, 0, 0) if rotated else False, cross_out=False)
        report.boxes += 1
        if edit.text and span is not None and not rotated:
            planned.append((edit, span, limit))

    image_plan = (_anonymize_images(page, store, anon_settings, matcher, settings,
                                    report, result, document_label)
                  if settings.redact_images else [])

    if report.boxes:
        # keep the table rules and row shading the replacement sits on
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
    if not edits:
        _draw_plan(page, image_plan)
        return
    limits = {id(edit): limit for edit, _span, limit in planned}
    if _rebuild(page, trace, edits, limits):
        _draw_plan(page, image_plan)
        return
    # The page could not be rebuilt - rotated text, a character the standard
    # faces cannot encode, text that would not come off cleanly. The hits are
    # already gone; the replacements are written on top instead, which reads
    # right on the page and to a parser that orders by position, but a parser
    # that reads in file order finds them at the end of the page.
    result.warnings.append(
        f"page {report.number}: replacements were added on top of the page rather than "
        "in its text order; a parser that reads in file order finds them at the end of the page.")
    for edit, span, limit in planned:
        _write(page, edit, span, limit)
    _draw_plan(page, image_plan)


def _colour_op(space: int, colour, stroke: bool) -> str:
    values = " ".join(f"{c:.4f}" for c in colour or (0,))
    if space == 1 or len(colour or ()) == 1:
        return f"{values} {'G' if stroke else 'g'}"
    if space == 4 or len(colour or ()) == 4:
        return f"{values} {'K' if stroke else 'k'}"
    return f"{values} {'RG' if stroke else 'rg'}"


def _rgb(space: int, colour) -> tuple[float, float, float]:
    colour = tuple(colour or (0,))
    if space == 1 or len(colour) == 1:
        return (colour[0],) * 3
    if space == 4 or len(colour) == 4:
        c, m, y, k = colour
        return ((1 - c) * (1 - k), (1 - m) * (1 - k), (1 - y) * (1 - k))
    return colour[:3]


def _encodable(text: str) -> bool:
    try:
        text.encode("cp1252")
    except UnicodeEncodeError:
        return False
    return True


_UNICODE_FONT: list = []


def _unicode_font(text: str):
    """The bundled Unicode face, if it has every character of ``text``."""
    if not _UNICODE_FONT:
        try:
            _UNICODE_FONT.append(pymupdf.Font("cjk"))
        except Exception:                          # pragma: no cover - build dependent
            _UNICODE_FONT.append(None)
    face = _UNICODE_FONT[0]
    if face is None or not all(face.has_glyph(ord(ch)) for ch in text if not ch.isspace()):
        return None
    return face


def _draw_plan(page: pymupdf.Page, plan: list[tuple]) -> None:
    """Draw ``(font, size, (colour op, rgb), render, x, baseline, text, width)``
    pieces in order. A positive width is the original's, matched exactly; a
    negative one is the room a replacement may take, squeezed only to fit it."""
    if not plan:
        return
    page.wrap_contents()
    document = page.parent
    to_pdf = ~page.transformation_matrix
    inserted: set[str] = set()
    ops: list[str] = []

    def flush() -> None:
        # each batch of standard-face text is its own stream, appended in turn,
        # so drawing order survives the occasional Unicode piece between them
        if not ops:
            return
        xref = document.get_new_xref()
        document.update_object(xref, "<<>>")
        document.update_stream(xref, "\n".join(["q", *ops, "Q"]).encode("latin-1"))
        contents = page.get_contents() + [xref]
        document.xref_set_key(page.xref, "Contents",
                              "[" + " ".join(f"{c} 0 R" for c in contents) + "]")
        ops.clear()

    for font, size, (colour, rgb), render, x, y, text, width in plan:
        unicode_font = None if _encodable(text) else _unicode_font(text)
        measure = (unicode_font.text_length(text, fontsize=size) if unicode_font
                   else pymupdf.get_text_length(text, fontname=font, fontsize=size))
        if not measure:
            continue
        if width >= 0:                                  # original text: its own width
            scale = width / measure if width > 0 else 1.0
        else:                                           # a replacement: fit the room
            scale = min(1.0, -width / measure)
            if scale < MIN_SQUEEZE:
                size *= scale / MIN_SQUEEZE
                scale = MIN_SQUEEZE
        scale = max(0.3, min(scale, 3.0))
        if unicode_font is not None:
            flush()
            origin = pymupdf.Point(x, y)
            writer = pymupdf.TextWriter(page.rect)
            writer.append(origin, text, font=unicode_font, fontsize=size)
            writer.write_text(page, color=rgb, render_mode=render,
                              morph=(origin, pymupdf.Matrix(scale, 1)))
            continue
        if font not in inserted:
            page.insert_font(fontname=font)
            inserted.add(font)
        at = pymupdf.Point(x, y) * to_pdf
        ops.append(f"BT /{font} {size:.3f} Tf {scale * 100:.2f} Tz {render} Tr {colour} "
                   f"1 0 0 1 {at.x:.3f} {at.y:.3f} Tm <{text.encode('cp1252').hex()}> Tj ET")
    flush()


def _rebuild(page: pymupdf.Page, trace: list[dict], edits: list[_Edit],
             limits: dict[int, float]) -> bool:
    """Rewrite the page's text in its original drawing order, edits in place.

    Text added to a PDF goes at the end of the page's content, so a replacement
    written on top of the page is found at the bottom of it by any parser that
    reads in file order - LangChain's PyPDFLoader, pypdf, PyMuPDF's own default
    - and the name lands after the page footer instead of in its transaction
    row. For a document headed for a language model that is the whole point
    lost. So every span of text is removed and drawn again, in the order the
    issuer drew it, each run set to its original width, with the replacement
    in the slot of the words it replaces. A parser that reads in file order and
    one that reads by position then both read the page as it was written.

    False - and nothing on the page changed by this function - when the page
    holds something the fourteen standard faces cannot draw.
    """
    if page.rotation:
        return False
    owners = [(edit, word) for edit in edits for word in edit.words]
    placed: set[int] = set()
    plan: list[tuple] = []        # (font, size, colour op, render, x, y, text, width)

    for span in sorted(trace, key=lambda t: t["seqno"]):
        if (span.get("type", 0) not in (0, 1, 2, 3) or span.get("wmode", 0)
                or tuple(round(d, 3) for d in span.get("dir", (1, 0))) != (1.0, 0.0)
                or (span.get("opacity") or 1.0) < 0.999):
            return False
        font = _style({"font": span.get("font", ""), "flags": span.get("flags", 0)})[0]
        size = float(span.get("size", 9))
        render = span.get("type", 0)
        colour = (_colour_op(span.get("colorspace", 3), span.get("color"), render in (1, 2)),
                  _rgb(span.get("colorspace", 3), span.get("color")))
        run: list = []

        def flush() -> None:
            # a fragment that is only the space after a replaced word draws
            # nothing, and drawn alone it reads as a blank line to a parser
            if run and "".join(chr(c[0]) for c in run).strip():
                # split where the standard faces stop being able to encode it,
                # so only a "■" itself, not its whole line, changes face
                pieces: list[list] = []
                for char in run:
                    fits = _encodable(chr(char[0]))
                    if pieces and _encodable(chr(pieces[-1][0][0])) == fits:
                        pieces[-1].append(char)
                    else:
                        pieces.append([char])
                for piece in pieces:
                    text = "".join(chr(c[0]) for c in piece)
                    width = piece[-1][3][2] - piece[0][3][0]
                    plan.append((font, size, colour, render, piece[0][2][0], piece[0][2][1],
                                 text, width))
            run.clear()

        for char in span["chars"]:
            box = char[3]
            centre = pymupdf.Point((box[0] + box[2]) / 2, (box[1] + box[3]) / 2)
            owner = next((edit for edit, word in owners if word.contains(centre)), None)
            if owner is None:
                run.append(char)
                continue
            flush()
            if id(owner) in placed:
                continue
            placed.add(id(owner))
            if owner.text:
                start = owner.words[0].x0
                room = max(limits.get(id(owner), owner.words[-1].x1) - start, 1.0)
                # a replacement for an invisible OCR layer is drawn visibly: the
                # scan under it has had the original pixels cleared
                plan.append((font, size, colour, 0 if render == 3 else render,
                             start, char[2][1], owner.text, -room))
        flush()
    if any(edit.text and id(edit) not in placed for edit in edits):
        return False
    # Every piece is drawn either in a standard face, or - for a character
    # those cannot encode, a bank's "■" logo or a name in another script -
    # in the bundled Unicode face. A piece neither can draw stops the rebuild.
    for item in plan:
        if not _encodable(item[6]) and _unicode_font(item[6]) is None:
            return False

    # Prove the remaining text comes off cleanly on a copy before touching
    # the real page: half-removed text with a full rewrite on top would read
    # every surviving word twice.
    probe = pymupdf.open()
    probe.insert_pdf(page.parent, from_page=page.number, to_page=page.number)
    trial = probe[0]
    trial.add_redact_annot(trial.rect, fill=False, cross_out=False)
    trial.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                           graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
    clean = not trial.get_text("text").strip()
    probe.close()
    if not clean:
        return False

    page.add_redact_annot(page.rect, fill=False, cross_out=False)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                          graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
    _draw_plan(page, plan)
    return True


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

    # The PDF's own mode decides what a hit becomes; the DOCX mode does not.
    # Scans are always redacted - invented text painted onto a photographed
    # page looks forged - so they scan with the redact settings either way.
    anonymize = settings.pdf_mode == "anonymize"
    redact_settings = Settings(**{**settings.__dict__, "docx_mode": "redact"})
    anon_settings = Settings(**{**settings.__dict__, "docx_mode": "anonymize"})
    anonymized_pages = 0

    ocr_ok, ocr_note = ocr_available()
    # probe once per document, not once per scanned page
    engines = ocr.usable_engines() if (settings.ocr_scanned_pdfs and ocr_ok) else []
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

            word_sets = [page.get_text("words")]
            page_text = page.get_text("text")
            report.text_chars = len(page_text.strip())

            if report.text_chars < MIN_TEXT_CHARS:
                report.image_only = True
                if settings.ocr_scanned_pdfs and ocr_ok:
                    word_sets, _scanned = _page_words(page, source, index, engines)
                    if word_sets is None:
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

            # OCR boxes come back in rendered (rotated) space; map them onto
            # the page's own coordinates or a rotated scan gets boxes in the
            # wrong place while the real values stay readable
            derotate = page.derotation_matrix if report.ocr_used else None
            padding = OCR_BOX_PADDING if report.ocr_used else BOX_PADDING

            if anonymize and not report.ocr_used:
                _anonymize_page(page, word_sets[0], store, anon_settings, matcher,
                                settings, report, result, document_label)
                anonymized_pages += 1
                result.pages.append(report)
                continue
            # a rotated scan keeps the redact path: its OCR boxes need mapping
            # back through the rotation, and a box is forgiving where text is not
            if anonymize and page.rotation == 0:
                _anonymize_scan(page, word_sets, store, anon_settings, matcher,
                                settings, report, result, document_label)
                anonymized_pages += 1
                result.pages.append(report)
                continue

            hits = []
            boxed: list = []
            readings = [reading for words in word_sets for reading in _readings(words)]
            for text, spans in readings:
                for hit in engine.scan_text(text, store, redact_settings, matcher):
                    rects = _rects_for_hit(hit, spans, padding, ocr=report.ocr_used)
                    # the second reading finds most things the first did; count
                    # each place once, so the report and review counts hold
                    if rects and all(any(r in b for b in boxed) for r in rects):
                        continue
                    boxed.extend(rects)
                    hits.append((hit, rects))
            if report.ocr_used:
                for words in word_sets:
                    for hit, rect in _glued_names(words, store, settings):
                        if any(rect in b for b in boxed):
                            continue
                        boxed.append(rect)
                        hits.append((hit, [rect]))
            report.hits = len(hits)

            for hit, rects in hits:
                entity = store.entities.get(hit.entity_key)
                if entity is not None:
                    store.record_hit(entity, document_label)
                for rect in rects:
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
                blocks = [b for words in word_sets
                          for b in address_blocks(words, ocr=report.ocr_used)]
                for rect in blocks:
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

            result.hits.extend(hit for hit, _rects in hits)
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

        if anonymize and ocr_pages_delivered:
            result.warnings.append(
                "Scanned pages were anonymized from their OCR: each name or number is "
                "whited out and replaced, and the page's text added beneath it so a "
                "parser can read it.")
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
        if anonymized_pages:
            # said in the document's properties, not on its pages
            meta = {k: v for k, v in (doc.metadata or {}).items()
                    if k in ("title", "author", "subject", "keywords", "creator",
                             "producer", "creationDate", "modDate", "trapped") and v}
            meta.update({"subject": ANONYMIZED_NOTE, "keywords": "anonymized"})
            doc.set_metadata(meta)

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

        # the bundled Unicode face is several megabytes; keep only the glyphs used
        if anonymized_pages and any("Droid Sans Fallback" in f[3]
                                    for page in doc for f in page.get_fonts()):
            doc.subset_fonts()
        output.parent.mkdir(parents=True, exist_ok=True)
        doc.save(output, garbage=4, deflate=True, clean=True)
    finally:
        doc.close()

    return result
