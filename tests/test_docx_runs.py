"""How Word actually stores a paragraph, and what that does to matching.

Everything here is a shape found in a real pleading, rebuilt with invented
names. Word keeps a tab stop and a line break as elements of their own rather
than as characters in a run, and it leaves a name flush against the underscores
of a signature rule. Both defeated the name boundary, and both shipped a real
surname in a document the tool called clean.
"""

import zipfile

import pytest
from lxml import etree

from redactor import docx_processor, engine, names as _names
from redactor.engine import Settings
from redactor.mapping import MappingStore

W = "http://schemas.openxmlformats.org/wordprocessingml/2006/main"


def paragraph(*pieces) -> etree._Element:
    """A <w:p> from ("text", "…") / ("tab",) / ("br",) pieces."""
    para = etree.Element(f"{{{W}}}p", nsmap={"w": W})
    for piece in pieces:
        run = etree.SubElement(para, f"{{{W}}}r")
        if piece[0] == "text":
            node = etree.SubElement(run, f"{{{W}}}t")
            node.text = piece[1]
        else:
            etree.SubElement(run, f"{{{W}}}{piece[0]}")
    return para


def body(*paragraphs) -> etree._Element:
    root = etree.Element(f"{{{W}}}document", nsmap={"w": W})
    doc_body = etree.SubElement(root, f"{{{W}}}body")
    for para in paragraphs:
        doc_body.append(para)
    return root


def loaded(name="Marisol Quenby Ashdown", category="person") -> MappingStore:
    store = MappingStore()
    store.add_person(name, category=category)
    return store


# ----------------------------------------------------------- tabs & breaks --

def test_a_tab_separates_the_runs_it_sits_between():
    """"Ashdown", tab, tab, "Born: " must not read as "AshdownBorn"."""
    root = body(paragraph(("text", "Marisol Quenby Ashdown"), ("tab",), ("tab",),
                          ("text", "Born: December 2017")))
    group = docx_processor._paragraph_texts(root)[0]
    text = docx_processor._group_text(group)
    assert "AshdownBorn" not in text
    assert text == "Marisol Quenby Ashdown  Born: December 2017"


def test_a_break_reads_as_a_line_break():
    root = body(paragraph(("text", "Marisol Ashdown"), ("br",), ("text", "Judge")))
    assert docx_processor._group_text(
        docx_processor._paragraph_texts(root)[0]) == "Marisol Ashdown\nJudge"


def test_the_surname_is_replaced_across_a_tab_stop():
    """The leak this file exists for: the surname used to survive untouched."""
    root = body(paragraph(("text", "Marisol Quenby Ashdown"), ("tab",),
                          ("text", "Born: December 2017")))
    group = docx_processor._paragraph_texts(root)[0]
    store, settings = loaded(), Settings(use_ner=False)
    text = docx_processor._group_text(group)
    hits = engine.scan_text(text, store, settings)
    docx_processor._set_group_text(group, hits)
    rewritten = docx_processor._group_text(group)
    assert "Ashdown" not in rewritten
    assert "Born: December 2017" in rewritten


def test_a_name_straddling_a_tab_is_replaced_and_the_tab_survives():
    """A tab renders as whitespace, so a variant's \\s+ join can span it."""
    root = body(paragraph(("text", "Marisol"), ("tab",), ("text", "Ashdown")))
    group = docx_processor._paragraph_texts(root)[0]
    store, settings = loaded(), Settings(use_ner=False)
    hits = engine.scan_text(docx_processor._group_text(group), store, settings)
    docx_processor._set_group_text(group, hits)
    assert "Ashdown" not in docx_processor._group_text(group)
    assert len(group[1].getparent().findall(f"{{{W}}}tab")) == 1, "the tab must stay"


def test_a_separator_is_never_written_into():
    root = body(paragraph(("text", "Marisol Quenby Ashdown"), ("tab",),
                          ("text", "Born: December 2017")))
    group = docx_processor._paragraph_texts(root)[0]
    separator = next(n for n in group if docx_processor._is_separator(n))
    store, settings = loaded(), Settings(use_ner=False)
    hits = engine.scan_text(docx_processor._group_text(group), store, settings)
    touched = docx_processor._set_group_text(group, hits)
    assert separator not in touched
    assert separator.text is None, "a <w:tab/> must not acquire text"


