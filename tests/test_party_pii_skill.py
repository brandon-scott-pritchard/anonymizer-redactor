"""The party-PII skill: its script against a fabricated case, and its copy of
the app's rules kept equal to the app's own."""

from __future__ import annotations

import csv
import importlib.util
import json
import re
import shutil
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

from redactor import parties

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "party-pii-extractor" / "scripts" / "extract_party_pii.py"
sys.path.insert(0, str(Path(__file__).resolve().parent))
import party_pii_fixture as fixture  # noqa: E402

spec = importlib.util.spec_from_file_location("extract_party_pii", SCRIPT)
extract = importlib.util.module_from_spec(spec)
sys.modules["extract_party_pii"] = extract          # dataclasses look their module up
spec.loader.exec_module(extract)


def _key(value: str) -> str:
    digits = re.sub(r"\D", "", value)
    alnum = re.sub(r"\W", "", value)
    return digits if len(digits) >= 4 and len(digits) >= len(alnum) - 2 else alnum.casefold()


def _rows(path: Path) -> list[dict]:
    with open(path, encoding="utf-8-sig") as fh:
        return list(csv.DictReader(fh))


def _decide(work: Path, drop_values=()) -> None:
    """Stand in for the model: an explicit answer for every item - the proposal,
    or drop for what is not a party's and for what nobody was tied to."""
    items = json.loads((work / "review.json").read_text())["items"]
    decisions = {}
    for item in items:
        if any(v.casefold() in item["value"].casefold() for v in drop_values) \
                or item["proposed"] is None:
            decisions[item["id"]] = "drop"
        else:
            decisions[item["id"]] = item["proposed"]
    (work / "decisions.json").write_text(json.dumps(decisions))


# ------------------------------------------------------------------ rules --

def test_rules_match_the_apps():
    assert extract.KIND_OF == parties.KIND_OF
    assert extract.LEAVE == parties.LEAVE
    assert extract.HEADER_LINES == parties.HEADER_LINES
    assert extract.BLOCK_LINES == parties.BLOCK_LINES
    for name in ("_HEADER_LABEL", "_TRANSFER_LINE", "_LEAVE_LINE", "_ACTIVITY_ROW",
                 "_ACTIVITY_HEADING", "_LOCKBOX_OR_BANK"):
        assert getattr(extract, name).pattern == getattr(parties, name).pattern, name


def test_csv_values_survive_a_spreadsheet():
    assert extract.display("account", "000488227145") == "0004 8822 7145"
    assert extract.display("ssn", "529381146") == "529-38-1146"
    assert extract.display("phone", "8015550123") == "(801) 555-0123"
    assert extract.display("dob", "June 22, 1988") == "06/22/1988"
    assert extract.display("address", "=HYPERLINK(1)").startswith("'")
    assert extract.display("handle", "@jsmith") == "@jsmith"


def test_a_zip_cannot_write_outside_its_folder(tmp_path):
    bad = tmp_path / "bad.zip"
    with zipfile.ZipFile(bad, "w") as z:
        z.writestr("../escaped.txt", "x")
        z.writestr("ok/inside.txt", "y")
    extract._safe_extract(bad, tmp_path / "out")
    assert not (tmp_path / "escaped.txt").exists()
    assert (tmp_path / "out" / "ok" / "inside.txt").exists()


# ------------------------------------------------------------ the case run --

@pytest.fixture()
def case(tmp_path):
    pytest.importorskip("pymupdf")
    pytest.importorskip("docx")
    return fixture.build(tmp_path / "in" / "Ashdown")


def _run(case: Path, out: Path, *extra, sweep: bool = False) -> int:
    return extract.main(["run", str(case), "--out", str(out), "--single-case", "--no-ocr",
                         *([] if sweep else ["--no-sweep"]), *extra])


