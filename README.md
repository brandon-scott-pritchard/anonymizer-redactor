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

### OCR

Scanned PDFs are pictures of text with no text layer. Two engines handle them,
tried in order:

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
rather than writing a file that looks redacted without being redacted.

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

Add DOCX and PDF files. Choose what happens to DOCX:

| Mode | Result |
| --- | --- |
| Anonymize | People become realistic invented names—`John Michael Smith` → `Tamsin Quentin Middleton`, consistently across the whole batch, with family members sharing a surname. Everything else becomes a tagged placeholder: `[SSN-1]`, `[ACCOUNT-2]`. The document still reads like a normal pleading, so a reader may not realize it was altered unless told. |
| Redact | Text is removed and replaced with `[REDACTED]` on a black bar. Nothing is invented. |

PDFs are always redacted, never anonymized. The glyphs are physically deleted
from the content stream with PyMuPDF redaction annotations and an opaque box is
drawn over the space. Copy, paste and text extraction find nothing.

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

Press Scan documents for more names and the offline spaCy model proposes
people, organizations and places the rules cannot see. It only ever proposes;
nothing is applied without a tick.

### 3. Review

Every proposed change, listed with what it found, what it will become, and how
many times it appears. Untick anything that should stay. The row editor changes
an item's type or replacement, and takes an optional what-went-wrong report
(logged locally, with a yes/no offer to open a pre-filled email draft—nothing
is ever sent on its own). A rescan keeps your ticks, type changes and edited
replacements. Nothing has been written yet.

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

56 categories across eight groups:

- People—person names, minor children, organizations, employers, schools, locations
- Contact—email, phone, fax, URLs, social handles, usernames, IP and MAC addresses, GPS coordinates
- Government ID—SSN, EIN/Tax ID, driver's license, passport, USCIS A-number, military, inmate, student, voter, bar, notary, professional license, tribal enrollment
- Health—medical record numbers, health plan/member/group numbers, ICD/CPT/DSM codes, prescription numbers
- Financial—bank accounts, routing numbers, IBAN, SWIFT, cards (Luhn-checked, including masked forms like `XXXX-XXXX-XXXX-1234` and "card ending in 9876"), Venmo/PayPal/Zelle/Cash App handles, crypto wallets, retirement and brokerage accounts, loans, policies, claims, checks and wires
- Property—street addresses, PO boxes, APN/parcel numbers, deed book-and-page, legal descriptions, VINs, plates, hull and tail numbers, safe deposit boxes, storage units
- Case—case and docket numbers, case names, other designators
- Vital—dates of birth (ISO forms included), places of birth

Structured values are found by pattern; anything whose shape is generic must
carry a label ("Account No. 44821") before it is touched, which is what keeps
false positives out of ordinary legal prose.

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

Produces `dist/Document Redactions & Anonymization.app`. It is unsigned, so the first launch
needs a right-click → Open, then Open again in the dialog.

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

195 tests covering detector accuracy, allowlist protection, judicial-officer
harvesting in every layout a court prints, name-variant expansion and merging,
overlapping-name folding, typo matching, surrogate determinism, image
redaction, mapping-key encryption, the web API, and end-to-end leak checks that
assert no original value survives in the delivered DOCX XML or PDF text. Every leak found
in review has a regression test that encodes the exact failing input.

---

## Layout

```
redactor/
  categories.py       the 56 categories and how each is replaced
  patterns.py         regex detectors, validators, and the allowlist
  names.py            full name -> every written form, and back again
  caption.py          party names harvested from legal captions and headers
  officials.py        the bench, and the do-not-change list built from it
  surrogates.py       deterministic fake names (HMAC, fixed salt)
  mapping.py          entity registry and the encrypted mapping key
  ner.py              optional spaCy suggestions (proposals only)
  engine.py           scan, resolve overlaps, apply; typo matching
  review.py           name-list and review decisions, shared by both UIs
  feedback.py         classification error reports and the mailto draft
  docx_processor.py   OOXML-level DOCX reading and rewriting
  pdf_processor.py    PyMuPDF redaction, image blackout, and the OCR path
  pipeline.py         orchestration, archive, report
  ocr.py              OCR back ends and locating the bundled Tesseract
  gui.py              the four-step Tkinter front end
  webapp.py           the local FastAPI server behind the web front end
webapp/static/        the React front end (vendored runtime, no build step)
build/                PyInstaller spec and frozen-app entry point
vendor/               vendored Tesseract (gitignored; rebuild with the scripts)
tests/                the suite described above
samples/              example pleading in DOCX and PDF
```

---

## License

MIT. See [LICENSE](LICENSE).