def test_a_paragraph_of_only_separators_is_not_a_text_unit():
    root = body(paragraph(("tab",), ("br",)))
    assert docx_processor._paragraph_texts(root) == []


def test_offsets_still_line_up_with_several_separators():
    root = body(paragraph(("text", "DATE"), ("tab",), ("tab",), ("tab",),
                          ("text", "MARISOL ASHDOWN, Respondent")))
    group = docx_processor._paragraph_texts(root)[0]
    store, settings = loaded(), Settings(use_ner=False)
    text = docx_processor._group_text(group)
    assert "DATEMARISOL" not in text
    hits = engine.scan_text(text, store, settings)
    docx_processor._set_group_text(group, hits)
    rewritten = docx_processor._group_text(group)
    assert "ASHDOWN" not in rewritten.upper()
    assert rewritten.startswith("DATE"), "text before the tabs must be intact"
    assert rewritten.endswith(", Respondent"), "text after them too"


# ------------------------------------------------------- signature rules --

@pytest.mark.parametrize("line", [
    "________Marisol Ashdown___________________",
    "_____ Marisol Ashdown _____",
    "Marisol Ashdown_______",
    "_______Marisol Ashdown",
])
def test_a_name_flush_against_a_signature_rule_is_replaced(line):
    """\\w counts "_" as a letter, so every signature line used to leak."""
    store, settings = loaded(), Settings(use_ner=False)
    out = engine.apply_hits(line, engine.scan_text(line, store, settings))
    assert "Ashdown" not in out


def test_the_underscore_boundary_does_not_loosen_the_real_ones():
    store, settings = loaded(), Settings(use_ner=False)
    for untouched in ["Ashdownshire is a place", "O'Ashdown is another surname"]:
        out = engine.apply_hits(untouched, engine.scan_text(untouched, store, settings))
        assert out == untouched, untouched
    possessive = "Marisol Ashdown's exhibit"
    assert "Ashdown's" not in engine.apply_hits(
        possessive, engine.scan_text(possessive, store, settings))


def test_a_misspelling_against_a_signature_rule_still_resolves():
    """The typo index carried the same boundary and missed these too."""
    store, settings = loaded(), Settings(use_ner=False)
    line = "________Marisoll Ashdown___________________"
    out = engine.apply_hits(line, engine.scan_text(line, store, settings))
    assert "Ashdown" not in out and "Marisoll" not in out


def test_the_do_not_change_list_uses_the_same_boundary():
    from redactor import patterns
    terms = ["Ashdown"]
    covered = patterns.allowlist_spans("____Ashdown____", terms)
    assert covered, "a shielded term flush against a rule must still be shielded"
    assert patterns.allowlist_spans("Ashdownshire", terms) == []


# --------------------------------------------------- the part-name collision --

def test_a_glossary_part_no_longer_hides_the_document_body(tmp_path):
    """word/document.xml and word/glossary/document.xml share a basename.

    collect_officials keyed regions on that basename, so a 67-byte glossary
    stub overwrote the entire body and the bench went unprotected.
    """
    from redactor import pipeline

    docx = pytest.importorskip("docx")
    path = tmp_path / "with-glossary.docx"
    document = docx.Document()
    document.add_paragraph("IN THE FOURTH JUDICIAL DISTRICT COURT")
    document.add_paragraph("Judge: Verity Ashgrove")
    document.save(path)

    # graft on a glossary part, the way a template-derived document carries one
    with zipfile.ZipFile(path, "a") as zf:
        zf.writestr("word/glossary/document.xml",
                    f'<w:document xmlns:w="{W}"><w:body><w:p><w:r><w:t>x</w:t>'
                    f"</w:r></w:p></w:body></w:document>")

    parts = docx_processor.extract_text(path)
    assert "word/glossary/document.xml" in parts, "the fixture must reproduce the clash"
    found = {o.name for o in pipeline.collect_officials([path])}
    assert "Verity Ashgrove" in found
