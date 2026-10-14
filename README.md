# Document Redactions & Anonymization

A deterministic tool that strips confidential client information out of legal
documents. Select any number of DOCX and PDF files, confirm what it found, and
get back a zip archive of cleaned documents plus an encrypted mapping key.

Deterministic means the same documents plus the same approved name list
produce byte-identical output every time. Fake names come from an HMAC of the
original string against a fixed salt—no random number generator, no clock, no
network call. Run it twice, diff the output, get nothing.

---

## Install

```bash
./setup.sh
```

That creates a virtual environment at `~/.venvs/anonymizer-redactor`, installs
the dependencies, and downloads the offline language model.

> The venv lives outside the project folder on purpose. This folder's name
> contains a colon (`Anonymizer:Redactor`), and Python refuses to create a venv
> under a path containing one. The build scripts stage to a temporary
> colon-free directory for the same reason. Renaming the folder to
> `Anonymizer-Redactor` would remove the need for both workarounds.

## Launching it

| Launch | What you get |
| --- | --- |
| `./run_app.command` (Windows: `run_app.bat`) | The app in its own window. This is the one to use. |
| `./run_web.command` (Windows: `run_web.bat`) | The same interface in your browser instead, if you would rather have tabs. |
| `./run.command` (Windows: `run.bat`) | The old Tkinter window, kept for anyone that wants it. |

The window and the browser are the same React interface, rendered by the
operating system's own web view (WKWebView on macOS, WebView2 on Windows),
which is why it looks and behaves identically on every machine. Under it, a
server bound to 127.0.0.1 on a random port does the work, guarded by a
one-time token; documents live in a per-session temp directory and results
come back as downloads. Nothing leaves the computer, and there is no
internet connection involved at any point.

The frozen `.app` and `.exe` open the window version by default. Pass
`--browser` for the browser, or `--tk` for the old desktop interface.

### Several sets, run overnight

