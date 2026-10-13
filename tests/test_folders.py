"""Whole folders in, the same tree out - folder names scrubbed, nothing else shipped."""

import zipfile
from pathlib import Path

import pytest

from redactor import pipeline, review
from redactor.engine import Settings

docx = pytest.importorskip("docx")


def _word(path: Path, *lines: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    document = docx.Document()
    for line in lines:
        document.add_paragraph(line)
    document.save(path)
    return path


@pytest.fixture
def case(tmp_path):
    root = tmp_path / "Harrington Divorce"
    _word(root / "2024 Bank Statements" / "Harrington_statement_Jan.docx",
          "Kyle D Harrington", "Account 4417220193", "Deposit $1,204.50")
    _word(root / "2024 Bank Statements" / "Feb.docx", "Kyle D Harrington paid $88.10")
    _word(root / "Pleadings" / "Petition.docx", "Petitioner Kyle D Harrington")
    _word(root / "Pleadings" / "Harrington" / "Exhibit.docx", "Exhibit for Kyle D Harrington")
    _word(root / "Pleadings" / "Kyle Harrington" / "Exhibit.docx", "Second exhibit")
    (root / "2024 Bank Statements" / "ledger.xlsx").write_bytes(b"PK\x03\x04")
    (root / "Pleadings" / "notes.txt").write_text("Kyle Harrington called")
    (root / ".DS_Store").write_bytes(b"\0")
    (root / "Pleadings" / "~$tition.docx").write_bytes(b"lock")
    return root


def _run(paths, tmp_path, mode="anonymize"):
    gathered = pipeline.gather(paths)
    files = [path for path, _rel in gathered.files]
    settings = Settings(use_ner=False, docx_mode=mode)
    store, _ = review.build_store([("Kyle D Harrington", "person")])
    pipeline.prescan(files, store, settings)
    result = pipeline.run_job(files, store, settings, tmp_path / "out", "pw-123456",
                              relative=dict(gathered.files), skipped=gathered.skipped)
    with zipfile.ZipFile(result.archive) as archive:
        names = archive.namelist()
    return gathered, result, names


def test_a_folder_is_gathered_with_its_own_name_at_the_top(case):
    gathered = pipeline.gather([case])
    relative = sorted(rel for _path, rel in gathered.files)
    assert relative == [
        "Harrington Divorce/2024 Bank Statements/Feb.docx",
        "Harrington Divorce/2024 Bank Statements/Harrington_statement_Jan.docx",
        "Harrington Divorce/Pleadings/Harrington/Exhibit.docx",
        "Harrington Divorce/Pleadings/Kyle Harrington/Exhibit.docx",
        "Harrington Divorce/Pleadings/Petition.docx",
    ]
    # other files are listed, never delivered; housekeeping is not even listed
    assert sorted(gathered.skipped) == ["Harrington Divorce/2024 Bank Statements/ledger.xlsx",
                                        "Harrington Divorce/Pleadings/notes.txt"]


def test_the_archive_repeats_the_tree_with_every_name_scrubbed(case, tmp_path):
    _gathered, result, names = _run([case], tmp_path)
    assert len(names) == 5 and not result.failed
    assert all(name.count("/") in (2, 3) for name in names)
    assert not any("harrington" in name.casefold() or "kyle" in name.casefold() for name in names)
    # one top folder, and the two statements still share theirs
    assert len({name.split("/")[0] for name in names}) == 1
    statements = [n for n in names if n.endswith(("Feb.docx", "_Jan.docx"))]
    assert len({n.rsplit("/", 1)[0] for n in statements}) == 1
    assert any(name.endswith("/Pleadings/Petition.docx") for name in names)
    # a name with nothing to take out goes through exactly as written
    assert any("/2024 Bank Statements/Feb.docx" in name for name in names)


def test_two_folders_that_scrub_alike_are_not_merged(case, tmp_path):
    _gathered, _result, names = _run([case], tmp_path)
    exhibits = [n for n in names if n.endswith("Exhibit.docx")]
    assert len(exhibits) == 2 and len({n.rsplit("/", 1)[0] for n in exhibits}) == 2


def test_what_was_left_out_is_in_the_report_and_not_the_archive(case, tmp_path):
    _gathered, result, names = _run([case], tmp_path)
    assert not any(n.endswith((".xlsx", ".txt")) for n in names)
    report = result.report_path.read_text()
    assert "LEFT OUT" in report
    assert "Harrington Divorce/2024 Bank Statements/ledger.xlsx" in report


def test_loose_files_and_folders_can_be_dropped_together(case, tmp_path):
    loose = _word(tmp_path / "loose" / "Kyle_Harrington_letter.docx", "Dear Kyle D Harrington")
    _gathered, _result, names = _run([case, loose], tmp_path)
    tops = [n for n in names if "/" not in n]
    assert len(tops) == 1 and "harrington" not in tops[0].casefold()


def test_folder_names_stay_when_filename_scrubbing_is_off(case, tmp_path):
    gathered = pipeline.gather([case])
    files = [path for path, _rel in gathered.files]
    settings = Settings(use_ner=False, anonymize_filenames=False)
    store, _ = review.build_store([("Kyle D Harrington", "person")])
    result = pipeline.run_job(files, store, settings, tmp_path / "out", "pw-123456",
                              relative=dict(gathered.files))
    with zipfile.ZipFile(result.archive) as archive:
        assert "Harrington Divorce/Pleadings/Petition.docx" in archive.namelist()


def test_a_link_inside_a_folder_is_not_followed(tmp_path):
    outside = _word(tmp_path / "elsewhere" / "private.docx", "Not part of the case")
    root = tmp_path / "Case"
    _word(root / "a.docx", "hello")
    (root / "link.docx").symlink_to(outside)
    gathered = pipeline.gather([root])
    assert [rel for _p, rel in gathered.files] == ["Case/a.docx"]
    assert gathered.skipped == ["Case/link.docx"]
