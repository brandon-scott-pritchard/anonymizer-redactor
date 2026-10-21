"""Reading a case folder into text: shared by the party-PII skills.

Every file type the skills read (PDF with its scans, Word, Excel, OpenDocument,
RTF, old Word, HTML, text, email with attachments, Outlook .msg, images, nested
zips) becomes a list of pages; a page with no usable text layer is OCR'd or
queued for a model to transcribe. Standard library only, with optional
PyMuPDF / pdfplumber / pypdf / Tesseract where present.
"""

from __future__ import annotations

import email
import email.policy
import fnmatch
import hashlib
import html.parser
import json
import re
import shutil
import subprocess
import unicodedata
import zipfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from xml.etree import ElementTree

# --------------------------------------------------------------------------
# reading files
# --------------------------------------------------------------------------

TEXT_EXT = {".txt", ".md", ".csv", ".tsv", ".json", ".xml", ".log"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".bmp", ".gif", ".webp", ".heic", ".heif"}
VIEWABLE = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
SKIP_NAMES = {".ds_store", "thumbs.db", "desktop.ini"}
MIN_TEXT_CHARS = 25          # fewer non-space characters than this: no text layer
SCAN_TEXT_CHARS = 200        # ...or this few, over a page that is mostly picture
OCR_MIN_CONFIDENCE = 70      # mean word confidence below which a model re-reads it
RENDER_DPI = 200
OCR_DPI = 300


class Unreadable(Exception):
    """A file that cannot be read here. Listed in the summary, never dropped quietly."""


@dataclass
class Page:
    file: str                  # path relative to the case folder
    page: str                  # "3", a sheet name, or "" for a Word file
    text: str
    first: bool = True         # the document's first page (its header block counts)
    method: str = "text"
    tables: list = field(default_factory=list)


def _have(module: str) -> bool:
    try:
        __import__(module)
        return True
    except Exception:
        return False


def _fitz():
    for name in ("pymupdf", "fitz"):
        try:
            return __import__(name)
        except Exception:
            continue
    return None


def tesseract() -> str | None:
    return shutil.which("tesseract")


def ocr_image(image: Path) -> tuple[str, float]:
    """Tesseract's reading of one image, and its mean word confidence."""
    exe = tesseract()
    if not exe:
        return "", 0.0
    try:
        out = subprocess.run([exe, str(image), "stdout", "tsv"], capture_output=True,
                             timeout=300, check=False).stdout.decode("utf-8", "replace")
    except (OSError, subprocess.SubprocessError):
        return "", 0.0
    lines: dict[tuple, list[str]] = {}
    confs: list[float] = []
    for row in out.splitlines()[1:]:
        cols = row.split("\t")
        if len(cols) < 12 or not cols[11].strip():
            continue
        try:
            conf = float(cols[10])
        except ValueError:
            continue
        if conf < 0:
            continue
        key = (int(cols[2]), int(cols[3]), int(cols[4]))      # block, paragraph, line
        lines.setdefault(key, []).append(cols[11])
        confs.append(conf)
    text = "\n".join(" ".join(words) for _, words in sorted(lines.items()))
    return text, (sum(confs) / len(confs) if confs else 0.0)


def _xml_text(element, ns_t: str, ns_tab: str, ns_br: str) -> str:
    out = []
    for node in element.iter():
        if node.tag == ns_t and node.text:
            out.append(node.text)
        elif node.tag == ns_tab:
            out.append("\t")
        elif node.tag == ns_br:
            out.append("\n")
    return "".join(out)


W = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"


