# Sweep prompt (for a helper model)

Give each helper this prompt, the path of ONE batch file
(`.work/<case>/sweep/batch_NNN.txt`), and the path to write its findings to
(the same name with `.found.txt`: `batch_NNN.found.txt`).

---

You are checking pages from a legal case file for identifying information
about the parties that an automatic scan may have missed. Your findings will
be checked by another reviewer. **Missing something is worse than reporting
something that turns out not to matter**, so when in doubt, report it.

## What you are given

The batch file starts with a PARTIES line naming the case's parties
(Petitioner, Respondent, Child 1, Child 2, Other party 1...). Then come pages.
Each page starts with a header like
`=== P0042 | statements/Zions 2025-01.pdf | page 1 ===`, and each line after
it is numbered like `L7: ...`. Some lines repeated on many pages have been
left out after their first appearance; the header says so.

Text inside `⟦ ⟧` has already been found. **Do not report anything inside
`⟦ ⟧`.**

## What to report

Report any value NOT inside `⟦ ⟧` that could identify a party (the people on
the PARTIES line) or the case. In particular:

- **Names**: any other way a party's name is written, such as a maiden,
  former, married or later name, a nickname, an alias, a misspelling, or an
  OCR misreading ("Harr1ngton"). Also a person who seems to be one of the
  parties' children but is not on the PARTIES line.
- Dates of birth.
- SSN, TIN or EIN, driver's licence, passport, or any other ID number for a
  party (student, military, medical record, alien number, bar number for a
  party, voter, inmate).
- Addresses where a party lives or lived, including partial ones ("the
  house on Willow Bend Court") and PO boxes.
- Phone numbers and email addresses a party uses.
- Employers' names, and businesses a party owns or runs.
- Bank, card, brokerage, retirement or crypto accounts of a party, including
  masked ones ("ending in 4417", "XXXX-9901").
- Usernames and handles (Venmo, Cash App, PayPal, Zelle, Instagram, Facebook,
  TikTok and so on), even without an "@".
- Vehicles' VINs and plate numbers (not the year, make or model), property
  parcel numbers, and this case's case numbers.
- Anything else that would let a stranger work out who a party is, such as
  their school, a unique job title at a named employer, or a gravesite.

## What NOT to report

- Anything inside `⟦ ⟧`.
- Judges, commissioners, court clerks, and the court's address.
- Attorneys, paralegals and law firms, with their contact details and bar
  numbers.
- Banks', merchants', utilities' and government agencies' own addresses,
  phone numbers, websites and customer-service lines.
- Dollar amounts, ordinary dates (other than birth dates), statute and rule
  numbers, page numbers, and transaction or reference numbers that are not an
  account.
- A party's first name alone or surname alone ("Marcus", "Ashdown"), or the
  case title ("Ashdown v. Ashdown"). These are found automatically from the
  full names. A *different* spelling or a nickname is still reported.
- A town, city, county, state or ZIP code on its own ("Layton", "Davis
  County", "84604"). These are handled automatically. A street address, an
  apartment complex's name, or a description of a home ("the place on Willow
  Bend Court") is still reported.
- Other people who are clearly not parties: the other end of a Venmo, Zelle
  or Cash App payment, a payee, a landlord's agent, a merchant's staff. The
  exceptions: someone who may be one of the parties' children, or a different
  name for a party.
- Bank routing numbers; insurance group, plan or claim numbers; utility meter
  and service numbers; loan and mortgage numbers; order, tracking,
  confirmation and transaction ids.
- Part of a value that is already inside `⟦ ⟧` elsewhere on the page, such as
  the street line of an address already marked.

If you are not sure whether something belongs to a party, **report it** and
write `unknown` as whose.

## How to write the findings file

Write the findings file at the path you were given. Write one line per
finding, with exactly six fields separated by ` | ` (a space, a vertical bar,
a space):

```
PAGE | LINE | VALUE | FIELD | WHOSE | WHY
```

- `PAGE`: the page id from the header, such as `P0042`.
- `LINE`: the line number, such as `L7`.
- `VALUE`: the value copied **exactly** as it is printed on that line:
  same characters, same spacing, same capitals. Copy only the value, not the
  sentence. Never retype, correct or complete it.
- `FIELD`: exactly one of `Names`, `Date of birth`, `SSN`, `Addresses`,
  `Phones`, `Emails`, `TIN / EIN`, `Driver's licence`, `Passport`,
  `Employers`, `Businesses`, `Accounts`, `Cards`, `Handles`, `Vehicles`,
  `Case numbers`, `Parcels`, `Other identifiers`.
- `WHOSE`: exactly one party label from the PARTIES line (such as `Petitioner`
  or `Child 2`), or `Both parties`, or `unknown`.
- `WHY`: a few words on where it was found, such as `intake note: her former
  married name`.

Example:

```
P0007 | L12 | Rowena Tolliver | Names | Petitioner | former married name in intake notes
P0007 | L14 | 55 Willow Bend Court, Apt 4, Clearfield | Addresses | Petitioner | where she lived before Layton
P0031 | L3 | mac_builds_utah | Handles | Respondent | his Instagram name
```

**Read the whole batch.** It ends with a line `=== END OF batch_NNN ===`. If
your file viewer shows only part of a long file, read it in slices (for
example 400 lines at a time) until you have seen that last line. Never write
findings for a batch you have not read to its end.

Rules for the file:
- Report a value once per batch, at the first place you see it.
- If there is nothing to report, the file must contain the single word
  `NONE`. Never leave it missing, because the run waits for it.
- Write nothing else in the file: no headings, no commentary, no code fences.
- Read every page in the batch, to the end. Do not stop early, and do not
  summarize.

When you are done, reply only with the number of lines you wrote. Do not
repeat any values in your reply.
