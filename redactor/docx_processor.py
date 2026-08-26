"""DOCX reading and rewriting, at the OOXML level.

python-docx only reaches the main body.  Captions, client names and account
numbers hide in headers, footers, footnotes, endnotes, text boxes, comments and
tracked-change markup, so this module opens the .docx as the zip archive it is
and walks every part that can hold text.
"""

from __future__ import annotations

import re
import shutil
import zipfile
from dataclasses import dataclass, field
from pathlib import Path

from lxml import etree

from . import engine
from .engine import Hit, PersonMatcher, Settings
from .mapping import MappingStore

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"
R = "http://schemas.openxmlformats.org/officeDocument/2006/relationships"
PKG_REL = "http://schemas.openxmlformats.org/package/2006/relationships"
NS = {"w": W, "r": R}


def w(tag: str) -> str:
    return f"{{{W}}}{tag}"


# parts that hold user-visible text
TEXT_PART_RE = re.compile(
    r"^word/(document\d*\.xml|header\d*\.xml|footer\d*\.xml|footnotes\.xml|"
    r"endnotes\.xml|comments\.xml|glossary/document\.xml)$"
)
# parts whose text lives in DrawingML (a:t) rather than WordprocessingML (w:t):
# SmartArt diagrams and the cached labels/values inside charts
DRAWING_PART_RE = re.compile(r"^word/(diagrams/data\d*\.xml|charts/chart\d*\.xml)$")
A = "http://schemas.openxmlformats.org/drawingml/2006/main"
C = "http://schemas.openxmlformats.org/drawingml/2006/chart"
# parts that exist only to carry comment metadata
COMMENT_PARTS = (
    "word/comments.xml", "word/commentsExtended.xml", "word/commentsIds.xml",
    "word/commentsExtensible.xml", "word/people.xml",
)
# drawn-ink handwriting (InkML content parts)
INK_PART_RE = re.compile(r"^word/(glossary/)?ink/")
# raster formats Pillow can rewrite as an all-black image of the same size
_RASTER_FORMATS = {
    "jpg": "JPEG", "jpeg": "JPEG", "png": "PNG", "gif": "GIF", "bmp": "BMP",
    "tif": "TIFF", "tiff": "TIFF",
}


@dataclass
class DocxResult:
    source: Path
    output: Path
    hits: list[Hit] = field(default_factory=list)
    parts_processed: list[str] = field(default_factory=list)
    comments_removed: int = 0
    revisions_resolved: int = 0
    hyperlinks_stripped: int = 0
    media_files: list[str] = field(default_factory=list)
    embedded_objects: list[str] = field(default_factory=list)
    images_redacted: int = 0
    ink_parts_removed: int = 0
    metadata_scrubbed: bool = False
    refused: bool = False
    warnings: list[str] = field(default_factory=list)


# --------------------------------------------------------------------------
# reading
# --------------------------------------------------------------------------


# Word stores a tab stop and a line break as elements of their own rather than
# as characters inside a run. Ignoring them glued the runs on either side
# together - a real decree yields "Zepeda", <w:tab/>, <w:tab/>, "Born: ", which
# read as "ZepedaBorn" - and the name boundary then refused to match the
# surname, so it shipped intact. They are carried as one character each, which
# also keeps every offset in _set_group_text aligned.
_SEPARATORS = {"tab": " ", "br": "\n"}


def _node_text(node: etree._Element) -> str:
    """What one node contributes to its paragraph's readable text."""
    separator = _SEPARATORS.get(etree.QName(node).localname)
    return separator if separator is not None else (node.text or "")


def _is_separator(node: etree._Element) -> bool:
    return etree.QName(node).localname in _SEPARATORS


def _paragraph_texts(root: etree._Element) -> list[list[etree._Element]]:
    """Text elements grouped by their own paragraph, nesting handled correctly."""
    groups: list[list[etree._Element]] = []
    for para in root.iter(w("p")):
        own: list[etree._Element] = []
        for node in para.iter(w("t"), w("tab"), w("br")):
            # a text box nests whole paragraphs inside a run; those get their own group
            ancestor = node.getparent()
            while ancestor is not None and ancestor.tag != w("p"):
                ancestor = ancestor.getparent()
            if ancestor is para:
                own.append(node)
        if any(not _is_separator(node) for node in own):
            groups.append(own)
    return groups