def read_docx(path: Path) -> list[Page]:
    """Body, tables, headers, footers, footnotes and comments of a Word file.

    Plain zip and XML rather than python-docx: that library reads neither
    headers' text boxes nor footnotes, and is not installed in every sandbox.
    A table row becomes one line with its cells spaced apart, as a PDF prints it.
    """
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise Unreadable(f"not a Word file ({exc})")
    parts = sorted(n for n in archive.namelist()
                   if re.fullmatch(r"word/(document|header\d*|footer\d*|footnotes|endnotes|"
                                   r"comments)\.xml", n))
    if "word/document.xml" not in parts:
        raise Unreadable("no document body")
    body_lines: list[str] = []
    extra_lines: list[str] = []
    tables: list[list[list[str]]] = []

    def para(p) -> str:
        return _xml_text(p, W + "t", W + "tab", W + "br")

    def walk(container, sink: list[str]):
        for child in container:
            if child.tag == W + "p":
                sink.append(para(child))
            elif child.tag == W + "tbl":
                rows = []
                for tr in child.iter(W + "tr"):
                    cells = []
                    for tc in tr.findall(W + "tc"):
                        cells.append(" ".join(para(p) for p in tc.iter(W + "p")).strip())
                    rows.append(cells)
                    sink.append("    ".join(cells))
                tables.append(rows)
            elif child.tag in (W + "body", W + "sdt", W + "sdtContent", W + "hdr", W + "ftr",
                               W + "footnotes", W + "endnotes", W + "footnote", W + "endnote",
                               W + "comments", W + "comment"):
                walk(child, sink)

    for part in parts:
        try:
            root = ElementTree.fromstring(archive.read(part))
        except ElementTree.ParseError:
            continue
        walk(root, body_lines if part == "word/document.xml" else extra_lines)
    # headers first: a pleading's running header carries the caption's names
    text = "\n".join(extra_lines + body_lines)
    return [Page("", "", text, True, "docx", tables)]


def read_odt(path: Path) -> list[Page]:
    try:
        root = ElementTree.fromstring(zipfile.ZipFile(path).read("content.xml"))
    except (zipfile.BadZipFile, KeyError, ElementTree.ParseError) as exc:
        raise Unreadable(f"not an OpenDocument file ({exc})")
    ns = "{urn:oasis:names:tc:opendocument:xmlns:text:1.0}"
    lines = ["".join(p.itertext()) for p in root.iter() if p.tag in (ns + "p", ns + "h")]
    return [Page("", "", "\n".join(lines), True, "odt")]


_BUILTIN_DATE_FORMATS = set(range(14, 23)) | {45, 46, 47}


def _col(ref: str) -> int:
    """Column index of a cell reference: "C5" -> 2."""
    n = 0
    for ch in re.match(r"[A-Z]*", ref or "").group(0):
        n = n * 26 + ord(ch) - 64
    return max(n - 1, 0)


