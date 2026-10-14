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
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from . import categories, engine, names as _names, ocr, patterns
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
# Up and down, an OCR box gets at most this share of its own height. Small type
# sits in lines a few points apart - a statement's 6-point rows touched - and
# three points above and below a town on a merchant line took the tops and
# bottoms off the balances on the rows either side. OCR's boxes are close
# around the ink up and down; the slack they need is along the line.
OCR_PAD_SHARE = 0.15
# A hit that fills less of one recognised word than this is boxed by its own
# share of the word's characters - see :func:`_rects_for_hit`.
WORD_SHARE_WHOLE = 0.6


def _pad_ocr(rect: pymupdf.Rect, padding: float = OCR_BOX_PADDING) -> pymupdf.Rect:
    """An OCR box with its slack: ``padding`` along the line, less across it."""
    rect = pymupdf.Rect(rect)
    up = min(padding, rect.height * OCR_PAD_SHARE)
    return pymupdf.Rect(rect.x0 - padding, rect.y0 - up, rect.x1 + padding, rect.y1 + up)


def _unpad_ocr(rect: pymupdf.Rect, padding: float = OCR_BOX_PADDING) -> pymupdf.Rect:
    """The box :func:`_pad_ocr` was given, from the box it returned."""
    rect = pymupdf.Rect(rect)
    padded = rect.height
    # padded = h + 2 * min(padding, h * share), whichever term is the smaller
    height = (padded - 2 * padding if padded - 2 * padding >= padding / OCR_PAD_SHARE
              else padded / (1 + 2 * OCR_PAD_SHARE))
    up = (padded - height) / 2
    return pymupdf.Rect(rect.x0 + padding, rect.y0 + up, rect.x1 - padding, rect.y1 - up)


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


ACROSS = (1.0, 0.0)              # the direction ordinary text reads in


def _axis(direction) -> str:
    """'across', 'down' or 'slanted': which page axis a line of text reads along.

    Upside-down text still reads across the page, and text turned a quarter
    either way reads down it; anything else runs at an angle to both axes.
    """
    dx, dy = direction
    if abs(abs(dx) - 1.0) < 0.01:
        return "across"
    if abs(abs(dy) - 1.0) < 0.01:
        return "down"
    return "slanted"


def _box(rect: pymupdf.Rect, padding: float, direction=ACROSS) -> pymupdf.Rect:
    """The redaction box for text-layer words, shaped to how the text runs.

    TEXT_BOX_INSET trims the box *across* the line, where neighbouring lines
    are. Applied to text set sideways - a statement's account number printed
    up the margin, a form's rotated column heading - the same trim took a
    quarter off each end of every word instead, along the line: "Oakley V
    Ward acct 3357 3537 2072" redacted to "O", "y", "d acct 3", "7 3". Text
    that reads down the page is therefore trimmed left and right and padded
    top and bottom; text at a slant has no axis that is safe to trim, so its
    box is padded on every side.
    """
    axis = _axis(direction)
    if axis == "across":
        return _inset(rect, padding)
    if axis == "down":
        trim = rect.width * TEXT_BOX_INSET
        return pymupdf.Rect(rect.x0 + trim, rect.y0 - padding, rect.x1 - trim, rect.y1 + padding)
    return rect + (-padding, -padding, padding, padding)


def _turned_lines(page: pymupdf.Page) -> list[tuple[pymupdf.Rect, tuple]]:
    """(box, direction) for every line of text on the page not set left to right.

    ``get_text("words")`` says nothing about direction; the line it came from
    does. PyMuPDF keeps the direction on the line, not on its spans.
    """
    out = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            direction = tuple(round(d, 3) for d in line.get("dir", ACROSS))
            if direction != ACROSS:
                out.append((pymupdf.Rect(line["bbox"]), direction))
    return out


def _direction_at(turned, rect: pymupdf.Rect) -> tuple:
    """The direction of the line ``rect`` sits on: ACROSS unless it is turned."""
    centre = pymupdf.Point((rect.x0 + rect.x1) / 2, (rect.y0 + rect.y1) / 2)
    for line, direction in turned:
        if line.contains(centre):
            return direction
    return ACROSS


def _rects_for_hit(hit: Hit, spans, padding: float = BOX_PADDING,
                   ocr: bool = False, turned=()) -> list[pymupdf.Rect]:
    """Boxes covering a hit, one per line the hit crosses.

    On a scan the box is padded outward on every side, because there it is the
    pixels under it that get removed and OCR boxes are approximate. On a text
    layer it is padded along the line and trimmed across it - see
    TEXT_BOX_INSET and :func:`_box` - which for sideways text (``turned``, from
    :func:`_turned_lines`) means trimmed left and right, not top and bottom.
    """
    lines: list[tuple[pymupdf.Rect, tuple]] = []
    for start, end, rect in spans:
        if end <= hit.start or start >= hit.end:
            continue
        if ocr:
            rect = _share_of_word(rect, start, end, hit.start, hit.end)
        direction = ACROSS if ocr else _direction_at(turned, rect)
        axis = _axis(direction)
        placed = False
        for i, (existing, existing_direction) in enumerate(lines):
            if existing_direction != direction or axis == "slanted":
                continue
            # same line if the ranges across the line substantially overlap:
            # the vertical ranges for ordinary text, horizontal for sideways
            if axis == "across":
                overlap = min(existing.y1, rect.y1) - max(existing.y0, rect.y0)
                share = 0.5 * min(existing.height, rect.height)
            else:
                overlap = min(existing.x1, rect.x1) - max(existing.x0, rect.x0)
                share = 0.5 * min(existing.width, rect.width)
            if overlap > share:
                # Rect union returns a new object, so assign it back into the
                # list - an in-place |= would only rebind the loop variable and
                # silently leave every word but the first uncovered.
                lines[i] = (existing | rect, direction)
                placed = True
                break
        if not placed:
            lines.append((pymupdf.Rect(rect), direction))
    if ocr:
        return [_pad_ocr(r, padding) for r, _d in lines]
    return [_box(r, padding, direction) for r, direction in lines]


def _share_of_word(rect: pymupdf.Rect, start: int, end: int, lo: int, hi: int) -> pymupdf.Rect:
    """The part of one recognised word that characters ``lo:hi`` take up.

    OCR glues a scanned row into one "word" as often as it splits it - a
    16-digit reference, the posting date and the balance beside it came back
    as one run 320 points wide - and the box for a hit in it was the whole
    run, which blacked out every figure on the row. Where the hit is most of
    the word, the word is boxed whole as before; otherwise the hit's share of
    the characters, a character's slack either side.
    """
    length = max(end - start, 1)
    inside = min(end, hi) - max(start, lo)
    if inside >= WORD_SHARE_WHOLE * length:
        return pymupdf.Rect(rect)
    width = rect.width / length
    cut = pymupdf.Rect(rect.x0 + width * (max(start, lo) - start - 1), rect.y0,
                       rect.x0 + width * (min(end, hi) - start + 1), rect.y1)
    return cut & rect


