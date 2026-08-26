"""Find the people named in transaction descriptor lines.

A bank statement names people the pleadings never mention, and they are exactly
the people a financial declaration gets fought over: the childcare provider, the
landlord, the person receiving a recurring Zelle transfer. Across a generated
sample of 25 financial documents, ten name-shaped values survived a correct run,
and the model proposed only two of the six counterparties - ALL-CAPS descriptor
lines are where ``en_core_web_sm`` is weakest, and ALL-CAPS is what a statement
prints.

    ZELLE TO SHAWNA KIRKENDALL          PAYPAL *HOLLENBECK
    CHECK 1042 TO D. TORVIK             SQ *ABERNATHY
    ACH PMT FROM DOUGLAS VANDENBROOK    TST* CALLOWAY

These are proposed as **people**, not as placeholders. ``ZELLE TO [NAME-4]``
reads as a redaction; ``ZELLE TO CARMEN LATTIMORE`` reads as a bank statement,
and the surrogate machinery to do that already exists.

Nothing here is applied without a tick. The guards matter more than the patterns
do - the same two regexes without them propose "TRANSFER TO SAVINGS XXXXXXXX",
"WIRE TO BANK OF THE WEST" and "PAYMENT TO CREDIT UNION" as human beings.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

from . import caption, ner

# The payment rails and the generic banking verbs. Every one of these except the
# verbs is already hardcoded in the payment_handle detector in patterns.py, so
# this extends a list the project already accepts rather than opening a new one.
# Deliberately no merchant names and no bank names.
_LEAD = (r"(?:zelle|venmo|pay ?pal|cash ?app|square ?cash|ach|wire|check|chk|"
         r"draft|xfer|transfer|payment|pmt|deposit|dep|eft|bill ?pay)")

_TOKEN = r"(?:[A-Z][A-Za-z'’\-]{1,20}|[A-Z]{2,20}|[A-Z]\.)"
_NAME = rf"{_TOKEN}(?:\s+{_TOKEN}){{0,3}}"

# "ZELLE TO SHAWNA KIRKENDALL", "CHECK 1042 TO D. TORVIK", "PMT FROM P. TORVIK".
# The cue is case-insensitive; the name after it is not, or the capitalised
# token requirement stops meaning anything and the match runs into the sentence.
TO_FROM = re.compile(
    rf"(?i:\b{_LEAD}\b)[^\n]{{0,20}}?(?i:\b(?:to|from|payee)\s+)({_NAME})"
)

# "PAYPAL *HOLLENBECK", "SQ *ABERNATHY", "TST* CALLOWAY" - the processor's
# prefix, a star, then the merchant or person. Case-sensitive throughout.
STAR = re.compile(rf"\b[A-Z][A-Z ]{{1,14}}\*\s?({_NAME})")

MIN_SOLO_LENGTH = 4
_MASK_RUN = re.compile(r"[Xx*•#]{2,}")

# Account types. "TRANSFER TO CHECKING" is the commonest line on any statement
# and it names nobody. These are generic banking nouns of the same kind as
# ner._ORG_MARKERS, not a list of institutions - the audit was explicit that a
# list of bank or merchant names would overfit, and this is deliberately not
# one. Kept local rather than added to the model layer's vocabularies, which
# are tuned for a different question.
_ACCOUNT_WORDS = frozenset({
    "checking", "chequing", "savings", "money", "market", "reserve", "escrow",
    "brokerage", "ira", "roth", "hsa", "fsa", "atm", "payroll", "billpay",
    "autopay", "overdraft", "principal", "interest", "line", "loan", "mortgage",
    "card", "visa", "mastercard", "amex", "discover", "self", "myself",
})


# Where an employer is named outright.
EMPLOYER_FIELD = re.compile(
    r"(?i:\b(?:employer(?:'s|’s)?(?:\s+name)?|employed\s+by|paid\s+by"
    r"|payer(?:'s|’s)?\s+name)\s*[:\-]\s*)"
    r"([^\n]{2,60})"
)

# "DIRECT DEP CASCADIA FREIGHT LOGISTICS PAYROLL" - the employer is the part
# between the deposit type and the word payroll.
PAYROLL_DEPOSIT = re.compile(
    r"(?i:\b(?:direct\s+dep(?:osit)?|dir\s+dep|ach\s+credit|ach|payroll)\s+)"
    rf"({_NAME})"
    r"(?i:\s+(?:payroll|pay|salary|wages|direct\s+dep|dir\s+dep))"
)

_EMPLOYER_JUNK = re.compile(r"^[\W\d_]+$")


@dataclass(frozen=True)
class Party:
    """Somebody named in a financial document who is not a party to the case."""

    name: str
    source: str = ""
    category: str = "person"

    @property
    def key(self) -> str:
        return " ".join(self.name.split()).casefold()


def _acceptable(name: str) -> bool:
    """Whether this capture is a person rather than an account or an institution."""
    tokens = name.split()
    if not tokens:
        return False
    # "TRANSFER TO SAVINGS XXXXXXXX7145" - an account, not a person
    if any(ch.isdigit() for ch in name) or _MASK_RUN.search(name):
        return False
    # "WIRE TO BANK OF THE WEST", "PAYMENT TO CREDIT UNION". Reusing the model
    # layer's vocabularies rather than writing a third copy: bank, credit,
    # union and savings are all already in one or the other.
    bare = [t.strip(".,'’").casefold() for t in tokens]
    if any(t in ner._ORG_MARKERS or t in ner._NOT_A_NAME or t in _ACCOUNT_WORDS
           for t in bare):
        return False
    # a lone short word is an abbreviation on a statement, not a surname
    if len(tokens) == 1 and len(tokens[0].strip(".")) < MIN_SOLO_LENGTH:
        return False
    if caption.is_address(name):
        return False
    return True


# A cheque register and a bill-pay list head the column that holds the person.
_PAYEE_HEADER = re.compile(
    r"\bpayee\b|\bpaid\s+to\b|\bpayable\s+to\b|\bpay\s+to\b|\bremit\s+to\b",
    re.IGNORECASE)

_HONORIFIC = re.compile(r"^(?:dr|mr|mrs|ms|miss|rev|fr|sr|prof)\.?\s+", re.IGNORECASE)
_CREDENTIAL = re.compile(
    r"\s+(?:dds|dmd|md|do|rn|lcsw|cpa|esq|jr|sr|ii|iii|iv|phd|psyd|pa-c)\.?$",
    re.IGNORECASE)
# "D. Torvik - March rent", "Shawna Kirkendall - childcare": the payee is the
# part before the dash, the memo is the part after.
_MEMO = re.compile(r"\s+[-–—]\s+.*$|\s*,\s*.*$")

PAYEE_TOKENS = 2


def from_tables(tables, source: str = "") -> list[Party]:
    """People named in a table's payee column.

    A cheque register does not write "CHECK 1042 TO D. TORVIK"; it puts the
    number, the date and the payee in three columns, so the descriptor patterns
    have nothing to anchor on.

    Only a two-token name is taken, after stripping an honorific and a
    credential. That is narrower than the prose patterns on purpose: a payee
    column is mostly businesses, and "Cascade Valley Power" is three
    capitalised tokens that pass every test for a person. Two tokens, or an
    initial and a surname, is what a person's name looks like in this column;
    a longer run there is nearly always a company.
    """
    found: dict[str, Party] = {}
    for rows in tables:
        if len(rows) < 2:
            continue
        header = rows[0]
        column = next((i for i, cell in enumerate(header)
                       if _PAYEE_HEADER.search(cell)), None)
        if column is None:
            continue
        for row in rows[1:]:
            if column >= len(row):
                continue
            cell = _MEMO.sub("", row[column]).strip()
            cell = _CREDENTIAL.sub("", _HONORIFIC.sub("", cell)).strip(" .,")
            if not cell or len(cell.split()) != PAYEE_TOKENS:
                continue
            if not re.fullmatch(_NAME, cell) or not _acceptable(cell):
                continue
            found.setdefault(cell.casefold(), Party(cell, source))
    return list(found.values())


def harvest(text: str, source: str = "") -> list[Party]:
    """Counterparties this document's transaction lines name."""
    found: dict[str, Party] = {}
    for pattern in (TO_FROM, STAR):
        for match in pattern.finditer(text):
            name = " ".join(match.group(1).split()).strip(" ,.")
            if not name or not _acceptable(name):
                continue
            found.setdefault(name.casefold(), Party(name, source))
    return list(found.values())