def read_xlsx(path: Path) -> list[Page]:
    """Every sheet, one row per line, and under each row its cells as
    "Header: value" - so a bare SSN or phone in a column headed "SSN" or
    "Phone" carries its label to the detectors. Dates stored as day numbers are
    written as dates. Plain zip and XML, for the same reason as Word."""
    try:
        archive = zipfile.ZipFile(path)
    except zipfile.BadZipFile as exc:
        raise Unreadable(f"not a spreadsheet ({exc})")
    s = "{http://schemas.openxmlformats.org/spreadsheetml/2006/main}"
    listing = set(archive.namelist())
    shared: list[str] = []
    if "xl/sharedStrings.xml" in listing:
        root = ElementTree.fromstring(archive.read("xl/sharedStrings.xml"))
        shared = ["".join(t.text or "" for t in si.iter(s + "t")) for si in root.iter(s + "si")]
    date_styles: set[int] = set()
    if "xl/styles.xml" in listing:
        styles = ElementTree.fromstring(archive.read("xl/styles.xml"))
        custom = {int(f.get("numFmtId", "0")): f.get("formatCode", "")
                  for f in styles.iter(s + "numFmt")}
        xfs = styles.find(s + "cellXfs")
        for i, xf in enumerate(xfs.findall(s + "xf") if xfs is not None else []):
            fmt = int(xf.get("numFmtId", "0"))
            code = re.sub(r'"[^"]*"|\[[^\]]*\]', "", custom.get(fmt, "")).casefold()
            if fmt in _BUILTIN_DATE_FORMATS or (re.search(r"[dy]", code) and "0.0" not in code):
                date_styles.add(i)
    names_by_id, base = {}, date(1899, 12, 30)
    try:
        book = ElementTree.fromstring(archive.read("xl/workbook.xml"))
        pr = book.find(s + "workbookPr")
        if pr is not None and pr.get("date1904") in ("1", "true"):
            base = date(1904, 1, 1)
        for i, sheet in enumerate(book.iter(s + "sheet"), 1):
            names_by_id[f"xl/worksheets/sheet{i}.xml"] = sheet.get("name") or f"Sheet{i}"
    except (KeyError, ElementTree.ParseError):
        pass

    def value_of(c) -> str:
        v = c.find(s + "v")
        kind = c.get("t")
        if kind == "s" and v is not None and (v.text or "").isdigit():
            idx = int(v.text)
            return shared[idx] if idx < len(shared) else ""
        if kind == "inlineStr":
            return "".join(t.text or "" for t in c.iter(s + "t"))
        if v is None or not v.text:
            return ""
        text = v.text
        if kind in (None, "n") and re.fullmatch(r"-?\d+(?:\.\d+)?(?:E[+-]?\d+)?", text, re.I):
            if int(c.get("s", "0") or 0) in date_styles:
                try:
                    from datetime import timedelta
                    d = base + timedelta(days=int(float(text)))
                    return f"{d.month:02d}/{d.day:02d}/{d.year}"
                except (OverflowError, ValueError):
                    return text
            if "e" in text.casefold():
                from decimal import Decimal
                try:
                    text = format(Decimal(text), "f")      # 4.11E+15 -> every digit
                except ArithmeticError:
                    pass
            if text.endswith(".0"):
                text = text[:-2]
        return text

    pages = []
    sheets = sorted((n for n in listing if re.fullmatch(r"xl/worksheets/sheet\d+\.xml", n)),
                    key=lambda n: int(re.search(r"(\d+)", n.rsplit("/", 1)[1]).group(1)))
    for part in sheets:
        root = ElementTree.fromstring(archive.read(part))
        rows: list[dict[int, str]] = []
        for row in root.iter(s + "row"):
            cells = {}
            for c in row.iter(s + "c"):
                text = value_of(c).strip()
                if text:
                    cells[_col(c.get("r", ""))] = text
            rows.append(cells)
        header: dict[int, str] = {}
        lines = []
        for cells in rows:
            if not cells:
                continue
            lines.append("    ".join(cells[k] for k in sorted(cells)))
            words = [v for v in cells.values() if not re.fullmatch(r"[\d\s.,$()/%:-]+", v)]
            if not header and len(cells) >= 2 and len(words) == len(cells):
                header = dict(cells)               # the first all-text row heads the columns
                continue
            for k in sorted(cells):
                if k in header and header[k] != cells[k]:
                    lines.append(f"{header[k]}: {cells[k]}")
        pages.append(Page("", names_by_id.get(part, part.rsplit("/", 1)[1]), "\n".join(lines),
                          True, "xlsx", [[[r[k] for k in sorted(r)] for r in rows if r]]))
    return pages


class _HTMLText(html.parser.HTMLParser):
    BLOCK = {"p", "div", "br", "tr", "li", "h1", "h2", "h3", "h4", "h5", "h6", "table", "section"}

    def __init__(self):
        super().__init__()
        self.out: list[str] = []
        self.skip = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.skip += 1
        if tag in self.BLOCK:
            self.out.append("\n")
        if tag in ("td", "th"):
            self.out.append("    ")

    def handle_endtag(self, tag):
        if tag in ("script", "style") and self.skip:
            self.skip -= 1

    def handle_data(self, data):
        if not self.skip:
            self.out.append(data)


def html_text(markup: str) -> str:
    parser = _HTMLText()
    parser.feed(markup)
    return re.sub(r"\n\s*\n+", "\n", "".join(parser.out))


