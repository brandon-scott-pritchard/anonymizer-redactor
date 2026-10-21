---
name: party-pii-leak-checker
description: Check anonymized or redacted case documents for any party identifier that survived - using the party CSV made by the party-pii-extractor skill - and produce a leak report listing every document, page and line where a party's name (any form, nickname, maiden name, misspelling), SSN, date of birth, address, phone, email, account, card, handle, licence, vehicle, employer, case or parcel number still appears. Also checks text hidden under black redaction boxes, words in images and scans (OCR), document properties and metadata, comments, tracked changes, annotations, form fields, link targets and file names, and has a model read the pages for anything else that still points to the parties. Use this whenever someone asks to verify, QC, audit, double-check or proof an anonymized or redacted document set, find remaining or missed PII, confirm a redaction is complete, or compare anonymized output against a party information spreadsheet - even if they do not say "leak" or name the skill.
---

# Party PII leak checker

The second half of the round trip. The party-PII extractor produced a CSV of
the parties' identifiers. The operator ran the Anonymizer/Redactor with it and
sent back an anonymized (or redacted) copy of the case. This skill searches
that copy for **every value on the CSV that survived, in any form, anywhere in
the files**, and writes a report of where each one is, so the operator can fix
it by hand.

**Accuracy comes first.** A surviving identifier that goes unreported is the
error this skill exists to prevent. Never skip a stage, skim a batch, or guess
to save time or tokens.

## Before you start

- **You need two things:**
  1. the anonymized output for ONE case, as a `.zip` or an unzipped folder;
  2. that case's party CSV from the party-PII extractor, with the columns
     Role, Field and Value.

  Run once per case. Never check one case's output against another case's
  CSV.
- **The report is as sensitive as the CSV.** It shows each surviving value so
  the operator can find it. Never paste values into the chat. Report counts
  only, and send the report through the same secure channel as the CSV.
- **Read-only.** The files are never changed. Output goes to a folder beside
  the input (`<input>_leak_check/`), or wherever `--out` says.
- **In a chat sandbox**, ask for the anonymized zip and the CSV as uploads, and
  pass their paths.

Requirements: Python 3.9+. It uses PyMuPDF if present for PDFs (it is needed
for black-box, annotation, form-field and picture checks; install it with
`pip install pymupdf` if missing), and Tesseract for OCR if present.

## The loop

Run the one command, do what its `NEXT:` line asks, and run it again with the
same arguments until it says `NEXT: nothing`:

```bash
python scripts/check_leaks.py run "<ANONYMIZED.zip or folder>" --csv "<PARTIES.csv>"
```

| Exit | `NEXT:` asks for | Section |
| --- | --- | --- |
| 2 | pictures transcribed | Transcribing pictures |
| 5 | the read-through batches read | The read-through |
| 0 | nothing - the report is written | Finishing |
| 1 | an error, named exactly (a bad CSV, a missing file) | fix it and rerun |

## What the script searches by itself

For every value on the CSV, every way it can still be printed:
- **Names:** every written form (`J. Smith`, `Smith, John`, `Mr. Smith`, the
  given name or surname alone), a surname glued into a word
  (`SmithDivorce.pdf`, `jsmith`), and near spellings (`Srnith`, `Smlth`).
- **Numbers:** SSN, phone, date and account formats; the same digits with
  other separators; masked tails (`****7145`).
- **Addresses:** abbreviations and the house number with its street.
- **Emails, handles, employers, and every other identifier.**

It searches all of these places:
- the visible text of every page, and scans (OCR);
- text still under a black box (a box drawn over text instead of a real
  redaction);
- words in pictures inside pages and documents (OCR);
- document properties (author, title, subject);
- annotations and comments, tracked deletions, form fields, bookmarks, alt
  text, link targets, and **the file and folder names**.

Each hit is graded:
- **certain:** an exact form of the value.
- **likely:** a single word of a name (in any case, even beside `_`, `.`,
  `-` or `@`), a masked tail, a glued surname, or the house number with the
  first street word.
- **possible:** a near spelling, or a name word that is also an everyday word
  (matched only when capitalised).
- **not searched:** a file, picture or hidden place that could not be read. It
  is listed so the operator checks it by hand.

## Transcribing pictures (exit 2)

Pictures OCR could not read are listed in `.work/<case>/queue.json`. For each
item, write a verbatim transcript to `.work/<case>/transcripts/<id>.txt`
following `references/transcribe.md`. Use a cheaper helper model for this if
your platform can delegate (for example Claude Code's Agent tool with
`model: "haiku"`). Write `[unreadable]` for a picture that truly cannot be
read; it is then reported as not searched.

## The read-through (exit 5)

The script only finds what is on the CSV. A model reads the anonymized pages
for anything else that still points to the parties: a surviving former name
not on the CSV, a description of their home, a child's school, an employer
named in passing. The pages are in `.work/<case>/readthrough/batch_NNN.txt`.
Each batch lists the real parties' names. Values already reported are marked
`⟦ ⟧`.

For each batch, write `batch_NNN.found.txt` following
`references/readthrough.md` exactly.
- **Delegate to a cheaper model on high effort** where your platform allows
  (one helper per batch, run several at once). If you cannot delegate, read
  every batch yourself by the same rules.
- Every batch needs its `.found.txt`, even if it only says `NONE`. A file
  with no findings and no `NONE` counts as unread.
- A batch ends with `=== END OF batch_NNN ===`. Read in slices until that line
  if your viewer truncates long files.

The script keeps only findings actually printed on the page they name.
Anything else is listed in `readthrough/rejected.txt`. Each kept finding is
then searched for everywhere, so the report lists every place it appears, not
just the one the model named, with the confidence `model`. Skip this stage (`--no-readthrough`) only
if the operator explicitly accepts that.

## Finishing (exit 0)

Tell the operator, in a few lines and with no values:
- the report path (`<out>/leak_report.csv`), and the counts the script printed
  (surviving values, files, and how many are certain, likely, possible and
  model);
- the number of **not searched** rows (files, pictures or hidden places that
  could not be read). Name them as needing a manual check;
- that the report holds the values themselves and goes back securely.

Then remove the work folder, which holds every page's text:

```bash
python scripts/check_leaks.py clean "<the output folder the run printed>"
```

## The report

One row per surviving value per place, sorted by document, page and line.
The columns:
- **Document**, **Page** (blank for Word files, which have no fixed pages),
  and **Line**: the line of the extracted text.
- **Where:** body text, a scanned page, text under a black box, image (OCR),
  document properties, annotation or comment, tracked deletion, change or
  comment author, field code, form field, link target, hidden layer, outside
  the visible page area, attached file, file name, and so on.
- **Found** (the text as it survives) and **Matched value** (the CSV value it
  is a form of).
- **Role** and **Field** (from the CSV).
- **How** (which form matched), **Confidence**, and **Context** (the line,
  with the hit in `[[ ]]`).

A "likely" or "possible" row can be a coincidence, such as a third party who
shares a first name or a store number that ends the same. The operator judges
those. A "certain" row is the value itself and always needs fixing.
