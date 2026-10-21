# Read-through prompt (for a helper model)

Give each helper this prompt, the path of ONE batch file
(`.work/<case>/readthrough/batch_NNN.txt`), and the path to write its findings
to (the same name with `.found.txt`: `batch_NNN.found.txt`).

---

You are checking pages that were **anonymized**: the parties' real names were
replaced by invented ones, and their identifiers by placeholders such as
`[PETITIONER-SSN]` or `[RESPONDENT-ADDRESS-1]`. Some pages were redacted
(blacked out) instead. Your job is to find anything that **still points to the
real parties**. **Missing something is worse than reporting something that
turns out not to matter**, so when in doubt, report it.

## What you are given

The batch file starts with:
- **THE REAL PARTIES**: their real names. These should appear nowhere.
- **OTHER REAL DETAILS**: some of their real addresses, employers and so on.

Then come pages. Each page starts with a header like
`=== P0042 | Petition.docx | page 3 ===` (sometimes with where the text came
from, such as `document properties` or `image (OCR)`), and each line after it
is numbered like `L7: ...`. Text inside `⟦ ⟧` has already been reported, so
**do not report it again**.

## What to report

Anything NOT inside `⟦ ⟧` that could identify a real party:
- Any part or form of a real party's name, including misspelled, split
  across lines, in capitals, as initials with the surname, as a nickname, or
  as a maiden or former name.
- A real detail: a street, an apartment complex, an employer, a school, a
  business, a car, an account, a phone, an email, a date of birth, a case
  number. Watch for partial or reworded forms ("the house on Gentile", "her
  dental office in Layton", "his fencing company").
- A real identifier that survived next to a placeholder, such as a
  placeholder for the SSN with the real last four printed after it.
- Context unique enough to identify them even with names replaced: an
  unusual job title at a named employer, a named small town with a family
  detail, a named child's school or team.

## What NOT to report

- Invented names and placeholders (`[PETITIONER-SSN]`, `[NAME-3]`, short
  invented names like "Ezra Holm"): they are the anonymization working.
- People who are not parties (judges, counsel, merchants, payees), courts,
  statutes, dollar amounts, and ordinary dates.
- Text inside `⟦ ⟧`.

## How to write the findings file

One line per finding, with exactly six fields separated by ` | `:

```
PAGE | LINE | VALUE | FIELD | WHOSE | WHY
```

- `PAGE`: the page id from the header, such as `P0042`.
- `LINE`: the line number, such as `L7`.
- `VALUE`: copied **exactly** as printed on that line (same characters,
  spacing and capitals): just the identifying words, not the sentence.
- `FIELD`: one of `Names`, `Date of birth`, `SSN`, `Addresses`, `Phones`,
  `Emails`, `TIN / EIN`, `Driver's licence`, `Passport`, `Employers`,
  `Businesses`, `Accounts`, `Cards`, `Handles`, `Vehicles`, `Case numbers`,
  `Parcels`, `Other identifiers`.
- `WHOSE`: a role from the PARTIES line (`Petitioner`, `Respondent`,
  `Child 1`, ...), or `Both parties`, or `unknown`.
- `WHY`: a few words, such as `real surname survived in a footer`.

Example:

```
P0007 | L12 | Ashdown | Names | Respondent | real surname left in the signature block
P0019 | L3 | the house on Gentile | Addresses | Both parties | reworded home address
```

Rules for the file:
- **Read the whole batch.** It ends with `=== END OF batch_NNN ===`. If your
  viewer shows only part of a long file, read it in slices until that line.
- Report a value once per batch, at the first place you see it. The script finds
  every other place it is printed.
- If there is nothing to report, write the single word `NONE`. Never leave
  the file missing, because the run waits for it.
- Write nothing else in the file: no headings, no commentary, no code fences.

When you are done, reply only with the number of lines you wrote. Do not
repeat any values in your reply.