def rtf_text(raw: str) -> str:
    """Plain text of an RTF file: striprtf when installed, else a minimal stripper."""
    try:
        from striprtf.striprtf import rtf_to_text
        return rtf_to_text(raw)
    except Exception:
        pass
    text = re.sub(r"\\par[d]?\b ?", "\n", raw)
    text = re.sub(r"\\'([0-9a-fA-F]{2})", lambda m: bytes.fromhex(m.group(1)).decode("cp1252", "replace"), text)
    text = re.sub(r"\\[a-zA-Z]+-?\d* ?", "", text)
    return re.sub(r"[{}]", "", text)


def read_legacy_doc(path: Path) -> list[Page]:
    """A .doc or .wpd: whichever converter this machine has."""
    for cmd in (["textutil", "-convert", "txt", "-stdout", str(path)],
                ["antiword", str(path)], ["catdoc", str(path)]):
        if shutil.which(cmd[0]):
            try:
                out = subprocess.run(cmd, capture_output=True, timeout=120, check=False)
            except (OSError, subprocess.SubprocessError):
                continue
            if out.returncode == 0 and out.stdout.strip():
                return [Page("", "", out.stdout.decode("utf-8", "replace"), True, cmd[0])]
    raise Unreadable("old Word format and no converter here (textutil, antiword or catdoc)")


@dataclass
class Scan:
    """What reading one case produced."""

    pages: list[Page] = field(default_factory=list)
    queue: list[dict] = field(default_factory=list)      # pages a model must transcribe
    unreadable: list[tuple[str, str]] = field(default_factory=list)
    ocr_pages: int = 0


def read_pdf(path: Path, rel: str, work: Path, scan: Scan, use_ocr: bool) -> None:
    fitz = _fitz()
    if fitz is None:
        _read_pdf_fallback(path, rel, work, scan)
        return
    try:
        doc = fitz.open(str(path))
    except Exception as exc:
        raise Unreadable(f"cannot open PDF ({exc})")
    if doc.needs_pass:
        raise Unreadable("password-protected PDF")
    for index, page in enumerate(doc):
        text = page.get_text("text", sort=True) or ""
        layer = _has_text(text, page)
        if layer == "text":
            scan.pages.append(Page(rel, str(index + 1), text, index == 0, "text"))
            continue
        if layer == "both":
            # a scan carrying a typed legend or Bates stamp: keep the typed text,
            # and read the picture too
            scan.pages.append(Page(rel, f"{index + 1}", text, index == 0, "text"))
        image = _queue_image(work, rel, index + 1)
        page.get_pixmap(dpi=OCR_DPI if use_ocr and tesseract() else RENDER_DPI).save(str(image))
        image = _name_by_content(image, rel, index + 1)
        _ocr_or_queue(image, rel, str(index + 1), index == 0, scan, use_ocr)
    doc.close()


THIN_LAYER_CHARS = 1000      # a page-size picture under less text than this is read too


def _has_text(text: str, page) -> str:
    """"text" (read the text layer), "image" (no usable text: read the picture),
    or "both" (a picture covering the page under a thin layer - a scan with a
    typed legend, a Bates stamp, a confidentiality footer)."""
    solid = len(re.sub(r"\s", "", text))
    if solid < MIN_TEXT_CHARS:
        return "image"
    try:
        area = abs(page.rect)
        covered = sum(abs(r) for img in page.get_images(full=True)
                      for r in page.get_image_rects(img[0]))
        pictured = bool(area) and covered / area > 0.5
    except Exception:
        pictured = False
    if pictured and solid < SCAN_TEXT_CHARS:
        return "image"
    if pictured and solid < THIN_LAYER_CHARS:
        return "both"
    return "text"


