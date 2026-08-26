import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

CAPTION = """IN THE THIRD JUDICIAL DISTRICT COURT IN AND FOR SALT LAKE COUNTY, STATE OF UTAH

JANE ELIZABETH SMITH,
        Petitioner,
v.
JOHN MICHAEL SMITH,
        Respondent.

Case No. 224900871
Judge Amber M. Cordova
Commissioner Delia Farnsworth
"""

BODY_LINES = [
    "1. Petitioner Jane Elizabeth Smith, DOB: 04/17/1985, resides at 1482 South Elmwood Drive, Sandy, UT 84070.",
    "2. Her SSN is 528-41-9963, cell (801) 555-0184, email jane.smith1985@gmail.com.",
    "3. Respondent John Michael Smith works at Wasatch Orthopedic Group. Fax: 801-555-0199.",
    "4. Pursuant to Utah Code Ann. Section 30-3-5(1)(a) and Rule 26, and per Jones v. Jones, 2019 UT App 12, the Court finds.",
    "5. The Smiths hold checking account no. 000148829371, routing number 124000054.",
    "6. Respondent's 401(k) account number RT-99183772; Visa card 4111 1111 1111 1111.",
    "7. Lot 14, Block 3, Willow Creek Subdivision, recorded in Book 9921, at Page 447. Parcel No. 22-14-377-009.",
    "8. The 2019 Toyota Highlander, VIN 5TDJZRFH8KS998172, license plate number D42XKQ.",
    "9. Petitioner's employer EIN is 87-1993822; her Venmo handle is @jsmith-slc.",
    "10. Passport number 561998223; medical record number MRN-88213; policy number POL-773322.",
]

# How a court actually signs an order: the title lands on the line *after* the
# name. Kept in the fixtures because the caption's "Judge Amber M. Cordova" is
# the one layout that was always shielded, so it hid the gap on its own.
SIGNATURE = """
BY THE COURT:

_______________________________
Amber M. Cordova
District Court Judge
"""

SECRETS = [
    "528-41-9963", "jane.smith1985@gmail.com", "(801) 555-0184", "801-555-0199",
    "1482 South Elmwood", "000148829371", "124000054", "RT-99183772",
    "4111 1111 1111 1111", "22-14-377-009", "5TDJZRFH8KS998172", "D42XKQ",
    "87-1993822", "jsmith-slc", "561998223", "MRN-88213", "POL-773322",
    "224900871", "04/17/1985", "Smith",
]

PRESERVED = ["Judge Amber M. Cordova", "Commissioner Delia Farnsworth", "Rule 26",
             "Utah Code Ann. Section 30-3-5",
             # the signing block, where the title follows the name
             "Amber M. Cordova\nDistrict Court Judge"]


@pytest.fixture(scope="session")
def sample_text():
    return CAPTION + "\n" + "\n".join(BODY_LINES) + "\n" + SIGNATURE


@pytest.fixture(scope="session")
def sample_docx(tmp_path_factory):
    docx = pytest.importorskip("docx")
    path = tmp_path_factory.mktemp("fixtures") / "Smith_Divorce_Findings.docx"
    document = docx.Document()
    section = document.sections[0]
    section.header.paragraphs[0].text = "Smith v. Smith, Case No. 224900871"
    section.footer.paragraphs[0].text = "jane.smith1985@gmail.com"
    document.core_properties.author = "Marcus T. Whitfield"
    document.core_properties.last_modified_by = "Danielle Okonkwo"
    document.core_properties.title = "Smith Divorce"
    document.core_properties.comments = "SSN 528-41-9963 on file"

    for line in CAPTION.splitlines():
        document.add_paragraph(line)

    # a name deliberately split across runs, the way real editing leaves it
    para = document.add_paragraph()
    for chunk in ("1. Petitioner ", "Jane ", "Elizabeth", " Smith", ", DOB: 04/17/1985, "
                  "resides at 1482 South Elmwood Drive, Sandy, UT 84070."):
        para.add_run(chunk)
    for line in BODY_LINES[1:]:
        document.add_paragraph(line)
    for line in SIGNATURE.strip("\n").splitlines():
        document.add_paragraph(line)

    table = document.add_table(rows=1, cols=2)
    table.cell(0, 0).text = "VIN"
    table.cell(0, 1).text = "5TDJZRFH8KS998172"

    document.save(path)
    return path


@pytest.fixture(scope="session")
def sample_pdf(tmp_path_factory):
    import pymupdf
    path = tmp_path_factory.mktemp("fixtures") / "Smith_Divorce_Findings.pdf"
    doc = pymupdf.open()
    page = doc.new_page()
    page.insert_text((50, 60), CAPTION + "\n" + "\n".join(BODY_LINES[:6]),
                     fontsize=9, fontname="helv")
    page2 = doc.new_page()
    page2.insert_text((50, 40), "Smith v. Smith, Case No. 224900871 - Page 2",
                      fontsize=8, fontname="helv")
    page2.insert_text((50, 80), "\n".join(BODY_LINES[6:]) + "\n" + SIGNATURE,
                      fontsize=9, fontname="helv")
    doc.set_metadata({"author": "Marcus T. Whitfield", "title": "Smith Divorce",
                      "subject": "SSN 528-41-9963"})
    doc.save(path)
    doc.close()
    return path


@pytest.fixture(scope="session")
def scanned_pdf(tmp_path_factory):
    """A PDF whose only content is a picture of text - no text layer at all."""
    import pymupdf
    path = tmp_path_factory.mktemp("fixtures") / "scanned.pdf"
    source = pymupdf.open()
    page = source.new_page()
    page.insert_text((50, 80), "CONFIDENTIAL: Jane Smith SSN 528-41-9963",
                     fontsize=14, fontname="helv")
    pixmap = page.get_pixmap(dpi=150)
    source.close()

    doc = pymupdf.open()
    target = doc.new_page()
    target.insert_image(target.rect, pixmap=pixmap)
    doc.save(path)
    doc.close()
    return path