def _group_text(group: list[etree._Element]) -> str:
    return "".join(_node_text(node) for node in group)


def extract_text(path: Path) -> dict[str, str]:
    """Readable text per part, for scanning and caption harvesting."""
    out: dict[str, str] = {}
    with zipfile.ZipFile(path) as zf:
        for name in zf.namelist():
            if not TEXT_PART_RE.match(name):
                continue
            try:
                root = etree.fromstring(zf.read(name))
            except etree.XMLSyntaxError:
                continue
            # the same joiner the scan uses, so caption harvesting and matching
            # never see two different versions of the document
            paragraphs = [_group_text(group) for group in _paragraph_texts(root)]
            out[name] = "\n".join(paragraphs)
    return out


def iter_text_units(path: Path):
    """The exact text units :func:`process` will scan, in the same order.

    The review screen depends on this: if it scanned differently it would show
    the operator a different set of findings from the ones actually applied.
    """
    with zipfile.ZipFile(path) as zf:
        for name in sorted(zf.namelist()):
            if not TEXT_PART_RE.match(name):
                continue
            try:
                root = etree.fromstring(zf.read(name))
            except etree.XMLSyntaxError:
                continue
            for group in _paragraph_texts(root):
                text = _group_text(group)
                if text.strip():
                    yield text


def caption_sources(path: Path) -> dict[str, str]:
    """The regions where a legal caption lives: headers first, then page one."""
    parts = extract_text(path)
    sources: dict[str, str] = {}
    for name, text in parts.items():
        if not text.strip():
            continue
        if "header" in name:
            sources[f"{path.name} [{Path(name).name}]"] = text
        elif re.match(r"^word/document\d*\.xml$", name):
            sources[f"{path.name} [caption]"] = text
    return sources


# --------------------------------------------------------------------------
# rewriting
# --------------------------------------------------------------------------


def _set_group_text(group: list[etree._Element], hits: list[Hit]) -> set:
    """Apply ``hits`` (computed over the joined group text) back onto the runs."""
    spans: list[tuple[etree._Element, int, int]] = []
    values: dict[etree._Element, str] = {}
    cursor = 0
    for node in group:
        text = _node_text(node)
        spans.append((node, cursor, cursor + len(text)))
        values[node] = text
        cursor += len(text)

    touched: set = set()
    for hit in sorted(hits, key=lambda h: h.start, reverse=True):
        pending = hit.replacement
        for node, start, end in spans:
            if end <= hit.start or start >= hit.end:
                continue
            if _is_separator(node):
                # a name may legitimately straddle a tab stop; the replacement
                # goes into the runs on either side and the tab stays put
                continue
            local_start = max(hit.start, start) - start
            local_end = min(hit.end, end) - start
            current = values[node]
            values[node] = current[:local_start] + pending + current[local_end:]
            pending = ""
            touched.add(node)

    for node in group:
        if _is_separator(node):
            continue
        new = values[node]
        if new != (node.text or ""):
            node.text = new
            if new != new.strip():
                node.set("{http://www.w3.org/XML/1998/namespace}space", "preserve")
    return touched


def _black_bar(nodes: set) -> None:
    """Mark the runs holding redacted text as a solid black bar."""
    for node in nodes:
        run = node.getparent()
        if run is None or run.tag != w("r"):
            continue
        props = run.find(w("rPr"))
        if props is None:
            props = etree.SubElement(run, w("rPr"))
            run.remove(props)
            run.insert(0, props)
        for tag, attrs in (("highlight", {w("val"): "black"}),
                           ("color", {w("val"): "FFFFFF"})):
            existing = props.find(w(tag))
            if existing is not None:
                props.remove(existing)
            element = etree.SubElement(props, w(tag))
            for key, value in attrs.items():
                element.set(key, value)


