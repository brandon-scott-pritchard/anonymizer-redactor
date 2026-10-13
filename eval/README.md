# Corpus evaluation

Runs the redactor over a labelled corpus of documents and scores every output
for what leaked and what was wrongly destroyed. It is how the tool is trained:
run, read the report, fix the class of defect, run again.

```bash
~/.venvs/anonymizer-redactor/bin/python -m eval.corpus_eval CORPUS_ROOT --out RUN_DIR
~/.venvs/anonymizer-redactor/bin/python -m eval.report RUN_DIR --types
~/.venvs/anonymizer-redactor/bin/python -m eval.report RUN_DIR --compare OLDER_RUN_DIR
```

The corpus is not in the repository. It lives wherever it was downloaded, as
PDFs each with a same-named `.json` label, grouped into households
(`HH01_HARRINGTON/…`), a two-household discovery set, and a set of statement
layouts. About 8,200 PDFs; a full run of both operator models takes about
fifteen minutes on 14 workers.

## What a run does

Every directory of PDFs is one batch, and each batch goes through the same four
steps the app takes, in the same order, with the same functions:

1. captions and children (`pipeline.collect_caption_names`) and the bench
   (`pipeline.collect_officials`);
2. suggestions (`pipeline.collect_suggestions`);
3. the review prescan (`pipeline.prescan`);
4. the run (`pipeline.run_job`), which writes the archive that gets scored.

Two operator models, both run by default:

| Mode | Name list | What it measures |
| --- | --- | --- |
| `operator` | high-confidence captions, every suggestion ticked, **plus the household's names typed in** | The realistic run. An attorney always knows the client, the spouse and the children. A leak here is a defect. |
| `auto` | high-confidence captions and every suggestion ticked, nothing typed | What the rules find on their own. Household-name leaks here mean a proposal is missing. |

The spaCy model is off by default (`--ner` turns it on); the rules are what is
being trained, and the model only proposes.

`--pdf-mode anonymize` runs the PDFs in anonymize mode instead of redact; the
report then also counts the pages that could not be redrawn in their own text
order and had their replacements written on top.

`--docx` renders every PDF as a Word document first (`eval/to_docx.py`: a
visual row with two or more columns becomes a table row, anything else a
paragraph) and runs the DOCX path instead, in `--docx-mode anonymize` or
`redact`. Scanned PDFs are skipped in that mode, as there is no text to render.

## Where the truth comes from

The corpus labels are extraction labels - a pay stub's JSON says who the
employee is and what the net pay was - not redaction labels. `eval/truth.py`
builds the list of secrets from three sources, pooled per household so a name
found in one document is checked in all of them:

1. **JSON fields that identify somebody**: holders, employees, both ends of a
   payment-app row, payroll counterparties, account/loan/policy numbers, VINs,
   plates, parcels, title numbers, handles, phones, emails, street lines, ZIP+4,
   employers and the client's own business; on an online order record, the
   order number, the tracking number, everyone it shipped to (a gift recipient
   is a counterparty) and the card's last four wherever a line names it as a
   card ("Visa ending in 4053", "Mastercard - 4053").
2. **The corpus's own conventions**, read off the original page: SSNs with a
   never-issued `9xx` prefix and EINs with a `00-` prefix are planted
   identifiers even where no JSON field carries them; a 1040's dependents line.
3. **Shapes swept over the output** that are wrong whatever the labels say: an
   SSN shape, a masked account tail with its last four, an `INDN:` field with a
   name still in it, an email address.

Numbers are compared with separators removed, and a surviving two-thirds of an
account number is reported as `<category>_partial`. The delivered filename is
checked too (`filename:<category>`).

What must **survive** is scored just as hard, because a redacted financial
exhibit gets read by something that sorts its transactions:

- **destroyed dollar figures** - every amount on the original page, compared
  as a multiset against the output page;
- **merchant names lost** - every merchant, store and institution the JSON
  names that was on the original page, and on an order record every seller
  and product title.

Dates are not scored. When a run turns up a new kind of document, read one
delivered page by eye as well: an order date taken as a birth date, or a year
registered as a card's last four, removes every date in the file and costs the
leak count nothing.

For scanned PDFs the output is OCR'd to be checked, so their leak counts are a
floor rather than an exact figure.

## Online order records

`eval/make_amazon.py` gives every client in the corpus - the ten households
and both discovery households - a year of Amazon orders in four layouts of at
least ten pages each, written into `<household>/amazon/` with a JSON label per
PDF in the corpus's own shape:

    ~/.venvs/anonymizer-redactor/bin/python -m eval.make_amazon CORPUS_ROOT

The people, home address and card last-fours come from the client's other
labels, so a household run sees one consistent client; the gift recipients,
their addresses, order and tracking numbers are invented and deterministic.
The invoice layout (`Amazon_Invoices_<year>.pdf`) was written after the fixes
the first three prompted and scored before any change for it - keep one layout
like that in reserve whenever a new kind of record is added, because a set
the rules were fitted to can only show that they fit it.

## Photos and screenshots

`--photos` adds the corpus's phone photos (`variants_phone_photos/`, about a
thousand JPEGs) and payment-app screenshots (`payment_apps/*/screenshots/`,
about five hundred PNGs). Each is made into a PDF exactly as the app does on
the way in, then scored like a scan - its output OCR'd - so its leak count is a
floor. A photographed page `…_p1.jpg` takes its document's label `….json`;
the screenshots have no labels of their own and are scored against the
household's pooled secrets.

    ~/.venvs/anonymizer-redactor/bin/python -m eval.corpus_eval CORPUS --out RUN \
        --photos --match "\.(jpg|png)$"

## Reading the report

```
hard leaks 2892 in 1924 files  (was 4540 in 2291)
destroyed dollar figures 1119 in 1079 files
merchant names lost 7355 (2594 distinct)

category                       leaks  files  values   baseline
counterparty                    1197    620     774      2166
```

A leak is counted once per (file, category, value). `household_first_name` is
reported but not totalled: a first name that is also a merchant word ("Summer",
"Hunter") is not reliably a leak.

`--category NAME --examples N` prints the surviving value and the text around it
for one category - that context is almost always enough to find the rule that
missed it. `--types` breaks leaks down by document type.

## Adding a fix

Every defect the corpus finds gets a regression test in
`tests/test_corpus_regressions.py` built from the exact failing line, cut down
to the few words that make it fail. Then rerun the corpus and compare against
the previous run with `--compare`, to see that nothing else moved the wrong
way - several fixes here were first measured making the dollar-figure count
worse.

Editing `redactor/` during a run is safe. The run copies `redactor/` and
`eval/` into `RUN_DIR/_code` before it starts and the workers import from that
copy - they are recycled every 20 batches, and before this a mid-run edit
silently split one run between two versions of the code. Each batch's result
records the path it imported from (`"code"`), and the copy stays with the
results as a record of what produced them.
