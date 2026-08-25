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

from dataclasses import dataclass, field
from pathlib import Path

import pymupdf

from . import categories, engine
from .engine import Hit, PersonMatcher, Settings
from .mapping import MappingStore

OCR_DPI = 300
MIN_TEXT_CHARS = 12          # below this a page is treated as image-only
BOX_PADDING = 1.0            # points of slack around each redaction box


@dataclass
class PageReport:
    number: int
    hits: int = 0
    boxes: int = 0
    ocr_used: bool = False
    image_only: bool = False
    text_chars: int = 0


@dataclass
class PdfResult:
    source: Path
    output: Path | None
    hits: list[Hit] = field(default_factory=list)
    pages: list[PageReport] = field(default_factory=list)
    annotations_removed: int = 0
    attachments_removed: int = 0
    bookmarks_removed: int = 0
    metadata_scrubbed: bool = False
    refused: bool = False
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# OCR availability
# --------------------------------------------------------------------------

_ocr_error: str | None = None


def ocr_available() -> tuple[bool, str]:
    """(can we OCR, human-readable explanation)."""
    global _ocr_error
    try:
        import pytesseract
    except Exception as exc:                      # pragma: no cover - env dependent
        _ocr_error = f"pytesseract is not installed ({exc})"
        return False, _ocr_error
    try:
        version = pytesseract.get_tesseract_version()
    except Exception:                             # pragma: no cover - env dependent
        _ocr_error = (
            "the Tesseract binary was not found. Install it with "
            "'brew install tesseract' on macOS, or from "
            "https://github.com/UB-Mannheim/tesseract/wiki on Windows."
        )
        return False, _ocr_error
    return True, f"Tesseract {version}"


# --------------------------------------------------------------------------
# words -> text, keeping a map back to rectangles
# --------------------------------------------------------------------------


def _words_to_text(words) -> tuple[str, list[tuple[int, int, pymupdf.Rect]]]:
    """Join extracted words into text, remembering each word's span and box.

    Line breaks are preserved so the detectors and the allowlist see the same
    shape of text they would see in a DOCX.
    """
    pieces: list[str] = []
    spans: list[tuple[int, int, pymupdf.Rect]] = []
    cursor = 0
    previous_line = None
    for x0, y0, x1, y1, word, block, line, _no in words:
        if not word:
            continue
        key = (block, line)
        if previous_line is not None:
            separator = " " if key == previous_line else "\n"
            pieces.append(separator)
            cursor += len(separator)
        previous_line = key
        start = cursor
        pieces.append(word)
        cursor += len(word)
        spans.append((start, cursor, pymupdf.Rect(x0, y0, x1, y1)))
    return "".join(pieces), spans


def _rects_for_hit(hit: Hit, spans) -> list[pymupdf.Rect]:
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
    return [r + (-BOX_PADDING, -BOX_PADDING, BOX_PADDING, BOX_PADDING) for r in lines]


# --------------------------------------------------------------------------
# OCR
# --------------------------------------------------------------------------


def _ocr_words(page: pymupdf.Page):
    """OCR one page, returning words in PDF coordinates."""
    import pytesseract
    from PIL import Image

    scale = OCR_DPI / 72.0
    pixmap = page.get_pixmap(dpi=OCR_DPI, colorspace=pymupdf.csRGB)
    image = Image.frombytes("RGB", (pixmap.width, pixmap.height), pixmap.samples)
    data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)

    words = []
    for i, raw in enumerate(data["text"]):
        word = (raw or "").strip()
        if not word:
            continue
        try:
            confidence = float(data["conf"][i])
        except (TypeError, ValueError):
            confidence = -1.0
        if confidence < 0:
            continue
        left, top = data["left"][i], data["top"][i]
        width, height = data["width"][i], data["height"][i]
        words.append((
            left / scale, top / scale, (left + width) / scale, (top + height) / scale,
            word, data["block_num"][i], data["line_num"][i], data["word_num"][i],
        ))
    return words


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


def _strip_page_extras(page: pymupdf.Page) -> int:
    """Remove annotations and form widgets, which carry their own text."""
    removed = 0
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
    return removed


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
    doc = pymupdf.open(source)

    try:
        image_only_pages: list[int] = []
        for index, page in enumerate(doc):
            report = PageReport(number=index + 1)

            if settings.scrub_embedded:
                result.annotations_removed += _strip_page_extras(page)

            words = page.get_text("words")
            page_text = page.get_text("text")
            report.text_chars = len(page_text.strip())

            if report.text_chars < MIN_TEXT_CHARS:
                report.image_only = True
                if settings.ocr_scanned_pdfs and ocr_ok:
                    words = _ocr_words(page)
                    report.ocr_used = True
                else:
                    image_only_pages.append(index + 1)
                    result.pages.append(report)
                    continue

            text, spans = _words_to_text(words)
            hits = engine.scan_text(text, store, redact_settings, matcher)
            report.hits = len(hits)

            for hit in hits:
                entity = store.entities.get(hit.entity_key)
                if entity is not None:
                    store.record_hit(entity, document_label)
                for rect in _rects_for_hit(hit, spans):
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

            result.hits.extend(hits)
            if report.boxes:
                # PDF_REDACT_IMAGE_PIXELS also clears image content under a box,
                # which is what makes OCR'd scans genuinely redacted
                page.apply_redactions(images=pymupdf.PDF_REDACT_IMAGE_PIXELS)
            result.pages.append(report)

        if image_only_pages:
            reason = ocr_note if not ocr_ok else "OCR was turned off"
            result.refused = True
            result.warnings.append(
                f"pages {', '.join(str(p) for p in image_only_pages)} carry no text layer "
                f"and could not be read ({reason}). The file was NOT written - a scanned "
                "page passed through untouched would look redacted without being redacted."
            )
            result.output = None
            return result

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