def _transcribe(work: Path) -> None:
    (work / "transcripts").mkdir(exist_ok=True)
    for item in json.loads((work / "queue.json").read_text()):
        (work / "transcripts" / f"{item['id']}.txt").write_text(fixture.SCANNED)


def test_whole_case_just_the_parties(case, tmp_path):
    out = tmp_path / "out"
    work = out / ".work" / "Ashdown"
    assert _run(case, out) == 2                     # the scan waits for a transcript
    _transcribe(work)
    assert _run(case, out) == 4                     # every candidate waits for a decision
    _decide(work, fixture.NOT_PARTIES + ["dana@"])
    assert _run(case, out) == 0
    rows = _rows(out / "Ashdown.csv")
    got = {(r["Role"], r["Field"], _key(r["Value"])) for r in rows}
    missing = [t for t in fixture.TRUTH if (t[0], t[1], _key(t[2])) not in got]
    assert not missing
    values = " ".join(r["Value"] for r in rows).casefold()
    assert not [v for v in fixture.NOT_PARTIES if v.casefold() in values]


def test_the_sweep_finds_what_only_prose_says(case, tmp_path):
    """The read-through's findings join the review: real ones kept, one the page
    does not print rejected, a derived first name and a known value ignored."""
    out = tmp_path / "out"
    work = out / ".work" / "Ashdown"
    assert _run(case, out, sweep=True) == 2
    _transcribe(work)
    assert _run(case, out, sweep=True) == 5                     # waits for the sweep
    batch = (work / "sweep" / "batch_001.txt").read_text()
    notes = re.search(r"=== (P\d{4}) \| intake notes.txt", batch).group(1)
    assert "⟦" in batch and " mac_builds_utah " in batch     # not marked as already found
    (work / "sweep" / "batch_001.found.txt").write_text("\n".join([
        f"{notes} | L3 | Rowena Tolliver | Names | Petitioner | former married name",
        f"{notes} | L4 | the place on Willow Bend Court in Clearfield | Addresses | Both parties | old rental",
        f"{notes} | L5 | mac_builds_utah | Handles | Respondent | his Instagram",
        f"{notes} | L6 | Whitesides Elementary | Other identifiers | Child 1 | Theo's school",
        f"{notes} | L6 | Theo | Names | Child 1 | first name",                 # derived: ignored
        f"{notes} | L4 | 999 Imaginary Lane | Addresses | Petitioner | invented",  # rejected
        "P9999 | L1 | anything | Names | unknown | no such page",               # rejected
    ]) + "\n")
    assert _run(case, out, sweep=True) == 4
    rejected = (work / "sweep" / "rejected.txt").read_text()
    assert "Imaginary" in rejected and "P9999" in rejected
    items = json.loads((work / "review.json").read_text())["items"]
    swept = {i["value"] for i in items if i["why"].startswith("sweep:")}
    assert swept == {"Rowena Tolliver", "the place on Willow Bend Court in Clearfield",
                     "mac_builds_utah", "Whitesides Elementary"}
    _decide(work, fixture.NOT_PARTIES + ["dana@"])
    assert _run(case, out, sweep=True) == 0
    got = {(r["Role"], r["Field"], _key(r["Value"])) for r in _rows(out / "Ashdown.csv")}
    for role, field, value in fixture.SWEEP_TRUTH:
        assert any(r in (role, "Both parties") and f == field and _key(value) in v
                   for r, f, v in got), value


def test_counsel_is_never_proposed_and_a_merchant_is_left_to_the_model(case, tmp_path):
    out = tmp_path / "out"
    _run(case, out)
    _transcribe(out / ".work" / "Ashdown")
    _run(case, out)
    items = json.loads((out / ".work" / "Ashdown" / "review.json").read_text())["items"]
    values = {i["value"]: i for i in items}
    # in counsel's signature block: offered for the model to drop, never proposed
    assert values["(801) 555-0440"]["proposed"] is None
    assert values["(801) 555-0440"]["why"] == "beside a non-party's name"
    assert values.get("priya@vfamilylaw.com", {"proposed": None})["proposed"] is None
    merchant = next(i for i in items if i["value"] == "801-555-0911")
    assert merchant["proposed"] is None             # nobody named near: the model decides