def _read_pdf_fallback(path: Path, rel: str, work: Path, scan: Scan) -> None:
    """Without PyMuPDF: pdfplumber, pypdf or pdftotext for text; pdftoppm for images."""
    texts: list[str] = []
    if _have("pdfplumber"):
        import pdfplumber
        try:
            with pdfplumber.open(str(path)) as pdf:
                texts = [p.extract_text() or "" for p in pdf.pages]
        except Exception as exc:
            raise Unreadable(f"cannot open PDF ({exc})")
    elif _have("pypdf"):
        import pypdf
        try:
            reader = pypdf.PdfReader(str(path))
            if reader.is_encrypted:
                raise Unreadable("password-protected PDF")
            texts = [p.extract_text() or "" for p in reader.pages]
        except Unreadable:
            raise
        except Exception as exc:
            raise Unreadable(f"cannot open PDF ({exc})")
    elif shutil.which("pdftotext"):
        out = subprocess.run(["pdftotext", "-layout", str(path), "-"], capture_output=True,
                             timeout=300, check=False)
        texts = out.stdout.decode("utf-8", "replace").split("\f")
    else:
        raise Unreadable("no PDF reader here (PyMuPDF, pdfplumber, pypdf or pdftotext)")
    covered = _plumber_coverage(path) if _have("pdfplumber") else {}
    for index, text in enumerate(texts):
        solid = len(re.sub(r"\s", "", text))
        if solid >= MIN_TEXT_CHARS:
            scan.pages.append(Page(rel, str(index + 1), text, index == 0, "text"))
            # a thin layer over what may be a picture (a scan under a Bates stamp):
            # without PyMuPDF's image map, read the picture too unless pdfplumber
            # says the page holds none
            if solid >= THIN_LAYER_CHARS or (covered and covered.get(index, 0) <= 0.5):
                continue
            if solid >= SCAN_TEXT_CHARS and not covered:
                continue
        image = _queue_image(work, rel, index + 1)
        if shutil.which("pdftoppm"):
            subprocess.run(["pdftoppm", "-png", "-r", str(RENDER_DPI), "-f", str(index + 1),
                            "-l", str(index + 1), "-singlefile", str(path),
                            str(image.with_suffix(""))], capture_output=True, check=False)
        if image.exists():
            image = _name_by_content(image, rel, index + 1)
        _ocr_or_queue(image if image.exists() else None, rel, str(index + 1), index == 0, scan,
                      True, pdf=path)


def _name_by_content(image: Path, rel: str, page: int) -> Path:
    """Rename a rendered page by what it shows, so a transcript made from an
    older version of a rescanned file is never reused."""
    digest = hashlib.sha1(f"{rel}#{page}".encode() + image.read_bytes()).hexdigest()[:12]
    target = image.with_name(f"{digest}{image.suffix}")
    image.replace(target)
    return target


def _plumber_coverage(path: Path) -> dict[int, float]:
    """Share of each page covered by pictures, by pdfplumber - {} if unknown."""
    try:
        import pdfplumber
        out = {}
        with pdfplumber.open(str(path)) as pdf:
            for i, page in enumerate(pdf.pages):
                area = float(page.width * page.height) or 1.0
                out[i] = sum(float((im["x1"] - im["x0"]) * (im["bottom"] - im["top"]))
                             for im in page.images) / area
        return out
    except Exception:
        return {}


def _queue_image(work: Path, rel: str, page: int) -> Path:
    digest = hashlib.sha1(f"{rel}#{page}".encode()).hexdigest()[:12]
    folder = work / "images"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{digest}.png"


def _ocr_or_queue(image: Path | None, rel: str, page: str, first: bool, scan: Scan,
                  use_ocr: bool, pdf: Path | None = None) -> None:
    if image is not None and use_ocr and tesseract():
        text, confidence = ocr_image(image)
        if len(re.sub(r"\s", "", text)) >= MIN_TEXT_CHARS and confidence >= OCR_MIN_CONFIDENCE:
            scan.pages.append(Page(rel, page, text, first, "ocr"))
            scan.ocr_pages += 1
            return
    entry = {"id": image.stem if image is not None
             else hashlib.sha1(f"{rel}#{page}".encode()).hexdigest()[:12],
             "file": rel, "page": page, "first": first,
             "image": str(image) if image is not None else None}
    if image is None and pdf is not None:
        entry["pdf"] = str(pdf)          # no renderer: the model opens the PDF page itself
    scan.queue.append(entry)


