---
name: party-pii-extractor
description: Pull every identifying detail of the parties in a legal case - petitioner, respondent, children and any other named party - out of a whole folder or zip of case files into one CSV per case, for typing into an anonymizer's party sheet. Covers every form of each name (maiden and former names, nicknames, misspellings), dates of birth, SSN/TIN, driver's licence, passport, addresses, phones, emails, employers, businesses, bank and card accounts, payment and social handles, vehicles, case and parcel numbers. Use this whenever an attorney or paralegal asks to extract, list, gather or pull a client's or the parties' identifying information, PII or personal details from case files, pleadings, financial declarations, bank statements, pay stubs, tax returns, emails or scans, or to prepare a party information sheet or spreadsheet for redaction or anonymization - even if they do not mention CSV or the anonymizer.
---

# Party PII extractor

Produces, for each case, a CSV of **only the parties' own identifying
information**, ready to be typed into the Anonymizer's party sheet. A bundled
script does the reading and the pattern work deterministically (same files,
same result). A model then reads every page for what the rules missed (the
sweep), and you decide every candidate.

**Accuracy comes first.** The point is that no identifier of a party is missed.
Never skip a stage, skim a batch, or guess to save time or tokens.

## Before you start

- **The CSV is highly sensitive.** It holds full SSNs and account numbers.
  Never paste values into the chat - report counts only, and remind the
  attorney to send the CSV through a secure channel (encrypted email or the
  firm's client portal), not plain email.
- **Read-only.** Nothing in the attorney's folder is changed. All work goes to
  an output folder beside it (`<input>_party_pii/`), or wherever `--out` says.
  Its `.work/` subfolder holds the full text of every page, page images and the
  review files. It is as sensitive as the case file, and is deleted at the end
  (see Finishing).
- **Where the files are.** With local file access, use the folder path the
  attorney gives. In a chat sandbox, ask them to upload the case folder as a
  single `.zip` and pass the zip's path.
- **One case or several?** Each top-level subfolder is treated as a separate
  case. If the folder is one client's file with sections (pleadings,
  financials, ...), add `--single-case`. The script warns when subfolders look
  like sections. Ask the attorney if you are unsure.
- **If the attorney names the parties, pass them** (`--petitioner "Jane E.
  Smith" --respondent "John M. Smith"`, repeat a flag for a maiden name, add
  `--child` per child). It saves a step and anchors everything better.

Requirements: Python 3.9+. It uses PyMuPDF, pdfplumber, pypdf or `pdftotext`
for PDFs, whichever is installed, and Tesseract for OCR if present. None of
them is required for Word, Excel, email or text files. If the script warns
that there is no PDF reader, install one (`pip install pymupdf` or `pip
install pypdf`) and rerun with `--rescan`. If nothing can be installed, tell
the attorney which files went unread. An unread file was not searched, so
never report the run as complete without saying so.

## The loop

Run the one command, do what its `NEXT:` line asks, and run it again with the
same arguments until it says `NEXT: nothing`:

```bash
python scripts/extract_party_pii.py run "<INPUT>" [--single-case] [--petitioner NAME] [--respondent NAME]
```

| Exit | `NEXT:` asks for | Section |
| --- | --- | --- |
| 2 | page images transcribed | Transcribing scans |
| 3 | the parties named | Naming the parties |
| 5 | the sweep batches read | The sweep |
| 4 | each candidate value decided | Reviewing |
| 0 | nothing - the CSVs are written | Finishing |

The stages always come in that order. If one run prints several `NEXT:`
lines (several cases at different stages), do them all, then run again.

The script keeps its state in `<out>/.work/<case>/`, so a rerun resumes from
where it stopped. Pass `--rescan` only if files were added or changed.

## Transcribing scans (exit 2)

Pages with no text layer are OCR'd when Tesseract is installed. Pages it cannot
read, or reads poorly, are rendered to images and listed in
`.work/<case>/queue.json` as `{id, file, page, image}` (or `pdf` + `page` when
nothing here can render a page - then open that page of the PDF yourself).

For each item, write a verbatim transcript to `.work/<case>/transcripts/<id>.txt`.
A page that truly cannot be read gets a transcript of just `[unreadable]`. The
script then reports it as an unread page instead of treating it as searched.