def test_the_same_files_give_the_same_review(case, tmp_path):
    reviews = []
    for name in ("a", "b"):
        out = tmp_path / name
        _run(case, out)
        _transcribe(out / ".work" / "Ashdown")
        _run(case, out)
        reviews.append((out / ".work" / "Ashdown" / "review.txt").read_text())
    assert reviews[0] == reviews[1]


def test_two_parties_on_one_line_each_get_their_own(case, tmp_path):
    out = tmp_path / "out"
    _run(case, out)
    _transcribe(out / ".work" / "Ashdown")
    _run(case, out)
    items = json.loads((out / ".work" / "Ashdown" / "review.json").read_text())["items"]
    owner = {i["value"]: i["proposed"] for i in items if i["kind"] == "employer"}
    assert owner.get("Red Rock Dental Arts") == "Petitioner"
    assert owner.get("Ashdown Fencing LLC") == "Respondent"


def test_stale_decisions_are_discarded(case, tmp_path):
    out = tmp_path / "out"
    work = out / ".work" / "Ashdown"
    _run(case, out)
    _transcribe(work)
    _run(case, out)
    (work / "decisions.json").write_text(json.dumps({"_rest": "accept"}))
    (case / "extra.txt").write_text("Respondent's second phone is (801) 555-0166.\n")
    assert _run(case, out, "--rescan") == 4         # a new item: asked again
    assert not (work / "decisions.json").exists()


def test_contradicting_documents_become_a_conflict(tmp_path):
    shutil.copytree(ROOT / "samples", tmp_path / "in")
    out = tmp_path / "out"
    assert extract.main(["run", str(tmp_path / "in"), "--out", str(out), "--no-sweep"]) == 4
    items = json.loads((out / ".work" / "in" / "review.json").read_text())["items"]
    ssn = next(i for i in items if i["kind"] == "ssn")
    assert ssn["proposed"] is None and ssn["why"].startswith("conflict")


def test_unreadable_files_are_reported(tmp_path):
    case = tmp_path / "in"
    case.mkdir()
    (case / "notes.pages").write_bytes(b"\x00binary")
    (case / "caption.txt").write_text(fixture.PETITION)
    out = tmp_path / "out"
    extract.main(["run", str(case), "--out", str(out)])
    scan = json.loads((out / ".work" / "in" / "scan.json").read_text())
    assert scan["unreadable"] and scan["unreadable"][0][0] == "notes.pages"


# ---------------------------------------------------------------- the fork --

def _texts() -> list[str]:
    """Pleadings, statements and stubs the detectors are exercised on."""
    from tests import conftest
    out = [fixture.PETITION, fixture.DECLARATION, fixture.STATEMENT, fixture.SCANNED,
           fixture.EMAIL_BODY, conftest.CAPTION, "\n".join(conftest.BODY_LINES)]
    corpus = Path.home() / "Downloads" / ("Redactor:Anonymizer & Financial Declaration "
                                          "Training Data") / "10 Household Training Data"
    pymupdf = pytest.importorskip("pymupdf")
    for pdf in sorted(corpus.glob("HH0[1-3]_*/**/*.pdf"))[::40][:60]:
        with pymupdf.open(pdf) as doc:
            out += [page.get_text("text", sort=True) for page in doc][:2]
    return out