def _resolve_revisions(root: etree._Element) -> int:
    """Accept insertions, drop deletions, and remove revision wrappers."""
    resolved = 0
    for ins in list(root.iter(w("ins"))):
        parent = ins.getparent()
        if parent is None:
            continue
        index = list(parent).index(ins)
        for child in list(ins):
            parent.insert(index, child)
            index += 1
        parent.remove(ins)
        resolved += 1
    for tag in ("del", "moveFrom"):
        for node in list(root.iter(w(tag))):
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)
                resolved += 1
    for tag in ("pPrChange", "rPrChange", "sectPrChange", "tblPrChange",
                "tcPrChange", "trPrChange", "cellIns", "cellDel", "cellMerge"):
        for node in list(root.iter(w(tag))):
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)
                resolved += 1
    return resolved


def _remove_comment_marks(root: etree._Element) -> int:
    removed = 0
    for tag in ("commentRangeStart", "commentRangeEnd", "commentReference",
                "annotationRef"):
        for node in list(root.iter(w(tag))):
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)
                removed += 1
    return removed


_FIELD_SENSITIVE = re.compile(
    r"(https?://\S+|www\.\S+|mailto:\S+|[\w.+-]+@[\w-]+\.[\w.-]+)", re.IGNORECASE
)


def _scrub_field_codes(root: etree._Element) -> int:
    """Field instructions carry raw hyperlink targets that no text scan sees.

    Both written forms: ``w:instrText`` elements and the single-element
    ``w:fldSimple`` whose instruction is the ``w:instr`` attribute.
    """
    count = 0
    for node in root.iter(w("instrText")):
        if not node.text:
            continue
        new = _FIELD_SENSITIVE.sub("[LINK-REMOVED]", node.text)
        if new != node.text:
            node.text = new
            count += 1
    for node in root.iter(w("fldSimple")):
        instr = node.get(w("instr"))
        if not instr:
            continue
        new = _FIELD_SENSITIVE.sub("[LINK-REMOVED]", instr)
        if new != instr:
            node.set(w("instr"), new)
            count += 1
    return count


def _blacked_image(data: bytes, suffix: str) -> bytes | None:
    """An all-black image of the same size and format, or None if not raster."""
    import io

    from PIL import Image

    fmt = _RASTER_FORMATS.get(suffix.lower().lstrip("."))
    if fmt is None:
        return None
    size = (8, 8)
    try:
        with Image.open(io.BytesIO(data)) as original:
            size = original.size
    except Exception:
        pass                       # unreadable image: black it at token size
    buffer = io.BytesIO()
    Image.new("RGB", size, (0, 0, 0)).save(buffer, fmt)
    return buffer.getvalue()


def _scan_drawing_text(root: etree._Element, store, settings, matcher,
                       document_label: str, result: "DocxResult") -> bool:
    """Scan and rewrite DrawingML text (SmartArt, chart labels). True if changed."""
    changed = False
    # a:t is rich text (SmartArt nodes, chart titles); c:v is a chart's cached
    # category names and values, where a registered account number can hide
    for tag in (f"{{{A}}}t", f"{{{C}}}v"):
        for node in root.iter(tag):
            text = node.text or ""
            if not text.strip():
                continue
            hits = engine.scan_text(text, store, settings, matcher)
            if not hits:
                continue
            for hit in hits:
                entity = store.entities.get(hit.entity_key)
                if entity is not None:
                    store.record_hit(entity, document_label)
            result.hits.extend(hits)
            node.text = engine.apply_hits(text, hits)
            changed = True
    return changed


def _scrub_core_properties(data: bytes) -> bytes:
    root = etree.fromstring(data)
    blanked = {
        "creator", "lastModifiedBy", "lastPrinted", "title", "subject",
        "description", "keywords", "category", "contentStatus", "identifier",
        "language", "version", "revision",
    }
    EPOCH = "2000-01-01T00:00:00Z"
    for child in list(root):
        local = etree.QName(child).localname
        if local in blanked:
            child.text = None
            for key in list(child.attrib):
                del child.attrib[key]
        elif local in {"created", "modified"}:
            # a fixed value keeps the file schema-valid while leaking nothing
            child.text = EPOCH
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _scrub_app_properties(data: bytes) -> bytes:
    root = etree.fromstring(data)
    blanked = {"Company", "Manager", "Title", "Subject", "Keywords", "Category",
               "TotalTime", "LastAuthor", "HyperlinkBase", "TitlesOfParts",
               "HeadingPairs", "HyperlinksChanged", "LinksUpToDate"}
    for child in list(root):
        if etree.QName(child).localname in blanked:
            root.remove(child)
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


