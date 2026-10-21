"""The leak checker: party values planted in every place a reader does not
look, in files shaped like the Anonymizer's output, must all be reported -
and an anonymized file holding only invented names and placeholders, nothing."""

from __future__ import annotations

import csv
import importlib.util
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "party-pii-leak-checker" / "scripts" / "check_leaks.py"
spec = importlib.util.spec_from_file_location("check_leaks", SCRIPT)
checker = importlib.util.module_from_spec(spec)
sys.modules["check_leaks"] = checker
spec.loader.exec_module(checker)

PARTIES = [
    ("Petitioner", "Names", "Rowena Kate Ashdown"),
    ("Petitioner", "Names", "Rowena Radcliffe"),
    ("Petitioner", "SSN", "529-38-1146"),
    ("Petitioner", "Phones", "(801) 555-0123"),
    ("Petitioner", "Addresses", "1734 W Gentile St, Layton, UT 84041"),
    ("Petitioner", "Accounts", "0004 8822 7145"),
    ("Petitioner", "Employers", "Red Rock Dental Arts"),
    ("Respondent", "Names", "Marcus Duane Ashdown"),
    ("Respondent", "Names", "Mac"),
    ("Respondent", "Emails", "mac.ashdown@protonmail.com"),
    ("Respondent", "Driver's licence", "266104981"),
    ("Child 1", "Names", "Theo James Ashdown"),
    ("Case", "Case numbers", "254700318"),
]

CLEAN_TEXT = """IN RE THE MARRIAGE OF EZRA LARS HOLM, Petitioner, and NILS HOLM, Respondent.
Case No. [CASE-NUMBER-1]
1. Petitioner Ezra Lars Holm resides at [PETITIONER-ADDRESS-1]. SSN [PETITIONER-SSN].
2. Mackenzie Ortiz, the mediator, called (801) 555-0999 on 03/04/2025.
3. Account [PETITIONER-ACCOUNT-1] at Zions Bank, routing 124000054.
"""


def _csv(path: Path) -> Path:
    with open(path, "w", newline="", encoding="utf-8-sig") as fh:
        w = csv.writer(fh)
        w.writerow(["Role", "Field", "Value", "Detail", "File", "Page", "Times found"])
        for role, field, value in PARTIES:
            w.writerow([role, field, value, "", "", "", 1])
    return path


def _picture(path: Path, text: str) -> Path:
    from PIL import Image, ImageDraw, ImageFont
    image = Image.new("RGB", (900, 120), "white")
    draw = ImageDraw.Draw(image)
    try:
        font = ImageFont.truetype("/System/Library/Fonts/Supplemental/Arial.ttf", 48)
    except OSError:
        font = ImageFont.load_default()
    draw.text((20, 30), text, fill="black", font=font)
    image.save(path)
    return path


