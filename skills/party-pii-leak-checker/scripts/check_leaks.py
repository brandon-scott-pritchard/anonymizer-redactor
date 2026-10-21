#!/usr/bin/env python3
"""Find every party identifier that survived anonymization or redaction.

One command, run again after each step it asks for:

    python check_leaks.py run ANONYMIZED --csv PARTIES.csv [--out DIR]

ANONYMIZED is the Anonymizer's output for one case (its zip, or the folder it
was unzipped to). PARTIES.csv is the party-PII extractor's CSV for the same
case: every party value to look for, in every form it can be printed.

What is searched, deterministically:

* the visible text of every page, and every written form of every value -
  name variants, SSN / phone / date / address / account formats, masked tails,
  the same digits with other separators, a surname glued into a longer word,
  an OCR-style near miss;
* where a reader does not look: text still under a black box, words in images
  (OCR), document properties, comments, tracked deletions, annotations, form
  fields, bookmarks, alt text, link targets, and the file names themselves.

Then a model reads the anonymized pages for anything else that still points
to the parties (the read-through). The result is ``leak_report.csv``: one row
per surviving value per place, with its document, page, line and context, for
the operator to fix by hand.

The command prints one ``NEXT:`` line and exits 0 done, 2 transcriptions
needed, 5 read-through batches needed, 1 an error. Values never go to
standard output - counts only.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import re
import shutil
import sys
import zipfile
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from xml.etree import ElementTree

# the Anonymizer's detectors and value forms (partycore/) and the shared file
# readers (partyread.py): beside this script in a packaged skill, in
# skills/_shared/ in the source tree
HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
if not (HERE / "partycore").is_dir():
    sys.path.insert(0, str(HERE.parents[1] / "_shared"))
from partycore import forms, names  # noqa: E402
from partyread import (  # noqa: E402
    EXCLUDE, Page, Scan, _fitz, _fold, _has_text, _norm, _safe_extract, load_json, load_pages,
    missing_transcripts, ocr_image, read_any, safe_name, save_json, scan_case, tesseract,
    unread_transcripts, walk)

FIELD_KIND = {"names": "name", "date of birth": "dob", "ssn": "ssn", "addresses": "address",
              "phones": "phone", "emails": "email", "tin / ein": "tin",
              "driver's licence": "dl", "driver's license": "dl", "passport": "passport",
              "employers": "employer", "businesses": "business", "accounts": "account",
              "cards": "card", "handles": "handle", "vehicles": "vehicle",
              "case numbers": "case_number", "parcels": "parcel",
              "other identifiers": "other"}
CONFIDENCE_ORDER = {"certain": 0, "likely": 1, "possible": 2, "model": 3, "not searched": 4}
REPORT_HEADER = ["Document", "Page", "Where", "Line", "Found", "Matched value", "Role", "Field",
                 "How", "Confidence", "Context"]


# --------------------------------------------------------------------------
# the party values to look for
# --------------------------------------------------------------------------

@dataclass
class Target:
    row: int            # the line of the party CSV it came from
    role: str
    field: str          # the CSV's own label
    kind: str
    value: str


def read_targets(path: Path) -> tuple[list[Target], list[str]]:
    """The party CSV's rows, and anything wrong with it named exactly."""
    problems, out = [], []
    try:
        with open(path, encoding="utf-8-sig", newline="") as fh:
            reader = csv.DictReader(fh)
            cols = {c.strip().casefold(): c for c in reader.fieldnames or []}
            for need in ("role", "field", "value"):
                if need not in cols:
                    problems.append(f'the CSV has no "{need.title()}" column')
            if problems:
                return [], problems
            for n, row in enumerate(reader, 2):
                value = (row.get(cols["value"]) or "").strip().lstrip("'").strip()
                field = (row.get(cols["field"]) or "").strip()
                if not value:
                    continue
                kind = FIELD_KIND.get(field.replace("’", "'").replace("‘", "'").casefold())
                if kind is None:
                    problems.append(f"row {n}: unknown Field {field!r}")
                    continue
                out.append(Target(n, (row.get(cols["role"]) or "").strip(), field, kind, value))
    except (OSError, UnicodeDecodeError, csv.Error) as exc:
        problems.append(f"cannot read it ({exc})")
    if not out and not problems:
        problems.append("it holds no values")
    return out, problems


# --------------------------------------------------------------------------
# where a reader does not look
# --------------------------------------------------------------------------

DARK = 0.25                  # a fill this dark (0 black .. 1 white) is a redaction box
COVERED = 0.5                # share of a word's box under a black box that hides it
IMAGE_SHARE = 0.02           # an image this much of a text page is read too


def hidden_pages(path: Path, rel: str, work: Path, use_ocr: bool) -> tuple[list[Page], list[dict]]:
    """Pages for everything in a file that is not its visible text, and images
    to queue for a model where OCR cannot read them."""
    ext = path.suffix.lower()
    try:
        if ext == ".pdf":
            return _pdf_hidden(path, rel, work, use_ocr)
        if ext in (".docx", ".docm", ".dotx", ".xlsx", ".xlsm", ".pptx"):
            return _ooxml_hidden(path, rel, work, use_ocr)
    except Exception as exc:                        # a broken file never stops the run
        return [Page(rel, "", f"the file's hidden places could not be inspected "
                              f"({type(exc).__name__}: {exc})", True, INSPECTION_FAILED)], []
    return [], []


INSPECTION_FAILED = "inspection failed"


def _step(failures: list[str], what: str, fn, *args, default=None):
    """One inspection step; a failure is recorded and the file's other steps go on."""
    try:
        return fn(*args)
    except Exception as exc:
        failures.append(f"{what}: {type(exc).__name__}: {exc}")
        return default