# --------------------------------------------------------------------------
# OCR
# --------------------------------------------------------------------------


def _ocr_words(page: pymupdf.Page, ocr_engine=None):
    """OCR one page, returning words in PDF coordinates.

    Returns ``None`` - not ``[]`` - when the engine cannot run: an empty list
    means "a page with nothing on it", while None means "we could not look",
    and the caller must refuse the page rather than deliver it as clean.
    """
    if ocr_engine is None:
        ocr_engine, _note = ocr.select_engine()
    if ocr_engine is None:
        return None
    return words_from_image(render_for_ocr(page), ocr_engine)


def render_for_ocr(page: pymupdf.Page):
    """The page as the OCR engines see it, at OCR_DPI."""
    from PIL import Image

    pixmap = page.get_pixmap(dpi=OCR_DPI, colorspace=pymupdf.csRGB)
    return Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)


def words_from_image(image, ocr_engine) -> list:
    """One engine's words on a rendered page, in PDF coordinates.

    Touches no PDF, so the engines can read one picture side by side.
    """
    scale = OCR_DPI / 72.0
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


# Told of each page as it is read, so the window can count pages rather than
# files: one scanned file can be forty pages, and "file 3 of 9" said nothing
# about how long was left. One job runs at a time, so one listener will do.
_page_listener = None


@contextmanager
def page_listener(callback):
    """Call ``callback(path, index)`` for each page read inside the block."""
    global _page_listener
    previous, _page_listener = _page_listener, callback
    try:
        yield
    finally:
        _page_listener = previous


def _page_read(path, index: int) -> None:
    listener = _page_listener
    if listener is not None:
        try:
            listener(str(Path(path).resolve()), index)
        except Exception:                         # a counter never stops a run
            pass


CAPTION_PAGES = 6                 # page one, and the header strip of five more


def _page_words(page: pymupdf.Page, path: Path, index: int, engines=None):
    """See :func:`_read_page_words`; the page is counted once it is read."""
    found = _read_page_words(page, path, index, engines)
    _page_read(path, index)
    return found


def _read_page_words(page: pymupdf.Page, path: Path, index: int, engines=None):
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
    base = _ocr_key(path, index)
    found: dict[str, list] = {}
    if base is not None:
        with _ocr_cache_lock:
            for engine_ in engines:
                words = _ocr_cache.get(base + (engine_.name,))
                if words is not None:
                    _ocr_cache.move_to_end(base + (engine_.name,))
                    found[engine_.name] = words
    missing = [engine_ for engine_ in engines if engine_.name not in found]
    if missing:
        # one picture of the page, read by every engine at once: each is a
        # program or a model of its own and neither touches the PDF, so a
        # page costs the slower engine's time rather than the sum of both
        image = render_for_ocr(page)
        found.update(zip((engine_.name for engine_ in missing),
                         _read_side_by_side(image, missing)))
        if base is not None:
            store_ocr(base, {name: found[name] for name in (e.name for e in missing)})
    sets: list[list] = []
    for engine_ in engines:
        words = found.get(engine_.name)
        if words is None:
            if not sets:
                return None, True            # the preferred engine failed: refuse
            continue
        sets.append(words)
    return (sets or None), True


def _read_side_by_side(image, engines) -> list:
    """Each engine's words for one rendered page, None where one failed."""
    def read(engine_):
        try:
            return words_from_image(image, engine_)
        except Exception:                          # refused below, never shipped
            return None

    if len(engines) == 1:
        return [read(engines[0])]
    from concurrent.futures import ThreadPoolExecutor
    with ThreadPoolExecutor(len(engines)) as threads:
        return list(threads.map(read, engines))


def store_ocr(base: tuple, by_engine: dict) -> None:
    """Keep each engine's reading of a page; a failed reading is not kept."""
    with _ocr_cache_lock:
        for name, words in by_engine.items():
            if words is None:
                continue
            _ocr_cache[base + (name,)] = words
            _ocr_cache.move_to_end(base + (name,))
        while len(_ocr_cache) > OCR_CACHE_PAGES:
            _ocr_cache.popitem(last=False)


def ocr_cached(base: tuple, names) -> bool:
    with _ocr_cache_lock:
        return all(base + (name,) in _ocr_cache for name in names)


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
# OCR runs the last line of an address together - "595SBLUFFST.STGEORGEUT84770"
# off a scanned pay stub, "LEHIUT 84043-3317" off a photographed statement -
# and the space the line above needs is gone. On a scan only, a state's own
# code glued to the town before it still ends an address.
_GLUED_ZIP_LINE = re.compile(
    rf"(?<=[A-Za-z.,])(?:{patterns._US_STATE})[ \t]*\d{{5}}(?:-\d{{4}})?[ \t]*$")
# a figure with cents, or a dollar sign on whole dollars: an appraisal's
# "2024 / $3,146" taxes line sat above a city line and was boxed as address
_MONEY_ON_LINE = re.compile(r"\d[\d,]*\.\d{2}|\$\s?\d")
# the lines above the ZIP: a street number, or a person's or company's name
_ADDRESS_LINE = re.compile(r"^\s*(?:\d{1,6}[A-Za-z]?\s+\S|P\.?\s?O\.?\s+Box\b|[A-Za-z])")
# The heading over a block names what the address is for - "Shipping Address",
# "Billing address", "Bill to:" - and is the one line of it worth keeping: an
# order's shipping and billing blocks were otherwise indistinguishable once
# covered. No name or street ends in a colon.
_ADDRESS_HEADING = re.compile(
    r"(?i)^\s*(?:[\w&/' ]{0,30}\baddress(?:ee)?|(?:ship|bill|sold|mail|deliver(?:ed)?|remit)"
    r"\s+to|[^:\n]{0,40}:)\s*:?\s*$")

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
        if (not (_ZIP_LINE.search(text) or (ocr and _GLUED_ZIP_LINE.search(text)))
                or _MONEY_ON_LINE.search(text)):
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
                    or _INSTITUTION_LINE.search(line)
                    or _ADDRESS_HEADING.match(line)):
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
        for index in range(1, min(doc.page_count, CAPTION_PAGES)):
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
    # PyMuPDF puts a line's direction on the line, not its spans;
    # :func:`_page_spans` copies it down so a sideways span is known as one
    return font, float(span.get("size", 9)), rgb, tuple(span.get("origin", (0, 0))), \
        tuple(span.get("dir", ACROSS))