# The addressee block: a name on its own line with a street address under it.
# This is how every statement, bill and remittance coupon prints the account
# holder, and it is the only place a statement names them at all.
ADDRESSEE = re.compile(
    rf"(?m)^[^\S\n]*({_NAME})[^\S\n]*$\n"
    r"(?=[^\S\n]*\d{1,6}[A-Za-z]?[^\S\n]+[A-Za-z0-9])"
)

# "BRANDON PRITCHARD For account ending in 1234" - the running header on every
# page after the first.
FOR_ACCOUNT = re.compile(
    rf"^[^\S\n]*({_NAME})[^\S\n]+(?i:for\s+account\s+(?:ending|number|no))",
    re.MULTILINE)

# A PO box under a name is the payee's lockbox, not where anybody lives.
_PO_BOX = re.compile(r"(?i)^\s*(?:P\.?\s?O\.?\s+box|post\s+office\s+box)\b")


def account_holders(text: str, source: str = "") -> list[Party]:
    """Whoever the document is addressed to.

    A pleading names its parties in the caption, so the name screen opens
    pre-populated. A bank statement has no caption: it prints the account
    holder once, in the address block on the remittance coupon, and again in
    the running header of every later page. Nothing harvested either, so the
    operator had to know to type their own name in - and if they did not, the
    name shipped on every page while the card number beside it was redacted.
    """
    found: dict[str, Party] = {}
    lines = text.splitlines()
    for match in ADDRESSEE.finditer(text):
        # the address line underneath decides whether this is a residence
        tail = text[match.end():].splitlines()
        if tail and _PO_BOX.match(tail[0]):
            continue
        name = " ".join(match.group(1).split()).strip(" ,.")
        if _acceptable(name) and len(name.split()) >= 2:
            found.setdefault(name.casefold(), Party(name, source, "person"))
    for match in FOR_ACCOUNT.finditer(text):
        name = " ".join(match.group(1).split()).strip(" ,.")
        if _acceptable(name) and len(name.split()) >= 2:
            found.setdefault(name.casefold(), Party(name, source, "person"))
    return list(found.values())