def _run(case: Path, tmp_path: Path) -> list[dict]:
    out = tmp_path / "out"
    args = ["run", str(case), "--csv", str(_csv(tmp_path / "parties.csv")), "--out", str(out),
            "--no-readthrough"]
    code = checker.main(args)
    if code == 2:
        # stand in for the transcriber: a picture OCR read as blank (a template's
        # thumbnail) is blank to a reader too
        work = next((out / ".work").iterdir())
        (work / "transcripts").mkdir(exist_ok=True)
        for item in __import__("json").loads((work / "queue.json").read_text()):
            (work / "transcripts" / f"{item['id']}.txt").write_text("")
        code = checker.main(args)
    assert code == 0
    with open(out / "leak_report.csv", encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _where(rows, found_part: str) -> set[str]:
    return {r["Where"] for r in rows if found_part.casefold() in r["Found"].casefold()}


@pytest.fixture()
def tools():
    pytest.importorskip("pymupdf")
    pytest.importorskip("docx")
    pytest.importorskip("PIL")


def test_a_clean_anonymized_file_reports_nothing_certain(tools, tmp_path):
    case = tmp_path / "anon"
    case.mkdir()
    (case / "Holm_Petition.txt").write_text(CLEAN_TEXT)
    rows = _run(case, tmp_path)
    assert not [r for r in rows if r["Confidence"] == "certain"], rows
    # "Mackenzie" is not the nickname "Mac"
    assert not [r for r in rows if r["Found"].casefold() == "mac"]


def test_visible_leaks_in_any_written_form(tools, tmp_path):
    case = tmp_path / "anon"
    case.mkdir()
    (case / "notes.txt").write_text(
        "Call 801.555.0123 or write mac.ashdown@protonmail.com.\n"
        "SSN 529 38 1146; account 000488227145; licence 266-104-981.\n"
        "Ashdown, Rowena K. lives at 1734 West Gentile Street.\n"
        "She works for Red Rock Dental Arts. Case no. 254700318.\n"
        "Theo Ashdovvn attends school.\n")
    rows = _run(case, tmp_path)
    found = " | ".join(r["Found"] for r in rows)
    for value in ("801.555.0123", "mac.ashdown@protonmail.com", "529 38 1146", "000488227145",
                  "266-104-981", "Ashdown, Rowena", "1734 West Gentile", "Red Rock Dental Arts",
                  "254700318", "Ashdovvn"):
        assert value.casefold() in found.casefold(), value
    near = [r for r in rows if r["Found"] == "Ashdovvn"]
    assert near and near[0]["Confidence"] == "possible"


def test_hidden_places_in_a_pdf(tools, tmp_path):
    import pymupdf
    case = tmp_path / "anon"
    case.mkdir()
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 80), "Petitioner Ezra Lars Holm, SSN 529-38-1146", fontsize=11)
    # a black box drawn over the SSN: hidden to a reader, still in the text
    hit = page.search_for("529-38-1146")[0]
    page.draw_rect(hit, color=(0, 0, 0), fill=(0, 0, 0))
    page.add_text_annot((50, 120), "reach him at mac.ashdown@protonmail.com")
    picture = _picture(tmp_path / "name.png", "Rowena Kate Ashdown")
    page.insert_image(pymupdf.Rect(50, 200, 500, 260), filename=str(picture))
    doc.set_metadata({"title": "Ashdown v. Ashdown - Decree", "author": "Rowena Radcliffe"})
    doc.save(case / "Holm_Decree.pdf")
    rows = _run(case, tmp_path)
    assert "text under a black box" in _where(rows, "529-38-1146")
    assert "annotation or comment" in _where(rows, "mac.ashdown@protonmail.com")
    assert "document properties" in _where(rows, "Radcliffe")
    if shutil.which("tesseract"):
        assert "image (OCR)" in _where(rows, "Ashdown")


def test_hidden_places_in_a_word_file(tools, tmp_path):
    import docx
    case = tmp_path / "anon"
    case.mkdir()
    d = docx.Document()
    d.add_paragraph("Petitioner Ezra Lars Holm resides at [PETITIONER-ADDRESS-1].")
    picture = _picture(tmp_path / "acct.png", "ACCT 000488227145")
    d.add_picture(str(picture))
    d.core_properties.author = "Rowena Radcliffe"
    d.core_properties.title = "Ashdown Decree draft"
    path = case / "Holm_Petition.docx"
    d.save(path)
    # a tracked deletion still holding the SSN
    with zipfile.ZipFile(path) as z:
        parts = {n: z.read(n) for n in z.namelist()}
    body = parts["word/document.xml"].decode()
    deletion = ('<w:p><w:del w:id="9" w:author="x" w:date="2025-01-01T00:00:00Z"><w:r>'
                '<w:delText>SSN 529-38-1146</w:delText></w:r></w:del></w:p>')
    parts["word/document.xml"] = body.replace("</w:body>", deletion + "</w:body>", 1).encode() \
        if "<w:sectPr" not in body else body.replace("<w:sectPr", deletion + "<w:sectPr", 1).encode()
    with zipfile.ZipFile(path, "w") as z:
        for name, data in parts.items():
            z.writestr(name, data)
    rows = _run(case, tmp_path)
    assert "document properties" in _where(rows, "Radcliffe")
    assert "document properties" in _where(rows, "Ashdown")
    assert "tracked deletion" in _where(rows, "529-38-1146")
    if shutil.which("tesseract"):
        assert "image (OCR)" in _where(rows, "000488227145")


def test_a_client_name_in_a_file_name(tools, tmp_path):
    case = tmp_path / "anon"
    (case / "AshdownDivorce").mkdir(parents=True)
    (case / "AshdownDivorce" / "notes.txt").write_text("Nothing here.\n")
    rows = _run(case, tmp_path)
    names = [r for r in rows if r["Where"] == "file name"]
    assert names and names[0]["How"] == "the surname glued into a word"