def _page_spans(page: pymupdf.Page) -> list[dict]:
    """Every text span on the page, each carrying its line's direction.

    PyMuPDF's dict output keeps ``dir`` on the line, not the span, so a span
    read alone always looked level. A name printed up a statement's margin
    was then given a horizontal replacement smeared across the lines beside
    it, instead of the black box sideways text gets.
    """
    out = []
    for block in page.get_text("dict")["blocks"]:
        for line in block.get("lines", []):
            direction = tuple(round(d, 3) for d in line.get("dir", ACROSS))
            for span in line["spans"]:
                out.append({**span, "dir": direction})
    return out


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


def _distance(a: str, b: str, limit: int) -> int:
    """Edit distance between ``a`` and ``b``, giving up past ``limit``."""
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1,
                               previous[j - 1] + (ca != cb)))
        if min(current) > limit:
            return limit + 1
        previous = current
    return previous[-1]


# Characters OCR swaps among themselves; a misread value is compared in this
# folded alphabet, so "1O27-4419" and "1027-4419" are the same number.
_OCR_FOLD = str.maketrans({"o": "0", "O": "0", "l": "1", "I": "1", "i": "1", "|": "1",
                           "s": "5", "S": "5", "b": "6", "B": "8", "z": "2", "Z": "2"})


def _allowance(length: int) -> int:
    return 2 if length >= 9 else 1


# Letters OCR draws as one another, folded together: "rn" read as "m" turns
# BLACKBURN into BLACKBUM, an i or a 1 is read as an l, an O as a 0.
_LOOKALIKE_PAIRS = (("rn", "m"), ("vv", "w"), ("cl", "d"))
_LOOKALIKE = str.maketrans({"i": "l", "1": "l", "|": "l", "!": "l", "0": "o"})


def _looks(text: str) -> str:
    """``text`` as OCR might confuse it: letters, with look-alikes folded."""
    folded = re.sub(r"[^a-z0-9|!]", "", text.casefold())
    for pair, single in _LOOKALIKE_PAIRS:
        folded = folded.replace(pair, single)
    return folded.translate(_LOOKALIKE)


def _misread_name(text: str, letters: str, part: str) -> bool:
    """Whether the OCR word ``text`` is the name part ``part`` misread.

    A misread changes letters; it does not add a word ending. "Stevenson",
    "Granted" and "Whitehall" carry Steven, Grant and White whole and are
    other words, and "Grand" is an ordinary word one letter from Grant - all
    four were rewritten on a scan. So a word holding the name intact is never
    a misreading of it; a name of five letters is matched only through the
    letters OCR actually confuses ("Smlth", "Srnith"); one of six to eight
    letters may also have one other letter wrong, but no letter added or lost;
    and a long name - nine letters or more - may be two edits off.
    """
    if letters == part:
        return True
    if part in letters:
        return False
    if _looks(text) == _looks(part):
        return True
    if len(part) >= 9:
        return _distance(letters, part, 2) <= 2
    return len(part) >= 6 and len(letters) == len(part) and _distance(letters, part, 1) <= 1


def _ocr_near_misses(words, store: MappingStore, settings: Settings):
    """(hit, rect) for every OCR word within a letter or two of a registered value.

    OCR misreads predictably - an l for a 1, an O for a 0, a dropped or doubled
    letter - and a misread name or number matches nothing exactly, while the
    picture under it shows the real one to anybody who looks. On a scanned page
    a registered name part of five letters or more is found misread as
    :func:`_misread_name` allows, and a registered number of six characters or
    more one character off, two if it is nine or longer. Only on scans: a text
    layer spells what it shows.
    """
    names: list[tuple[str, object, str]] = []
    values: list[tuple[str, object]] = []
    for entity in store.entities.values():
        if not entity.enabled or not settings.category_enabled(entity.category):
            continue
        if entity.is_person and entity.person is not None:
            person, fake = entity.person, entity.surrogate
            parts = [(person.first, fake.first if fake else "")]
            parts += list(zip(person.middles, (fake.middles if fake else ()) or [""] * 9))
            parts += [(person.last, fake.last if fake else "")]
            for piece, stand_in in parts:
                bit = re.sub(r"[^a-z]", "", (piece or "").casefold())
                if len(bit) >= GLUED_NAME_MIN:
                    names.append((bit, entity, stand_in))
        elif not entity.is_person:
            folded = re.sub(r"[^0-9a-z]", "", entity.canonical.translate(_OCR_FOLD).casefold())
            if len(folded) >= 6 and any(ch.isdigit() for ch in folded):
                values.append((folded, entity))
    if not names and not values:
        return []
    out = []
    for x0, y0, x1, y1, text, *_rest in words:
        letters = re.sub(r"[^a-z]", "", text.casefold())
        folded = re.sub(r"[^0-9a-z]", "", text.translate(_OCR_FOLD).casefold())
        match, replacement = None, ""
        if len(letters) >= GLUED_NAME_MIN:
            found = next(((e, part) for bit, e, part in names
                          if _misread_name(text, letters, bit)), None)
            if found:
                match, replacement = found[0], _names.match_case(text, found[1] or found[0].replacement)
        # a figure or a date is never a misread identifier, however its digits fold
        if match is None and len(folded) >= 6 and not (
                patterns._CURRENCY_SHAPE.match(text.strip("+-()$ ")) or patterns._DATE_ONLY.match(text)):
            match = next((e for value, e in values
                          if _distance(folded, value, _allowance(len(value)))
                          <= _allowance(len(value))), None)
            replacement = match.replacement if match is not None else ""
        if match is None:
            continue
        rect = _pad_ocr(pymupdf.Rect(x0, y0, x1, y1))
        out.append((Hit(0, len(text), text, match.category, replacement or engine.REDACTION_TEXT,
                        match.key, engine.FUZZY_PRIORITY), rect))
    return out


# A recognised word that begins or ends with a registered name is that name
# with its neighbours glued on. RapidOCR returns a line with its spaces lost
# as often as not - "NATHANCBLACKBURN", "fromBank***1711" - and no pattern
# with word boundaries can find a name inside one token. Scans only, and only
# names of five letters or more, so "Ross" is never found inside "ACROSS".
GLUED_NAME_MIN = 5


# Endings that turn a name into another ordinary word or name - Steven into
# Stevenson, Grant into Granted, White into Whitehall. A name glued to one of
# these in a word with no case to go by is that other word, not the name.
_WORD_ENDINGS = frozenset((
    "ed", "er", "ers", "es", "est", "ing", "ings", "ly", "ness", "ment", "ish", "ful",
    "son", "sons", "sen", "ton", "hall", "ham", "ley", "land", "field", "ford", "wood",
    "ville", "worth"))