def _pdf_hidden(path: Path, rel: str, work: Path, use_ocr: bool):
    fitz = _fitz()
    if fitz is None:
        return [Page(rel, "", "PyMuPDF is not installed: black boxes, annotations, form fields "
                              "and pictures in this PDF were not checked", True,
                     INSPECTION_FAILED)], []
    pages, queue, failures = [], [], []
    doc = fitz.open(str(path))

    def add(text: str, number: str, first: bool, where: str):
        if text and text.strip():
            pages.append(Page(rel, number, text, first, where))

    def properties():
        meta = "\n".join(f"{k}: {v}" for k, v in (doc.metadata or {}).items() if v)
        xmp = doc.get_xml_metadata() or ""
        xmp_text = " ".join(t.strip() for t in re.findall(r">([^<>]+)<", xmp) if t.strip())
        add(meta + ("\n" + xmp_text if xmp_text else ""), "", True, "document properties")
    _step(failures, "document properties", properties)
    _step(failures, "bookmarks", lambda: add("\n".join(t[1] for t in doc.get_toc()), "", True,
                                             "bookmarks"))

    def attachments():
        # a file attached inside the PDF: its name, and what it holds
        folder = work / "attachments" / hashlib.sha1(rel.encode()).hexdigest()[:12]
        for i in range(doc.embfile_count()):
            info = doc.embfile_info(i)
            name = Path(info.get("filename") or f"attachment{i + 1}").name
            add(f"{name} {info.get('desc', '')}", "", True, "attached file name")
            folder.mkdir(parents=True, exist_ok=True)
            target = folder / name
            target.write_bytes(doc.embfile_get(i))
            scan = Scan()
            read_any(target, f"{rel} > {name}", work, scan, use_ocr, 1)
            for page in scan.pages:
                page.method = "attached file"
            pages.extend(scan.pages)
            queue.extend(scan.queue)
    _step(failures, "attached files", attachments)

    # text on a layer someone switched off (optional content): turned on to read it
    layered = {}
    if _step(failures, "layers", lambda: bool(doc.get_ocgs()), default=False):
        def all_layers_on():
            for n in range(len(doc.layer_ui_configs())):
                doc.set_layer_ui_config(n, action=0)
        _step(failures, "layers", all_layers_on)
        layered = {i: _step(failures, f"page {i + 1} layers", lambda pg=pg: pg.get_text("text"),
                            default="") for i, pg in enumerate(doc)}

    for index, page in enumerate(doc):
        number, first = str(index + 1), index == 0
        _step(failures, f"page {number}", _pdf_page, fitz, doc, page, rel, number, first, work,
              use_ocr, add, queue, layered.get(index, ""), failures)
    doc.close()
    if failures:
        pages.append(Page(rel, "", "some hidden places could not be checked: "
                          + "; ".join(failures[:6]), True, INSPECTION_FAILED))
    return pages, queue


def _pdf_page(fitz, doc, page, rel, number, first, work, use_ocr, add, queue, layered, failures):
    visible = page.get_text("text") or ""
    # text a viewer crops away still sits in the file
    try:
        whole = page.get_text("text", clip=page.mediabox) or ""
        if len(_norm(whole)) > len(_norm(visible)) + 3:
            extra = [l for l in whole.split("\n") if l.strip() and l not in visible]
            add("\n".join(extra), number, first, "outside the visible page area")
    except Exception as exc:
        failures.append(f"page {number} cropped area: {type(exc).__name__}")
    if layered and len(_norm(layered)) > len(_norm(visible)) + 3:
        add("\n".join(l for l in layered.split("\n") if l.strip() and l not in visible),
            number, first, "hidden layer")
    try:
        words = page.get_text("words")
        boxes = []
        for d in page.get_drawings():
            fill = d.get("fill")
            if fill and max(fill) <= DARK and d.get("rect") is not None:
                boxes.append(fitz.Rect(d["rect"]))
        under = [w[4] for w in words
                 if any(abs(fitz.Rect(w[:4]) & b) / (abs(fitz.Rect(w[:4])) or 1.0) >= COVERED
                        for b in boxes)]
        add(" ".join(under), number, first, "text under a black box")
    except Exception as exc:
        failures.append(f"page {number} black boxes: {type(exc).__name__}")
    try:
        notes = []
        for annot in page.annots() or []:
            kind = annot.type[1] if annot.type else ""
            text = " ".join(x for x in (annot.info.get("content", ""), annot.info.get("title", ""),
                                        annot.info.get("subject", "")) if x)
            if kind == "Redact":
                notes.append(f"(unapplied redaction) {page.get_textbox(annot.rect).strip()}")
            if text:
                notes.append(text)
        add("\n".join(notes), number, first, "annotation or comment")
    except Exception as exc:
        failures.append(f"page {number} annotations: {type(exc).__name__}")
    try:
        add("\n".join(f"{w.field_name}: {w.field_value}" for w in page.widgets() or []
                      if w.field_value not in (None, "", False)), number, first, "form field")
    except Exception as exc:
        failures.append(f"page {number} form fields: {type(exc).__name__}")
    try:
        add("\n".join(l.get("uri", "") for l in page.get_links() if l.get("uri")), number,
            first, "link target")
    except Exception as exc:
        failures.append(f"page {number} links: {type(exc).__name__}")
    # a picture on a page that also has text - a pasted photo, a signature - can
    # carry a value no text replacement touched. A scan the reader already OCR'd
    # (a thin layer over a page-size picture) is not read twice.
    try:
        if len(re.sub(r"\s", "", visible)) >= 25 and _has_text(visible, page) == "text":
            share = sum(abs(r) for img in page.get_images(full=True)
                        for r in page.get_image_rects(img[0])) / (abs(page.rect) or 1.0)
            if share >= IMAGE_SHARE:
                image = _image_path(work, rel, number)
                page.get_pixmap(dpi=300 if use_ocr and tesseract() else 200).save(str(image))
                read = _ocr_or_queue(image, rel, number, use_ocr)
                if isinstance(read, Page):
                    add(read.text, number, first, read.method)
                elif read:
                    queue.append(read)
    except Exception as exc:
        failures.append(f"page {number} pictures: {type(exc).__name__}")


CP = "{http://schemas.openxmlformats.org/package/2006/metadata/core-properties}"
_DOCX_IMAGES = re.compile(r"(?:(?:word|xl|ppt)/media/[^/]+|docProps/thumbnail)"
                          r"\.(?:png|jpe?g|gif|bmp|tiff?|emf|wmf)$", re.I)
# the parts whose every word is hidden from a reader of the page
_HIDDEN_PARTS = re.compile(r"(?:docProps/[^/]+|customXml/item\d*|word/people|"
                           r"word/glossary/[^/]+)\.xml$")