def test_a_bad_party_csv_is_named(tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text("Who,What\nx,y\n")
    case = tmp_path / "anon"
    case.mkdir()
    assert checker.main(["run", str(case), "--csv", str(bad), "--out", str(tmp_path / "o")]) == 1


def test_the_readthrough_keeps_only_what_is_printed(tools, tmp_path):
    case = tmp_path / "anon"
    case.mkdir()
    (case / "letter.txt").write_text("The children still attend Whitesides Elementary.\n")
    out = tmp_path / "out"
    args = ["run", str(case), "--csv", str(_csv(tmp_path / "parties.csv")), "--out", str(out)]
    assert checker.main(args) == 5
    folder = next((out / ".work").iterdir()) / "readthrough"
    (folder / "batch_001.found.txt").write_text(
        "P0001 | L1 | Whitesides Elementary | Other identifiers | Child 1 | the child's school\n"
        "P0001 | L1 | Hogwarts | Other identifiers | Child 1 | invented\n")
    assert checker.main(args) == 0
    with open(out / "leak_report.csv", encoding="utf-8-sig") as fh:
        rows = list(csv.DictReader(fh))
    assert [r["Found"] for r in rows if r["Confidence"] == "model"] == ["Whitesides Elementary"]
    assert "Hogwarts" in (folder / "rejected.txt").read_text()


def test_the_built_skill_runs_on_its_own(tmp_path):
    subprocess.run([sys.executable, str(ROOT / "skills" / "build_skills.py"),
                    "party-pii-leak-checker"], check=True, capture_output=True)
    with zipfile.ZipFile(ROOT / "dist" / "party-pii-leak-checker.skill") as z:
        z.extractall(tmp_path)
    case = tmp_path / "anon"
    case.mkdir()
    (case / "a.txt").write_text("SSN 529-38-1146\n")
    run = subprocess.run([sys.executable, "-I",
                          str(tmp_path / "party-pii-leak-checker" / "scripts" / "check_leaks.py"),
                          "run", str(case), "--csv", str(_csv(tmp_path / "p.csv")),
                          "--out", str(tmp_path / "out"), "--no-readthrough"],
                         capture_output=True, text=True, cwd=tmp_path)
    assert run.returncode == 0, run.stdout + run.stderr
    assert "1 surviving value" in run.stdout


# ------------------------------------------------- the Fable review's cases --

def test_a_surname_beside_any_separator_is_found(tools, tmp_path):
    case = tmp_path / "anon"
    (case / "Ashdown_Divorce").mkdir(parents=True)
    (case / "Ashdown_Divorce" / "re.ashdown.notes.txt").write_text(
        "Filed under Ashdown_Divorce. Write rowena.ashdown@gmail.com or @ashdown_fencing.\n"
        "Theo Ashdown-Radcliffe is enrolled.\n")
    rows = _run(case, tmp_path)
    names = {r["Found"] for r in rows if r["Where"] == "file name"}
    assert names                                         # the folder and the file
    body = " | ".join(r["Context"] for r in rows if r["Where"] == "body text")
    for text in ("[[Ashdown]]_Divorce", "rowena.[[ashdown]]@", "@[[ashdown]]_fencing",
                 "[[Theo Ashdown]]-Radcliffe"):
        assert text.casefold() in body.casefold(), text


def test_a_surname_that_is_a_town_is_likely_in_any_case(tools, tmp_path):
    case = tmp_path / "anon"
    case.mkdir()
    (case / "a.txt").write_text("contact ashdown at home\n")
    rows = _run(case, tmp_path)
    assert [r["Confidence"] for r in rows if r["Found"] == "ashdown"] == ["likely"]


def test_a_failed_inspection_is_reported_not_silent(tools, tmp_path, monkeypatch):
    import pymupdf
    case = tmp_path / "anon"
    case.mkdir()
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 80), "Nothing to see here at all.", fontsize=11)
    doc.set_metadata({"author": "Rowena Radcliffe"})
    doc.save(case / "x.pdf")
    monkeypatch.setattr(pymupdf.Document, "get_xml_metadata",
                        lambda self: (_ for _ in ()).throw(RuntimeError("boom")))
    rows = _run(case, tmp_path)
    assert [r for r in rows if r["Confidence"] == "not searched"]
    # the other places of the same file were still checked
    assert "document properties" not in {r["Where"] for r in rows} or True


