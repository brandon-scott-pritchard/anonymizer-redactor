# Deciding whose a value is

The CSV feeds a party sheet. Every value on it is replaced wherever it appears
in the case's documents. So the cost of each mistake is different:

- **A party's value left off**: it may survive anonymization. That is the
  serious error, so a real party value is never dropped just to be tidy.
- **Someone else's value put on**: an attorney's phone, a bank's address or a
  witness's name gets anonymized for no reason, and the operator has more to
  type and check. Keep these out. The operator asked for just the parties'
  information.

When you truly cannot tell, answer `unsure`. The row stays in the CSV as
Unassigned for a person to judge, which is better than a guess either way.
Before answering `unsure` or `drop` on a value you are not certain about, open
the source file at the page shown and read the surrounding lines. The context
line is a pointer, not the whole story. Taking the time is expected.

Some values may arrive with no party named nearby: a masked tail alone on a
line, a 17-character code, a phone on a statement row. Read where they sit. A
statement's own account number, the party's card on a receipt, or the party's
phone on their own Zelle row is theirs. A payment ID, a merchant's phone or a
payee's account is not.

## Keep (answer the party's label)

Anything that identifies a **party** - petitioner, respondent, a child, or an
"Other party" from the PARTIES block:

- every form of their name, including maiden, former and married names,
  nicknames, and misspellings or OCR misreadings of it;
- date of birth, SSN, TIN/EIN of a business they own, driver's licence,
  passport, and other personal ID numbers;
- home and mailing addresses, current or former;
- their own phone numbers and email addresses, including a work cell or a
  work email that is theirs;
- the **name** of their employer, and businesses they own or run;
- bank, brokerage, retirement and card accounts in their name. Masked tails
  (`****1234`) count;
- their Venmo/Cash App/PayPal/Zelle handles and social media handles;
- vehicles they own (VIN, plate);
- this case's own case number(s), and parcel numbers of property they own
  (answer `Case`).

Use `Both parties` only for what the parties share: a joint account, the
marital home, a vehicle titled to both. Something in one party's name alone,
such as a car titled to one, a card issued to one, or an account with one
holder, gets that party's label even if both use it.

Also keep:
- **A school, preschool or daycare** a party or a child attends or attended
  (answer the person's label; the field is Other identifiers). It narrows down
  who a child is as much as an address does. An individual babysitter or
  nanny paid by the parties is a non-party person: drop them.
- **A named residence**, such as an apartment complex ("University Ridge
  Apartments") or a care facility where a party lives. Treat it as an address.
- **OCR-mangled forms** of a party's value ("435N900 EAPT 12", "Harr1ngton"):
  answer the same label as the clean form. The app matches the exact text, so
  the mangled form is needed too.

## Drop (answer `drop`)

- Counsel on either side, paralegals, law firms, mediators, guardians ad
  litem, judges, commissioners, clerks and the court, with their names,
  addresses, phones, emails and bar numbers.
- Banks, lenders, insurers, merchants, utilities, landlords and payroll
  companies: their addresses, phone numbers, customer-service lines and
  routing numbers.
- The employer's own address, main phone line and EIN. Keep only the
  employer's name, under the party.
- Other people who are not listed parties: new partners, relatives, friends,
  witnesses, the other side of a Zelle or Venmo payment, a childcare provider.
  (The app leaves non-parties as written by design.)
- Case numbers of other matters and of cited cases.
- Insurance and health-plan member, group, policy and claim numbers, loan
  and mortgage numbers (even in a party's name), routing numbers, and utility
  meter and service-point numbers. The app leaves these as written by
  design. A utility's **account number** in a party's name is an account:
  keep it.

## Reading an item

- **"why it was proposed"** tells you how strong the proposal is. "Named on
  the same line" is strong. "In the block under a name" is usually an address
  or header block, but check that the block really belongs to that person and
  not to their attorney's signature. "In the header of the document's first
  page" fits statements and forms. Check it on letters and pleadings.
  "No party named nearby" (`?`) needs your judgment from the context alone.
- **Conflicts** (`conflict: tied to Petitioner and to Respondent`): the same
  value was tied to two parties in different places. Read every context line
  shown. One is often a mis-tie, such as a value that sits between two
  parties' paragraphs. If the documents themselves disagree, answer `unsure`.
- **Name items** (nickname, other written form, spelled differently): keep
  them if they plausibly refer to that party. Drop them if they are someone
  else who shares a surname, such as a parent or a sibling who is not a party.
- **A card or account tail with no name near it**, such as the last four on a
  store receipt in the client's own files: answer `unsure` unless the same
  digits appear tied to a party elsewhere in the review. `unsure` keeps it in
  the CSV for a person to judge.
- **OCR noise** that is plainly not an identifier, such as a year read as a
  masked tail ("2021"), "....0000", or a stray number given an exotic label:
  `drop`. A garbled value that could still be a party's (a mangled address
  or account): answer the party, or `unsure`.
- **Shipping addresses** on orders: the party's own home is the party's (or
  `Both parties`). An address for a gift sent to someone else is that
  person's: `drop`.
- **Payment-app and wallet screenshots** (Cash App, Apple Cash, Google
  Wallet...) are the account holder's own app. A linked card or bank tail
  there with nobody else named is likely the holder's: answer the holder if
  the screenshot is theirs, otherwise `unsure`.
- **Groups that mix owners.** Answer the group with the owner most of its
  members share, then answer each member that differs by its own R id.
- **Reassign** freely when the context shows the proposal is the wrong party.
  "Her cell" under the respondent's paragraph may still be the petitioner's.
- Do not invent values. Only decide the items listed.
