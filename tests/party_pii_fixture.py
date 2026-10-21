"""A fabricated divorce case folder for the party-PII skill's tests.

Every person, number and address here is invented. The folder holds what an
attorney's file holds: a petition with a caption, a child roster, a restored
maiden name and a nickname; a financial declaration; a bank statement; a
scanned page with no text layer; an email; counsel's signature block, whose
phone and address belong to nobody in the case.

``TRUTH`` is what the CSV must carry, as (role, field, value-as-key) - and
``NOT_PARTIES`` what it must not.
"""

from __future__ import annotations

from email.message import EmailMessage
from pathlib import Path

PETITION = """IN THE SECOND JUDICIAL DISTRICT COURT, DAVIS COUNTY, STATE OF UTAH

IN RE THE MARRIAGE OF
ROWENA KATE ASHDOWN,
        Petitioner,
and
MARCUS DUANE ASHDOWN,
        Respondent.

VERIFIED PETITION FOR DIVORCE
Case No. 254700318
Judge Halvorsen

1. Petitioner Rowena Kate Ashdown resides at 1734 W Gentile St, Layton, UT 84041. Her date of birth is 06/22/1988.
2. Respondent Marcus Duane Ashdown, also known as Marcus "Mac" Ashdown, resides at 412 North 300 East, Kaysville, UT 84037.
3. Respondent's cell phone is (801) 555-0177 and his email is mac.ashdown@protonmail.com.
4. The parties have the following minor children:
Theo James Ashdown, born 03/14/2015
Rory Ashdown, born 07/02/2018
5. Petitioner is employed by Red Rock Dental Arts. Respondent works at Ashdown Fencing LLC.
6. Rowena Ashdown shall be restored to her former name of Rowena Radcliffe.
7. ASHDOWN, MARCUS D. holds Visa card ending in 9876.
8. Mac drives a 2016 Ford F-150, VIN 1FTEW1EF5GFA12345.

DATED this 3rd day of March, 2025.

/s/ Priya Venkataraman
Priya Venkataraman (Bar No. 14422)
Attorney for Petitioner
Venkataraman Family Law
2150 S 1300 E Ste 500, Salt Lake City, UT 84106
Telephone: (801) 555-0440
priya@vfamilylaw.com
"""

DECLARATION = """FINANCIAL DECLARATION OF PETITIONER
Ashdown v. Ashdown, Case No. 254700318

Name: Rowena K. Ashdown
SSN: 529-38-1146
Date of Birth: 06/22/1988
Home Phone: 801-555-0123
Employer: Red Rock Dental Arts
Driver License No. 187264530 (UT)

Accounts
Checking  Zions Bank  Account No. 000488227145
Savings   Zions Bank  Account No. 000488229901
"""

STATEMENT = """ZIONS BANK                                    Statement Period 01/01/2025 - 01/31/2025
ROWENA K ASHDOWN
1734 W GENTILE ST
LAYTON UT 84041
Account Number: 000488227145

ACCOUNT ACTIVITY
01/03  ZELLE TO SHAWNA KIRKENDALL                 120.00
01/09  TRANSFER TO SAVINGS XXXXXX9901              500.00
01/15  SMITHS FOOD #4412  801-555-0911              86.12
01/20  RED ROCK DENTAL ARTS PAYROLL              2,104.55
"""

SCANNED = """DECLARATION OF RESPONDENT

Name: Marcus Duane Ashdown
Date of Birth: 11/05/1986
Social Security No. 531-77-2490
Driver License No. 266104981
"""

EMAIL_BODY = """Priya,

The respondent's new address is 88 Canyon Rim Dr, Kaysville, UT 84037.
His work phone at the fencing company is (801) 555-0188.

Thanks,
Dana Whitlock
Paralegal
"""

# What no rule can find - only a reader of the prose: the sweep's test.
NOTES = """Client intake notes (P. Venkataraman)

Rowena went by Rowena Tolliver during her first marriage, before she married Marcus.
Before the move to Layton the family rented the place on Willow Bend Court in Clearfield.
Marcus posts his fence jobs on Instagram as mac_builds_utah and gets most referrals there.
Theo is in third grade at Whitesides Elementary.
"""
# (role, field, value-as-key) only a sweep finds
SWEEP_TRUTH = [
    ("Petitioner", "Names", "Rowena Tolliver"),
    ("Petitioner", "Other identifiers", "Willow Bend Court"),   # no house number: the sheet takes it there
    ("Respondent", "Handles", "mac_builds_utah"),
    ("Child 1", "Other identifiers", "Whitesides Elementary"),
]