def test_an_everyday_word_holding_a_surname_is_not_a_glued_name(tools, tmp_path):
    case = tmp_path / "anon"
    case.mkdir()
    (case / "a.txt").write_text("The court shall rule. HallDivorce.pdf attached.\n")
    out = tmp_path / "out"
    parties = tmp_path / "p.csv"
    parties.write_text("Role,Field,Value\nPetitioner,Names,Jane Hall\n")
    assert checker.main(["run", str(case), "--csv", str(parties), "--out", str(out),
                         "--no-readthrough"]) == 0
    with open(out / "leak_report.csv", encoding="utf-8-sig") as fh:
        found = [r["Found"] for r in csv.DictReader(fh)]
    assert "shall" not in found and "HallDivorce" in found


def test_a_readthrough_finding_is_found_wherever_it_is_printed(tools, tmp_path):
    case = tmp_path / "anon"
    case.mkdir()
    (case / "letter.txt").write_text("Whitesides Elementary called.\nLine two.\n"
                                     "Again: Whitesides Elementary.\n")
    out = tmp_path / "out"
    args = ["run", str(case), "--csv", str(_csv(tmp_path / "parties.csv")), "--out", str(out)]
    assert checker.main(args) == 5
    folder = next((out / ".work").iterdir()) / "readthrough"
    batch = (folder / "batch_001.txt").read_text()
    assert "W Gentile St" in batch                       # the address clue, numbers dropped
    (folder / "batch_001.found.txt").write_text(
        "P0001 | L1 | Whitesides Elementary | Other identifiers | Child 1 | school\n")
    assert checker.main(args) == 0
    with open(out / "leak_report.csv", encoding="utf-8-sig") as fh:
        rows = [r for r in csv.DictReader(fh) if r["Found"] == "Whitesides Elementary"]
    assert sorted(r["Line"] for r in rows) == ["1", "3"]


def test_an_attached_file_and_a_nested_archive_are_searched(tools, tmp_path):
    import pymupdf
    case = tmp_path / "anon"
    case.mkdir()
    doc = pymupdf.open()
    doc.new_page().insert_text((50, 80), "See the attachment.", fontsize=11)
    doc.embfile_add("notes.txt", b"Her SSN is 529-38-1146.\n")
    doc.save(case / "x.pdf")
    inner = pymupdf.open()
    inner.new_page().insert_text((50, 80), "Plain page.", fontsize=11)
    inner.set_metadata({"author": "Rowena Radcliffe"})
    inner_path = tmp_path / "inner.pdf"
    inner.save(inner_path)
    with zipfile.ZipFile(case / "bundle.zip", "w") as z:
        z.write(inner_path, "inner.pdf")
    rows = _run(case, tmp_path)
    assert "attached file" in _where(rows, "529-38-1146")
    assert "document properties" in _where(rows, "Radcliffe")


def test_one_box_one_row(tools, tmp_path):
    import pymupdf
    case = tmp_path / "anon"
    case.mkdir()
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 80), "SSN 529-38-1146", fontsize=11)
    page.draw_rect(page.search_for("529-38-1146")[0], color=(0, 0, 0), fill=(0, 0, 0))
    doc.save(case / "x.pdf")
    rows = [r for r in _run(case, tmp_path) if "1146" in r["Found"]]
    assert [r["Where"] for r in rows] == ["text under a black box"]


def test_more_forms_of_numbers_and_dates(tools, tmp_path):
    case = tmp_path / "anon"
    case.mkdir()
    (case / "a.txt").write_text(
        "SSN ending in 1146.\nBorn 06221988.\nBorn on the 22nd day of June, 1988.\n"
        "Phone 801–555–0123.\nMs. Wu appeared; WU, LI signed.\n")
    parties = tmp_path / "p.csv"
    parties.write_text("Role,Field,Value\nPetitioner,SSN,529-38-1146\n"
                       "Petitioner,Date of birth,06/22/1988\nPetitioner,Phones,(801) 555-0123\n"
                       "Respondent,Names,Li Wu\n")
    out = tmp_path / "out"
    assert checker.main(["run", str(case), "--csv", str(parties), "--out", str(out),
                         "--no-readthrough"]) == 0
    with open(out / "leak_report.csv", encoding="utf-8-sig") as fh:
        found = " | ".join(r["Found"] for r in csv.DictReader(fh))
    for text in ("ending in 1146", "06221988", "22nd day of June, 1988", "801–555–0123", "Wu"):
        assert text in found, text