def read_image(path: Path, rel: str, work: Path, scan: Scan, use_ocr: bool) -> None:
    """A photo or a scan saved as an image: copied (as PNG where it is not a
    format a model can view) into the work folder, then OCR'd or queued."""
    ext = path.suffix.lower()
    target = _queue_image(work, rel, 1)
    if ext in VIEWABLE:
        target = target.with_suffix(ext)
        shutil.copyfile(path, target)
    else:
        # HEIC, TIFF, BMP: converted where the machine can, so a model can see it
        if shutil.which("sips"):
            subprocess.run(["sips", "-s", "format", "png", str(path), "--out", str(target)],
                           capture_output=True, check=False)
        if not target.exists() and _have("PIL"):
            try:
                from PIL import Image
                Image.open(path).save(target)
            except Exception:
                pass
        if not target.exists():
            raise Unreadable("image format this machine cannot convert")
    target = _name_by_content(target, rel, 1)
    _ocr_or_queue(target, rel, "1", True, scan, use_ocr)


def read_email(path: Path, rel: str, work: Path, scan: Scan, use_ocr: bool, depth: int) -> None:
    try:
        message = email.message_from_bytes(path.read_bytes(), policy=email.policy.default)
    except Exception as exc:
        raise Unreadable(f"cannot parse email ({exc})")
    head = "\n".join(f"{h}: {message[h]}" for h in ("From", "To", "Cc", "Bcc", "Reply-To",
                                                  "Subject", "Date") if message[h])
    body = []
    attach_dir = work / "attachments" / hashlib.sha1(rel.encode()).hexdigest()[:12]
    inline = 0
    for part in message.walk():
        if part.is_multipart():
            continue
        name = part.get_filename()
        ctype = part.get_content_type()
        if not name and not ctype.startswith("text/"):
            # a phone photo pasted into the message has no file name
            inline += 1
            ext = {"image/jpeg": ".jpg", "image/png": ".png", "image/gif": ".gif",
                   "image/heic": ".heic", "image/tiff": ".tif",
                   "application/pdf": ".pdf"}.get(ctype)
            if ext:
                name = f"inline_{inline}{ext}"
        if name:
            attach_dir.mkdir(parents=True, exist_ok=True)
            target = attach_dir / Path(name).name
            try:
                target.write_bytes(part.get_payload(decode=True) or b"")
            except OSError:
                continue
            read_any(target, f"{rel} > {target.name}", work, scan, use_ocr, depth + 1)
        elif ctype == "text/plain":
            body.append(part.get_content())
        elif ctype == "text/html" and not body:
            body.append(html_text(part.get_content()))
    scan.pages.append(Page(rel, "", head + "\n\n" + "\n".join(body), True, "email"))


def read_msg(path: Path, rel: str) -> list[Page]:
    try:
        import extract_msg
    except Exception:
        raise Unreadable("Outlook .msg and extract-msg is not installed")
    try:
        msg = extract_msg.Message(str(path))
        head = "\n".join(f"{k}: {v}" for k, v in (("From", msg.sender), ("To", msg.to),
                                                  ("Cc", msg.cc), ("Subject", msg.subject),
                                                  ("Date", msg.date)) if v)
        return [Page(rel, "", f"{head}\n\n{msg.body or ''}", True, "msg")]
    except Exception as exc:
        raise Unreadable(f"cannot parse .msg ({exc})")