**Use a cheaper model for this if your platform can.** Transcription is
copying, not judgment. If you can start helper agents on a smaller model
(for example Claude Code's Agent tool with `model: "haiku"`), give each one a
batch of about 10 items and the prompt in `references/transcribe.md`, and
keep the analysis for yourself. If you cannot delegate, transcribe them
yourself by the same rules. Either way, transcribe; do not summarize. The
script analyzes the transcripts with the same rules as the text pages.

## Naming the parties (exit 3)

No caption named both a petitioner and a respondent (common in a folder of
financial records only). Open `.work/<case>/parties.json`. `candidates` lists
the account holders found. Fill in what you can, ask the attorney for what you
cannot, and set `"status": "confirmed"`:

```json
{
  "status": "confirmed",
  "petitioner": {"names": ["Jane Elizabeth Smith", "Jane Elizabeth Doe"]},
  "respondent": {"names": ["John Michael Smith"]},
  "children": [{"names": ["Theo James Smith"], "dob": "03/14/2015"}],
  "others": [{"names": ["Robert Smith"]}],
  "non_parties": ["Priya Venkataraman"]
}
```

The first name in each list is the full name. Add former or maiden names after
it. `others` is only for people named as parties in the case (an intervenor,
a grandparent seeking visitation), not for witnesses or relatives.
`non_parties` (counsel, a guardian ad litem, a judge) makes values in their
signature block drop out automatically. List only people you are sure are not
parties. A party who signs a document is still a party.

After the parents are known, the script also proposes children it finds under
a patient, dependent or student label (a bill, a 1040). They appear in the
PARTIES block marked `[proposed: ...]`. Remove any who are not the parties'
children. The script checks `parties.json` and names anything malformed.

## The sweep (exit 5)

The rules find identifiers by their shape and by the names around them. They
cannot read prose: a former married name mentioned in passing, "the place on
Willow Bend Court", a handle written without its "@", a child's school. So a
model reads every page once for what the rules missed. The script has written
the pages into `.work/<case>/sweep/batch_NNN.txt`, with everything already
found marked `⟦ ⟧` and with repeated boilerplate shown only once.

For each batch, write `.work/<case>/sweep/batch_NNN.found.txt` following
`references/sweep.md` exactly. That file holds the full prompt and the output
format.

- **Use a cheaper model on high effort if your platform can delegate.** For
  example, Claude Code's Agent tool with `model: "haiku"`: one helper per
  batch, run several at once, each given the prompt in
  `references/sweep.md`, its batch path and its output path. The findings come
  back to you in the review, so the stronger model still makes every decision.
- If you cannot delegate, read each batch yourself by the same rules. Do not
  skip batches and do not skim. Missing an identifier is the error this step
  exists to prevent.
- Every batch needs its `.found.txt`, even if it only says `NONE`. A file that
  is empty, or holds no finding lines and no `NONE`, counts as unread. The run
  says which batches to read again.
- A batch ends with `=== END OF batch_NNN ===`. Read in slices until that line
  if your viewer truncates long files.

The script then checks each finding: a value that is not printed on the page
it names is rejected (listed in `sweep/rejected.txt`), and one already found
is ignored. The rest join the review, marked `sweep:`. Skip this stage
(`--no-sweep`) only if the attorney explicitly accepts lower recall.

## Reviewing (exit 4)

Read `.work/<case>/review.txt`, not `review.json`; it is the compact copy. It
ends with `=== END OF REVIEW (... items) ===`. If your file viewer shows only
part of a long file, read it in slices until you reach that line, and never
answer from part of it.
It opens with the PARTIES the script settled on. **Check that block first.**
If a party is wrong or missing, fix `parties.json` (as above) and rerun before
deciding anything.

Every candidate value is one item. The item shows the owner the rules propose,
why they proposed it, and the line where it was found:

```
R5 | Petitioner | Phones | 801-555-0123 | block under a name (3 up) | x1
    Financial Declaration.pdf p.1: Name: Rowena K. Ashdown / ... / Home Phone: [[801-555-0123]]
```

The review also says, at its top, how many values rules kept out of it, and
why. These are values the app replaces or leaves as written by design, such as
loan numbers, masked tails on money lines, and counsel's signature block. Every
one is listed in `.work/<case>/not_proposed.json` if the attorney wants to
check.

Decide every item using the rules in `references/review.md`. **Accuracy
matters more than speed or tokens.** Missing a party's identifier is the
serious error. Putting someone else's on the sheet is the lesser one, and
`unsure` is always available. When the context line does not settle an item,
open the file at the page shown and read around it before deciding. The
goal is **just the parties' information**: drop a value only when you can see
it belongs to someone else (counsel, a bank, a merchant, a non-party person).
Then write `.work/<case>/decisions.json`:

```json
{"G2": "drop", "R3": "Respondent", "R7": "drop", "R12": "unsure", "R40": "Petitioner"}
```

Each answer is a party label exactly as listed (`Petitioner`, `Respondent`,
`Child 1`, `Other party 1`, ...), or `Both parties`, `Case`, `drop` or
`unsure`.

- **Groups.** Items of one shape are folded into a `G` block, for example 37
  masked account numbers each alone on a line in one bank's statements. The
  block shows two examples and every value with its `R` id. Answer the
  group by its `G` id, and answer any member that differs by its own `R` id.
  An `R` answer always wins over its group's.
- `"_rest": "accept"` keeps the proposed owner for items you did not answer,
  but **only** where the reason is `same line` or `case number` (the strongest
  ties). Every other item and every group, including all `?` items, needs its
  own answer. Use `_rest` only after reading everything.
- The script checks the file. An unknown id, a misspelled answer or broken
  JSON is reported with exactly what to fix, and nothing is written until it
  is right.
- If the items change on a rerun (new files, edited parties), the old
  decisions are discarded and you are asked again.

## Finishing (exit 0)

Tell the attorney, in a few lines and with no values:
- the CSV path for each case and the per-party counts the script printed;
- how many files or scanned pages could not be read, if any (the run printed
  them; files are listed in `.work/<case>/scan.json`), and that those were
  **not searched**, so they should be checked by hand;
- to send the CSV securely. In a sandbox, offer the CSV for download.

Then remove the work folder, which holds every page's text:

```bash
python scripts/extract_party_pii.py clean "<the output folder the run printed>"
```

Skip the clean-up only if the attorney says they will add files and rerun.

## The CSV

`Role, Field, Value, Detail, File, Page, Times found`, sorted in the party
sheet's order:
- **Role**: Petitioner, Respondent, Child n, Other party n, Both parties
  (joint), Case (case and parcel numbers), Unassigned (you answered `unsure`).
- **Field**: the party sheet's own labels.
- **Detail**: for names, whether the form is the full name, a former name, a
  nickname, a misspelling, or *derived by the app* (no need to type).
- Digit strings (accounts, cards, licence, passport, case and parcel numbers)
  are written grouped (`0004 8822 7145`) so a spreadsheet does not strip
  leading zeros or round them. The app accepts any spacing.
- An SSN marked "fails the SSA validity rules" is SSN-shaped but could not be
  issued (a 9xx area, a typo). It was kept because it may still identify the
  person.
