"""End-to-end: what actually ends up in the delivered files."""

import zipfile

import pymupdf
import pytest

from redactor import docx_processor, pdf_processor, pipeline
from redactor.engine import Settings
from redactor.mapping import MappingStore, read_encrypted_key
from tests.conftest import PRESERVED, SECRETS


def loaded_store():
    store = MappingStore()
    store.add_person("Jane Elizabeth Smith", role="Petitioner")
    store.add_person("John Michael Smith", role="Respondent")
    return store


def docx_raw_xml(path):
    with zipfile.ZipFile(path) as zf:
        return "\n".join(
            zf.read(name).decode("utf-8", "ignore")
            for name in zf.namelist()
            if name.endswith(".xml")
        )


def pdf_all_text(path):
    with pymupdf.open(path) as doc:
        return "\n".join(page.get_text("text") for page in doc)


# ------------------------------------------------------------------- DOCX --


@pytest.mark.parametrize("mode", ["anonymize", "redact"])
def test_docx_leaks_nothing_in_either_mode(sample_docx, tmp_path, mode):
    out = tmp_path / f"out-{mode}.docx"
    docx_processor.process(sample_docx, out, loaded_store(), Settings(docx_mode=mode))
    xml = docx_raw_xml(out)
    leaked = [secret for secret in SECRETS if secret in xml]
    assert leaked == [], f"{mode} mode leaked: {leaked}"


def test_docx_keeps_the_parts_of_the_record_that_must_survive(sample_docx, tmp_path):
    out = tmp_path / "out.docx"
    docx_processor.process(sample_docx, out, loaded_store(), Settings())
    body = "\n".join(docx_processor.extract_text(out).values())
    for phrase in PRESERVED:
        assert phrase in body


def test_docx_replaces_a_name_split_across_runs(sample_docx, tmp_path):
    """Word splits names across runs constantly; a run-by-run scan would miss them."""
    out = tmp_path / "out.docx"
    store = loaded_store()
    docx_processor.process(sample_docx, out, store, Settings())
    body = "\n".join(docx_processor.extract_text(out).values())
    surrogate = store.get("person", "Jane Elizabeth Smith").replacement
    assert surrogate in body


def test_docx_headers_and_footers_are_processed(sample_docx, tmp_path):
    out = tmp_path / "out.docx"
    docx_processor.process(sample_docx, out, loaded_store(), Settings())
    parts = docx_processor.extract_text(out)
    header = next(v for k, v in parts.items() if "header" in k)
    footer = next(v for k, v in parts.items() if "footer" in k)
    assert "Smith" not in header
    assert "jane.smith1985@gmail.com" not in footer


def test_docx_metadata_is_scrubbed(sample_docx, tmp_path):
    out = tmp_path / "out.docx"
    docx_processor.process(sample_docx, out, loaded_store(), Settings())
    with zipfile.ZipFile(out) as zf:
        core = zf.read("docProps/core.xml").decode()
    for leak in ("Marcus T. Whitfield", "Danielle Okonkwo", "Smith Divorce", "528-41-9963"):
        assert leak not in core


def test_docx_redact_mode_draws_black_bars(sample_docx, tmp_path):
    out = tmp_path / "out.docx"
    docx_processor.process(sample_docx, out, loaded_store(), Settings(docx_mode="redact"))
    with zipfile.ZipFile(out) as zf:
        document = zf.read("word/document.xml").decode()
    assert 'w:highlight w:val="black"' in document
    assert "[REDACTED]" in document


def test_docx_output_is_a_readable_word_file(sample_docx, tmp_path):
    docx = pytest.importorskip("docx")
    out = tmp_path / "out.docx"
    docx_processor.process(sample_docx, out, loaded_store(), Settings())
    reopened = docx.Document(out)
    assert len(reopened.paragraphs) > 5


# -------------------------------------------------------------------- PDF --


def test_pdf_text_is_truly_removed(sample_pdf, tmp_path):
    out = tmp_path / "out.pdf"
    result = pdf_processor.process(sample_pdf, out, loaded_store(), Settings())
    assert not result.refused
    text = pdf_all_text(out)
    leaked = [secret for secret in SECRETS if secret in text]
    assert leaked == [], f"leaked: {leaked}"