def _letter_positions(text: str) -> tuple[list[int], str]:
    """Where each a-z letter of ``text`` sits, and those letters lower-cased."""
    positions = [i for i, ch in enumerate(text)
                 if len(ch.casefold()) == 1 and "a" <= ch.casefold() <= "z"]
    return positions, "".join(text[i].casefold() for i in positions)


def _explained(rest: str, others) -> bool:
    """True when ``rest`` is the person's other name parts, an initial aside."""
    removed = False
    for other in sorted(others, key=len, reverse=True):
        if len(other) >= 2 and other in rest:
            rest = rest.replace(other, "")
            removed = True
    return removed and len(rest) <= 1


def _glued_at_edge(text: str, part: str, others) -> bool:
    """Whether ``part`` opens or closes the word ``text`` as a name glued on.

    OCR loses the spaces in a line - "NATHANCBLACKBURN", "fromBlackburn",
    "BLACKBURN.LAW" - but a name at the start of a longer word is as often
    just the start of that word: "Stevenson", "Granted", "Whitehall". The
    name counts when something shows a join: a mark or a digit between it and
    the rest, a change of case ("StevenGrant"), the rest being the person's
    other names, or an "s" a lost apostrophe left ("Garcias"). In a word with
    no case to go by - all capitals, as statements print - the name counts
    unless the rest is an ending that makes it an ordinary word.
    """
    positions, letters = _letter_positions(text)
    if len(letters) <= len(part):
        return False
    # Cased means written in mixed case, not one stray small letter: OCR reads
    # an I in a line of capitals as "i" - "HARRiNGTONENTERPRISES" - and that
    # one letter made the word count as cased, so the surname glued to the
    # business stayed in the rewritten line.
    lower = sum(1 for i in positions if text[i].islower())
    cased = lower > 1 and lower * 4 >= len(positions) and any(ch.isupper() for ch in text)
    for at_start in (True, False):
        if at_start:
            if not letters.startswith(part):
                continue
            rest = letters[len(part):]
            inner, outer = positions[len(part) - 1], positions[len(part)]
            case_break = text[inner].islower() and text[outer].isupper()
        else:
            if not letters.endswith(part):
                continue
            cut = len(letters) - len(part)
            rest = letters[:cut]
            inner, outer = positions[cut], positions[cut - 1]
            case_break = text[inner].isupper() and text[outer].islower()
        if abs(inner - outer) > 1 or case_break:
            return True                          # a mark, a digit or a case change
        if at_start and rest == "s":
            return True                          # "Garcia's" with the apostrophe lost
        if _explained(rest, others):
            return True
        if not cased and not (at_start and rest in _WORD_ENDINGS):
            return True
    return False


def _people_parts(store: MappingStore, settings: Settings) -> list:
    """(entity, [name parts as bare lowercase letters]) for each ticked person."""
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
    return people


# An organization's name OCR ran together has to be this long to be looked for
# inside other text - shorter, and two ordinary words could spell it.
GLUED_ORG_MIN = 8


def _glued_orgs(store: MappingStore, settings: Settings) -> list:
    """(entity, pattern) for each ticked employer or organization of two words
    or more, its words allowed to run together as OCR runs them.

    A scanned payroll line came back as one word - "RedCanyonLandsPayroll
    2305319913065380CODYBASHWORTH" - and the employer, ticked and replaced
    everywhere else, matched nothing in it: a name's words need the spaces
    between them. Only the store's own entities: a merchant nobody ticked
    is left for the reader.
    """
    out = []
    suffix = engine._COMPANY_SUFFIX.replace(r"\.?", "")
    for entity in store.entities.values():
        if entity.category not in engine._ORG_CATEGORIES or not entity.enabled:
            continue
        if not settings.category_enabled(entity.category):
            continue
        forms = {entity.canonical, *(v.text for v in entity.variants)}
        for form in forms:
            words = re.findall(r"[A-Za-z0-9]+", form.replace("&", " "))
            core = [w for w in words
                    if not re.fullmatch(suffix, w, re.IGNORECASE) and w.casefold() != "the"]
            if len(core) < 2 or sum(len(w) for w in core) < GLUED_ORG_MIN:
                continue
            # two words with a space between are ordinary prose as often as a
            # name - "the home depot" - and the engine matches them spaced
            # already; three are this company
            gap = r"[\s,.&\-]*" if len(core) > 2 else r"[,.&\-]*"
            body = gap.join(re.escape(w) for w in core)
            out.append((entity, re.compile(
                rf"(?i){body}(?:[\s,.]*{engine._COMPANY_SUFFIX}(?![a-z]))?")))
    return out


def _no_glued_names(say: str, store: MappingStore, settings: Settings) -> str:
    """A rewritten OCR word with any name still glued inside it replaced too.

    A pattern replaces only its own span of a word, and OCR glues a whole ACH
    line into one: "Clover/iservSettlement2403211971540395BLACKBURNLAW.PLLC"
    came back as "…[CARD-1]BLACKBURNLAW.PLLC", drawn on the page and written
    into the text layer with the surname in it.
    """
    for entity, bits in _people_parts(store, settings):
        others = list(bits)
        for bit in bits:
            if len(bit) < GLUED_NAME_MIN or bit not in say.casefold():
                continue
            stand_in = _stand_in_part(entity, bit) or engine.REDACTION_TEXT
            pattern = re.compile(rf"(?i)(?<![a-z]){re.escape(bit)}|{re.escape(bit)}(?![a-z])")

            def swap(m, say=say, bit=bit, stand_in=stand_in):
                # the run of letters the name sits in: a whole word is the
                # name; a longer one only when the name is glued on, so a
                # "Whitehall" beside a replaced name is not made "Daalhall"
                lo, hi = m.start(), m.end()
                while lo > 0 and say[lo - 1].isalpha():
                    lo -= 1
                while hi < len(say) and say[hi].isalpha():
                    hi += 1
                if (lo, hi) != (m.start(), m.end()) and not _glued_at_edge(
                        say[lo:hi], bit, [o for o in others if o != bit]):
                    return m.group(0)
                return _names.match_case(m.group(0), stand_in)
            say = pattern.sub(swap, say)
            # A change of case is a join as plain as a mark: "Transfer
            # HARRINGTONENTERPRISES" read as one word keeps the surname in its
            # middle, with letters on both sides.
            say = _case_joined(bit).sub(
                lambda m, stand_in=stand_in: _names.match_case(m.group(0), stand_in), say)
        # misread as well as glued on - "4904228163HARRNGTON" - as _name_inside
        def misread(m, bits=bits, entity=entity):
            if m.group(0) == say:
                return m.group(0)
            letters = m.group(0).casefold()
            bit = next((b for b in bits if len(b) >= GLUED_NAME_MIN and letters != b
                        and _misread_name(m.group(0), letters, b)), None)
            if bit is None:
                return m.group(0)
            return _names.match_case(m.group(0), _stand_in_part(entity, bit) or engine.REDACTION_TEXT)
        say = re.sub(r"(?<![A-Za-z\s])[A-Za-z]+|[A-Za-z]+(?![A-Za-z\s]|$)", misread, say)
    for entity, pattern in _glued_orgs(store, settings):
        say = pattern.sub(lambda m, e=entity: e.replacement or engine.REDACTION_TEXT, say)
    return say