def employers(text: str, source: str = "") -> list[Party]:
    """The employer a financial document names.

    Where the money comes from identifies the client; where it goes mostly does
    not. A grocery store, a utility and an insurer appear on millions of
    statements and say nothing about who this is - and if the delivered file is
    going to be read by something that looks merchants up, renaming them is
    worse than useless, because it sends the reader after a business that does
    not exist. The employer is the exception, and it is the one every pay stub,
    W-2 and declaration names outright.
    """
    found: dict[str, Party] = {}
    for pattern in (EMPLOYER_FIELD, PAYROLL_DEPOSIT):
        for match in pattern.finditer(text):
            name = " ".join(match.group(1).split()).strip(" ,.;:-")
            if not name or _EMPLOYER_JUNK.match(name):
                continue
            if name.casefold() in ner._NOT_A_NAME or caption.is_address(name):
                continue
            if any(t.strip(".,").casefold() in _ACCOUNT_WORDS for t in name.split()):
                continue
            found.setdefault(name.casefold(), Party(name, source, "employer"))
    return list(found.values())


def harvest_documents(regions: dict[str, str],
                      tables: dict[str, list] | None = None,
                      avoid: frozenset[str] = frozenset()) -> list[Party]:
    """Counterparties across the batch, minus anybody already spoken for."""
    tables = tables or {}
    merged: dict[str, Party] = {}
    for source, text in regions.items():
        for party in [*harvest(text, source),
                      *from_tables(tables.get(source, ()), source),
                      *employers(text, source),
                      *account_holders(text, source)]:
            if party.key in avoid:
                continue
            merged.setdefault(party.key, party)
    return list(merged.values())