def test_pdf_keeps_the_public_record(sample_pdf, tmp_path):
    out = tmp_path / "out.pdf"
    pdf_processor.process(sample_pdf, out, loaded_store(), Settings())
    text = pdf_all_text(out)
    assert "Judge Amber M. Cordova" in text
    assert "Rule 26" in text


def test_pdf_metadata_is_scrubbed(sample_pdf, tmp_path):
    out = tmp_path / "out.pdf"
    pdf_processor.process(sample_pdf, out, loaded_store(), Settings())
    with pymupdf.open(out) as doc:
        values = " ".join(v or "" for v in doc.metadata.values())
    assert "Whitfield" not in values
    assert "528-41-9963" not in values


def test_an_image_only_pdf_is_refused_not_passed_through(scanned_pdf, tmp_path):
    """The dangerous failure is a scanned page that *looks* processed."""
    out = tmp_path / "out.pdf"
    result = pdf_processor.process(scanned_pdf, out, loaded_store(),
                                   Settings(ocr_scanned_pdfs=False))
    assert result.refused
    assert result.output is None
    assert not out.exists()
    assert result.warnings


# --------------------------------------------------------------- pipeline --


def test_prescan_finds_what_the_run_applies(sample_docx, sample_pdf, tmp_path):
    files = [sample_docx, sample_pdf]
    settings = Settings()

    previewed = loaded_store()
    pipeline.prescan(files, previewed, settings)

    applied = loaded_store()
    pipeline.run_job(files, applied, settings, tmp_path / "out", "pw")

    assert set(previewed.entities) == set(applied.entities)
    assert {e.replacement for e in previewed.entities.values()} == \
           {e.replacement for e in applied.entities.values()}


def test_run_produces_an_archive_with_the_key_left_outside(sample_docx, sample_pdf, tmp_path):
    result = pipeline.run_job([sample_docx, sample_pdf], loaded_store(), Settings(),
                              tmp_path / "out", "pw")
    assert result.archive and result.archive.exists()
    assert result.key_path and result.key_path.exists()
    assert result.report_path and result.report_path.exists()

    names = zipfile.ZipFile(result.archive).namelist()
    assert len(names) == 2
    assert not any(name.endswith(".json") or "mapping" in name for name in names)
    assert result.key_path.parent == result.archive.parent


def test_filenames_are_anonymized_too(sample_docx, tmp_path):
    """Smith_Divorce_Findings.docx names the client before anyone opens it."""
    result = pipeline.run_job([sample_docx], loaded_store(), Settings(),
                              tmp_path / "out", "pw")
    delivered = result.processed[0].delivered_name
    assert "Smith" not in delivered
    assert delivered.endswith(".docx")


def test_filenames_are_left_alone_when_the_option_is_off(sample_docx, tmp_path):
    result = pipeline.run_job([sample_docx], loaded_store(),
                              Settings(anonymize_filenames=False), tmp_path / "out", "pw")
    assert result.processed[0].delivered_name == sample_docx.name


def test_the_report_carries_no_original_values(sample_docx, tmp_path):
    result = pipeline.run_job([sample_docx], loaded_store(), Settings(),
                              tmp_path / "out", "pw")
    report = result.report_path.read_text()
    for secret in ("528-41-9963", "jane.smith1985@gmail.com", "000148829371"):
        assert secret not in report


def test_the_key_recovers_the_mapping(sample_docx, tmp_path):
    store = loaded_store()
    result = pipeline.run_job([sample_docx], store, Settings(), tmp_path / "out", "s3cret")
    recovered = read_encrypted_key(result.key_path, "s3cret")
    pairs = {row["original"]: row["replacement"] for row in recovered["entities"]}
    assert pairs["Jane Elizabeth Smith"] == store.get("person", "Jane Elizabeth Smith").replacement


def test_the_whole_run_is_deterministic(sample_docx, sample_pdf, tmp_path):
    """Same documents, same name list, same output - every time."""
    digests = []
    for attempt in range(2):
        result = pipeline.run_job([sample_docx, sample_pdf], loaded_store(), Settings(),
                                  tmp_path / f"run{attempt}", "pw")
        with zipfile.ZipFile(result.archive) as zf:
            docx_name = next(n for n in zf.namelist() if n.endswith(".docx"))
            digests.append(docx_raw_xml_from_bytes(zf.read(docx_name)))
    assert digests[0] == digests[1]