def _case_joined(bit: str) -> re.Pattern:
    """``bit`` in capitals or capitalised, straight after a lowercase letter."""
    return re.compile(rf"(?<=[a-z])(?:{re.escape(bit.upper())}|{re.escape(bit.capitalize())})")


def _glued_names(words, store: MappingStore, settings: Settings):
    """(hit, rect) for every OCR token that carries a person's name, or a
    ticked organization's, glued in."""
    people = _people_parts(store, settings)
    orgs = _glued_orgs(store, settings)
    if not people and not orgs:
        return []
    out = []
    for x0, y0, x1, y1, text, *_rest in words:
        for entity, pattern in orgs:
            for match in pattern.finditer(text):
                out.append((Hit(match.start(), match.end(), match.group(0), entity.category,
                                entity.replacement or engine.REDACTION_TEXT, entity.key,
                                engine.FUZZY_PRIORITY),
                            _share_rect(x0, y0, x1, y1, text, match.start(), match.end())))
        if not people:
            continue
        letters = re.sub(r"[^a-z]", "", text.casefold())
        # "REBECCALBLACKBURN" carries two Blackburns' surname; the one whose
        # other names are in it too is the one it is
        best, score, edge = None, 0, None
        for entity, bits in people:
            glued = next((b for b in bits
                          if len(b) >= GLUED_NAME_MIN and len(letters) > len(b)
                          and (letters.startswith(b) or letters.endswith(b))
                          and _glued_at_edge(text, b, [o for o in bits if o != b])), None)
            if glued is None:
                continue
            explained = sum(len(b) for b in bits if b in letters)
            if explained > score:
                best, score, edge = entity, explained, (glued, bits)
        if best is None:
            out.extend(_name_inside(x0, y0, x1, y1, text, people))
            continue
        start, end = _name_span(text, *edge)
        if end - start < GLUED_WHOLE_SHARE * len(text):
            # A scanned ACH line read as one word - "LittleSproutsAcademy
            # Tuition2409059000000000CODYBASHWORTH" - ends in the name, but
            # most of it is the merchant: only the name's share is covered,
            # as inside a word, or every description on the page went white.
            out.extend(_slice_hit(x0, y0, x1, y1, text, start, end, best, edge[1]))
            continue
        rect = _pad_ocr(pymupdf.Rect(x0, y0, x1, y1))
        replacement = _names.match_case(text, best.replacement) if best.replacement else ""
        out.append((Hit(0, len(text), text, best.category, replacement or engine.REDACTION_TEXT,
                        best.key, engine.FUZZY_PRIORITY), rect))
    return out


# a name that fills less of an OCR word than this is covered by its own share
GLUED_WHOLE_SHARE = 0.6


def _name_span(text: str, part: str, bits: list[str]) -> tuple[int, int]:
    """Where in ``text`` the name glued at its edge lies, with the person's
    other name parts run on beside it: "…0000CODYBASHWORTH" is the whole
    name, not just the surname."""
    positions, letters = _letter_positions(text)
    if letters.startswith(part):
        start, end = 0, positions[len(part) - 1] + 1
        while True:
            run = re.match(r"[A-Za-z]+", text[end:])
            more = next((b for b in bits if run and run.group(0).casefold().startswith(b)), None)
            if not more:
                break
            end += len(more)
        return start, end
    start, end = positions[len(letters) - len(part)], len(text)
    while True:
        run = re.search(r"[A-Za-z]+$", text[:start])
        more = next((b for b in bits if run and run.group(0).casefold().endswith(b)), None)
        if not more:
            break
        start -= len(more)
    return start, end


def _share_rect(x0, y0, x1, y1, text: str, start: int, end: int) -> pymupdf.Rect:
    """The box of ``text[start:end]`` inside an OCR word, by its share of the
    characters, a character's slack either side."""
    width = (x1 - x0) / max(len(text), 1)
    rect = pymupdf.Rect(x0 + width * (start - 1), y0, x0 + width * (end + 1), y1)
    return _pad_ocr(rect & pymupdf.Rect(x0, y0, x1, y1))


def _slice_hit(x0, y0, x1, y1, text: str, start: int, end: int, entity, bits) -> list:
    """(hit, rect) for ``text[start:end]``, boxed by its share of the word."""
    rect = _share_rect(x0, y0, x1, y1, text, start, end)
    said = text[start:end]
    folded = re.sub(r"[^a-z]", "", said.casefold())
    stand_in = (_stand_in_part(entity, folded) if folded in bits
                else entity.replacement)
    return [(Hit(start, end, said, entity.category,
                 _names.match_case(said, stand_in) if stand_in else engine.REDACTION_TEXT,
                 entity.key, engine.FUZZY_PRIORITY), rect)]