def test_the_fork_finds_what_the_app_finds():
    """partycore is the app's detectors trimmed for the skill: same input, same finds."""
    from redactor import caption, children, patterns, transactions
    fork = sys.modules["extract_party_pii"]
    for text in _texts():
        assert [(m.category, m.text) for m in fork.patterns.scan(text)] == \
            [(m.category, m.text) for m in patterns.scan(text)]
        assert fork.patterns.allowlist_spans(text) == patterns.allowlist_spans(text)
        region = caption.caption_region(text)
        assert [(c.name, c.role) for c in fork.caption.harvest(region)] == \
            [(c.name, c.role) for c in caption.harvest(region)]
        assert fork.caption.former_names(text) == caption.former_names(text)
        assert [(c.name, c.born) for c in fork.children.harvest(text)] == \
            [(c.name, c.born) for c in children.harvest(text)]
        for fn in ("employers", "businesses", "account_holders"):
            assert [p.name for p in getattr(fork.transactions, fn)(text)] == \
                [p.name for p in getattr(transactions, fn)(text)], fn
        assert fork.transactions.pay_stub_employers(text) == transactions.pay_stub_employers(text)


# ------------------------------------------------------------- the package --

def test_the_built_skill_runs_on_its_own(tmp_path):
    subprocess.run([sys.executable, str(ROOT / "skills" / "build_skills.py"), "party-pii-extractor"],
                   check=True, capture_output=True)
    with zipfile.ZipFile(ROOT / "dist" / "party-pii-extractor.skill") as z:
        z.extractall(tmp_path)
        names = z.namelist()
    assert "party-pii-extractor/SKILL.md" in names
    assert "party-pii-extractor/scripts/partycore/patterns.py" in names
    case = tmp_path / "case"
    shutil.copytree(ROOT / "samples", case)
    run = subprocess.run([sys.executable, "-I",
                          str(tmp_path / "party-pii-extractor" / "scripts" / "extract_party_pii.py"),
                          "run", str(case), "--out", str(tmp_path / "out")],
                         capture_output=True, text=True, cwd=tmp_path)
    assert run.returncode == 5, run.stdout + run.stderr
    assert "NEXT: read the sweep batches" in run.stdout


# ------------------------------------------------- the Fable review's cases --

CAPTION_TXT = """IN THE THIRD JUDICIAL DISTRICT COURT, SALT LAKE COUNTY, STATE OF UTAH
ROWENA KATE ASHDOWN,
        Petitioner,
v.
MARCUS DUANE ASHDOWN,
        Respondent.
Case No. 254700318
"""


def _case(tmp_path, **files) -> Path:
    case = tmp_path / "in"
    case.mkdir()
    (case / "caption.txt").write_text(CAPTION_TXT)
    for name, text in files.items():
        (case / name.replace("__", ".")).write_text(text)
    return case


def _items(case: Path, tmp_path, *extra) -> dict:
    out = tmp_path / "out"
    extract.main(["run", str(case), "--out", str(out), "--no-sweep", *extra])
    review = out / ".work" / "in" / "review.json"
    items = json.loads(review.read_text())["items"] if review.exists() else []
    return {i["value"]: i for i in items}


def test_a_party_signing_under_her_restored_name_is_not_counsel(tmp_path):
    case = _case(tmp_path, decree__txt="""DECREE OF DIVORCE
1. Petitioner is restored to her former name of Rowena Radcliffe.

/s/ Rowena Radcliffe
Rowena Radcliffe
Telephone: (801) 555-0190
rowena.radcliffe@gmail.com
""")
    items = _items(case, tmp_path)
    parties = json.loads((tmp_path / "out" / ".work" / "in" / "parties.json").read_text())
    assert parties["non_parties"] == []
    assert "(801) 555-0190" in items and "rowena.radcliffe@gmail.com" in items
    assert items["Rowena Radcliffe"]["proposed"] == "Petitioner"


def test_a_guardian_named_like_a_town_erases_nothing(tmp_path):
    case = _case(tmp_path, statement__txt="""ACCOUNT STATEMENT  Statement Period 01/01/2025 - 01/31/2025
ROWENA K ASHDOWN
9120 S Redwood Rd, South Jordan, UT 84095
Account Number: 000488227145
""")
    parties = tmp_path / "out" / ".work" / "in" / "parties.json"
    _items(case, tmp_path)
    data = json.loads(parties.read_text())
    data["non_parties"] = ["Jordan Lee"]
    data["status"] = "confirmed"
    parties.write_text(json.dumps(data))
    items = _items(case, tmp_path)
    assert "9120 S Redwood Rd, South Jordan, UT 84095" in items
    assert "000488227145" in items