def read_any(path: Path, rel: str, work: Path, scan: Scan, use_ocr: bool, depth: int = 0) -> None:
    """Read one file into ``scan``; an unreadable one is recorded, not raised."""
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            read_pdf(path, rel, work, scan, use_ocr)
        elif ext in (".docx", ".docm", ".dotx"):
            scan.pages += _named(read_docx(path), rel)
        elif ext in (".xlsx", ".xlsm"):
            scan.pages += _named(read_xlsx(path), rel)
        elif ext == ".odt":
            scan.pages += _named(read_odt(path), rel)
        elif ext in (".doc", ".wpd"):
            scan.pages += _named(read_legacy_doc(path), rel)
        elif ext == ".rtf":
            scan.pages.append(Page(rel, "", rtf_text(path.read_text("latin-1")), True, "rtf"))
        elif ext in (".htm", ".html"):
            scan.pages.append(Page(rel, "", html_text(path.read_text("utf-8", "replace")), True,
                                   "html"))
        elif ext in TEXT_EXT:
            scan.pages.append(Page(rel, "", path.read_text("utf-8", "replace"), True, "text"))
        elif ext == ".eml":
            read_email(path, rel, work, scan, use_ocr, depth)
        elif ext == ".msg":
            scan.pages += read_msg(path, rel)
        elif ext in IMAGE_EXT:
            read_image(path, rel, work, scan, use_ocr)
        elif ext == ".zip" and depth < 3:
            inner = work / "archives" / hashlib.sha1(rel.encode()).hexdigest()[:12]
            _safe_extract(path, inner)
            for child in walk(inner):
                read_any(child, f"{rel} > {child.relative_to(inner).as_posix()}", work, scan,
                         use_ocr, depth + 1)
        else:
            raise Unreadable(f"file type {ext or '(none)'} is not read")
    except Unreadable as exc:
        scan.unreadable.append((rel, str(exc)))
    except Exception as exc:                       # a broken file never stops the run
        scan.unreadable.append((rel, f"failed while reading ({type(exc).__name__}: {exc})"))


def _named(pages: list[Page], rel: str) -> list[Page]:
    for p in pages:
        p.file = rel
    return pages


def _safe_extract(archive: Path, target: Path) -> None:
    """Unzip without letting a member path climb out of ``target``."""
    target.mkdir(parents=True, exist_ok=True)
    root = target.resolve()
    with zipfile.ZipFile(archive) as z:
        for member in z.infolist():
            dest = (target / member.filename).resolve()
            if root not in dest.parents and dest != root:
                continue
            if member.is_dir():
                dest.mkdir(parents=True, exist_ok=True)
            else:
                dest.parent.mkdir(parents=True, exist_ok=True)
                with z.open(member) as src, open(dest, "wb") as out:
                    shutil.copyfileobj(src, out)


EXCLUDE: list[str] = []          # --exclude patterns, matched to a name or a relative path


def excluded(rel: str) -> bool:
    name = rel.rsplit("/", 1)[-1]
    return any(fnmatch.fnmatch(name.casefold(), pat.casefold())
               or fnmatch.fnmatch(rel.casefold(), pat.casefold()) for pat in EXCLUDE)


def walk(folder: Path):
    """Every file under ``folder``, in a fixed order, skipping system and lock
    files and whatever ``--exclude`` names."""
    for path in sorted(folder.rglob("*"), key=lambda p: p.as_posix().casefold()):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(folder).parts
        if any(part.startswith(".") or part == "__MACOSX" for part in rel_parts):
            continue
        if path.name.casefold() in SKIP_NAMES or path.name.startswith("~$"):
            continue
        if EXCLUDE and excluded(path.relative_to(folder).as_posix()):
            continue
        yield path


# --------------------------------------------------------------------------
# cases
# --------------------------------------------------------------------------

SECTION_WORDS = re.compile(
    r"(?i)^(?:\d+[\s._-]*)?(?:pleadings?|discovery|financials?|finances|correspondence|letters|"
    r"exhibits?|banks?|bank\s*statements|statements|accounts?|tax(?:es|\s*returns)?|paystubs?|"
    r"pay\s*stubs|income|medical|emails?|drafts?|orders?|court|filings?|notes|misc|other|"
    r"expenses|receipts|credit(?:\s*reports?)?|benefits|real\s*estate|property|vehicles?|"
    r"amazon|payment\s*apps?|scans?|scanned|photos?|images|disclosures?|declarations?|"
    r"motions?|decrees?|evidence|documents?|docs|intake|client|records|variants?.*)$")