def _name_inside(x0, y0, x1, y1, text: str, people) -> list:
    """A name inside a longer run OCR glued together, at a boundary it keeps.

    A photographed ACH line came back from one engine as a single word -
    "RockyMountainPowerUtility2404039000000000BLACKBURNLAW.PLLC" - with the
    household surname in the middle, after the company ID's digits. Only that
    name's share of the word is covered: the merchant at the front stays.
    The name has to touch a digit or a mark on one side - the word's own
    edges are :func:`_glued_names`' business - so a surname is not found
    inside an unrelated longer word.
    """
    out = []

    def cover(start: int, end: int, said: str, entity, replacement: str):
        out.append((Hit(start, end, said, entity.category, replacement or engine.REDACTION_TEXT,
                        entity.key, engine.FUZZY_PRIORITY),
                    _share_rect(x0, y0, x1, y1, text, start, end)))

    for entity, bits in people:
        # The whole name with its spaces lost, wherever it sits: a scanned ACH
        # line read as "CardSvcsEpay2408296158391052CODYBASHWORTH" ends in the
        # name, so no boundary follows the surname and the initial sits right
        # before it - neither rule below could see it, and the name shipped
        # beside its own stand-in. A first name, an optional middle and a
        # surname run together is never an accident of spelling.
        whole_spans: list[tuple[int, int]] = []
        for joined in _joined_names(bits):
            for match in re.finditer(re.escape(joined), text, re.IGNORECASE):
                if any(lo <= match.start() < hi for lo, hi in whole_spans):
                    continue
                whole_spans.append((match.start(), match.end()))
                cover(match.start(), match.end(), match.group(0), entity,
                      _names.match_case(match.group(0), entity.replacement)
                      if entity.replacement else "")
        for bit in bits:
            if len(bit) < GLUED_NAME_MIN:
                continue
            pattern = re.compile(rf"(?i)(?<=[^a-z]){re.escape(bit)}|{re.escape(bit)}(?=[^a-z])")
            matches = list(pattern.finditer(text)) + [
                m for m in _case_joined(bit).finditer(text)
                if not any(m.start() == n.start() for n in pattern.finditer(text))]
            for match in matches:
                if any(lo <= match.start() < hi for lo, hi in whole_spans):
                    continue
                whole_spans.append((match.start(), match.end()))
                stand_in = _stand_in_part(entity, bit)
                cover(match.start(), match.end(), match.group(0), entity,
                      _names.match_case(match.group(0), stand_in) if stand_in else "")
        # The surname misread as well as glued on: "…4904228163HARRNGTON",
        # one letter lost after the company ID's digits. A run of letters
        # that a digit or a mark cuts out of the word is held to the same
        # test as a misread word standing alone.
        for run in re.finditer(r"[A-Za-z]+", text):
            if (run.start(), run.end()) == (0, len(text)):
                continue
            if any(lo < run.end() and run.start() < hi for lo, hi in whole_spans):
                continue
            letters = run.group(0).casefold()
            bit = next((b for b in bits if len(b) >= GLUED_NAME_MIN and letters != b
                        and _misread_name(run.group(0), letters, b)), None)
            if bit is None:
                continue
            whole_spans.append((run.start(), run.end()))
            stand_in = _stand_in_part(entity, bit)
            cover(run.start(), run.end(), run.group(0), entity,
                  _names.match_case(run.group(0), stand_in) if stand_in else "")
    return out


# a run-together name this short could be part of an ordinary word
JOINED_NAME_MIN = 8


def _joined_names(bits: list[str]) -> list[str]:
    """A person's name parts run together as OCR loses the spaces, longest first:
    "codybashworth", "codyashworth", "ashworthcody"."""
    if len(bits) < 2:
        return []
    first, middles, last = bits[0], bits[1:-1], bits[-1]
    forms = {first + last, last + first}
    if middles:
        forms.add(first + "".join(m[0] for m in middles) + last)
        forms.add(first + "".join(middles) + last)
    return sorted((f for f in forms if len(f) >= JOINED_NAME_MIN), key=len, reverse=True)