def docx_raw_xml_from_bytes(data):
    import io
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        return "\n".join(
            zf.read(name).decode("utf-8", "ignore")
            for name in sorted(zf.namelist()) if name.endswith(".xml")
        )


def test_an_excluded_person_is_left_in_place(sample_docx, tmp_path):
    store = MappingStore()
    store.add_person("John Michael Smith").enabled = False
    out = tmp_path / "out.docx"
    docx_processor.process(sample_docx, out, store, Settings())
    body = "\n".join(docx_processor.extract_text(out).values())
    assert "John Michael Smith" in body


def test_an_excluded_value_is_left_in_place(sample_docx, tmp_path):
    store = loaded_store()
    pipeline.prescan([sample_docx], store, Settings())
    store.get("ssn", "528-41-9963").enabled = False
    out = tmp_path / "out.docx"
    docx_processor.process(sample_docx, out, store, Settings())
    body = "\n".join(docx_processor.extract_text(out).values())
    assert "528-41-9963" in body


def test_excluding_one_of_two_people_who_share_a_surname(sample_docx, tmp_path):
    """A surname belongs to both parties, so it goes when either of them is in.

    Excluding John does not preserve "Smith" while Jane is still being replaced -
    the same six letters cannot be both kept and removed. His given names survive;
    the shared surname does not. Worth knowing before relying on the tick box.
    """
    store = loaded_store()
    store.get("person", "John Michael Smith").enabled = False
    out = tmp_path / "out.docx"
    docx_processor.process(sample_docx, out, store, Settings())
    body = "\n".join(docx_processor.extract_text(out).values())
    assert "John Michael" in body
    assert "John Michael Smith" not in body


# ------------------------------------------------- regressions: leak classes --


@pytest.mark.parametrize("filename", [
    "Smith_Divorce_Findings.docx",       # separator-delimited
    "SmithDivorcePetition.docx",         # glued, no separators at all
    "Smith2024Decree.docx",              # glued to digits
    "JSmith-Findings.docx",              # glued behind an initial
    "SMITH_John_Decree.docx",            # upper case
    "smith_divorce.docx",                # lower case
    "Smith, Jane - Petition.docx",       # comma form
])
def test_no_filename_form_leaks_the_surname(filename, tmp_path):
    """A surname glued to its neighbours has no word boundary to match on.

    Before this was fixed, SmithDivorcePetition.docx shipped unchanged: the
    matcher could not see the name, and the safety check only looked for the
    full canonical "Jane Elizabeth Smith", never the surname alone.
    """
    from redactor.engine import EntityMatcher
    from pathlib import Path

    store = loaded_store()
    settings = Settings()
    delivered = pipeline.anonymized_filename(
        Path(filename), store, settings, EntityMatcher(store, settings), 1)
    assert "smith" not in delivered.casefold()
    assert delivered.endswith(".docx")


def test_a_name_with_an_internal_capital_is_not_mangled():
    """Splitting glued names must not break MacDonald into Mac Donald."""
    from redactor.engine import EntityMatcher
    from pathlib import Path

    store = loaded_store()
    settings = Settings()
    delivered = pipeline.anonymized_filename(
        Path("MacDonald_Petition.docx"), store, settings,
        EntityMatcher(store, settings), 1)
    assert delivered == "MacDonald_Petition.docx"


def test_a_bare_recurrence_of_a_known_value_is_caught():
    """Labels appear once; the value itself recurs.

    "Case No. 224900871" matches on its label. A bare "224900871" further down
    matched nothing at all until registered values were also matched literally.
    """
    from redactor.engine import Settings as S, scan_and_apply

    store = MappingStore()
    text = ("Case No. 224900871 is assigned. The 224900871 matter was heard. "
            "Account no. 000148829371, and later just 000148829371.")
    result = scan_and_apply(text, store, S(), "t")[0]
    assert "224900871" not in result
    assert "000148829371" not in result


def test_literal_value_matching_still_respects_the_allowlist():
    from redactor.engine import Settings as S, scan_and_apply

    store = MappingStore()
    text = ("Case No. 26 is assigned. Rule 26 governs, and Section 30-3-5 applies. "
            "See also Case No. 224900871 and the 224900871 matter.")
    result = scan_and_apply(text, store, S(), "t")[0]
    assert "Rule 26" in result
    assert "Section 30-3-5" in result
    assert "224900871" not in result