# who did what: tracked changes and comments carry their author's name
_PERSON_ATTRS = ("author", "initials", "providerId", "userId")


def _ooxml_hidden(path: Path, rel: str, work: Path, use_ocr: bool):
    """A Word, Excel or PowerPoint file's properties, custom XML, people,
    glossary, tracked deletions, change and comment authors, field codes, link
    targets, alt text, and the pictures inside it (the thumbnail too)."""
    pages, queue, failures = [], [], []
    with zipfile.ZipFile(path) as z:
        listing = z.namelist()

        def xml(part):
            return ElementTree.fromstring(z.read(part))
        props = []
        for part in sorted(p for p in listing if _HIDDEN_PARTS.search(p)):
            root = _step(failures, part, xml, part)
            if root is None:
                continue
            props += [f"{el.tag.split('}')[-1]}: {el.text.strip()}" for el in root.iter()
                      if el.text and el.text.strip()]
        if props:
            pages.append(Page(rel, "", "\n".join(props), True, "document properties"))
        deleted, fields, alt, people = [], [], [], []
        for part in listing:
            if not re.fullmatch(r"(?:word|xl|ppt)/[^/]+\.xml|ppt/slides/[^/]+\.xml", part):
                continue
            root = _step(failures, part, xml, part)
            if root is None:
                continue
            for el in root.iter():
                tag = el.tag.split("}")[-1]
                if tag == "delText" and el.text:
                    deleted.append(el.text)
                elif tag == "instrText" and el.text:
                    fields.append(el.text.strip())
                elif tag == "docPr":
                    alt += [el.get(a) for a in ("descr", "title", "name") if el.get(a)]
                for key, val in el.attrib.items():
                    name = key.split("}")[-1]
                    if name in _PERSON_ATTRS and val:
                        people.append(val)
                    elif name == "instr" and val:
                        fields.append(val.strip())     # w:fldSimple w:instr="HYPERLINK mailto:..."
        if deleted:
            pages.append(Page(rel, "", "".join(deleted), True, "tracked deletion"))
        if fields:
            pages.append(Page(rel, "", "\n".join(fields), True, "field code"))
        if alt:
            pages.append(Page(rel, "", "\n".join(alt), True, "image alt text"))
        if people:
            pages.append(Page(rel, "", "\n".join(sorted(set(people))), True,
                              "change or comment author"))
        targets = []
        for part in (p for p in listing if p.endswith(".rels")):
            root = _step(failures, part, xml, part)
            if root is not None:
                targets += [el.get("Target", "") for el in root.iter()
                            if el.get("TargetMode") == "External" and el.get("Target")]
        if targets:
            pages.append(Page(rel, "", "\n".join(targets), True, "link target"))
        for n, part in enumerate(sorted(p for p in listing if _DOCX_IMAGES.search(p)), 1):
            suffix = Path(part).suffix.lower()
            if suffix in (".emf", ".wmf"):
                continue
            image = _image_path(work, rel, f"image{n}").with_suffix(suffix)
            image.write_bytes(z.read(part))
            label = "thumbnail" if part.startswith("docProps/") else f"image {n}"
            read = _step(failures, part, _ocr_or_queue, image, rel, label, use_ocr)
            if isinstance(read, Page):
                pages.append(read)
            elif read:
                queue.append(read)
    if failures:
        pages.append(Page(rel, "", "some hidden places could not be checked: "
                          + "; ".join(failures[:6]), True, INSPECTION_FAILED))
    return pages, queue


def _image_path(work: Path, rel: str, page: str) -> Path:
    folder = work / "hidden_images"
    folder.mkdir(parents=True, exist_ok=True)
    return folder / f"{hashlib.sha1(f'{rel}#{page}'.encode()).hexdigest()[:12]}.png"


MIN_PICTURE = (120, 40)          # pixels: smaller is a bullet, a rule line, an icon


def _ocr_or_queue(image: Path, rel: str, page: str, use_ocr: bool):
    """A Page of what OCR read in the picture, or a queue entry for a model. A
    picture OCR reads as blank is queued too when it is big enough to hold a
    signature or a handwritten note - those are what OCR cannot read."""
    if use_ocr and tesseract():
        text, confidence = ocr_image(image)
        if len(re.sub(r"\s", "", text)) >= 4 and confidence >= 70:
            return Page(rel, page, text, False, "image (OCR)")
        if len(re.sub(r"\s", "", text)) < 4 and not _big_enough(image):
            return None
    digest = hashlib.sha1(f"{rel}#{page}".encode() + image.read_bytes()).hexdigest()[:12]
    return {"id": digest, "file": rel, "page": page, "first": False, "image": str(image),
            "where": "image"}


def _big_enough(image: Path) -> bool:
    try:
        from PIL import Image
        with Image.open(image) as im:
            return im.width >= MIN_PICTURE[0] and im.height >= MIN_PICTURE[1]
    except Exception:
        return True                    # cannot tell: a model looks at it


# --------------------------------------------------------------------------
# matching
# --------------------------------------------------------------------------

NOT_SEARCHED = Target(0, "", "", "other", "")


@dataclass
class Leak:
    file: str
    page: str
    where: str
    line: int
    found: str
    target: Target
    how: str
    confidence: str
    context: str