_CT = "http://schemas.openxmlformats.org/package/2006/content-types"


def _prune_dropped_parts(payload: dict[str, bytes], drop: set[str]) -> None:
    """Remove relationship and content-type entries for dropped parts.

    Dangling entries make Word offer a "repair" prompt on open; a repaired
    file draws attention to exactly what was removed.
    """
    import posixpath

    for rels_name in [n for n in payload if n.endswith(".rels")]:
        owner_dir = posixpath.dirname(posixpath.dirname(rels_name))
        try:
            root = etree.fromstring(payload[rels_name])
        except etree.XMLSyntaxError:
            continue
        changed = False
        for rel in root.findall(f"{{{PKG_REL}}}Relationship"):
            if rel.get("TargetMode") == "External":
                continue
            target = rel.get("Target", "")
            resolved = posixpath.normpath(posixpath.join(owner_dir, target)).lstrip("/")
            if resolved in drop:
                root.remove(rel)
                changed = True
        if changed:
            payload[rels_name] = etree.tostring(
                root, xml_declaration=True, encoding="UTF-8", standalone=True)

    types_name = "[Content_Types].xml"
    if types_name in payload:
        try:
            root = etree.fromstring(payload[types_name])
        except etree.XMLSyntaxError:
            return
        changed = False
        for override in root.findall(f"{{{_CT}}}Override"):
            part = override.get("PartName", "").lstrip("/")
            if part in drop:
                root.remove(override)
                changed = True
        if changed:
            payload[types_name] = etree.tostring(
                root, xml_declaration=True, encoding="UTF-8", standalone=True)


def _strip_external_links(data: bytes) -> tuple[bytes, int]:
    root = etree.fromstring(data)
    count = 0
    for rel in root.findall(f"{{{PKG_REL}}}Relationship"):
        rel_type = rel.get("Type", "")
        if rel_type.endswith("/hyperlink") and rel.get("TargetMode") == "External":
            if rel.get("Target"):
                rel.set("Target", "")
                count += 1
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True), count


def _scrub_settings(data: bytes) -> bytes:
    root = etree.fromstring(data)
    for tag in ("trackChanges", "documentProtection", "proofState", "rsid", "rsids",
                "removePersonalInformation", "removeDateAndTime"):
        for node in list(root.iter(w(tag))):
            parent = node.getparent()
            if parent is not None:
                parent.remove(node)
    # ask Word itself to strip personal info on save; these belong at the head
    # of CT_Settings, so insert rather than append
    for offset, tag in enumerate(("removePersonalInformation", "removeDateAndTime")):
        root.insert(offset, etree.Element(w(tag)))
    return etree.tostring(root, xml_declaration=True, encoding="UTF-8", standalone=True)


# --------------------------------------------------------------------------
# the public entry point
# --------------------------------------------------------------------------