def _loosely(letters: str, part: str) -> bool:
    """A word beside a name OCR read that may be the rest of that name misread.

    Looser than :func:`_misread_name`, because the place is already known:
    the same first letter, and no more than a third of the letters wrong. An
    initial must be that letter.
    """
    if len(part) < 3:
        return letters == part
    allowance = max(1, len(part) // 3)
    return (bool(letters) and letters[0] == part[0]
            and _distance(letters, part, allowance) <= allowance)


def _name_tails(text: str, spans, hits: list[Hit], store: MappingStore,
                settings: Settings) -> list[Hit]:
    """Hits for the rest of a name, beside a part of it a hit already found.

    On a photographed statement "Online Transfer to Kyle D Harrington" came
    back from one engine as "Kyle D Haningion": "Kyle D" matched, the misread
    surname matched nothing, and the page went out reading "Ezra Oskar
    Harrington". Once any part of a person's name is found on a scan, the
    words either side of it, on the same line and running on from it, are
    tested against the parts not yet found - each in turn, outward, until one
    is not one of them.
    """
    out: list[Hit] = []
    for hit in hits:
        entity = store.entities.get(hit.entity_key)
        if entity is None or not entity.is_person or entity.person is None:
            continue
        if not entity.enabled or not settings.category_enabled(entity.category):
            continue
        person = entity.person
        said = set(re.findall(r"[a-z]+", hit.text.casefold()))
        missing = [bit for bit in (re.sub(r"[^a-z]", "", (piece or "").casefold())
                                   for piece in (person.first, *person.middles, person.last))
                   if bit and bit not in said]
        touched = [i for i, (start, end, _r) in enumerate(spans)
                   if start < hit.end and hit.start < end]
        if not missing or not touched:
            continue
        for step, edge in ((1, touched[-1]), (-1, touched[0])):
            index = edge + step
            while missing and 0 <= index < len(spans):
                start, end, rect = spans[index]
                near = spans[index - step][2]
                gap = (rect.x0 - near.x1) if step > 0 else (near.x0 - rect.x1)
                overlap = min(near.y1, rect.y1) - max(near.y0, rect.y0)
                if overlap <= 0.5 * min(near.height, rect.height) or \
                        gap > 2.0 * max(near.height, rect.height):
                    break                           # another line, or another column
                word = text[start:end]
                letters = re.sub(r"[^a-z]", "", word.casefold())
                part = next((bit for bit in missing if _loosely(letters, bit)), None)
                if part is None:
                    break
                missing.remove(part)
                stand_in = _stand_in_part(entity, part)
                out.append(Hit(start, end, word, entity.category,
                               _names.match_case(word, stand_in) if stand_in
                               else engine.REDACTION_TEXT, entity.key, engine.FUZZY_PRIORITY))
                index += step
    return out


def _stand_in_part(entity, bit: str) -> str:
    """The stand-in's matching name part: a surname for a surname."""
    person, surrogate = entity.person, entity.surrogate
    if person is None or surrogate is None:
        return ""
    for real, fake in ((person.last, surrogate.last), (person.first, surrogate.first),
                       *zip(person.middles, surrogate.middles)):
        if real and re.sub(r"[^a-z]", "", real.casefold()) == bit:
            return fake or ""
    return surrogate.last or ""


def _covered(rect: pymupdf.Rect, boxes: list[pymupdf.Rect]) -> float:
    """The share of ``rect`` that ``boxes`` cover (overlaps between boxes
    counted twice, so this can only overstate)."""
    area = rect.get_area()
    if area <= 0:
        return 1.0
    return min(1.0, sum((rect & box).get_area() for box in boxes if rect.intersects(box)) / area)


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
            hits = list(engine.scan_text(text, store, anon_settings, matcher))
            if ocr:
                hits += _name_tails(text, spans, hits, store, settings)
            for run, run_hits in _runs(hits, spans):
                words_in = [rect for _s, _e, rect in run]
                fresh = claim(words_in)
                if not fresh:
                    # Taken by overlap is not taken whole. The other engine
                    # read "SOURCE ***7039" as one wide word; the first read
                    # "SOURCE" and a garbled "**+#7039", and its edit to
                    # "SOURCE" counted the wide word as claimed - so the
                    # masked digits it held were never covered at all.
                    bare = [rect for rect in words_in if _covered(rect, claimed) < 0.9]
                    if bare:
                        edits.append(_Edit(bare, "", run_hits[0]))
                        counted.extend(h for h in run_hits if h not in counted)
                    continue
                counted.extend(h for h in run_hits if h not in counted)
                if len(fresh) == len(words_in):
                    say = _rewrite(text, run, run_hits)
                    if ocr:
                        say = _no_glued_names(say, store, settings)
                else:
                    say, words_in = "", fresh
                edits.append(_Edit(words_in, say, run_hits[0]))
        if ocr:
            for hit, rect in [*_glued_names(words, store, settings),
                              *_ocr_near_misses(words, store, settings)]:
                rect = _unpad_ocr(rect)
                if claim([rect]):
                    edits.append(_Edit([rect], hit.replacement, hit))
                    counted.append(hit)
                elif _covered(rect, claimed) < 0.9:
                    # Taken by overlap is not taken whole, here as above: the
                    # other engine boxed "CODYBASHWORTH" a third of its width,
                    # its edit claimed the start of the name, and the surname
                    # past the end of that box stayed on the page.
                    edits.append(_Edit([rect], "", hit))
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
    covers = [_pad_ocr(_union(edit.words)) for edit in edits]
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
    before = len(result.hits)          # result.hits runs across the whole document
    edits = _plan_edits(word_sets, store, anon_settings, matcher, settings, result,
                        document_label, ocr=True)
    report.hits = len(result.hits) - before
    marks = _white_out(page, page.rect, word_sets, edits, report)
    if report.boxes:
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
    _draw_plan(page, _ocr_layer(word_sets, edits, marks))
    if marks:
        result.warnings.append(
            f"page {report.number}: {len(marks)} area(s) of handwriting or unread ink "
            "blacked out.")


def _white_out(page, region, word_sets, edits, report: PageReport,
               extra=(), read=()) -> list[pymupdf.Rect]:
    """Queue the redactions for one OCR'd region: white under each replacement,
    black over every patch of unread ink. Returns the black boxes.

    ``extra`` is more to white out - the picture under a text-layer edit that
    sits over an image. ``read`` is ink already accounted for - the image under
    a page's own text layer - which is never mistaken for handwriting.
    """
    cleared = [_pad_ocr(_union(edit.words)) for edit in edits] + list(extra)
    marks = _unread_ink(page, region, word_sets, cleared + list(read))
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


# An OCR word this much covered by the page's own words is one the text layer
# already holds - see :func:`_anonymize_images`.
LIVE_COVER = 0.5


def _covered(rect: pymupdf.Rect, by) -> float:
    """The share of ``rect`` that the rectangles ``by`` lie over."""
    area = rect.get_area()
    if area <= 0:
        return 0.0
    total = 0.0
    for other in by:
        overlap = rect & other
        if not overlap.is_empty:
            total += overlap.get_area()
    return min(total / area, 1.0)


def _anonymize_images(page, store, anon_settings, matcher, settings, report: PageReport,
                      result: PdfResult, document_label: str,
                      live_words=(), live_edits=()) -> list[tuple]:
    """Images on a text page: their text anonymized like a scan's, or replaced.

    Each image is read by OCR. One that holds text - a pasted statement, a
    check image - has its hits whited out and replaced, and its words added to
    the page's text layer. One with almost no text - a photo, a logo, a
    signature - is whited out whole and marked [IMAGE]. With no OCR engine an
    image is blacked out, as when redacting. Returns the pieces to draw once
    the redactions are applied.

    The region is OCR'd as it renders, so it reads the page's own text too
    wherever that lies over the image - every word of a searchable scan's
    hidden layer, and every word printed over a tinted background. Those
    words are the text layer's business (``live_words``; its edits are
    ``live_edits``): anonymizing them again here wrote each line into the text
    layer twice and drew every replacement twice. So an OCR word the page's
    own words cover is left out of the plan and the OCR layer; it still
    counts as read ink, and the picture under a text-layer edit is whited out
    in full, so the name in the image goes with the name in the text.

    These boxes clear pixels only (PDF_REDACT_TEXT_NONE): the page's own text
    is the text layer's to remove, and a box padded for an approximate OCR
    word must not take a neighbouring line of it with it.
    """
    live_words = [pymupdf.Rect(w[:4]) for w in live_words]
    queued = report.boxes
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
            live_here = [w for w in live_words if w.intersects(rect)]
            fresh = [[w for w in words
                      if _covered(pymupdf.Rect(w[:4]), live_here) < LIVE_COVER]
                     for words in sets]
            edited = [_pad_ocr(e) for e in live_edits if e.intersects(rect)]
            # the picture of an edited word, wherever the OCR boxed it - but
            # only a word mostly inside the edit: one engine boxes a whole
            # line as one "word", and whiting that out took the figures too
            edited += [_pad_ocr(pymupdf.Rect(w[:4])) for words in sets for w in words
                       if w[4].strip() and _covered(pymupdf.Rect(w[:4]), edited) >= LIVE_COVER]
            edited = [(e & rect) for e in edited]
            edits = _plan_edits(fresh, store, anon_settings, matcher, settings, result,
                                document_label, ocr=True)
            marks = _white_out(page, rect, sets, edits, report,
                               extra=[e for e in edited if not e.is_empty], read=live_here)
            plan.extend(_ocr_layer(fresh, edits, marks))
            inked += len(marks)
    if report.boxes > queued:
        page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS,
                              graphics=pymupdf.PDF_REDACT_LINE_ART_NONE,
                              text=pymupdf.PDF_REDACT_TEXT_NONE)
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
    before = len(result.hits)                # result.hits runs across the whole document
    edits = _plan_edits([words], store, anon_settings, matcher, settings, result,
                        document_label)

    spans = _page_spans(page)
    turned = _turned_lines(page)
    rows = _row_groups(words)
    planned: list[tuple[_Edit, dict, float]] = []
    boxes: list[tuple[pymupdf.Rect, object]] = []
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
        direction = tuple(span["dir"]) if span is not None else _direction_at(turned, line)
        rotated = _axis(direction) != "across" or direction[0] < 0
        # sideways or upside-down text cannot take a level replacement: it is
        # boxed in black, the box shaped to the way the text runs
        boxes.append((_box(line, BOX_PADDING, direction), (0, 0, 0) if rotated else False))
        if edit.text and span is not None and not rotated:
            planned.append((edit, span, limit))

    # images first, with the page's text still on it, so OCR reads the picture
    # as a person sees it; their own boxes clear pixels only
    image_plan = (_anonymize_images(page, store, anon_settings, matcher, settings,
                                    report, result, document_label, live_words=words,
                                    live_edits=[_union(e.words) for e in edits])
                  if settings.redact_images else [])

    for rect, fill in boxes:
        page.add_redact_annot(rect, fill=fill, cross_out=False)
        report.boxes += 1
    report.hits = len(result.hits) - before
    if boxes:
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
    if planned:
        result.warnings.append(
            f"page {report.number}: replacements were added on top of the page rather than "
            "in its text order; a parser that reads in file order finds them at the end of "
            "the page.")
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