def find_cases(root: Path, single: bool) -> dict[str, Path | list[Path]]:
    """Case name -> its folder (or, for loose files at the top, the list of them)."""
    if single:
        return {root.name: root}
    subdirs = sorted((p for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")
                      and p.name != "__MACOSX"), key=lambda p: p.name.casefold())
    loose = [p for p in walk(root) if p.parent == root]
    if not subdirs:
        return {root.name: root}
    cases: dict[str, Path | list[Path]] = {d.name: d for d in subdirs}
    if loose:
        cases[f"{root.name} (top-level files)"] = loose
    return cases


def looks_like_sections(root: Path) -> bool:
    subdirs = [p.name for p in root.iterdir() if p.is_dir() and not p.name.startswith(".")]
    return bool(subdirs) and sum(bool(SECTION_WORDS.match(n)) for n in subdirs) >= max(
        1, (len(subdirs) + 1) // 2)


def safe_name(name: str) -> str:
    return re.sub(r"[^\w.\- ]+", "_", name).strip() or "case"


def save_json(path: Path, data) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    tmp.replace(path)


def load_json(path: Path, default=None):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return default


def scan_case(name: str, source, work: Path, use_ocr: bool) -> dict:
    scan = Scan()
    if isinstance(source, list):
        base = source[0].parent if source else work
        files = source
    else:
        base, files = source, list(walk(source))
    for path in files:
        read_any(path, path.relative_to(base).as_posix(), work, scan, use_ocr)
    with open(work / "pages.jsonl", "w", encoding="utf-8") as fh:
        for p in scan.pages:
            fh.write(json.dumps({"file": p.file, "page": p.page, "first": p.first,
                                 "method": p.method, "text": p.text, "tables": p.tables},
                                ensure_ascii=False) + "\n")
    save_json(work / "queue.json", scan.queue)
    info = {"case": name, "files": len(files), "pages": len(scan.pages) + len(scan.queue),
            "ocr_pages": scan.ocr_pages, "queued": len(scan.queue),
            "unreadable": scan.unreadable}
    save_json(work / "scan.json", info)
    return info


def load_pages(work: Path) -> list[Page]:
    pages = []
    with open(work / "pages.jsonl", encoding="utf-8") as fh:
        for line in fh:
            d = json.loads(line)
            pages.append(Page(d["file"], d["page"], d["text"], d["first"], d["method"],
                              d.get("tables") or []))
    for item in load_json(work / "queue.json", []):
        t = work / "transcripts" / f"{item['id']}.txt"
        if t.exists():
            text = t.read_text("utf-8", "replace")
            if UNREADABLE.fullmatch(text.strip()):
                continue                       # counted by unread_transcripts()
            pages.append(Page(item["file"], item["page"], text, item["first"], "transcribed"))
    pages.sort(key=lambda p: (p.file.casefold(), int(p.page) if p.page.isdigit() else 0, p.page))
    return pages


UNREADABLE = re.compile(r"(?i)(?:\[(?:illegible|unreadable)\]\s*)+")


def unread_transcripts(work: Path) -> list[str]:
    """Pages the transcriber could not read at all - reported like unread files."""
    out = []
    for item in load_json(work / "queue.json", []):
        t = work / "transcripts" / f"{item['id']}.txt"
        if t.exists() and UNREADABLE.fullmatch(t.read_text("utf-8", "replace").strip()):
            out.append(f"{item['file']} p.{item['page']}")
    return out


def missing_transcripts(work: Path) -> list[dict]:
    return [q for q in load_json(work / "queue.json", [])
            if not (work / "transcripts" / f"{q['id']}.txt").exists()]



# text as a reader sees it

def _norm(text: str) -> str:
    return " ".join(text.split())


_FOLD = str.maketrans({"’": "'", "‘": "'", "“": '"', "”": '"', "–": "-", "—": "-",
                       "\u00a0": " "})


def _fold(text: str) -> str:
    """Text compared as a reader sees it: one space, one apostrophe, one dash,
    compatibility forms unfolded (NFKC), no case."""
    return _norm(unicodedata.normalize("NFKC", text).translate(_FOLD)).casefold()