# (role, field, value) the CSV must carry; values compared loosely (digits / casefold)
TRUTH = [
    ("Petitioner", "Names", "Rowena Kate Ashdown"),
    ("Petitioner", "Names", "Rowena Radcliffe"),
    ("Petitioner", "Date of birth", "06/22/1988"),
    ("Petitioner", "SSN", "529-38-1146"),
    ("Petitioner", "Addresses", "1734 W Gentile St, Layton, UT 84041"),
    ("Petitioner", "Phones", "801-555-0123"),
    ("Petitioner", "Employers", "Red Rock Dental Arts"),
    ("Petitioner", "Driver's licence", "187264530"),
    ("Petitioner", "Accounts", "000488227145"),
    ("Petitioner", "Accounts", "000488229901"),
    ("Respondent", "Names", "Marcus Duane Ashdown"),
    ("Respondent", "Names", "Mac Ashdown"),
    ("Respondent", "Date of birth", "11/05/1986"),
    ("Respondent", "SSN", "531-77-2490"),
    ("Respondent", "Addresses", "412 North 300 East, Kaysville, UT 84037"),
    ("Respondent", "Phones", "(801) 555-0177"),
    ("Respondent", "Emails", "mac.ashdown@protonmail.com"),
    ("Respondent", "Employers", "Ashdown Fencing LLC"),
    ("Respondent", "Driver's licence", "266104981"),
    ("Respondent", "Vehicles", "1FTEW1EF5GFA12345"),
    ("Respondent", "Cards", "9876"),
    ("Respondent", "Addresses", "88 Canyon Rim Dr, Kaysville, UT 84037"),
    ("Respondent", "Phones", "(801) 555-0188"),
    ("Child 1", "Names", "Theo James Ashdown"),
    ("Child 1", "Date of birth", "03/14/2015"),
    ("Child 2", "Names", "Rory Ashdown"),
    ("Child 2", "Date of birth", "07/02/2018"),
    ("Case", "Case numbers", "254700318"),
]
# what belongs to nobody in the case: counsel, the bank, a merchant, a payee
NOT_PARTIES = ["(801) 555-0440", "priya@vfamilylaw.com", "2150 S 1300 E", "801-555-0911",
               "Shawna Kirkendall"]


def build(folder: Path) -> Path:
    """Write the case into ``folder`` and return it."""
    import docx
    import pymupdf

    folder.mkdir(parents=True, exist_ok=True)
    d = docx.Document()
    for line in PETITION.splitlines():
        d.add_paragraph(line)
    d.save(folder / "Ashdown Petition.docx")

    def pdf(path: Path, text: str, scanned: bool = False):
        doc = pymupdf.open()
        page = doc.new_page()
        page.insert_text((50, 60), text, fontsize=9, fontname="cour")
        if scanned:
            pix = page.get_pixmap(dpi=200)
            doc.close()
            doc = pymupdf.open()
            img_page = doc.new_page()
            img_page.insert_image(img_page.rect, stream=pix.tobytes("png"))
        doc.save(path)
        doc.close()

    pdf(folder / "Financial Declaration.pdf", DECLARATION)
    (folder / "statements").mkdir(exist_ok=True)
    pdf(folder / "statements" / "Zions 2025-01.pdf", STATEMENT)
    pdf(folder / "Respondent declaration (scan).pdf", SCANNED, scanned=True)

    msg = EmailMessage()
    msg["From"] = "Dana Whitlock <dana@vfamilylaw.com>"
    msg["To"] = "priya@vfamilylaw.com"
    msg["Subject"] = "Ashdown - respondent's new address"
    msg["Date"] = "Mon, 10 Mar 2025 09:14:00 -0600"
    msg.set_content(EMAIL_BODY)
    (folder / "Re Ashdown address.eml").write_bytes(bytes(msg))
    (folder / "intake notes.txt").write_text(NOTES)
    return folder