The row across the top of the window holds any number of **sets**—separate
cases, each with its own documents, names, review decisions, invented names
and mapping-key password (`redactor/sets.py`). Make a set, name it, drop its
documents in; make the next. The sets' scans are read in the background in
the order the sets were made, each with its own counter ("Reading 340 of
1,200 scanned pages"). Once a set is read, everything its name screen shows
—the captions, the children, the bench, the suggestions—is worked out ahead
in a process of its own ("working out the name lists"), so a set reads
"ready to review" with its name screen ready to open at once: drop every set
in at night and review them all in the morning. A set given more files is
worked out again. Review a set while the next one reads, then **Queue this
set to run** on the Run step.

Queued sets run one at a time, in the order queued, with no further input,
into the results folder chosen at the top of the window—a folder per set,
named after it, holding the archive, the encrypted mapping key and the
report. Each run happens in a process of its own, so the window stays free
for reviewing while one runs; it reads its pages from the copy of the
readings kept on disk in the app's private temporary folder, so no page is
read twice. Once a set's results are written, its copies of the documents
and what was read off them are deleted. While anything is reading, queued or
running, the Mac is kept from idle sleep (`caffeinate -i`; the screen still
sleeps). Everything is held by the running app: quitting it ends the queue.

### OCR

Scanned PDFs are pictures of text with no text layer. Two engines read them,
and where both are available both read every scanned page, and what either one
finds is redacted. They misread differently: on one statement Tesseract read
the mask in `***8697` as a quotation mark and did not see `***1711` at all,
while RapidOCR read both. A scan is also read when the documents are added,
not only during the run, so what is on it is proposed on the Names screen and
counted on the Review screen like everything else.

1. Tesseract—preferred. `vendor_tesseract_macos.py` copies the binary, its
   whole dylib chain and `eng.traineddata` into `vendor/`, rewrites the absolute
   Homebrew paths to relative ones, and re-signs everything, so the frozen app
   works on a Mac that has never installed Homebrew. A system install on PATH is
   used if no vendored copy is present.
2. RapidOCR—fallback, and an ordinary pip dependency with its ONNX models
   inside the package. It needs no binary, no vendoring and no architecture
   match, so OCR works out of the box everywhere, including on Windows, where
   vendoring Tesseract is entirely optional.

Only if BOTH are unavailable does the tool refuse a scanned page; it refuses
rather than writing a file that looks redacted without being redacted. With
only one available, that one is used alone.

Reading a scan is the slow part, so it is done as fast as the machine allows
without changing a word of what is read:

- both engines read each page at the same time, from one rendering of it;
- up to four pages are read at once in worker processes (`redactor/ocr_pool.py`),
  ahead of whichever step needs them, into the same per-page cache every step
  reads. `REDACTOR_OCR_WORKERS=N` sets the number; `0` reads in-line;
- scans start being read as soon as they are added, while the options and
  names are being filled in.

On 41 scanned pages that took the caption step from 126 s to 50 s on a 16-core
Mac. Each step's progress bar counts pages—"Page 10 of 24 · about 1 min
left"—since the time is in the pages, not the files. The app's `--selftest`
reads a page in the workers, because a frozen build that cannot start them
falls back to one page at a time without saying so.

To bundle Tesseract into the macOS app:

```bash
brew install tesseract
python3 vendor_tesseract_macos.py --arch both
./build_macos.sh
```

`--arch both` vendors Apple Silicon AND Intel. One will not execute on the
other, so each gets its own directory and its own archive, and `ocr.py` picks by
`platform.machine()` at run time. The Apple Silicon build comes from Homebrew;
the Intel one comes from conda-forge, because homebrew-core no longer publishes
Intel bottles for Tesseract. Only the archive matching the build's architecture
is bundled, so neither app carries the other's binaries.

The equivalent on Windows is `python vendor_tesseract_windows.py`, run on a
Windows machine with Tesseract installed. Skip it and RapidOCR covers you.

> Intel Macs need an Intel build of the app itself. PyInstaller freezes for
> whichever machine runs `build_macos.sh`, so the `.app` produced on Apple
> Silicon will not launch on an Intel Mac no matter which Tesseract is inside
> it. Run `build_macos.sh` on an Intel Mac to produce that one; the vendored
> Intel Tesseract is already there waiting for it.

---

## How it works

### 1. Documents & options

Add DOCX and PDF files, photos of documents, or whole folders: drop a folder on the window, or use
**Choose folder…** (**Add folder…** in the Tk version). Every PDF and Word file
inside it, at any depth, is processed as one batch, and the results come back
in the same tree:

```
Harrington Divorce/                     Baird_Divorce/
  2024 Bank Statements/                   2024 Bank Statements/
    Harrington_statement_Jan.pdf   →        Baird_statement_Jan.pdf
  Pleadings/Petition.docx                 Pleadings/Petition.docx
```

Folder names are scrubbed exactly like filenames (and left alone when that
option is off); a name with nothing identifying in it comes back exactly as it
was. Two folders that scrub to the same name stay separate. Anything in the
folder that is not a document or a photo—a spreadsheet, an old `.doc`, a CSV
export—is left out, never delivered, and listed by its path in the run
report.

**Photos and screenshots of documents** are taken too: JPEG, PNG, HEIC/HEIF
(the iPhone's own format), AVIF, TIFF (every page of a multi-page fax or
scan), WEBP, GIF, BMP and JPEG 2000. Each comes back as a PDF in the same
place (`receipt.jpg` → `receipt.pdf`) and goes through the same OCR path as a
scanned PDF, in either mode. On the way in the photo is turned upright by the
orientation the camera recorded, cropped to the page and squared up when the
page stands out from what it lies on (the desk, a hand or the next sheet never
reaches the output), evenly lit so a shadow is not taken for handwriting, and
a dark-mode screenshot is turned light. The PDF is rebuilt from the pixels
alone, so the photo's EXIF block—GPS position, the phone's make and serial,
when it was taken—is left behind. On a payment app's activity screen, the
people paid and paid by are proposed from the layout itself: a name over a
payment type or a note, beside a signed amount. Hidden files and Word's `~$` lock files are ignored, and links are not
followed. In the app window the folder is read straight from disk; the
originals are never changed.

Choose what happens to each kind - DOCX defaults to anonymize, PDF to redact:

| Mode | Result |
| --- | --- |
| Anonymize | People become short invented names—`John Michael Smith` → `Ezra Lars Holm`, consistently across the whole batch and across both kinds of file, with family members sharing a surname and no two families sharing one. Everything else becomes a tagged placeholder: `[SSN-1]`, `[ACCOUNT-2]`. The document still reads like a normal one, so a reader may not realize it was altered unless told. |
| Redact | DOCX: text is removed and replaced with `[REDACTED]` on a black bar. PDF: the glyphs are removed and a black box drawn over the space. Nothing is invented. |

Either way, a PDF's original glyphs are physically deleted from the content
stream with PyMuPDF redaction annotations. Copy, paste and text extraction find
nothing of them. Text printed past the edge of the page—where no reader shows
it, but a parser that ignores the page edge still finds it—is removed too, with
a warning, and sideways text is boxed along its own direction.

Anonymizing a PDF then writes the replacement where the original was. The
invented names are kept short - three to five letters a part - because a PDF
cannot reflow: the replacement has to fit the space the real name took, and is
squeezed sideways in the rare case it does not. It is set in the nearest of
the fourteen standard faces at the original size, weight and colour; the
issuer's own font is almost always a subset holding only the letters it used,
so it cannot spell anything new. Table rules and row shading are kept.

Because a delivered PDF is usually read by a language model, the page's text
is redrawn in the order the issuer drew it, with each replacement in the slot
of the words it replaced. Text added to a PDF otherwise lands at the end of the
page, so a parser that reads in file order - LangChain's PyPDFLoader, pypdf,
PyMuPDF's default - would find every name after the page footer instead of in
its transaction row. Every piece is drawn back at its original width, so the
layout holds. Where a page cannot be redrawn (rotated text, a character no
available face can draw) the replacement is written on top instead, and the
run's warnings say which pages.

The file's properties say it is an anonymized copy; the pages do not. Scanned
pages and embedded images are still blacked out in anonymize mode - invented
text painted onto a photograph of a page looks forged.

A PDF has no tables, only words with coordinates, so its columns are rebuilt
from the gaps between those words before anything is scanned. That matters
because bank statements arrive as PDFs and a statement is nothing but columns.
Each page is also read twice - in the order the file wrote it, and row by row
as it sits on the page - and whatever either reading finds is boxed. See
"Financial exhibits" below.

Each box is trimmed to the middle of its line. PyMuPDF deletes every character
whose box touches a redaction, and in a tightly set table a character's box is
taller than the gap between rows, so a full-height box took letters off the
lines above and below it.

### 2. Names

Opens pre-populated with the party names harvested from the document captions:
the caption block on page one and the running headers on later pages. It reads
`NAME, Petitioner,` / `NAME, Respondent,` pairs, bare `v.` separators,
`In re the Marriage of X and Y`, attorney signature blocks, and
`Smith v. Jones, Case No. …` running headers, tagging each with the role it was
found under.

You then add names one at a time, or type them one full name per line. Each name
is matched in every written form it might appear in:

```
John Michael Smith    John Smith      Smith, John       J. Smith
John M. Smith         Mr. Smith       Smith             John
Michael               Smith's         the Smiths        J. M. Smith
```

…and any combination of the parts entered. Mark a minor child by ending the line
with `| minor`; an employer or school with `| organization`.

Two things happen behind the scenes that matter:

- "John Smith" and "John Michael Smith" are one person. Compatible written
  forms (same surname, first names equal or an initial, middles absent or
  aligned) register as a single entity with a single pseudonym; the fullest
  form wins. Suffixes separate people—Robert Smith Sr. and Robert Smith Jr.
  are father and son, not one entity.
- Close typos are caught. A word one inserted letter (`Johhn`, `Smiith`) or
  one adjacent transposition (`Jonh`) away from a registered name resolves to
  that person. Tokens under 4 characters stay exact-only, so ordinary words
  are never typo-matched.
- A second pass catches names that swallow each other. Tick `Smith` alongside
  `Jane Elizabeth Smith` and the two fold into one person, because `Smith` is
  already one of the forms the first name generates—left apart they would get
  two different pseudonyms and the surname would come out inconsistent. When
  the short form is genuinely ambiguous (`Smith` with two Smiths on the list)
  nothing is guessed: both stay, and the Names screen says why.

**Children are proposed too**, and they are never in the caption. The screen
reads three layouts: a roster table (`Name | Month & Year of Birth`), a cue
line followed by `Name Born: date`, and a bare custody or tax clause—"primary
custodial parent of Theo and Rory Ashdown", "Petitioner shall claim Rory"—which
is how a decree can name every child and never print a single date of birth.
Whatever it finds is typed **minor** automatically, and where the document says
a child has aged out ("legally emancipated … when she turned 18") it says so
rather than filing them as a minor anyway.

Auto-typing them as minors is also what keeps a son from being folded into the
father he is named after. `Marcus Ashdown` and `Marcus Shai Ashdown` look
exactly like one person written two ways, and nothing in the strings can tell
you otherwise—only the roster can.

Two other things get proposed that a caption never mentions: a **former or
maiden name** being restored ("shall return to her former name of Rowena
Radcliffe"), and a **nickname** of somebody already on the list. `Chrissy` is
pre-ticked and labelled *Short for Christine Annell Vaughn* rather than left
for you to notice.

Press Scan documents for more names and the offline spaCy model proposes
people, organizations and places the rules cannot see. It only ever proposes;
nothing is applied without a tick.

### 3. Review

Every proposed change, listed with what it found, what it will become, and how
many times it appears. **Ticked means it will be changed** (redacted or
anonymized) everywhere it appears; unticked means it is left exactly as
written. The screen says so above the list, and the tick column is headed
"Change?".

One row stands for one thing however many ways it was written. A row with ▸
gathers every form found and counts them together:

- **People**: a partial name under the one full name it is part of—"BRETT H"
  and "PRITCHARD BRETT" under "Brett H. Pritchard"—each written with the full
  name's stand-in in its own shape ("Arlo E", "Nolt Arlo"). A partial that fits
  two people ("PRITCHARD" with two Pritchards on the list) is not guessed at: it
  keeps its own row, with a note naming who it could be.
- **Addresses**: "N" and "North", "St" and "Street", case, punctuation, a
  dropped or doubled letter in the street name, the street with or without its
  city line, and the ZIP+4 on its own. The row shows the form written most
  often. A different house number, unit or ZIP is a different address—a near
  miss on the house number is flagged, never merged—and a form with no town
  joins only when one town fits.
- **Companies** with and without "Inc."/"LLC" and their punctuation;
  **accounts and cards** however they are spaced, with their masked tails
  ("XXXX4417", "ending in 4417") when exactly one number ends that way;
  **phones** however they are punctuated; **emails** in any case.

Ticking a group ticks every form in it; open it to untick a single form, which
is then left as written even where the full form's rules would have reached it.
A form that is not the same thing can be split out, and gets its own
replacement. Forms first met during the run join their group rather than
getting a new placeholder, and the mapping key records which group each form
belongs to.

The row editor changes an item's type or replacement (on a group, for every
form), and takes an optional what-went-wrong report (logged locally, with a
yes/no offer to open a pre-filled email draft—nothing is ever sent on its own).
A rescan keeps your ticks, splits, type changes and edited replacements.
Nothing has been written yet.

### 4. Run

Produces three things:

| File | Contents |
| --- | --- |
| `anonymized-<timestamp>.zip` | The cleaned documents, and nothing else |
| `mapping-key-<timestamp>.json` | The original → replacement table, AES-encrypted (PBKDF2-SHA256, 480,000 iterations), written outside the archive |
| `redaction-report-<timestamp>.txt` | Counts by category, warnings, and what was not covered. It never contains a redacted value, but it does name the original files—keep it with the mapping key, not with anything you deliver |

Filenames are anonymized too, since `Smith_Divorce_Petition.docx` names the
client before anyone opens it.

---

## What it looks for

63 categories across eight groups:

- People—person names, minor children, organizations, employers (their own category), schools, locations, counterparties in a transaction line
- Contact—email, phone, fax, URLs, social handles, usernames, IP and MAC addresses, GPS coordinates
- Government ID—SSN, EIN/Tax ID, driver's license, passport, USCIS A-number, military, inmate, student, voter, bar, notary, professional license, tribal enrollment
- Health—medical record numbers, health plan/member/group numbers, ICD/CPT/DSM codes, prescription numbers
- Financial—bank accounts, routing numbers, IBAN, SWIFT, cards (Luhn-checked, including masked forms like `XXXX-XXXX-XXXX-1234` and "card ending in 9876"), Venmo/PayPal/Zelle/Cash App handles, crypto wallets, retirement and brokerage accounts, loans, policies, claims, checks and wires, online order numbers (Amazon's `112-4749744-4480307` and `D01-` forms, or any labelled one) and shipment tracking numbers (UPS `1Z…`, Amazon `TBA…`, USPS, or any labelled one)
- Property—street addresses, PO boxes, ZIP+4 codes, APN/parcel numbers, deed book-and-page, legal descriptions, VINs, plates, vehicle title numbers, hull and tail numbers, safe deposit boxes, storage units
- Case—case and docket numbers, case names, other designators
- Vital—dates of birth (ISO forms included), places of birth

Structured values are found by pattern; anything whose shape is generic must
carry a label ("Account No. 44821") before it is touched, which is what keeps
false positives out of ordinary legal prose.

### Financial exhibits

A Financial Declaration is built out of bank statements, pay stubs, tax forms
and account statements. Those break differently from pleadings. Against a
generated corpus of 25 such documents the tool originally leaked 28% of the
planted identifiers and destroyed 23 dollar figures. Both are now zero on DOCX,
and one identifier out of 207 on PDF.

Here is the thing that shapes every decision below: a redacted statement
usually gets read by something else. An LLM sorting line items into a
declaration has to research the transactions it cannot derive from the
statement itself. That constrains the job in a direction most redaction tools
do not consider—OVER-REDACTING IS A FAILURE, not a safe default.

So the policy is narrow on purpose:

- The employer is redacted. Where the money comes from identifies the client,
  and every pay stub, W-2 and declaration names it outright. It gets its own
  category rather than hiding among the organizations, so you can see it on the
  review screen and tick it without hunting.
- Counterparty names on the payment rails are redacted: the person on the other
  end of a Zelle, a Venmo, a PayPal, a check. That is the childcare provider,
  the landlord, the paramour—the exact facts a declaration gets fought over.
- The client's own business is redacted: the name a business account is
  addressed to, the heading on its profit-and-loss, the entity a financial
  statement says they own. For a self-employed client it is the employer.
- Every other merchant is LEFT ALONE. A grocery store, a utility, an insurer
  and a mortgage servicer appear on millions of statements and say nothing
  about who this is. Renaming one is worse than blanking it, because it sends
  the reader after a business that does not exist.

Nothing proposes a merchant, so nothing can accidentally tick one. Amounts,
dates, running balances and transaction types all survive untouched.

Three things did most of the work.

MONEY IS NEVER AN IDENTIFIER. `V_ALNUM` allows a decimal point, so a figure
written without a thousands separator is a valid identifier shape. And in a
financial document the label in front of it is always a financial one:
`Savings account 842.16` was redacted as an account number. Worse, a registered
value matches everywhere, so one bad registration of `412.55` from a mortgage
statement destroyed the pay stub's federal withholding and three insurance rows
in a P&L—documents where no detector fired at all. A currency shape is now
refused for every category except the two where decimals are the native
notation (ICD-9 codes and GPS). Stating it as an exception list is deliberate.
Guessing which labels sit next to money is a game that keeps being lost:
`Summit Ridge Visa   8,412.19   212.00` matched the alien-number detector,
whose vocabulary carries "visa" for the immigration document.

A TABLE CELL IS ITS OWN PARAGRAPH. Word puts every cell in its own `w:p`,
so the scanner was handed `Savings account` and `000488227145` as two separate
strings. On the declaration's own asset schedule that produced the exact
inversion you would least want:

```
['Certificate of deposit', '44-8821-0033', '[ACCOUNT-8]', 'Joint']
```

The account number shipped and the dollar value was redacted. Tables are now
read for their label/value pairs four ways—the column heading, the cell above,
the cell to the left, and the first cell of the row. A statement, a check
register and an IRS form each put the label somewhere different. A 1099
alternates label rows and value rows, so the heading for its account number is
not in row 0 at all. Those four readings REGISTER ONLY; the strings are
reconstructions that exist nowhere on the page, so nothing is applied to them.

STATEMENTS ARE PDFS, SO PDFS GET THE SAME TREATMENT. A PDF has no table
elements at all. It has words with coordinates. But a statement, a pay stub and
a tax form are all columns, and the column a value sits under is its label just
as surely as a `<w:tc>` header would be. Columns are reconstructed from the
gaps between words and handed to the same code that reads a DOCX table. That
took the PDF path from 16 leaks and 7 destroyed figures down to 1 and 0.

Two PDF-specific bugs came out of that work. Page text was built by joining
every word on a line with one space, which destroys the column structure of a
document that is nothing but columns; it put an account number one space from
the money beside it, and the bare-digits card detector ran from one into the
other. And `street_address` matched across a line break, so the box drawn over
it physically deleted a dollar figure from the row above. Nothing in the report
would have mentioned it, because no detector ever matched it.

Also fixed along the way: space-grouped account numbers (`8102 4477 9301`) had
only their first group taken, which ships two thirds of the number while
reading as redacted; the bare `457` and `529` plan labels carried no word
boundary and sliced digits out of amounts (`4,571.30` → `457[INVACCOUNT-1]`);
masked tails (`****3907`, `...6613`, `XXXX-XXXX-XXXX-4417`) had no detector at
all; the ABA checksum function had been written and never referenced, so bare
routing numbers and check MICR lines shipped; and `Bend`, `Row` and `Crossing`
were missing from the street-suffix list, so a home address on any of them
survived.

### Tested against 8,236 documents

The tool is now measured against a labelled corpus of 8,236 generated financial
PDFs - ten households' worth of bank, card, loan, brokerage, retirement and
payment-app statements, pay stubs in six payroll formats, tax returns, W-2s and
1099s, credit reports, utility bills, insurance, vehicle and property records,
and a year of Amazon orders per client in four layouts - including 268
image-only scans. Every document is also rendered as a Word file
and run through the DOCX path. `eval/README.md` explains how it is scored.

| | Before | After |
| --- | --- | --- |
| PDF: files with a surviving identifier (names typed in) | 2,172 | 25 |
| PDF: identifiers surviving (names typed in) | 4,248 | 25 |
| PDF: files leaking when only the tool's own proposals are ticked | 5,714 | 478 |
| PDF: dollar figures destroyed | 1,119 | 0 |
| PDF: merchant names removed | 7,355 | 426 |
| PDF anonymize: files with a surviving identifier (names typed in) | - | 32 |
| DOCX (anonymize): files with a surviving identifier | 6,403 | 26 |
| DOCX: dollar figures destroyed | 751 | 0 |

Of the 25 PDF survivors, 17 are the scorer's own noise: "State of Utah" on a
title certificate and a school district on a property-tax line, both of which
share a name with somebody's employer, and two chance digit overlaps. Five are
a property-management company paid by Zelle, which the policy above leaves
alone as a business. Three are on scans: an employer whose payroll line OCR
gave a minus sign (fixed after the run), a payment-app counterparty the OCR'd
table never proposes, and a masked tail OCR misread. The merchant losses that
remain are mostly payment-app names the corpus invented (its "Pingo" is
Venmo), which the tool does not know are merchants and should not be taught
from a test set.

What the run found, in the order it mattered:

- A PDF writes each table cell as its own line far more often than not, so
  columns, labels and payee headings were invisible. Every page is now read
  twice, in file order and row by row, and cells are aligned to their column.
- The account holder, the counterparty and the employer are printed in more
  shapes than the rules knew: stacked joint holders, ACH `INDN:` fields,
  `ZELLE CREDIT JESSICA COOK`, `Pay Friends to`, `TYPE: P2P CO:`, a Venmo
  ledger's From and To columns, seven ways of printing a payroll deposit, a pay
  stub's letterhead, a W-2's employer box, `Employee:`, `Shareholder`, a 1040's
  name line, a plan named for its sponsor.
- Scans were read only during the run, so nothing on them was ever proposed.
  They are now read up front, by both OCR engines.
- Debits printed `636.09-`, cheque-protected `**832.81`, a masked account beside
  a figure, grouped digits across a line break, a date or a store number read
  as a house number, and redaction boxes taller than the line pitch - each
  destroyed figures or neighbouring text without any detector reporting it.
- A Word letterhead laid out as a two-column table reads across its columns,
  so tables are now also read down them, and the mailing block is recognised
  as text as well as by geometry.

One limit, stated plainly: in redact mode every hit in a PDF becomes the same
black bar. You keep the line items and the numbers, but within a statement you
cannot tell one redacted payee from another. For sorting transactions that is
usually fine. For tracking a recurring payment to one person across six months
it is not - which is what anonymize mode is for.

### Town and city names

A client's home town identifies them nearly as well as their name does, so the
tool ships a gazetteer of all 101,160 populated places in the United States—the
Census Bureau's national place file plus the USGS unincorporated communities,
both public domain, 399 KB.

The list never triggers anything. It only confirms. A context pattern proposes
first—`South Jordan, UT 84095`, `resides in Provo`, `MCKINNEY TX 75070`—and the
gazetteer is asked whether that is a real place; the do-not-change list and your
name list get the last word after that.

That ordering is not fussiness, and here is why both halves are needed. Across
eleven real pleadings, the context patterns alone produced 14 proposals and **6
of them were wrong**—"Uniform Parentage Act", "Interstate Family Support Act",
"Enforcement Act", "Salt Lake County", "West Provo", and a petitioner's actual
name. Run it the other way, just looking every capitalized word up in the
gazetteer, and it flags `Rule` in every statute citation (a town in Texas),
plus `Telephone`, `Holiday`, `Golden`, and three of the tool's own invented
replacement names. Together they were right 8 times out of 8, and the 8 include
one unincorporated Utah town of 40 people that had shipped in three files.

The reason a bare list cannot work: **5,478 US place names are also US
surnames, carried by about 101.8 million people.** Hull, Fowler and Falcon are
all real towns and all three were a real client's surname. If your client
shares a name with a town, the client wins—their name is on the do-not-change
list, and the town is not proposed at that spelling.

Rebuild the list from the two sources with `python build/build_places.py`.

### What it will not touch

Protected automatically, because a pleading stops making sense without them:

- Statutes and codes in every written form—`§ 30-3-5`, `Section 30-3-5`, `Sec. 1983`, `Sec 78B-12-202`
- Rules of procedure and evidence (`Rule 26`, `Fed. R. Civ. P. 12(b)`, `Utah R. Civ. P. 26`)
- Reported citations, including the party names in cited authority (`Jones v. Jones, 2019 UT App 12`)
- Court names, judicial districts, clerks of court

**Judicial officers get their own pass.** Before anything is scanned, the tool
reads every document looking for the bench—captions, running headers and the
signing block at the bottom—and puts what it finds on a do-not-change list that
holds for the whole batch. It reads all the layouts a court actually prints:

```
Judge Amber M. Cordova              Judge: Amber M. Cordova
Assigned Judge: Amber M. Cordova    Before the Honorable Amber M. Cordova
AMBER M. CORDOVA, District Court Judge

BY THE COURT:
_______________________
Amber M. Cordova
District Court Judge
```

Not just judges. Commissioners, justices, magistrate judges, referees and
hearing officers are all recognized, in the same set of layouts plus the ones
particular to a commissioner—which matters, because in domestic practice most
of what actually gets signed is a commissioner's recommendation, and those
blocks routinely carry the name with no title line under it:

```
COMMISSIONER'S RECOMMENDATION:          RECOMMENDED BY THE COURT COMMISSIONER:
_______________________                 _______________________
Delia Farnsworth                        /s/ Delia Farnsworth
```

That list covers the name everywhere it appears afterward, not just where the
title sits beside it—so a later "the Cordova ruling" survives too. A judge is
not a party, and an order that comes back with the bench renamed reads as
tampered with.

One exception, and it goes the other way: if a judge shares a surname with
somebody on your name list, the client wins. Only the judge's full name is
shielded, the bare surname follows the client's pseudonym, and the Names screen
tells you that is what happened. Shielding a shared surname would ship the
client's name in every sentence it appears in, which is the worse failure.

Whoever it found is listed on the Names screen and named in the run report.
Add your own never-touch terms in the box on the first screen.

### Beyond the body text

Confidential data escapes through channels a text scan never sees. All of these
are handled:

- DOCX—headers, footers, footnotes, endnotes, text boxes, shapes, table
  cells, comments, tracked changes and their authors, SmartArt diagrams, chart
  label caches, the glossary, `docProps` metadata and thumbnails, custom
  properties, hyperlink targets, field-code URLs (both written forms), revision IDs
- PDF—annotations (including drawn-ink signatures), form widgets, links,
  bookmarks, embedded attachments, JavaScript, document metadata, XMP metadata
- Images—on by default, every embedded image is blacked out whole, same
  size so the layout holds: a photographed bank statement, an exhibit scan
  sitting under a typed header, a signature image. DOCX ink parts (drawn
  handwriting) are removed. Turn it off and the images pass through with a
  warning instead.

---

## Limits—read these

- Handwriting flattened into a scan is not detected. OCR reads machine
  print; a handwritten margin note on a scanned page can survive, and the
  file's warnings say so when scanned pages were involved. Review scans by hand.
- The macOS app is architecture-specific. It runs on the kind of Mac it was
  built on. Tesseract is vendored for both, but the app itself is not universal;
  building for Intel means running the build on an Intel Mac.
- Unreadable input is refused, not passed through. An image-only PDF with
  no working OCR, a DOCX part that will not parse, an OCR engine dying mid-run:
  each refuses the file rather than delivering something that looks redacted
  without being redacted. That is deliberate.
- OCR word boxes are approximate, especially RapidOCR's, which detects whole
  lines and apportions word positions across them by character offset. Boxes are
  padded generously to compensate—a box slightly too wide costs nothing, one
  slightly too narrow leaves text on the page.
- A shared surname cannot be half-removed. If both parties are named Smith
  and you exclude one of them, "Smith" still goes; the same six letters cannot
  be simultaneously kept and replaced.
- The model only proposes. Anything it misses and you do not add stays in
  the document. The review screen is the control, not a formality.
- The mapping key is the whole secret. Keep it away from anything you
  deliver. Without the password it cannot be recovered.

---

## Building a standalone app

macOS:

```bash
./build_macos.sh
```

Produces `dist/Document Redactions & Anonymization.app` and installs it in
`/Applications`, replacing the copy there, so Applications always holds the
latest build. `INSTALL=0` skips the install; `INSTALL_DIR=…` puts it elsewhere.
If the installed copy is open, the build leaves it alone and says so: quit it,
then run `./install_macos.sh` to finish. Quit any copy running from `dist/`
before building—the build replaces it and refuses to start while it is open.
It is unsigned, so the first launch needs a right-click → Open, then Open again
in the dialog.

The icon is `build/icon.icns` (and `build/icon.ico` for Windows), drawn by
`build/make_icon.py` from the interface's own maroon and EB Garamond. The
outputs are committed; run the script again only to change the design.

The build needs Python 3.11 built for the Mac it runs on—PyInstaller freezes
for the interpreter's architecture, and an Intel 3.11 on an Apple Silicon Mac
would produce an Intel app. The script looks in the usual places and stops if
none fits; `PYTHON=/path/to/python3.11 ./build_macos.sh` chooses one.

To check a build, run
`"dist/Document Redactions & Anonymization.app/Contents/MacOS/Anonymizer-Redactor" --selftest`.
It prints the OCR engine and language model the bundle actually loaded, and
writes the same to `~/Library/Logs/Anonymizer-Redactor/selftest.txt`—never into
the app, where a new file would break its code signature.

Windows—the `.exe` must be built on a Windows machine; it cannot be
cross-built from macOS. Package the sources to send over:

```bash
./make_windows_kit.sh
```

That writes `Anonymizer-Redactor-Windows.zip` (sources only—no virtual
environment, no git history, no macOS app). Copy it to the Windows machine,
unzip, and follow `START-HERE-WINDOWS.txt` inside. It walks through installing
Python, then either `run.bat` / `run_web.bat` to just use the tool or
`build_windows.bat` to produce
`dist\Anonymizer-Redactor\Anonymizer-Redactor.exe`.

Both scripts stage to a temporary directory, build a clean environment, bundle
the language model, and copy the result back into `dist/`.

---

## Tests

```bash
~/.venvs/anonymizer-redactor/bin/python -m pytest tests/ -q
```

1,045 tests covering detector accuracy, allowlist protection, judicial-officer
harvesting in every layout a court prints, child rosters and custody clauses,
name-variant expansion and merging, overlapping-name folding, typo matching,
Word's own run and tab structure, surrogate determinism, image redaction,
mapping-key encryption, the web API, and end-to-end leak checks that assert no
original value survives in the delivered DOCX XML or PDF text. Every leak found
in review has a regression test that encodes the exact failing input;
`tests/test_corpus_regressions.py` holds the ones the corpus run found.

The corpus run itself is not part of the suite - the corpus lives outside the
repository and a full run takes about half an hour. See `eval/README.md`.

---

## Layout

```
redactor/
  categories.py       the 63 categories and how each is replaced
  patterns.py         regex detectors, validators, and the allowlist
  names.py            full name -> every written form, and back again
  caption.py          party names harvested from legal captions and headers
  officials.py        the bench, and the do-not-change list built from it
  children.py         child rosters, cue lines and bare custody clauses
  places.py           town and city names: context proposes, gazetteer confirms
  transactions.py     account holders, counterparties, employers and the
                      client's business, as financial documents name them
  data/               the 101,160-name US place list (Census + USGS, 399 KB)
  surrogates.py       deterministic fake names (HMAC, fixed salt)
  mapping.py          entity registry and the encrypted mapping key
  ner.py              optional spaCy suggestions (proposals only)
  engine.py           scan, resolve overlaps, apply; typo matching
  review.py           name-list and review decisions, shared by both UIs
  photo.py            photos and screenshots of documents, made into clean PDFs
  grouping.py         one review row per person, address or number, however written
  feedback.py         classification error reports and the mailto draft
  docx_processor.py   OOXML-level DOCX reading and rewriting
  pdf_processor.py    PyMuPDF redaction, image blackout, and the OCR path
  pipeline.py         orchestration, archive, report
  ocr.py              OCR back ends and locating the bundled Tesseract
  gui.py              the four-step Tkinter front end
  webapp.py           the local FastAPI server behind the web front end
webapp/static/        the React front end (vendored runtime, no build step)
build/                PyInstaller spec, frozen-app entry point, gazetteer builder
vendor/               vendored Tesseract (gitignored; rebuild with the scripts)
tests/                the suite described above
eval/                 the corpus evaluation: run, score, report (see eval/README.md)
samples/              example pleading in DOCX and PDF
```

---

## License

MIT. See [LICENSE](LICENSE).