def _clip(line: str, at: int, end: int, width: int = 140) -> str:
    text = line[:at] + "[[" + line[at:end] + "]]" + line[end:]
    if len(text) <= width:
        return " ".join(text.split())
    start = max(0, at - width // 3)
    return ("..." if start else "") + " ".join(text[start:start + width].split()) + "..."


def _boundary_regex(forms_: list[str], ignore_case: bool = True) -> re.Pattern | None:
    bodies = sorted({r"\s+".join(names.escape_token(t) for t in f.split()) for f in forms_ if f},
                    key=len, reverse=True)
    if not bodies:
        return None
    # the app's own boundaries: only a letter or digit joins a word, so a surname
    # beside "_", "-", "." or "@" - Ashdown_Divorce, rowena.ashdown@, Ashdown-Radcliffe -
    # is found
    return re.compile(rf"{names.BOUNDARY_LEFT}(?:{'|'.join(bodies)}){names.BOUNDARY_RIGHT}",
                      re.IGNORECASE if ignore_case else 0)


class Matcher:
    """Every way one party value can still be printed, as patterns tagged with
    how sure a hit is."""

    def __init__(self, t: Target, family: set[str]):
        self.t = t
        self.rules: list[tuple[re.Pattern, str, str]] = []      # (regex, how, confidence)
        self.digits = ""            # the value's digits, found however they are separated
        self.alnum = ""             # an id's letters and digits, however separated
        self.surname = ""           # glued forms: "AshdownDivorce", "jsmith"
        self.fuzzy: list[str] = []  # words an OCR misreading could stand for
        getattr(self, f"_{t.kind}", self._other)(t.value, family)

    def add(self, forms_: list[str], how: str, confidence: str, ignore_case: bool = True):
        regex = _boundary_regex(forms_, ignore_case)
        if regex is not None:
            self.rules.append((regex, how, confidence))

    # -- per kind -----------------------------------------------------------

    def _name(self, value: str, family: set[str]):
        person = names.parse(value)
        multi, single, everyday, short = [], [], [], []
        for v in names.variants(person):
            words = v.text.split()
            word = v.text.strip(".")
            if len(words) > 1:
                multi.append(v.text)
            elif len(word) == 2 and word.isalpha() and v.text == person.last:
                short.append(word)               # "Wu", "Li", "Ng": a surname all the same
            elif len(word) >= 3:
                (everyday if (v.everyday or v.risky) else single).append(v.text)
        self.add(multi, "a written form of the name", "certain")
        # a single word of the name, in any case - a surname that is also a town
        # (Ashdown, Jordan) is still the surname
        self.add(single, "one word of the name", "likely")
        # a two-letter surname: only as a name is written, capitalised
        self.add([w.title() for w in short] + [w.upper() for w in short],
                 "one word of the name", "likely", ignore_case=False)
        # an everyday word as a name ("Hope", "Baker"): only capitalised
        self.add([w.title() for w in everyday] + [w.upper() for w in everyday],
                 "one word of the name (also an everyday word)", "possible",
                 ignore_case=False)
        if person.last and len(person.last) >= 4:
            self.surname = person.last
            if len(person.last) >= 5:
                self.fuzzy.append(person.last)
        if person.first and len(person.first) >= 5:
            self.fuzzy.append(person.first)

    def _ssn(self, value, family):
        self.add(forms.ssn_forms(value), "the SSN as printed", "certain")
        d = re.sub(r"\D", "", value)
        self.digits = d if len(d) == 9 else ""
        if len(d) >= 4:
            # "SSN ending in 1146", "•••-••-1146", "xxx-xx-1146"
            self.rules.append((re.compile(
                rf"(?i)(?:[x*•#]{{2,}}[\s-]?[x*•#]{{0,3}}[\s-]?|ending\s+(?:in\s+)?|last\s*"
                rf"(?:4|four)\s*(?:digits)?\s*(?:of\s+(?:the\s+)?ssn)?\s*:?\s*){d[-4:]}(?!\d)"),
                "its last four digits beside a mask", "likely"))

    def _tin(self, value, family):
        self.add(forms.tin_forms(value), "the TIN as printed", "certain")
        self.digits = re.sub(r"\D", "", value) if len(re.sub(r"\D", "", value)) == 9 else ""

    def _phone(self, value, family):
        self.add(forms.phone_forms(value), "the phone number as printed", "certain")
        d = re.sub(r"\D", "", value)
        self.digits = d[-10:] if len(d) >= 10 else ""

    def _dob(self, value, family):
        self.add(forms.dob_forms(value), "the date of birth as printed", "certain")
        d = forms.parse_date(" ".join(value.split()))
        if d is not None:
            # 06221988, 19880622, and "22nd day of June, 1988"
            for run in (f"{d.month:02d}{d.day:02d}{d.year}", f"{d.year}{d.month:02d}{d.day:02d}"):
                self.rules.append((re.compile(rf"(?<!\d){run}(?!\d)"),
                                   "the date of birth without separators", "certain"))
            month = forms._MONTH_NAMES[d.month - 1]
            self.rules.append((re.compile(
                rf"(?i)\b{d.day}(?:st|nd|rd|th)?\s+day\s+of\s+{month}(?:\.|,)?\s+(?:in\s+the\s+"
                rf"year\s+)?{d.year}\b"), "the date of birth in words", "certain"))

    def _address(self, value, family):
        self.add([value] + forms.address_forms(value), "the address as printed", "certain")
        # the street line however it is spelled: house number, then the first
        # street word ("1734 W Gentile" for "1734 West Gentile Street")
        m = re.match(r"\s*(\d+[A-Za-z]?)\s+(.*)", value.split(",")[0])
        if m:
            words = [w for w in re.findall(r"[A-Za-z0-9]+", m.group(2))
                     if w.casefold() not in ("n", "s", "e", "w", "north", "south", "east", "west",
                                             "ne", "nw", "se", "sw")]
            if words:
                self.rules.append((re.compile(
                    rf"(?<![\w-]){re.escape(m.group(1))}\s+(?:[NSEW]\.?\s+|(?:north|south|east|"
                    rf"west)\s+)?{re.escape(words[0])}\b", re.IGNORECASE),
                    "the house number and street", "likely"))

    def _account(self, value, family):
        clean = value.replace(" ", "") if re.fullmatch(r"[\d ]+", value) else value
        d = re.sub(r"\D", "", clean)
        # fewer than four digits ("**44") could be anyone's: likely, never certain
        self.add(forms.account_forms(clean), "the account as printed",
                 "certain" if len(d) >= 4 else "likely")
        if len(d) >= 7:
            self.digits = d
        if len(d) >= 4:
            # its last four beside a mask or "ending in" - anyone's masked number
            # could end the same, so only likely
            self.rules.append((re.compile(
                rf"(?i)(?:[x*•#]{{2,}}[\s-]?|\.{{2,4}}|ending\s+(?:in\s+)?|last\s*(?:4|four)"
                rf"\s*(?:digits)?\s*:?\s*)(?:[x*•#\d]*[\s-]?)?{d[-4:]}(?!\d)"),
                "its last four digits beside a mask", "likely"))

    _card = _account

    def _email(self, value, family):
        self.add(forms.email_forms(value), "the email address", "certain")
        local = value.split("@")[0]
        if len(local) >= 6:
            self.add([local], "the email's name part", "likely")

    def _handle(self, value, family):
        self.add(forms.handle_forms(value), "the handle", "certain")

    def _employer(self, value, family):
        self.add([value], "the name as printed", "certain")
        core = re.sub(r"(?i)[,.]?\s+(?:inc|llc|pllc|llp|ltd|co|corp|corporation|company|pc|"
                      r"p\.c)\.?$", "", value).strip()
        if core != value and len(core) >= 6:
            self.add([core], "the name without its company suffix", "likely")

    _business = _employer

    def _other(self, value, family):
        self.add([value] + forms.id_forms(value), "the value as printed", "certain")
        a = re.sub(r"[^0-9A-Za-z]", "", value).upper()
        if len(a) >= 6 and re.search(r"\d", a):
            self.alnum = a
        elif len(value) >= 4 and not re.search(r"\d", value):
            # a word value - a school, a nickname filed here: as a phrase
            self.add([value], "the phrase", "certain")

    _dl = _passport = _vehicle = _case_number = _parcel = _other


# en and em dashes, no-break spaces: read as "-" and " " - one character each
_SAME_LENGTH_FOLD = str.maketrans({"–": "-", "—": "-", "‐": "-", "‑": "-", "\u00a0": " ",
                                   "\u2007": " ", "\u202f": " ", "’": "'", "‘": "'"})


def search(pages: list[Page], targets: list[Target]) -> list[Leak]:
    family = {names.parse(t.value).last.casefold() for t in targets
              if t.kind == "name" and names.parse(t.value).last}
    matchers = [Matcher(t, family) for t in targets]
    leaks: dict[tuple, Leak] = {}

    def hit(page: Page, li: int, line: str, at: int, end: int, m: Matcher, how: str, conf: str):
        # one row per place: two CSV rows that both match "Mac" there are one leak,
        # filed under the surer match (then the longer value, then the CSV's order)
        line = original                        # the file's own characters, not the folded copy
        found = line[at:end]
        key = (page.file, page.page, page.method, li, at, end)
        old = leaks.get(key)
        rank = (CONFIDENCE_ORDER[conf], -len(m.t.value), m.t.row)
        if old and (CONFIDENCE_ORDER[old.confidence], -len(old.target.value),
                    old.target.row) <= rank:
            return
        leaks[key] = Leak(page.file, page.page, where_of(page.method), li, found, m.t, how, conf,
                          _clip(line, at, end))

    original = ""
    for page in pages:
        lines = page.text.split("\n")
        for li, line in enumerate(lines, 1):
            if not line.strip():
                continue
            # matched on a folded copy (dashes, no-break spaces - one character for
            # one, so the positions agree), reported as the file prints it
            original, line = line, line.translate(_SAME_LENGTH_FOLD)
            low = line.lower()                     # lower(), not casefold(): same length
            digit_map = [(i, ch) for i, ch in enumerate(line) if ch.isdigit()]
            digit_run = "".join(ch for _, ch in digit_map)
            alnum_map = [(i, ch) for i, ch in enumerate(line) if ch.isalnum()]
            alnum_run = "".join(ch for _, ch in alnum_map).upper()
            for m in matchers:
                for regex, how, conf in m.rules:
                    for x in regex.finditer(line):
                        hit(page, li, line, x.start(), x.end(), m, how, conf)
                if m.digits and m.digits in digit_run:
                    for k in _all(digit_run, m.digits):
                        a, b = digit_map[k][0], digit_map[k + len(m.digits) - 1][0] + 1
                        # the same digits inside a longer number are not this value
                        before = line[a - 1] if a else ""
                        after = line[b] if b < len(line) else ""
                        if before.isdigit() or after.isdigit():
                            continue
                        hit(page, li, line, a, b, m, "the same digits, other separators",
                            "certain")
                if m.alnum and m.alnum in alnum_run:
                    for k in _all(alnum_run, m.alnum):
                        a, b = alnum_map[k][0], alnum_map[k + len(m.alnum) - 1][0] + 1
                        if (a and line[a - 1].isalnum()) or (b < len(line) and line[b].isalnum()):
                            continue
                        hit(page, li, line, a, b, m, "the same characters, other separators",
                            "certain")
                if m.surname and m.surname.lower() in low:
                    for k in _all(low, m.surname.lower()):
                        a, b = k, k + len(m.surname)
                        glued_left = a and line[a - 1].isalpha()
                        glued_right = b < len(line) and line[b].isalpha() and \
                            not line[b:b + 2].casefold() in ("s ", "s.", "s,") and \
                            line[b:].casefold()[:2] != "'s"
                        if glued_left or glued_right:
                            w0 = a
                            while w0 and line[w0 - 1].isalnum():
                                w0 -= 1
                            w1 = b
                            while w1 < len(line) and line[w1].isalnum():
                                w1 += 1
                            word = line[w0:w1]
                            # "shall" holds Hall, "granted" Grant: an everyday word is
                            # not a glued surname - CamelCase, digits or a file name are
                            if page.method != "file name" and names.ordinary_word(word) and \
                                    not re.search(r"\d|[a-z][A-Z]", word):
                                continue
                            hit(page, li, line, w0, w1, m, "the surname glued into a word",
                                "likely")
                if m.fuzzy:
                    for word in re.finditer(r"[A-Za-z0-9|!$][A-Za-z0-9|!$\-]{3,}", line):
                        w = word.group(0)
                        if any(w.casefold() != f.casefold() and names.alike(w, f)
                               for f in m.fuzzy):
                            hit(page, li, line, word.start(), word.end(), m,
                                "a near spelling (an OCR misreading or a typo)", "possible")
    return _drop_contained(leaks)


def _drop_contained(leaks: dict) -> list[Leak]:
    """A hit inside a surer or equal hit at the same place says nothing new:
    "Smith" inside "John Smith" is one leak, not two."""
    kept = []
    spans = defaultdict(list)
    for (f, p, m, li, a, b), l in leaks.items():
        spans[(f, p, m, li)].append((a, b, l))
    for group in spans.values():
        for a, b, l in group:
            if any(a2 <= a and b <= b2 and (a2, b2) != (a, b)
                   and CONFIDENCE_ORDER[l2.confidence] <= CONFIDENCE_ORDER[l.confidence]
                   for a2, b2, l2 in group):
                continue
            kept.append(l)
    return sorted(kept, key=lambda l: (l.file.casefold(), _page_key(l.page), l.where, l.line,
                                       CONFIDENCE_ORDER[l.confidence]))


WHERE = {"text": "body text", "docx": "body text", "odt": "body text", "xlsx": "sheet",
         "html": "body text", "rtf": "body text", "email": "email text", "msg": "email text",
         "ocr": "scanned page (OCR)", "transcribed": "scanned page (transcribed)"}


def where_of(method: str) -> str:
    """Where in the file a page's text came from, in the operator's words."""
    return WHERE.get(method, method)


def _one_place_one_row(leaks: list[Leak]) -> list[Leak]:
    """A value under a black box is also in the page's text layer: one row, the
    box's, which says what is wrong with it."""
    boxed = {(l.file, l.page, _fold(l.found)) for l in leaks if l.where == "text under a black box"}
    return [l for l in leaks if not (l.where in ("body text", "scanned page (OCR)")
                                     and (l.file, l.page, _fold(l.found)) in boxed)]


def _all(haystack: str, needle: str):
    i = haystack.find(needle)
    while i >= 0:
        yield i
        i = haystack.find(needle, i + 1)


def _page_key(page: str) -> tuple:
    m = re.match(r"(\d+)", page or "")
    return (int(m.group(1)) if m else 0, page or "")


def filename_pages(root: Path) -> list[Page]:
    """Every file's path, so a client name in a file name is found too."""
    return [Page(p.relative_to(root).as_posix(), "", p.relative_to(root).as_posix(), True,
                 "file name") for p in walk(root)]


# --------------------------------------------------------------------------
# the read-through: a model reads the anonymized pages for what is left
# --------------------------------------------------------------------------

READ_BATCH_CHARS = 60_000
READ_BATCH_LINES = 1200
READ_REPEAT = 3
READ_FORMAT = 1


def readthrough_stage(work: Path, pages: list[Page], targets: list[Target],
                      leaks: list[Leak]) -> tuple[str, list[Leak]]:
    folder = work / "readthrough"
    signature = hashlib.sha1(json.dumps(
        [READ_FORMAT] + [[p.file, p.page, p.method, p.text] for p in pages]
        + [[t.role, t.field, t.value] for t in targets], ensure_ascii=False).encode()).hexdigest()
    manifest = load_json(folder / "manifest.json", {}) or {}
    if manifest.get("signature") != signature:
        manifest = write_batches(folder, pages, targets, leaks, signature)
    waiting = [b for b in manifest["batches"] if not _read(folder / f"{b}.found.txt")]
    if waiting:
        bad = [b for b in waiting if (folder / f"{b}.found.txt").exists()]
        return (f"{len(waiting)} of {len(manifest['batches'])} batch(es) to read in {folder} - "
                f"write <batch>.found.txt for each"
                + (f" ({', '.join(bad[:5])} hold no findings and no NONE: read them again)"
                   if bad else "")), []
    return "done", read_findings(folder, manifest, pages, targets)


def _read(path: Path) -> bool:
    if not path.exists():
        return False
    lines = [l.strip().strip("`").lstrip("| ") for l in
             path.read_text("utf-8", "replace").splitlines()]
    return any(l.upper() == "NONE" or (l.count("|") >= 4 and re.match(r"P\d+\s*\|", l))
               for l in lines)


def write_batches(folder: Path, pages: list[Page], targets: list[Target], leaks: list[Leak],
                  signature: str) -> dict:
    folder.mkdir(parents=True, exist_ok=True)
    for old in folder.glob("batch_*"):
        old.unlink()
    found = sorted({_norm(l.found) for l in leaks if len(_norm(l.found)) >= 3}, key=len,
                   reverse=True)
    mark = re.compile(r"(?<![\w@$])(?:" + "|".join(re.escape(f).replace(r"\ ", r"\s+")
                                                   for f in found) + r")(?![\w])",
                      re.IGNORECASE) if found else None
    repeats = Counter()
    for p in pages:
        repeats.update({_norm(line) for line in p.text.split("\n") if line.strip()})
    shown: set[str] = set()
    page_lines = defaultdict(set)
    for p in pages:
        if p.method not in ("image (OCR)", "image (transcribed)"):
            page_lines[(p.file, p.page)].update(_norm(l) for l in p.text.split("\n"))
    people = defaultdict(list)
    for t in targets:
        if t.kind == "name":
            people[t.role].append(t.value)
    clues = set()
    for t in targets:
        if t.kind == "address":
            # "W Gentile St, Layton" - the words a reworded address would keep
            words = _norm(re.sub(r"\b\d[\d-]*\b|#\s*\w+", " ", t.value)).strip(" ,")
            if len(words) >= 4:
                clues.add(words)
        elif t.kind in ("employer", "business", "other") and not re.search(r"\d{3}", t.value):
            clues.add(t.value)
    clues = sorted(clues)
    blocks = []
    for pid, p in page_ids(pages).items():
        out, omitted = [], 0
        for n, line in enumerate(p.text.split("\n"), 1):
            text = _norm(line)
            if not text:
                continue
            if p.method in ("image (OCR)", "image (transcribed)") and \
                    text in page_lines.get((p.file, p.page), ()):
                omitted += 1                       # the page's own text already shows it
                continue
            if repeats[text] >= READ_REPEAT:
                if text in shown:
                    omitted += 1
                    continue
                shown.add(text)
            if mark is not None:
                text = mark.sub(lambda m: f"⟦{m.group(0)}⟧", text)
            out.append(f"L{n}: {text}")
        head = f"=== {pid} | {p.file}" + (f" | page {p.page}" if p.page else "") + \
            (f" | {p.method}" if p.method not in ("text", "docx", "ocr", "transcribed") else "")
        if omitted:
            head += f" | {omitted} line(s) shown on earlier pages left out"
        blocks.append(head + " ===\n" + ("\n".join(out) if out else "(no new lines)"))
    batches, current, size, rows = [], [], 0, 0
    for block in blocks:
        n = block.count("\n") + 1
        if current and (size + len(block) > READ_BATCH_CHARS or rows + n > READ_BATCH_LINES):
            batches.append(current)
            current, size, rows = [], 0, 0
        current.append(block)
        size += len(block)
        rows += n
    if current:
        batches.append(current)
    ids = []
    roster = "; ".join(f"{role} = {' / '.join(v)}" for role, v in people.items())
    for i, batch in enumerate(batches, 1):
        bid = f"batch_{i:03d}"
        ids.append(bid)
        header = (f"READ-THROUGH {bid} ({i} of {len(batches)})\n"
                  f"THE REAL PARTIES (their names should NOT appear anywhere): {roster}\n"
                  + (f"OTHER REAL DETAILS OF THEIRS: {'; '.join(clues)}\n" if clues else "")
                  + "Text in ⟦ ⟧ is already reported as a leak - do not report it again.\n\n")
        (folder / f"{bid}.txt").write_text(header + "\n\n".join(batch)
                                           + f"\n\n=== END OF {bid} ===\n", encoding="utf-8")
    manifest = {"signature": signature, "batches": ids}
    save_json(folder / "manifest.json", manifest)
    return manifest


def page_ids(pages: list[Page]) -> dict[str, Page]:
    return {f"P{i:04d}": p for i, p in enumerate(pages, 1)}


def read_findings(folder: Path, manifest: dict, pages: list[Page],
                  targets: list[Target]) -> list[Leak]:
    """The read-through's findings as report rows. A finding not printed on the
    page it names is rejected (listed in rejected.txt), never reported."""
    by_id = page_ids(pages)
    roles = {t.role.casefold(): t.role for t in targets} | {"unknown": "", "both parties":
                                                            "Both parties"}
    out, rejected = [], []
    for bid in manifest["batches"]:
        for raw in (folder / f"{bid}.found.txt").read_text("utf-8", "replace").splitlines():
            line = raw.strip().strip("`")
            if not line or line.upper() == "NONE" or line.startswith("#"):
                continue
            parts = [x.strip() for x in line.split("|")]
            if parts and parts[0] == "":
                parts = parts[1:]
            while parts and parts[-1] == "":
                parts = parts[:-1]
            if len(parts) < 5:
                rejected.append(f"{bid}: not 6 fields: {line[:80]}")
                continue
            pid, lno, value, field, whose = parts[:5]
            reason = " | ".join(parts[5:]) or "found by the read-through"
            value = value.replace("⟦", "").replace("⟧", "").strip().strip("\"'`").rstrip(".,;:")
            digits_id = re.sub(r"\D", "", pid)
            page = by_id.get(f"P{int(digits_id):04d}" if digits_id else pid.upper())
            if page is None:
                rejected.append(f"{bid}: no page {pid}")
                continue
            if not value or _fold(value) not in _fold(page.text):
                rejected.append(f"{bid}: {pid} does not print {value[:40]!r}")
                continue
            plines = page.text.split("\n")
            n = int(re.sub(r"\D", "", lno) or 0)
            if not (1 <= n <= len(plines)) or _fold(value) not in _fold(plines[n - 1]):
                n = next((i for i, l in enumerate(plines, 1) if _fold(value) in _fold(l)), 0)
            text = plines[n - 1] if n else ""
            at = _fold(text).find(_fold(value)) if n else -1
            context = _clip(text, at, at + len(value)) if at >= 0 else ""
            target = Target(0, roles.get(whose.casefold(), whose), field or "Other identifiers",
                            "other", value)
            out.append(Leak(page.file, page.page, where_of(page.method), n, value, target,
                            f"read-through: {reason[:100]}", "model", context))
    (folder / "rejected.txt").write_text("\n".join(rejected) + ("\n" if rejected else ""),
                                         encoding="utf-8")
    if rejected:
        print(f"Read-through: {len(rejected)} finding(s) rejected as not printed where stated - "
              f"see {folder / 'rejected.txt'}")
    return out


# --------------------------------------------------------------------------
# the report
# --------------------------------------------------------------------------

def write_report(path: Path, leaks: list[Leak]) -> None:
    tmp = path.with_suffix(".csv.tmp")
    with open(tmp, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(REPORT_HEADER)
        for l in leaks:
            w.writerow([_safe(l.file), l.page, l.where, l.line or "", _safe(l.found),
                        _safe(l.target.value), l.target.role, l.target.field, l.how,
                        l.confidence, _safe(l.context)])
    tmp.replace(path)


def _safe(text: str) -> str:
    """Never a spreadsheet formula."""
    return "'" + text if text[:1] in ("=", "+", "-", "@") else text


# --------------------------------------------------------------------------
# the command
# --------------------------------------------------------------------------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    run = sub.add_parser("run", help="search the anonymized files, stopping where a model is needed")
    run.add_argument("input", type=Path, help="the anonymized output: a folder or a .zip")
    run.add_argument("--csv", type=Path, required=True,
                     help="the party CSV the party-PII extractor wrote for this case")
    run.add_argument("--out", type=Path, help="where the report goes (default: beside the input)")
    run.add_argument("--rescan", action="store_true", help="read every file again")
    run.add_argument("--no-ocr", action="store_true", help="queue every picture for a model")
    run.add_argument("--no-readthrough", action="store_true",
                     help="skip the model's read-through (only if the operator accepts that)")
    run.add_argument("--exclude", action="append", default=[], metavar="GLOB",
                     help="skip files whose name or relative path matches (repeat)")
    clean = sub.add_parser("clean", help="delete the work folder once the report is delivered")
    clean.add_argument("out", type=Path)
    args = ap.parse_args(argv)
    if args.cmd == "clean":
        work = args.out.expanduser().resolve() / ".work"
        if not work.is_dir():
            print(f"Nothing to clean: {work} does not exist")
            return 0
        shutil.rmtree(work)
        print(f"Deleted {work}. The report beside it is kept.")
        return 0

    EXCLUDE[:] = args.exclude
    source = args.input.expanduser().resolve()
    if not source.exists():
        print(f"ERROR: {source} does not exist")
        return 1
    targets, problems = read_targets(args.csv.expanduser())
    if problems:
        print(f"ERROR: the party CSV {args.csv} needs fixing - " + "; ".join(problems[:12]))
        return 1
    out = (args.out.expanduser().resolve() if args.out
           else source.parent / f"{source.stem if source.is_file() else source.name}_leak_check")
    if out == source or source in out.parents:
        print("ERROR: --out must be outside the folder being checked")
        return 1
    work = out / ".work" / safe_name(source.stem if source.is_file() else source.name)
    work.mkdir(parents=True, exist_ok=True)
    if source.is_file():
        if source.suffix.lower() != ".zip":
            print("ERROR: INPUT must be a folder or a .zip")
            return 1
        unzipped = work / "_input"
        if args.rescan or not unzipped.exists():
            shutil.rmtree(unzipped, ignore_errors=True)
            _safe_extract(source, unzipped)
        source = unzipped
    use_ocr = not args.no_ocr
    print(f"Checking {source.name} for {len(targets)} party value(s). Output folder: {out}")
    if use_ocr and not tesseract():
        print("No OCR engine here: pictures will be queued for a model to transcribe.")

    if args.rescan or not (work / "scan.json").exists() or not (work / "hidden.jsonl").exists():
        info = scan_case(source.name, source, work, use_ocr)
        hidden, queue = [], load_json(work / "queue.json", []) or []
        inner = [(work / sub, "(inside an archive or attachment) ")
                 for sub in ("archives", "attachments") if (work / sub).is_dir()]
        for root, prefix in [(source, "")] + inner:
            for path in walk(root):
                rel = prefix + path.relative_to(root).as_posix()
                pages, more = hidden_pages(path, rel, work, use_ocr)
                hidden += pages
                queue += more
            if prefix:
                hidden += [Page(prefix + p.file, "", prefix + p.text, True, "file name")
                           for p in filename_pages(root)]
        save_json(work / "queue.json", queue)
        with open(work / "hidden.jsonl", "w", encoding="utf-8") as fh:
            for p in hidden + filename_pages(source):
                fh.write(json.dumps({"file": p.file, "page": p.page, "method": p.method,
                                     "text": p.text}, ensure_ascii=False) + "\n")
    else:
        info = load_json(work / "scan.json", {})
    unread = info.get("unreadable", [])
    if unread:
        print(f"WARNING: {len(unread)} file(s) could not be read and were not searched - "
              f"listed in {work / 'scan.json'}")
        if any("no PDF reader" in why for _, why in unread):
            print("  No PDF reader here: install one (pip install pymupdf, or pypdf), then rerun "
                  "with --rescan.")
    blank = unread_transcripts(work)
    if blank:
        print(f"WARNING: {len(blank)} picture(s) could not be read by the transcriber and were "
              f"not searched.")
    waiting = missing_transcripts(work)
    if waiting:
        print(f"NEXT: transcribe the queued page images (see SKILL.md, 'Transcribing pictures'), "
              f"then run this command again.\n  {len(waiting)} picture(s) - {work / 'queue.json'}")
        return 2

    pages = load_pages(work)
    for line in open(work / "hidden.jsonl", encoding="utf-8"):
        d = json.loads(line)
        pages.append(Page(d["file"], d["page"], d["text"], True, d["method"]))
    # a transcript of a picture inside a page keeps its place in the report
    pictures = {(q["file"], q["page"]) for q in load_json(work / "queue.json", []) or []
                if q.get("where") == "image"}
    for p in pages:
        if p.method == "transcribed" and (p.file, p.page) in pictures:
            p.method = "image (transcribed)"
    failed = [p for p in pages if p.method == INSPECTION_FAILED]
    if failed:
        print(f"WARNING: the hidden places of {len({p.file for p in failed})} file(s) could not "
              f"all be checked - listed in the report as not searched.")
    pages = [p for p in pages if p.method != INSPECTION_FAILED]
    leaks = search(pages, targets)
    if not args.no_readthrough:
        state, found = readthrough_stage(work, pages, targets, leaks)
        if state != "done":
            print(f"NEXT: read the read-through batches for anything else that still identifies "
                  f"the parties (see SKILL.md, 'The read-through'), then run this command again."
                  f"\n  {state}")
            return 5
        if found:
            # each finding becomes a value to search for: every place it is printed is
            # found, not only the one the model named
            extra = [f.target for f in found]
            reasons = {(f.target.value, f.target.role): f.how for f in found}
            for leak in search(pages, extra):
                leak.how = reasons.get((leak.target.value, leak.target.role), leak.how)
                leak.confidence = "model"
                leaks.append(leak)
            known, merged = set(), []
            for l in sorted(leaks, key=lambda l: CONFIDENCE_ORDER[l.confidence]):
                key = (l.file, l.page, l.where, l.line, _fold(l.found))
                if key not in known:
                    known.add(key)
                    merged.append(l)
            leaks = merged
    leaks = _one_place_one_row(leaks)
    leaks += [Leak(f, "", "not searched", 0, "", NOT_SEARCHED, why, "not searched", "")
              for f, why in unread]
    leaks += [Leak(item.split(" p.")[0], item.split(" p.")[-1] if " p." in item else "",
                   "not searched", 0, "", NOT_SEARCHED,
                   "the transcriber could not read this picture", "not searched", "")
              for item in blank]
    leaks += [Leak(p.file, p.page, "not searched", 0, "", NOT_SEARCHED, p.text, "not searched",
                   "") for p in failed]
    leaks.sort(key=lambda l: (l.file.casefold(), _page_key(l.page), l.where, l.line,
                              CONFIDENCE_ORDER[l.confidence]))
    report = out / "leak_report.csv"
    write_report(report, leaks)
    counts = Counter(l.confidence for l in leaks)
    found_rows = [l for l in leaks if l.confidence != "not searched"]
    files = len({l.file for l in found_rows})
    print(f"DONE: {report} - {len(found_rows)} surviving value(s) in {files} file(s)"
          + (": " + ", ".join(f"{counts[c]} {c}" for c in CONFIDENCE_ORDER if counts[c])
             if found_rows else "")
          + (f"; {counts['not searched']} place(s) NOT SEARCHED - listed in the report"
             if counts["not searched"] else ""))
    print("NEXT: nothing - the report is written.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