def process(
    source: Path,
    output: Path,
    store: MappingStore,
    settings: Settings,
    matcher: PersonMatcher | None = None,
) -> DocxResult:
    """Write an anonymized or redacted copy of ``source`` to ``output``."""
    source, output = Path(source), Path(output)
    result = DocxResult(source=source, output=output)
    redact = settings.docx_mode == "redact"
    matcher = matcher or PersonMatcher(store, settings)
    document_label = source.name

    with zipfile.ZipFile(source) as zin:
        names = zin.namelist()
        payload = {name: zin.read(name) for name in names}

    drop: set[str] = set()
    if settings.scrub_comments:
        for part in COMMENT_PARTS:
            if part in payload:
                drop.add(part)
                result.comments_removed += 1

    for name in list(payload):
        if re.match(r"^word/(glossary/)?media/", name):
            result.media_files.append(name)
        elif re.match(r"^word/(glossary/)?embeddings/", name):
            result.embedded_objects.append(name)

    if settings.scrub_metadata:
        # "save with preview picture" stores a readable image of page one
        for name in list(payload):
            if re.match(r"^docProps/thumbnail\.", name, re.IGNORECASE):
                drop.add(name)
                result.metadata_scrubbed = True

    if settings.redact_images:
        for name in result.media_files:
            blacked = _blacked_image(payload[name], Path(name).suffix)
            if blacked is None:
                # vector or unknown format Pillow cannot rewrite - remove it
                # outright rather than deliver it readable
                drop.add(name)
            else:
                payload[name] = blacked
            result.images_redacted += 1
        for name in list(payload):
            if INK_PART_RE.match(name):
                drop.add(name)
                result.ink_parts_removed += 1

    # sorted, so placeholder numbering matches iter_text_units exactly - the
    # review screen must show the same findings the run applies
    for name in sorted(payload):
        data = payload[name]
        if name in drop:
            continue

        if TEXT_PART_RE.match(name):
            try:
                root = etree.fromstring(data)
            except etree.XMLSyntaxError:
                # delivering this part unredacted would be a silent leak; the
                # whole file is refused instead
                result.refused = True
                result.warnings.append(
                    f"{name} could not be parsed, so its text could not be "
                    "redacted. The file was NOT written."
                )
                break

            if settings.scrub_comments:
                _remove_comment_marks(root)
            result.revisions_resolved += _resolve_revisions(root)
            if settings.scrub_embedded:
                _scrub_field_codes(root)

            for group in _paragraph_texts(root):
                text = _group_text(group)
                if not text.strip():
                    continue
                hits = engine.scan_text(text, store, settings, matcher)
                if not hits:
                    continue
                for hit in hits:
                    entity = store.entities.get(hit.entity_key)
                    if entity is not None:
                        store.record_hit(entity, document_label)
                result.hits.extend(hits)
                touched = _set_group_text(group, hits)
                if redact:
                    _black_bar(touched)

            payload[name] = etree.tostring(
                root, xml_declaration=True, encoding="UTF-8", standalone=True
            )
            result.parts_processed.append(name)

        elif DRAWING_PART_RE.match(name):
            try:
                root = etree.fromstring(data)
            except etree.XMLSyntaxError:
                result.refused = True
                result.warnings.append(
                    f"{name} could not be parsed, so its text could not be "
                    "redacted. The file was NOT written."
                )
                break
            if _scan_drawing_text(root, store, settings, matcher,
                                  document_label, result):
                payload[name] = etree.tostring(
                    root, xml_declaration=True, encoding="UTF-8", standalone=True
                )
            result.parts_processed.append(name)

        elif name == "docProps/core.xml" and settings.scrub_metadata:
            payload[name] = _scrub_core_properties(data)
            result.metadata_scrubbed = True
        elif name == "docProps/app.xml" and settings.scrub_metadata:
            payload[name] = _scrub_app_properties(data)
            result.metadata_scrubbed = True
        elif name == "docProps/custom.xml" and settings.scrub_metadata:
            drop.add(name)
            result.metadata_scrubbed = True
        elif name == "word/settings.xml" and settings.scrub_metadata:
            try:
                payload[name] = _scrub_settings(data)
            except etree.XMLSyntaxError:
                pass
        elif name.endswith(".rels") and settings.scrub_embedded:
            try:
                payload[name], stripped = _strip_external_links(data)
                result.hyperlinks_stripped += stripped
            except etree.XMLSyntaxError:
                pass

    if result.refused:
        return result

    if drop:
        _prune_dropped_parts(payload, drop)

    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", zipfile.ZIP_DEFLATED) as zout:
        for name, data in payload.items():
            if name in drop:
                continue
            zout.writestr(name, data)

    if result.media_files and not settings.redact_images:
        result.warnings.append(
            f"{len(result.media_files)} embedded image(s) were copied through unchanged - "
            "a scanned signature or a screenshot of a statement will still be readable. "
            "Review them by hand."
        )
    if result.embedded_objects:
        result.warnings.append(
            f"{len(result.embedded_objects)} embedded object(s) (spreadsheets, documents) "
            "were copied through unchanged and may contain unredacted data."
        )
    return result