def test_iban_without_python_stdnum(monkeypatch):
    monkeypatch.setitem(sys.modules, "stdnum", None)       # as in a bare sandbox
    assert extract.patterns.valid_iban("GB82 WEST 1234 5698 7654 32")
    assert not extract.patterns.valid_iban("GB82 WEST 1234 5698 7654 33")
    assert extract.patterns.scan("IBAN: GB82 WEST 1234 5698 7654 32")


def test_a_student_checking_account_is_offered(tmp_path):
    case = _case(tmp_path, bank__txt="""Respondent Marcus Duane Ashdown
Student Checking Account 0004991827
""")
    assert "0004991827" in _items(case, tmp_path)


def test_a_letters_re_line_is_not_a_statement_header(tmp_path):
    case = _case(tmp_path, letter__txt="""Venkataraman Family Law
2150 S 1300 E Ste 500, Salt Lake City, UT 84106
Telephone: (801) 555-0440
Re: Ashdown v. Ashdown - Respondent's financial disclosures
Dear Counsel,
Please find enclosed the disclosures.
""")
    items = _items(case, tmp_path)
    for value in ("2150 S 1300 E Ste 500, Salt Lake City, UT 84106", "(801) 555-0440"):
        assert value not in items or items[value]["proposed"] is None


def test_a_possessive_relationship_names_someone_else(tmp_path):
    case = _case(tmp_path, notes__txt="""Petitioner's counsel can be reached at (801) 555-0441.
Robert Ashdown (Respondent's father) lives at 555 Elm St, Ogden, UT 84401.
Respondent's cell phone is (801) 555-0177.
""")
    items = _items(case, tmp_path)
    assert items["(801) 555-0441"]["proposed"] != "Petitioner"
    assert items["555 Elm St, Ogden, UT 84401"]["proposed"] != "Respondent"
    assert items["(801) 555-0177"]["proposed"] == "Respondent"


def test_household_children_are_proposed_once_the_parents_are_confirmed(tmp_path):
    case = tmp_path / "in"
    case.mkdir()
    (case / "eob.txt").write_text("EXPLANATION OF BENEFITS\nMember: Kyle D Harrington\n"
                                  "Patient: Luke Harrington\nAmount due 40.00\n")
    out = tmp_path / "out"
    assert extract.main(["run", str(case), "--out", str(out), "--no-sweep"]) == 3
    parties = out / ".work" / "in" / "parties.json"
    data = json.loads(parties.read_text())
    data.update(status="confirmed", petitioner={"names": ["Jenna R Harrington"]},
                respondent={"names": ["Kyle D Harrington"]})
    parties.write_text(json.dumps(data))
    extract.main(["run", str(case), "--out", str(out), "--no-sweep"])
    data = json.loads(parties.read_text())
    assert [c["names"] for c in data["children"]] == [["Luke Harrington"]]


def test_spreadsheet_cells_carry_their_column_headers(tmp_path):
    book = tmp_path / "parties.xlsx"
    sheet = ('<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
             '<sheetData><row r="1"><c r="A1" t="inlineStr"><is><t>SSN</t></is></c>'
             '<c r="B1" t="inlineStr"><is><t>Date of Birth</t></is></c></row>'
             '<row r="2"><c r="A2"><v>531772490</v></c><c r="B2" s="1"><v>31721</v></c></row>'
             '</sheetData></worksheet>')
    styles = ('<styleSheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
              '<cellXfs count="2"><xf numFmtId="0"/><xf numFmtId="14"/></cellXfs></styleSheet>')
    with zipfile.ZipFile(book, "w") as z:
        z.writestr("xl/worksheets/sheet1.xml", sheet)
        z.writestr("xl/styles.xml", styles)
    text = extract.read_xlsx(book)[0].text
    assert "SSN: 531772490" in text and "Date of Birth: 11/05/1986" in text