def _moved(previous, char, size: float) -> bool:
    """True when ``char`` is not set straight on from ``previous``.

    One span of the text trace is one text object, and a text object is not
    one line: a generator writes a whole statement header as one BT ... ET,
    moving down with T* or Td between lines, and a table puts each cell with
    a Td of its own. Drawn from its first character's origin as one run, a
    span like that put every line on the first line's baseline - "Statement
    period 01/31/2025Account holder: ..." - and pushed a date off the right
    edge of the page. A new baseline, a step back, or a jump forward of more
    than a word space starts a new piece, drawn at its own origin.
    """
    if abs(char[2][1] - previous[2][1]) > 0.01 * max(size, 1.0):
        return True
    gap = char[2][0] - previous[3][2]
    return gap < -0.25 * size or gap > 0.5 * size


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
                # Split where the standard faces stop being able to encode it,
                # so only a "■" itself, not its whole line, changes face - and
                # wherever the text moves: see :func:`_moved`.
                pieces: list[list] = []
                for char in run:
                    fits = _encodable(chr(char[0]))
                    if (pieces and _encodable(chr(pieces[-1][0][0])) == fits
                            and not _moved(pieces[-1][-1], char, size)):
                        pieces[-1].append(char)
                    else:
                        pieces.append([char])
                for piece in pieces:
                    text = "".join(chr(c[0]) for c in piece)
                    if not text.strip():
                        continue
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


def _remove_off_page_text(page: pymupdf.Page) -> int:
    """Remove text drawn past the page's edge, and any word the edge cuts.

    Text extraction stops at the page's edge, so nothing outside it is ever
    scanned - but it is still in the file, and a parser that does not clip
    reads it. A line set up the margin ran off the top of the page: "Statement
    for Oakley V Ward acct 3357 3537 2072" was read as far as "acct 3357 3",
    the account number never matched, and "3537 2072" shipped in the content
    stream with the half-visible "3" on the page. A character whose centre
    lies outside the page goes, and so does the rest of its word, so no
    fragment of a value is left at the edge for anybody to read. Returns how
    many characters went.
    """
    # the trace, like the words, is in the unrotated page's coordinates
    visible = page.rect * page.derotation_matrix
    marked: list[pymupdf.Rect] = []
    for span in page.get_texttrace():
        chars = span.get("chars", ())
        if not chars:
            continue
        outside = [not visible.contains(pymupdf.Point((c[3][0] + c[3][2]) / 2,
                                                      (c[3][1] + c[3][3]) / 2))
                   for c in chars]
        if not any(outside):
            continue
        take = list(outside)
        # widen to whole words: a word the edge cuts is a fragment of a value
        start = 0
        for end in range(len(chars) + 1):
            if end == len(chars) or chr(chars[end][0]).isspace():
                if any(outside[start:end]):
                    take[start:end] = [True] * (end - start)
                start = end + 1
        for char, gone in zip(chars, take):
            if gone and not chr(char[0]).isspace():
                box = pymupdf.Rect(char[3])
                # the middle of the character: enough to remove it, too
                # little to reach a neighbouring line's taller boxes
                marked.append(box + (box.width / 4, box.height / 4,
                                     -box.width / 4, -box.height / 4))
    if not marked:
        return 0
    for rect in marked:
        page.add_redact_annot(rect, fill=False, cross_out=False)
    page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_NONE,
                          graphics=pymupdf.PDF_REDACT_LINE_ART_NONE)
    return len(marked)


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
        anonymized_scans = 0
        boxed_scans: list[int] = []      # rotated scans, redacted in anonymize mode
        for index, page in enumerate(doc):
            report = PageReport(number=index + 1)
            _page_read(source, index)

            if settings.scrub_embedded:
                removed, links = _strip_page_extras(page)
                result.annotations_removed += removed
                result.links_removed += links

            removed_off_page = _remove_off_page_text(page)
            if removed_off_page:
                result.warnings.append(
                    f"page {index + 1}: {removed_off_page} character(s) of text printed past "
                    "the edge of the page were removed - nothing on the page shows them, "
                    "so they could not be checked, but a parser would still read them.")

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
                anonymized_scans += 1
                result.pages.append(report)
                continue
            if anonymize:
                boxed_scans.append(index + 1)

            hits = []
            boxed: list = []
            readings = [reading for words in word_sets for reading in _readings(words)]
            turned = [] if report.ocr_used else _turned_lines(page)
            for text, spans in readings:
                found = list(engine.scan_text(text, store, redact_settings, matcher))
                if report.ocr_used:
                    found += _name_tails(text, spans, found, store, settings)
                for hit in found:
                    rects = _rects_for_hit(hit, spans, padding, ocr=report.ocr_used,
                                           turned=turned)
                    # the second reading finds most things the first did; count
                    # each place once, so the report and review counts hold
                    if rects and all(any(r in b for b in boxed) for r in rects):
                        continue
                    boxed.extend(rects)
                    hits.append((hit, rects))
            if report.ocr_used:
                for words in word_sets:
                    for hit, rect in [*_glued_names(words, store, settings),
                                      *_ocr_near_misses(words, store, settings)]:
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

        if anonymize and anonymized_scans:
            result.warnings.append(
                "Scanned pages were anonymized from their OCR: each name or number is "
                "whited out and replaced, and the page's text added beneath it so a "
                "parser can read it.")
        if boxed_scans:
            # a rotated scan keeps the redact path, so saying it was anonymized
            # would describe replacements that are not on it
            result.warnings.append(
                f"page{'s' if len(boxed_scans) > 1 else ''} "
                f"{', '.join(str(n) for n in boxed_scans)}: rotated scan"
                f"{'s' if len(boxed_scans) > 1 else ''} redacted rather than anonymized - "
                "each name or number is blacked out, with no replacement and no text layer.")
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