def test_malformed_edits_are_named_not_crashed(tmp_path):
    case = _case(tmp_path)
    out = tmp_path / "out"
    extract.main(["run", str(case), "--out", str(out), "--no-sweep"])
    work = out / ".work" / "in"
    (work / "decisions.json").write_text("[1, 2]")
    assert extract.main(["run", str(case), "--out", str(out), "--no-sweep"]) == 4
    data = json.loads((work / "parties.json").read_text())
    data["children"] = [{"name": "Theo Ashdown"}]
    (work / "parties.json").write_text(json.dumps(data))
    assert extract.main(["run", str(case), "--out", str(out), "--no-sweep"]) == 3


def test_leading_zeros_survive_for_every_id(tmp_path):
    for kind in ("dl", "case_number", "parcel", "passport"):
        assert extract.display(kind, "0012345") == "0012 345"
    assert extract.display("parcel", "0812300045") == "0812 3000 45"


def test_restored_names_in_decree_wording_are_offered(tmp_path):
    case = _case(tmp_path, decree__txt="""Petitioner is restored to her former name of Rowena Radcliffe.
Respondent Marcus Duane Ashdown, f/k/a Marcus Tolliver, appeared.
""")
    items = _items(case, tmp_path)
    assert items["Rowena Radcliffe"]["proposed"] == "Petitioner"
    assert items["Marcus Tolliver"]["proposed"] == "Respondent"


# ------------------------------------------- the second Fable review's cases --

def test_a_clients_email_beside_counsels_name_is_offered(tmp_path):
    case = _case(tmp_path, mail__txt="""MEMO
/s/ Priya Venkataraman
Attorney for Petitioner
Cc: Priya Venkataraman <priya@vfamilylaw.com>, rowena.ashdown@gmail.com
""")
    items = _items(case, tmp_path)
    assert "rowena.ashdown@gmail.com" in items          # reviewed, not dropped by rule


def test_a_weak_clause_does_not_make_a_name(tmp_path):
    case = _case(tmp_path, notes__txt="""Petitioner returned to Utah in 2019.
Respondent's direct deposit changed to Zions in March.
Mail to 2150 S 1300 E Ste 500, Salt Lake City, Utah 84106.
""")
    items = _items(case, tmp_path)
    assert not [v for v in items if v.endswith("Utah") and items[v]["kind"] == "name"]
    addr = items.get("2150 S 1300 E Ste 500, Salt Lake City, Utah 84106")
    assert addr is None or not extract.strong(addr)    # a block tie: always answered by hand


def test_counsel_on_a_role_word_line_is_never_a_strong_tie(tmp_path):
    case = _case(tmp_path, notes__txt="Respondent is represented by Jane Doe, (801) 555-0199.\n")
    items = _items(case, tmp_path)
    assert not extract.strong(items["(801) 555-0199"])


def test_the_sweep_keeps_a_surname_that_is_also_a_town(tmp_path):
    folder = tmp_path / "sweep"
    folder.mkdir()
    pages = [extract.Page("notes.txt", "", "Her maiden name was Rowena Sandy.\nShe went by Blackburn.\n")]
    labels = [("Petitioner", {"names": ["Rowena Kate Ashdown"]})]
    (folder / "batch_001.found.txt").write_text(
        "P0001 | L2 | Blackburn | Names | Petitioner | maiden surname\n"
        "P1 | L1 | ⟦Rowena⟧ Sandy. | Names | Petitioner | maiden name\n")
    found = extract.read_sweep(folder, {"batches": ["batch_001"]}, pages, labels, [], [])
    assert {f["value"] for f in found} == {"Blackburn", "Rowena Sandy"}
