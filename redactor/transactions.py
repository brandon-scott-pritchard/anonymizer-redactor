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

from . import caption, ner, patterns, places
from . import names as _names

# The payment rails and the generic banking verbs. Every one of these except the
# verbs is already hardcoded in the payment_handle detector in patterns.py, so
# this extends a list the project already accepts rather than opening a new one.
# Deliberately no merchant names and no bank names.
_LEAD = (r"(?:zelle|venmo|pay ?pal|cash ?app|square ?cash|ach|wire|check|chk|"
         r"draft|xfer|transfer|payment|pmt|deposit|dep|eft|bill ?pay|"
         # a neobank's own verbs: "Pay Friends to Brynlee Nelson"
         r"pay\s+friends|pay\s+anyone|send\s+money|sent|received|request(?:ed)?)")

_TOKEN = r"(?:[A-Z][A-Za-z'’\-]{1,20}|[A-Z]{2,20}|[A-Z]\.)"
# A middle initial without its period - "OAKLEY V WARD", "Kyle D Harrington" -
# is how a statement's address block and a payroll system print a name. Bare,
# it is only accepted between two other tokens, so a stray capital letter at
# the end of a line never becomes part of a name.
#
# The words of a name are joined by ONE space. Two or more is a column gap -
# the PDF reader renders one that way on purpose - and a name that crossed it
# came back as "Brynlee Nelson Pay Friends", the payee plus the next column.
_SP = r"[ \u00a0]"
_INITIAL = rf"[A-Z](?={_SP}[A-Z])"
_NAME = rf"{_TOKEN}(?:{_SP}(?:{_TOKEN}|{_INITIAL})){{0,3}}"

# "ZELLE TO SHAWNA KIRKENDALL", "CHECK 1042 TO D. TORVIK", "PMT FROM P. TORVIK".
# The cue is case-insensitive; the name after it is not, or the capitalised
# token requirement stops meaning anything and the match runs into the sentence.
#
# A bank that prints only the first initial - "ZELLE TO J SMITH" - left no
# token for the name to start on, and the payee shipped. Only after "to" or
# "from", and never A or I, which open "Payment To A Vendor" as an article.
# The payee follows "to" on its own line. A wallet's column reading ends a
# cell "Bank Deposit to" and starts the next with "FUNDING SOURCE ***9268",
# and reaching across the break proposed that label - and "Etsy" - as people.
TO_FROM = re.compile(
    rf"(?i:\b{_LEAD}\b)[^\n]{{0,20}}?(?i:\b(?:to|from|payee)[^\S\n]+)((?:[B-HJ-Z]{_SP})?{_NAME})"
)

# "PAYPAL *HOLLENBECK" - a person-to-person rail's prefix, a star, then who was
# paid. Case-sensitive after the prefix.
#
# Only the person-to-person rails. A card processor's prefix - "SQ *", "TST*",
# "DOORDASH*", "GOOGLE *", "AMAZON MKTPL*" - is followed by a merchant, and on
# a corpus of ~7,000 generated statements this pattern with any prefix at all
# proposed "SLAB PIZZA LAYTON", "CHIPOTLE SAN FRANCISCO", "YOUTUBEPREMIUM G" and
# Amazon order codes as human beings. Ticking them all - which is what a busy
# operator does - removed 7,000 merchant names from the statements.
STAR = re.compile(
    r"(?i:\b(?:pay\s?pal|pp|venmo|zelle|cash\s?app|sqc|square\s+cash|apple\s+cash)"
    rf"[^\S\n]*\*[^\S\n]?)({_NAME})")

# The rail, an optional direction, and the person - no "to" or "from" at all:
#   ZELLE CREDIT JESSICA COOK        Zelle Debit Jennifer Lewis
#   Venmo Credit Bryce Parker        ZELLE KENZIE LEWIS
_RAIL = r"(?:zelle|venmo|pay\s?pal|cash\s?app|apple\s+cash|sqc|square\s+cash)"
_RAIL_VERB = (r"(?:credit|debit|payment|pmt|instant|inst|verifybank|sent|received|p2p|"
              r"transfer|xfer|cashout|cash\s?out|request|deposit|withdrawal|des|type|to|"
              r"from|in|out)")
# PayPal's own descriptor runs two verbs and a YYMMDD date before the payee -
# "PAYPAL INST XFER 240315 TANNER JENSEN WEB ID: PAYPALSI77" - and one verb
# slot with no room for the date never reached the name.
RAIL_NAME = re.compile(
    rf"(?i:\b{_RAIL}\b)[^\S\n]+(?:(?i:{_RAIL_VERB})[^\S\n]+){{0,2}}(?:\d{{6}}[^\S\n]+)?"
    rf"(?!(?i:{_RAIL_VERB})\b)({_NAME})")
# "Deposit ACH VENMO TYPE: P2P CO: MADISON HARRIS" - the ACH record's
# company-name field, which on a person-to-person transfer is the person.
P2P_COMPANY = re.compile(rf"(?i:\btype:[^\S\n]*p2p\b)[^\n]{{0,30}}?(?i:\bco:)[^\S\n]*({_NAME})")
# A payment processor's own descriptor for money from a customer:
# "Website Payment - Tyler Foster ID: RCJIMIQ..." on a PayPal business report.
PROCESSOR_PAYMENT = re.compile(
    r"(?i:\b(?:website|express[^\S\n]+checkout|mobile|invoice|subscription|general)"
    rf"[^\S\n]+payment)[^\S\n]*[-–:][^\S\n]*({_NAME})")
# Any other processor line of the form "<type> Payment - Kenzie Cook ID: FDAL..."
# - the transaction-ID field is what marks the name before it as the other
# party. Person-shaped only, since the same slot holds "eBay Inc." and "Steam".
#
# The description wraps, so the ID is often on the next line, and read across
# the row the name is followed by the currency instead: "... Payment - Kenzie
# Cook USD -125.86".
# The name itself may wrap - "User Payment - Brian" / "Gomez ID: OYLV..." - and
# with the transaction ID anchoring its end, one line break inside it is safe.
_NAME_WRAPPED = rf"{_TOKEN}(?:(?:{_SP}|[^\S\n]*\n[^\S\n]*)(?:{_TOKEN}|{_INITIAL})){{0,3}}"
# A scanned or photographed report reads none of it cleanly, and every one of
# these shipped the customer's name off a scanned PayPal-style statement:
#   WebsitePayment-Madison Long ID:        the type glued to "Payment"
#   Website Paymént- Kevin Anderson ID:    an accent out of a speck
#   PaymentRelund-Tyler Pratt ID:          a second word, misread, glued on
#   Website Payment : Kimberly Price ID    the dash read as a colon
#   Website Payment Joshua Turner iD uso   the dash lost; "USD" read "uso"
#   Website Payment - Tyler Brooks !D      "ID" read "!D", "I:", "ID;"
#   Website Payment-Rachel BellID:         the field glued to the surname
# The ID field is still what anchors the name's end, so these widen what the
# line around the name may look like, not what a name may be.
_PAYMENT_CUE = r"(?:\b|(?<=[a-z]))(?i:paym[eéèë]nt)"
_PAYMENT_SEP = r"(?:[^\S\n]*(?:[A-Za-z]{3,10}[^\S\n]*)?[-–—:~=]+[^\S\n]*|[^\S\n]+)"
_ID_FIELD = (r"(?:(?:[^\S\n]+|[^\S\n]*\n[^\S\n]*)"
             r"(?:(?i:transaction[^\S\n]+)?[iIl1!|][dD]\b|I[:;]|(?i:us[dop]|eur|gbp|cad|mxn|aud)\b)"
             # glued on, only with a capital D: "Harold" ends in "ld" too, but
             # "GarcialD:" is "Garcia" and the field with its I read as an l
             r"|(?<=[a-z])[Il1|]D\b)")
PAYMENT_WITH_ID = re.compile(rf"{_PAYMENT_CUE}{_PAYMENT_SEP}({_NAME_WRAPPED}){_ID_FIELD}")
# what a processor's merchant line ends in, where a person's never does
_CORPORATE = frozenset({
    "technologies", "technology", "games", "entertainment", "software", "systems",
    "labs", "media", "digital", "online", "store", "stores", "shop", "market",
    "marketplace", "studios", "studio", "network", "networks", "music", "pay",
    "subscription", "subscriptions", "delivery", "rides", "eats", "prime",
    # a payment app's Name column: "Spotify USA", "Uber Technologies"
    "usa", "com", "app", "apps", "tv", "plus", "premium", "services",
})

# What a descriptor prints after the counterparty: "Zelle From Brian Foster on
# 01/25 Ref # Bac65742743663", "Online Transfer to Cody B Ashworth Ref #Ib01".
# The name pattern takes any capitalised token, so these rode along with it.
_TRAILING = frozenset({
    "ref", "reference", "conf", "confirmation", "id", "memo", "trace", "on",
    "via", "for", "at", "acct", "account", "no", "number", "xfer", "pmt",
    "payment", "transfer", "web", "ppd", "ccd", "tel", "online", "mobile",
    # the statement's own words, when the next column sits one space away
    "checks", "check", "processed", "pending", "posted", "completed", "complete",
    "withdrawal", "deposit", "purchase", "fee", "balance", "total", "amount",
    "date", "p2p", "usd", "status", "canceled", "cancelled", "declined",
})

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
# the parties to an account or a lease, named by their role rather than by name
_ROLE_WORDS = frozenset({
    "cardholder", "accountholder", "landlord", "tenant", "lessor", "lessee",
    # "Deposits received from Other Financial Institutions"; "Zions
    # Bancorporation" over its street - ner's marker is only "bancorp"
    "financial", "institution", "institutions", "bancorporation",
})


# Where an employer is named outright. A column gap is a separator too - a
# credit report prints "Employer(s)   U.S. Air Force - 75th Air Base Wing" -
# and the value ends at the next column gap, not at the end of the row.
EMPLOYER_FIELD = re.compile(
    r"(?i:\b(?:employer(?:\(s\)|s)?(?:'s|’s)?(?:\s+name)?|employed\s+by|paid\s+by"
    r"|payer(?:'s|’s)?\s+name)(?:\s*[:\-]\s*|[^\S\n]{2,}))"
    r"([^\n]{2,60}?)(?=[^\S\n]{2,}|[^\S\n]*$)", re.MULTILINE
)
# The label alone on its line and the employer on the next, which is how a
# PDF whose cells are separate text runs reads in file order.
EMPLOYER_BLOCK = re.compile(
    r"(?im)^[^\S\n]*(?:employer(?:\(s\)|s)?|employer(?:'s|’s)[^\S\n]+name)[^\S\n]*:?[^\S\n]*\n"
    r"[^\S\n]*([^\n]{2,60}?)[^\S\n]*$")

# "DIRECT DEP CASCADIA FREIGHT LOGISTICS PAYROLL" - the employer is the part
# between the deposit type and the word payroll.
PAYROLL_DEPOSIT = re.compile(
    r"(?i:\b(?:direct\s+dep(?:osit)?|dir\s+dep|ach\s+credit|ach|payroll)\s+)"
    rf"({_NAME})"
    # a whole word: "pay" is also the start of "Payments", and "Ach Debit
    # Verizon Wireless Payments" proposed the phone company as an employer
    r"(?i:\s+(?:payroll|pay|salary|wages|direct\s+dep|dir\s+dep)\b)"
)

_EMPLOYER_JUNK = re.compile(r"^[\W\d_]+$")

# How a statement's descriptor line actually prints a pay deposit. Measured on
# a corpus of ~7,000 generated statements, which print it seven ways:
#
#   ACH DEPOSIT QUALTRICS LLC PAYROLL          Deposit ACH DOMO INC TYPE: PAYROLL
#   Northpoint Aeros Payroll 200110 71482349   UNIV OF UTAH DES:PAYROLL ID:0386
#   Domo Inc Payroll PPD ID: 1234567890        Direct deposit - Overstock.com
#   Ach Credit Western Governors Dir Dep
#
# The name is cut to whatever the bank's field holds ("Northpoint Aeros",
# "Iron County Medi"), so it must be proposed as printed - the full name off
# the pay stub never matches the truncated one.
_EMP_TOKEN = r"[A-Za-z0-9&][A-Za-z0-9&.'’\-]*"
_EMP = rf"{_EMP_TOKEN}(?:[^\S\n]{_EMP_TOKEN}){{0,5}}"
_PAY_CUE = r"(?:payroll|payrll|dir[^\S\n]+dep|direct[^\S\n]+dep(?:osit)?|salary|net[^\S\n]+pay)\b"
_EMP_LEAD = (r"(?:(?:ach[^\S\n]+(?:deposit|credit|dep)|deposit[^\S\n]+ach|ach)"
             r"[^\S\n]+)?")
# Where a cell starts: the line's start, a column gap, or a date's last digit.
# Written as lookbehinds rather than consumed. "(?:^|[^\S\n]{2,})[^\S\n]*" can
# split one run of spaces between its two quantifiers in every possible way, at
# every starting offset, and a scanned page's 1,000-space blank row ("A", the
# gap, "x") then took seconds per pattern - four patterns, every page.
_COLUMN_START = r"(?:^[^\S\n]*|(?<=[^\S\n]{2})|(?<=\d[^\S\n]))"
PAY_DEPOSIT_LINES = (
    # the employer immediately before the cue, the column start before it
    re.compile(rf"(?i){_COLUMN_START}{_EMP_LEAD}({_EMP})"
               rf"[^\S\n]+(?:des:[^\S\n]*|type:[^\S\n]*)?{_PAY_CUE}", re.MULTILINE),
    re.compile(rf"(?i)\bdirect[^\S\n]+deposit[^\S\n]*[-–:][^\S\n]*({_EMP})"),
    re.compile(rf"(?i)\btype:[^\S\n]*{_PAY_CUE}[^\n]*?\bco:[^\S\n]*({_EMP})"),
)
PAY_DEPOSIT_LINES += (
    # The same ACH record off a phone photo, every space lost and the labels
    # misread: "DePOSACHOVERSTOCLCOMTIPE:DiRDOPD:2214822131CO:OVERSTOCK.COM".
    # The fields keep their colons, and that is what this anchors on - no
    # word boundary, since the digits and the label run together.
    re.compile(rf"(?i)t[yi]pe:[^\S\n]*(?:{_PAY_CUE}|d[i1l]r[^\S\n]?d[eo]p)[^\n]*?"
               rf"(?<![a-z])co:[^\S\n]*({_EMP})"),
    re.compile(rf"(?i)\bdirect[^\S\n]+deposit[^\S\n]+from[^\S\n]+({_EMP})"),
    # an HSA or a 401(k) statement: "PAYROLL CONTRIBUTION NU SKIN ENTERPRISE"
    # - and the word whole: "Employer Contributions" over a figure proposed "s"
    re.compile(rf"(?i)\b(?:payroll|employer)[^\S\n]+contributions?\b[^\S\n]*[-–:]?[^\S\n]*({_EMP})"),
)
# The plan is named for the employer that sponsors it:
# "NORTHPOINT AEROSPACE SERVICES 401(K) SAVINGS PLAN".
PLAN_SPONSOR = re.compile(
    rf"(?im)(?:^[^\S\n]*|(?<=[^\S\n]{{2}}))({_EMP}(?:,[^\S\n](?:inc|llc|corp|co|ltd|llp|pllc|lp)\.?)?)[^\S\n]+"
    r"(?:401[^\S\n]?\(?k\)?|403[^\S\n]?\(?b\)?|457[^\S\n]?\(?b\)?|profit[\s-]sharing|"
    r"retirement|pension|thrift|savings[^\S\n]+and[^\S\n]+investment)"
    r"(?:[^\S\n]+(?:savings|retirement|investment|incentive))?[^\S\n]+plan\b")

# words a descriptor carries that are never part of the employer's name
_EMP_NOISE = {"ach", "deposit", "credit", "debit", "dep", "direct", "dir", "type", "id",
              "ppd", "ccd", "co", "des", "the", "from", "online", "mobile", "transfer",
              # a joiner at either end is the heading's, not a name's: Gusto's
              # "Employer contributions & taxes"
              "&"}
# "For Direct Deposit use, give your employer..." is instructions, not a payer
_EMP_NOT_A_NAME = {"for", "to", "your", "our", "use", "please", "by", "with", "via",
                   "and", "or", "set", "sign", "enroll", "enrol", "a", "an", "of",
                   "in", "on", "is", "are", "cash", "tax", "refund", "irs",
                   # the posting date a descriptor line opens with
                   "jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep",
                   "sept", "oct", "nov", "dec", "january", "february", "march",
                   "april", "june", "july", "august", "september", "october",
                   "november", "december",
                   # "DIRECT DEPOSIT - VOID - NON-NEGOTIABLE" on a stub's check
                   "void", "non-negotiable", "nonnegotiable", "sample", "copy",
                   # a stub's own column headings: "YTD NET PAY", "CURRENT NET PAY"
                   "ytd", "current", "total", "gross", "net", "amount", "this", "period"}
# A pay stub's earnings rows and a form's empty answers, which have the same
# shape as a bank's pay line: "Regular Salary  2,500.00" proposed "Regular" as
# the employer, "Company Retirement Plan" proposed "Company", "Paid by  Check"
# proposed "Check" and "Employer:" over "(none)" proposed "(none)". Rejected
# only when the whole value is made of them, so "Check Into Cash" or "Base
# Camp Outfitters" still reads as the employer it is.
_EMP_GENERIC = {"regular", "base", "annual", "hourly", "rate", "salary", "salaried",
                "wages", "wage", "pay", "earnings", "overtime", "bonus", "holiday",
                "vacation", "sick", "pto", "commission", "retirement", "employee",
                "employees", "employer", "company", "check", "cheque", "none", "n/a",
                "na", "nil", "unemployed", "self-employed", "retired", "disabled",
                "unknown", "not", "applicable", "student", "homemaker", "other", "taxes"}
_DECIMAL = re.compile(r"\d\.\d")

# The receiver's own name, which the ACH record carries in its individual-name
# field: "INDN:TYSON J OKAFOR CO ID:..." and, printed plainly, after the trace
# numbers - "Northpoint Aeros Payroll 200110 7148234928 KYLE D HARRINGTON".
RECEIVER = (
    # the field ends where the company-ID field starts, and "CO" is a
    # capitalised token the name pattern would otherwise swallow
    re.compile(rf"\bINDN:[^\S\n]*({_NAME}?)(?=[^\S\n]+CO[^\S\n]+ID\b|[^\S\n]*$)",
               re.MULTILINE),
    re.compile(rf"(?i:{_PAY_CUE})[^\S\n]+\d{{4,}}[^\S\n]+\d{{6,}}[^\S\n]+({_NAME})[^\S\n]*$",
               re.MULTILINE),
)
# Some banks fill the field surname first: "INDN:OKAFOR,TYSON CO ID:9876"
RECEIVER_LAST_FIRST = re.compile(
    r"\bINDN:[^\S\n]*([A-Z][A-Za-z'’\-]+),[^\S\n]?([A-Z][A-Za-z'’\-]+)(?:[^\S\n]([A-Z]))?"
    r"(?=[^\S\n]+CO[^\S\n]+ID\b|[^\S\n]*$)", re.MULTILINE)


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
           or t in _CORPORATE for t in bare):
        return False
    # a statement's or a form's role word in the slot a payee sits in: "Direct
    # deposit from Employer", "Payment to Beneficiary", "Transfer to
    # Cardholder", "Request to Change Address". The address-block and labelled
    # paths already refuse these; every other path takes its answer from here.
    if any(t in ner._NEVER_IN_A_NAME or t in _ROLE_WORDS for t in bare):
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
# A payment app's ledger has a column for each end of the transfer, headed by a
# bare preposition. Matched as the whole cell only - "To" inside a sentence, or
# "Transfer to", is not a heading.
_PARTY_WORD = (r"(?:from|to|sender|recipient|counterparty|sent\s+to|received\s+from"
               r"|paid\s+by|payer|name)")
# "Recipient / Sender", "From or To" - one column holding either end
_PARTY_HEADER = re.compile(
    rf"^\s*{_PARTY_WORD}(?:\s*(?:/|or|&)\s*{_PARTY_WORD})*\s*:?\s*$",
    re.IGNORECASE)
# how far down a table its heading row can sit - a letterhead line or two
# above it lands in the same block when the geometry cannot separate them
_HEADER_SEARCH_ROWS = 4

# "Coach Tanner Reed" is how a kids' sports payment names the coach
_HONORIFIC = re.compile(
    r"^(?:dr|mr|mrs|ms|miss|rev|fr|sr|prof|coach|pastor|bishop|officer|nurse)\.?\s+",
    re.IGNORECASE)
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
        for at, header in enumerate(rows[:_HEADER_SEARCH_ROWS]):
            columns = [i for i, cell in enumerate(header)
                       if _PAYEE_HEADER.search(cell) or _PARTY_HEADER.match(cell)]
            if columns:
                break
        else:
            continue
        for row in rows[at + 1:]:
            for column in columns:
                if column >= len(row):
                    continue
                cell = _MEMO.sub("", row[column]).strip()
                cell = _CREDENTIAL.sub("", _HONORIFIC.sub("", cell)).strip(" .,")
                if not cell or not _person_shaped(cell.split()):
                    continue
                if not re.fullmatch(_NAME, cell) or not _acceptable(cell):
                    continue
                found.setdefault(cell.casefold(), Party(cell, source))
    return list(found.values())


# A W-2 heads the employer's box "PAYER'S / EMPLOYER'S name, street address,
# city, state, ZIP" and puts the name on the first line under it; an
# unemployment determination heads a column "Employer" and lists them.
_EMPLOYER_HEADER = re.compile(
    r"(?i)^\s*(?:(?:payer'?s?|employer'?s?)(?:\s*/\s*(?:payer'?s?|employer'?s?))?\s+name\b.*"
    r"|employer(?:\(s\)|s)?\s*:?)\s*$")


def employers_from_tables(tables, source: str = "") -> list[Party]:
    """The employer named in the cell under an employer heading."""
    found: dict[str, Party] = {}
    for rows in tables:
        for r, row in enumerate(rows[:-1]):
            for column, cell in enumerate(row):
                if not _EMPLOYER_HEADER.match(cell or ""):
                    continue
                for below in rows[r + 1:]:
                    if column >= len(below) or not below[column].strip():
                        break
                    first = below[column].strip().splitlines()[0]
                    name = _trim_employer(first)
                    if (not name or any(ch.isdigit() for ch in name)
                            or caption.is_address(name) or _EMPLOYER_HEADER.match(name)):
                        break
                    found.setdefault(name.casefold(), Party(name, source, "employer"))
    return list(found.values())


def _person_shaped(tokens: list[str]) -> bool:
    """Two tokens, or three with an initial in the middle: ``Kyle D Harrington``."""
    if len(tokens) == PAYEE_TOKENS:
        return True
    return len(tokens) == 3 and len(tokens[1].rstrip(".")) == 1


def harvest(text: str, source: str = "") -> list[Party]:
    """Counterparties this document's transaction lines name."""
    found: dict[str, Party] = {}
    for pattern, least in ((TO_FROM, 1), (STAR, 1), (RAIL_NAME, 2),
                           (P2P_COMPANY, 2), (PROCESSOR_PAYMENT, 2), (PAYMENT_WITH_ID, 2)):
        for match in pattern.finditer(text):
            tokens = " ".join(match.group(1).split()).strip(" ,.").split()
            # only in a processor's payment line, whose slot holds a customer:
            # elsewhere a join is a brand's own as often as OCR's - DoorDash,
            # GoDaddy, PayPal
            tokens = (_unglued(tokens) if pattern in (PROCESSOR_PAYMENT, PAYMENT_WITH_ID)
                      else [_GLUED_ID.sub("", t) or t for t in tokens])
            if len(tokens) == 1:
                tokens += _surname_across_double_space(text, match.end(1), tokens[0])
            # cut at the first of the descriptor's own words, not just trailing
            # ones: "KENZIE LEWIS CHECKS PROCESSED"
            for at, token in enumerate(tokens):
                if at and token.strip(".:#").casefold() in _TRAILING:
                    del tokens[at:]
                    break
            name = " ".join(tokens).strip(" ,.")
            # a rail followed by one word is the rail's own vocabulary far more
            # often than a person: "ZELLE CONFIRMATION", "VENMO CASHOUT"
            if not name or len(tokens) < least or not _acceptable(name):
                continue
            if pattern is PAYMENT_WITH_ID and (
                    not _person_shaped(tokens)
                    or any(t.casefold() in _CORPORATE for t in tokens)):
                continue
            found.setdefault(name.casefold(), Party(name, source))
    return list(found.values())


# OCR loses the space inside a name as often as it doubles one: a scanned
# processor report read "WebsitePayment-KimberlyPrice ID:" and "Whitney
# MorganID:", and the name came back as one token, or with the field on it -
# its I read as an l as often as not: "Aubrey GarcialD:".
_GLUED_ID = re.compile(r"(?<=[a-z])[Il1|]D$")
_CASE_JOIN = re.compile(r"(?<=[a-z])(?=[A-Z])")


def _unglued(tokens: list[str]) -> list[str]:
    """``KimberlyPrice`` as two tokens, ``MorganID`` without its field.

    Only a capital straight after a small letter is a join. A surname that
    carries one of its own - McKenzie, DeShawn, LaToya, MacDonald, VanBuren -
    keeps it: a piece of two letters or fewer, or a name particle, is the
    front of the next piece, not a name of its own.
    """
    out: list[str] = []
    for token in tokens:
        token = _GLUED_ID.sub("", token) or token
        pieces: list[str] = []
        for piece in _CASE_JOIN.split(token):
            if pieces and (len(pieces[-1]) <= 2 or pieces[-1].casefold() in _names.PARTICLES):
                pieces[-1] += piece
            else:
                pieces.append(piece)
        out.extend(pieces)
    return out


# OCR doubles a space inside a name as often as it doubles one anywhere:
# "ZELLE TO TANNER  JENSEN" proposed "TANNER" and left the surname. _NAME will
# not cross two spaces - that is a column gap more often than not - so the
# surname is taken here, and only when it ends its cell: the line ends, another
# gap follows, or the descriptor's own words do ("JENSEN  CONF# 1234",
# "JENSEN ON 10/03"). "Brynlee Nelson  Pay Friends" is a two-word capture and
# never gets here.
_DOUBLE_SPACED_TOKEN = re.compile(rf"[ \u00a0]{{2}}({_TOKEN})(?=[^\S\n]{{2,}}|[^\S\n]*$|[^\S\n]+(\S+))",
                                  re.MULTILINE)


def _surname_across_double_space(text: str, at: int, first: str) -> list[str]:
    if first.strip(".:#").casefold() in _TRAILING:
        return []
    found = _DOUBLE_SPACED_TOKEN.match(text, at)
    if not found or found.group(1).strip(".:#").casefold() in _TRAILING:
        return []
    after = found.group(2)
    if after and after.strip(".:#").casefold() not in _TRAILING:
        return []
    # "TRANSFER TO KENZIE  SAVINGS" is the next column, and taking it would
    # sink the name that was found
    if not _acceptable(f"{first} {found.group(1)}"):
        return []
    return [found.group(1)]


# The addressee block: a name on its own line with a street address under it.
# This is how every statement, bill and remittance coupon prints the account
# holder, and it is the only place a statement names them at all.
_STREET_LINE = re.compile(r"^[^\S\n]*\d{1,6}[A-Za-z]?[^\S\n]+[A-Za-z0-9]")
_TRAILING_ZIP = re.compile(r"\s*\b\d{5}(?:-\d{4})?\s*$")
# a ZIP after a state is the address's own - "1234 MAPLE GROVE, LOGAN UT 84321"
_STATE_ZIP = re.compile(r"\b[A-Z]{2}\.?,?[^\S\n]+\d{5}(?:-\d{4})?[^\S\n]*$")


def _is_street(line: str) -> bool:
    """A line that starts with a house number and really is a street.

    Starting with a number and a word is not enough: a tax form's "10
    Adjustments to income" under its "Part II Adjustments" heading proposed
    the heading as a human being.
    """
    if not _STREET_LINE.match(line):
        return False
    # Ending in five digits does not make it one: "2 of: LEGO Classic Large
    # Creative Brick Box 10698" is an invoice's item and its set number, and
    # "Items Ordered" above it was proposed as the account holder.
    # Unless a state comes before it: then the ZIP is the address's, and with
    # it gone "1234 MAPLE GROVE, LOGAN UT" read as nothing at all.
    if caption.is_address(line if _STATE_ZIP.search(line) else _TRAILING_ZIP.sub("", line)):
        return True
    return any(m.category == "street_address" and m.start <= len(line) - len(line.lstrip()) + 1
               for m in patterns.scan(line, enabled=["street_address"]))
# A joint account stacks its holders, one per line, or joins them on one:
#   OAKLEY V WARD                 GERALD W PACKARD AND DIANE M PACKARD
#   JACOB N WARD                  1488 E DESERT ROSE DR
#   12469 W CENTER ST
# Reading only the line directly above the street found Jacob and never Oakley.
ADDRESSEE_LINES = 3
_JOINT = re.compile(r"[^\S\n]+(?:&|AND|and)[^\S\n]+")

# "BRANDON PRITCHARD For account ending in 1234" - the running header on every
# page after the first.
FOR_ACCOUNT = re.compile(
    rf"^[^\S\n]*({_NAME})[^\S\n]+(?i:for\s+account\s+(?:ending|number|no))",
    re.MULTILINE)
# A card statement heads each cardholder's charges with their name, and on a
# business card every employee's card gets its own heading:
# "New Charges  BRIAN ROBERTS Card Ending 2-3164".
CARDHOLDER = re.compile(
    rf"(?m)(?:^[^\S\n]*|(?<=[^\S\n]{{2}}))({_NAME})[^\S\n]+"
    r"(?i:card|acct\.?|account)[^\S\n]+(?i:ending|no\.?|number|#)")

# A PO box under a name is the payee's lockbox, not where anybody lives.
_PO_BOX = re.compile(r"(?i)^\s*(?:P\.?\s?O\.?\s+box|post\s+office\s+box)\b")


def _names_on_line(line: str) -> list[str]:
    """Every name on an addressee line, or nothing if any part is not one."""
    # A name never spans a column gap. A photographed stub read in rows put
    # the employee beside the next column's heading - "KAYLA J RUIZ   Current"
    # - and the two together were proposed as one person, boxed across the
    # row and through the figure under it. The address block is the first
    # cell; what lies past the gap is another column's.
    line = re.split(r"[^\S\n]{3,}", line.strip())[0]
    parts = [" ".join(p.split()).strip(" ,.") for p in _JOINT.split(line.strip())]
    # "Colby & Jess Morgan" - the first holder borrows the shared surname
    if len(parts) == 2 and len(parts[0].split()) == 1 and len(parts[1].split()) >= 2:
        parts[0] = f"{parts[0]} {parts[1].split()[-1]}"
    names = []
    for part in parts:
        if (not part or not re.fullmatch(_NAME, part) or not _acceptable(part)
                or len(part.split()) < 2):
            return []
        # a heading stacked over the holder - "Monthly Statement" over "Tyson J
        # Okafor" once the page is read down its columns
        bare = [t.strip(".,").casefold() for t in part.split()]
        if any(t in _FORM_WORDS or t in ner._NEVER_IN_A_NAME for t in bare):
            return []
        names.append(part)
    return names


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
    for index, line in enumerate(lines):
        # the address line decides whether this is a residence; a PO box under
        # a name is the payee's lockbox, not where anybody lives
        if not _is_street(line) or _PO_BOX.match(line):
            continue
        for above in range(index - 1, max(index - 1 - ADDRESSEE_LINES, -1), -1):
            names = _names_on_line(lines[above])
            # further up the stack only a person-shaped line is a co-holder;
            # the issuer's letterhead sits there too
            if above < index - 1:
                names = [n for n in names if _person_shaped(n.split())]
            if not names:
                break
            for name in names:
                found.setdefault(name.casefold(), Party(name, source, "person"))
    for match in CARDHOLDER.finditer(text):
        name = " ".join(match.group(1).split()).strip(" ,.")
        if _acceptable(name) and _person_shaped(name.split()):
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
    readings = [(pattern, text) for pattern in (EMPLOYER_FIELD, EMPLOYER_BLOCK, PAYROLL_DEPOSIT,
                                                *PAY_DEPOSIT_LINES, PLAN_SPONSOR)]
    # a pay line OCR glued shut is read again with its joins opened
    opened = _opened_pay_lines(text)
    if opened:
        readings.append((PAY_DEPOSIT_LINES[0], opened))
    for pattern, reading in readings:
        for match in pattern.finditer(reading):
            name = _trim_employer(match.group(1))
            if not name or _EMPLOYER_JUNK.match(name):
                continue
            if name.casefold() in ner._NOT_A_NAME or caption.is_address(name):
                continue
            if any(t.strip(".,").casefold() in _ACCOUNT_WORDS for t in name.split()):
                continue
            if pattern is PAY_DEPOSIT_LINES[0] and _earnings_row(reading, match, name):
                continue
            found.setdefault(name.casefold(), Party(name, source, "employer"))
    for name in pay_stub_employers(text):
        found.setdefault(name.casefold(), Party(name, source, "employer"))
    return [party for party in found.values() if not _paid_by_the_account(text, party.name)]


# A scanned statement's pay line with every space lost to OCR -
# "5/31   RedCanyonLandsPayroll2305319913065380CODYBASHWORTH" - is one word,
# and a scan of one may never read the row any other way. The cue glued to
# the name behind it is what marks one: a small letter, then "Payroll".
_GLUED_PAY_CUE = re.compile(r"[a-z](?:Payroll|PAYROLL|Payrll)")
_DIGIT_JOIN = re.compile(r"(?<=[A-Za-z])(?=\d)|(?<=\d)(?=[A-Za-z])")


def _opened_pay_lines(text: str) -> str:
    """The glued pay lines alone, each join opened: "Red Canyon Lands Payroll
    2305319913065380 CODYBASHWORTH". Empty when there are none."""
    lines = []
    for line in text.splitlines():
        if not _GLUED_PAY_CUE.search(line):
            continue
        words = [" ".join(_unglued(_DIGIT_JOIN.split(word))) if _GLUED_PAY_CUE.search(word)
                 else word for word in line.split(" ")]
        lines.append(" ".join(words))
    return "\n".join(lines)


# what marks a line as the bank's record of a deposit rather than a stub's row
_BANK_LINE_HEAD = re.compile(r"(?i)\b(?:ach|deposit|dep|credit)\b|\d")


def _earnings_row(text: str, match: re.Match, name: str) -> bool:
    """A pay stub's "<kind> Salary" or "<kind> Net Pay" row, not a bank line.

    A bank's descriptor carries the employer and the word salary too - but it
    carries it after a posting date or an ACH lead, and an employer it names in
    one word is rare. A stub's row is one word, the cue and the figure:
    "Merit Salary  2,500.00", "Employee Net Pay  1,900.00". "Payroll" and
    "Dir Dep" are never a stub's row heading, so only salary and net pay are
    held to this.
    """
    cue = text[match.end(1):match.end()]
    if not re.search(r"(?i)salary|net[^\S\n]+pay", cue):
        return False
    if len(name.split()) >= 2 or _is_org(name):
        return False
    head = text[text.rfind("\n", 0, match.start(1)) + 1:match.start(1)]
    return not _BANK_LINE_HEAD.search(head)


# a figure printed as money going out: "-$139.27", "-139.27", "139.27-"
_OUTGOING = re.compile(r"(?<![\w.,])-[^\S\n]?\$?\d[\d,]*\.\d{2}\b|\b\d[\d,]*\.\d{2}-(?!\d)")


def _paid_by_the_account(text: str, name: str) -> bool:
    """True when the statement also sends money to ``name``.

    An employer only ever pays in. A payment app, a brokerage or the client's
    own other account shows up as a "Direct deposit - Pingo" line and, on the
    same statement, as "Pingo - ACH  -$139.27" going the other way - money
    moving both directions is a transfer partner, not a payroll.

    Only a transfer going out counts. A card purchase is not one: the client
    who works at Home Depot also shops there, and "THE HOME DEPOT #4401 WEST
    JORDAN UT  -$33.12" beside "ACH DEPOSIT HOME DEPOT PAYROLL" dropped the
    employer - the worst thing this module can do.
    """
    folded = name.casefold()
    return any(folded in line.casefold() and _OUTGOING.search(line)
               and not _PAID_IN.search(line)
               and _TRANSFER_OUT.search(line) and not _CARD_PURCHASE.search(line)
               for line in text.splitlines())


# how a statement describes money it sent rather than spent
# Funding a wallet is a transfer too: "ORCHARD CASH ADD MONEY  -$50.00" is
# the client topping up an app, and without it the app went back to being
# proposed as an employer from its own cash-out deposits.
_TRANSFER_OUT = re.compile(
    r"(?i)\b(?:ach|transfer|xfer|pmt|payment|p2p|withdrawal|cash[^\S\n]?out|to|"
    r"add[^\S\n]+money|add[^\S\n]+funds|top[^\S\n-]?up|load(?:ed)?|funding)\b")
# how it describes a card swipe: "CHECKCARD 1002", "POS PURCHASE", "DEBIT CARD
# 4401". Not a "#1234" store number - a transfer's "REF # 12345678" is the same
# shape, and the transfer word is already required above.
_CARD_PURCHASE = re.compile(
    r"(?i)\b(?:check[^\S\n]?card|pos|purchase|debit[^\S\n]+card|card[^\S\n]+\d{4})\b")


# A line that says it is pay is money coming in, whatever its sign: a scanned
# HSA statement's "PAYROLL CONTRIBUTION NU SKIN ENTERPRISE  -$236.01" carried
# a minus OCR made out of a table rule, and the employer was dropped as a
# transfer partner.
_PAID_IN = re.compile(r"(?i)pay\s*roll|direct\s*dep|dir\s*dep|salary|wages|contribution")


# A pay stub names the employer in its letterhead and nowhere else - no label,
# because the whole document is the employer's. Six payroll systems, all the
# same shape: the name, usually its street and ZIP, then the stub's own title.
#   RED ROCK DENTAL ARTS             Cache County School District
#   595 S BLUFF ST, ST GEORGE UT     2063 N 1200 E, Logan UT 84341
#   EARNINGS STATEMENT               Earnings Statement
# The gaps are optional: OCR of a photographed stub drops the space in small
# caps - "NETPAY", "GROSSEARNINGS", "CHECKDATE" - and a stub that was never
# recognised as one kept its letterhead employer.
_PAY_STUB = re.compile(r"(?i)\b(?:gross\s*(?:pay|earnings)|net\s*pay)\b")
_PAY_STUB_TOO = re.compile(r"(?i)\b(?:pay\s*(?:date|period|day)|check\s*date|period\s*(?:begin|end))")
_STUB_TITLE = re.compile(
    r"(?i)^[^\S\n]*(?:earnings\s*statement|pay\s*statement|statement\s+of\s+earnings"
    r"|employee\s+pay\s*stub|pay\s*stub|payroll\s*advice|direct\s+deposit\s+advice)[^\S\n]*$")
_CITY_ZIP = re.compile(r"\b[A-Z]{2}\.?[^\S\n]+\d{5}(?:-\d{4})?[^\S\n]*$", re.IGNORECASE)
LETTERHEAD_LINES = 4


def _letterhead_name(line: str) -> str:
    line = " ".join(line.split()).strip(" ,.;:")
    tokens = line.split()
    if not 1 <= len(tokens) <= 8 or any(ch.isdigit() for ch in line):
        return ""
    if "@" in line or _STUB_TITLE.match(line) or caption.is_address(line):
        return ""
    bare = [t.strip(".,'’").casefold() for t in tokens]
    if bare[0] in _EMP_NOT_A_NAME or bare[0] in _EMP_NOISE:
        return ""
    # A photographed stub read down its columns puts a tax or leave row's label
    # straight over the title - "Social Security Tax", "Sick/PTO" - and a
    # column gap cuts the town out of the employee's street line. Neither is a
    # letterhead, and the label proposed as an employer was boxed in every row.
    # A business that says it is one keeps its name: "Liberty Tax Service LLC".
    words = [w for t in bare for w in re.split(r"[/\-]", t) if w]
    if not _is_org(line) and any(w in _STUB_ROW_WORDS or w.endswith(_STUB_ROW_ENDINGS)
                                 for w in words):
        return ""
    if places.known(line):
        return ""
    return line


# the vocabulary of a stub's tax, deduction and leave rows - never a business's
_STUB_ROW_WORDS = frozenset((
    "tax", "taxes", "withholding", "withholdings", "medicare", "fica", "oasdi", "sdi",
    "futa", "suta", "pto", "sick", "vacation", "deduction", "deductions", "earnings",
    "hours", "rate", "overtime", "ytd", "current", "gross", "net", "garnishment",
    "reimbursement", "allowances", "exemptions", "accrued", "available", "used"))
# the same words OCR glued to the one before: "SocialSecurityTax", "UTStateWithholding"
_STUB_ROW_ENDINGS = ("tax", "taxes", "withholding", "withholdings")


def pay_stub_employers(text: str) -> list[str]:
    """The employer a pay stub's letterhead names."""
    if not (_PAY_STUB.search(text) and _PAY_STUB_TOO.search(text)):
        return []
    # a column gap splits a line into cells, and a letterhead shares its line
    # with the stub's title as often as not: "RED ROCK DENTAL ARTS    EARNINGS
    # STATEMENT" is two cells, the employer and the title
    lines: list[str] = []
    # the whole line each cell came from: OCR spaces a typed name as widely as
    # a column gap - "MEGAN L   HOLBROOK" over the employee's street - and
    # the surname alone, a cell of its own, was taken for the employer
    whole: list[str] = []
    on_address: list[bool] = []
    for line in text.splitlines():
        cells = [cell.strip() for cell in (re.split(r"[^\S\n]{2,}", line.strip()) or [""])]
        # the title is a cell of its own, not part of anybody's name
        joined = " ".join(cell for cell in cells if not _STUB_TITLE.match(cell))
        # a cell of the street line - "1321 S 1100 E Unit 4,   Salt Lake
        # City,   UT 84105" is three cells - is the street line too
        address = any(_STREET_LINE.match(cell) or _CITY_ZIP.search(cell) for cell in cells)
        for cell in cells:
            lines.append(cell)
            whole.append(joined)
            on_address.append(address)
    found: list[str] = []
    for index, line in enumerate(lines):
        if not _STUB_TITLE.match(line):
            continue
        # walk up past the employer's address to its name
        for above in range(index - 1, max(index - 1 - LETTERHEAD_LINES, -1), -1):
            candidate = lines[above]
            if not candidate:
                continue
            if on_address[above]:
                continue
            name = _letterhead_name(candidate)
            # the employee's own name sits between the employer and the title
            # on some stubs - "New Hope Pacific Islander Church / SAMUEL T
            # FONOTI / Employee Pay Stub" - so a person is stepped over, not
            # taken and not a reason to stop
            if name and not _is_org(name) and (
                    _person_shaped(name.split())
                    or (whole[above] != name and not _is_org(whole[above])
                        and _person_shaped(whole[above].split()))):
                continue
            if name:
                found.append(name)
            break
    # the first lines of the stub: a name over a street-and-ZIP line, with at
    # most one "Label: value" line between them - and a "Period Beginning:"
    # sharing the name's own line when the stub was typed up in Word
    head = lines[:LETTERHEAD_LINES + 6]
    for index, line in enumerate(head):
        name = _letterhead_name(_TRAILING_LABEL.sub("", line))
        if not name:
            continue
        below = [l for l in head[index + 1:index + 3]]
        if any(_STREET_LINE.match(l) and _CITY_ZIP.search(l) for l in below):
            if not _person_shaped(name.split()) or _is_org(name):
                found.append(name)
            break
    return found


# "Cache County School District Period Beginning: 01/01/2021" - a label and its
# value run on after the name
_TRAILING_LABEL = re.compile(
    r"(?i)\s+(?:pay\s+)?(?:period\s+(?:beginning|ending|begin|end|start)|pay\s+date"
    r"|check\s+(?:date|no\.?|number)|advice\s+(?:no\.?|number|date)|employee(?:\s+id)?"
    r"|ssn|date|file|dept)\s*[:#].*$")


def _is_org(name: str) -> bool:
    return any(t.strip(".,'’").casefold() in ner._ORG_MARKERS for t in name.split())


# Money that arrives by "Direct deposit" from a payment app or a brokerage is a
# transfer of the client's own money, not wages. Real names only: a list of the
# test corpus's invented apps would pass its own test and nothing else.
_NOT_AN_EMPLOYER = re.compile(
    r"(?i)^(?:venmo|pay\s?pal|zelle|cash\s?app|square\s+cash|apple\s+cash|apple\s+card"
    r"|google\s+(?:pay|wallet)|coinbase|robinhood|chime|stripe|wise|revolut|acorns|stash"
    r"|betterment|wealthfront|fidelity|vanguard|schwab|etrade|e\*trade|sofi|ally|marcus"
    r"|irs|us\s+treasury|treas(?:ury)?|state\s+tax|tax\s+refund)\b")


def _trim_employer(raw: str) -> str:
    """The employer's name with the descriptor's own words shaved off."""
    tokens = " ".join(raw.split()).strip(" ,.;:-").split()
    while tokens and tokens[0].casefold().strip(":") in _EMP_NOISE:
        tokens.pop(0)
    while tokens and (tokens[-1].casefold().strip(":") in _EMP_NOISE
                      or tokens[-1].isdigit()):
        tokens.pop()
    if not tokens or all(t.casefold().strip(".,()") in _EMP_NOT_A_NAME | _EMP_GENERIC
                         for t in tokens):
        return ""
    if tokens[0].casefold() in _EMP_NOT_A_NAME:
        return ""
    name = " ".join(tokens)
    # "Hourly Rate 25.00 Salary" - a figure is not part of anybody's name, and a
    # value with no letters at all is a column of them
    if _DECIMAL.search(name) or len(re.findall(r"[A-Za-z]", name)) < 2:
        return ""
    if _NOT_AN_EMPLOYER.match(name):
        return ""
    return name


# A business account is addressed to the business: "HARRINGTON ENTERPRISES LLC"
# over the owner's street. Only a name carrying an entity suffix, and never a
# financial institution's - a servicer's return address is a company over a
# street too, and it is a merchant the reader needs.
_ENTITY_SUFFIX = re.compile(
    r"(?i)(?:,?\s+(?:llc|l\.l\.c\.|inc\.?|incorporated|corp\.?|corporation|co\.?|company"
    r"|pllc|p\.l\.l\.c\.|llp|lp|ltd\.?|pc|p\.c\.)|\s+enterprises|\s+&\s+sons)\s*$")
_INSTITUTION = re.compile(
    r"(?i)\b(?:bank|bancorp(?:oration)?|credit\s+union|cu|fcu|federal|savings|trust|financial|mortgage"
    r"|servicing|lending|loans?|capital|insurance|assurance|mutual|brokerage|securities"
    r"|investments?|card|services|n\.a\.?)\b")
# a P&L or a balance sheet is headed with the business it belongs to
BOOKS_TITLE = re.compile(
    # one whitespace quantifier per gap, not two side by side - see _COLUMN_START
    r"(?im)^[^\S\n]*([^\n]{3,60}?)(?:[^\S\n]*\n[^\S\n]*|[^\S\n]+)"
    r"(?:profit\s*(?:&|and)\s*loss|income\s+statement|balance\s+sheet"
    r"|statement\s+of\s+(?:operations|cash\s+flows|income))\b")


_ORG_RUN = r"[A-Z0-9&][A-Za-z0-9&.,'’\-]*(?:[ \u00a0][A-Za-z0-9&][A-Za-z0-9&.,'’\-]*){0,6}"
OWNED_BUSINESS = re.compile(
    r"(?:(?i:\b(?:business(?:[^\S\n]+name)?|company(?:[^\S\n]+name)?|d/?b/?a|doing[^\S\n]+business[^\S\n]+as"
    r"|entity(?:[^\S\n]+name)?))(?:[^\S\n]*:[^\S\n]*|[^\S\n]{2,}|[^\S\n]*\n[^\S\n]*)"
    r"|(?i:\b(?:interest|ownership|owner|member|shareholder|partner)(?:[^\S\n]+(?:in|of))?)[^\S\n]+)"
    rf"({_ORG_RUN})")


_BOOKS_NOT_A_NAME = frozenset({
    "statement", "report", "summary", "monthly", "quarterly", "annual", "year",
    "comparative", "standard", "detail", "detailed", "consolidated", "accrual",
    "cash", "basis", "total", "net", "page", "period", "ytd", "the", "of",
})


# The kind of account a business line names after the business - a law
# firm's "RIDGEVIEW FAMILY LAW PLLC IOLTA TRUST ACCOUNT" - which hides the
# entity suffix at the end of the name, and whose "trust" is not a bank's.
_ACCOUNT_KIND = re.compile(
    r"(?i)[\s,]+(?:(?:iolta|client|operating|escrow|payroll|business|general)\s+)*"
    r"(?:trust\s+)?account\s*$")
_ACCOUNT_DETAIL = re.compile(r"(?i)^(?:(?:business|personal|merchant)\s+account|account\s+(?:id|number|no\.?|#)\b.*)$")


def businesses(text: str, source: str = "") -> list[Party]:
    """The client's own business, where a statement or its books name it."""
    found: dict[str, Party] = {}

    def put(name: str) -> None:
        name = " ".join(name.split()).strip(" ,.;:")
        name = _ACCOUNT_KIND.sub("", name).strip(" ,.;:")
        if len(name) < 4 or any(ch.isdigit() for ch in name) or "@" in name:
            return
        if _INSTITUTION.search(name) or caption.is_address(name):
            return
        found.setdefault(name.casefold(), Party(name, source, "organization"))

    lines = text.splitlines()
    for index, line in enumerate(lines):
        if not _is_street(line) or _PO_BOX.match(line):
            continue
        for above in range(index - 1, max(index - 2 - ADDRESSEE_LINES, -1), -1):
            candidate = lines[above].strip()
            # a payment processor stacks the account's own details between the
            # business and its street: "Account ID: SNBK...", a billing email,
            # "Business account"
            if "@" in candidate or _ACCOUNT_DETAIL.search(candidate):
                continue
            if _ENTITY_SUFFIX.search(_ACCOUNT_KIND.sub("", candidate)):
                put(candidate)
                continue
            if not _names_on_line(candidate):
                break
    for match in re.finditer(rf"\bINDN:[^\S\n]*([^\n]{{3,60}}?)(?=[^\S\n]+CO[^\S\n]+ID\b)", text):
        if _ENTITY_SUFFIX.search(match.group(1)):
            put(match.group(1))
    # A set of books is the business's own - "Lindquist Family Dairy & Hay
    # Profit and Loss" - so its heading needs no entity suffix to count.
    for match in BOOKS_TITLE.finditer(text):
        heading = " ".join(match.group(1).split())
        tokens = heading.split()
        if 1 <= len(tokens) <= 8 and not any(t.casefold() in _BOOKS_NOT_A_NAME for t in tokens):
            put(heading)
    # a financial statement's "Business  Red Canyon Landscaping Inc" field, and
    # an asset line's "(100% interest Redstone Auto Repair LLC)"
    for match in OWNED_BUSINESS.finditer(text):
        name = match.group(1)
        if _ENTITY_SUFFIX.search(name):
            put(name)
    return list(found.values())


# The people a form or a statement names beside a label rather than over an
# address: "Employee: JENNA R HARRINGTON", "Shareholder CODY B ASHWORTH",
# "Names KYLE D HARRINGTON & JENNA R HARRINGTON", "Your first name and middle
# initial / Last name" with the name on the next line. The label may end in a
# colon, a column gap or a line break; the name must look like a person's.
_PERSON_LABEL = (
    r"(?:employee(?:'s)?(?:[^\S\n]+name)?|name\(s\)|names?|participant|borrower|co-borrower"
    r"|account[^\S\n]+holders?|cardholder|card[^\S\n]+member|member[^\S\n]+name|owners?"
    r"|primary|joint[^\S\n]+owner|insured|policy[^\S\n]*holder|applicant|co-applicant|taxpayer"
    r"|spouse(?:'s)?(?:[^\S\n]+name)?|shareholder|partner|trustee|recipient(?:'s)?(?:[^\S\n]+name)?"
    r"|beneficiary|patient|student|guarantor|subscriber|lender|payee|last[^\S\n]+name"
    r"|tenants?|lessees?|renters?|occupants?|resident|landlord|lessor"
    r"|(?:pay[^\S\n]+to[^\S\n]+the[^\S\n]+)?order[^\S\n]+of|prepared[^\S\n]+for|activity[^\S\n]+for"
    r"|statement[^\S\n]+for|checking[^\S\n]+for|savings[^\S\n]+for)")
LABELLED_PERSON = re.compile(
    rf"(?im)\b{_PERSON_LABEL}(?:[^\S\n]*[:\-][^\S\n]*\n?[^\S\n]*|[^\S\n]*\n[^\S\n]*|[^\S\n]+)"
    rf"((?-i:{_NAME})(?:{_SP}(?:&|and|AND){_SP}(?-i:{_NAME}))?)")
# the words a form prints in the slot where a name might be
_FORM_WORDS = frozenset({
    "residence", "business", "address", "date", "signature", "title", "phone",
    "email", "number", "amount", "type", "status", "description", "balance",
    "account", "ownership", "percent", "total", "social", "security", "ssn",
    "id", "information", "personal", "summary", "detail", "details", "period",
    "distribution", "savings", "checking", "contributions", "employer", "income",
    "name", "names", "lending", "loan", "bank", "report", "statement",
    "lease", "rent", "deposit", "unit", "move", "start", "end",
    # a label word opening an ordinary phrase: "Owner Occupied Property",
    # "Lender Fees Paid", "Landlord Tenant Act", "Patient Portal Login"
    "occupied", "property", "fees", "paid", "act", "portal", "login",
})
# "NAKAMURA, DEREK L" under a "NAME (LAST, FIRST, MI)" heading
LAST_FIRST = re.compile(r"(?m)^[^\S\n]*([A-Z][A-Za-z'’\-]+),[ ]([A-Z][A-Za-z'’\-]+)(?:[ ]([A-Z])\.?)?[^\S\n]*$")
_LAST_FIRST_CUE = re.compile(r"(?i)\blast,\s*first")
# "MARCO A RUIZ TTEE", "Jonathan E Whitfield #3813"
AFTER_NAME = re.compile(rf"({_NAME})[ ](?:TTEE|TRUSTEE|CUST|POA|JTWROS)\b")
# A card tail after the cardholder - but a store prints its number the same
# way ("SUMMIT HOME #0845", "WALGREENS #3805"), so only First M Last qualifies.
CARD_TAIL = re.compile(rf"({_TOKEN}{_SP}[A-Z]\.?{_SP}{_TOKEN})[ ]#\d{{4}}\b")


def labelled_people(text: str, source: str = "") -> list[Party]:
    """People named beside a label: the employee, the taxpayer, the trustee."""
    found: dict[str, Party] = {}

    def window(tokens: list[str], from_end: bool) -> list[str]:
        """The person-shaped run at the start (or end) of a longer capture:
        "CODY B ASHWORTH Ownership", "Number MARCO A RUIZ"."""
        for size in (3, 2):
            if len(tokens) < size:
                continue
            part = tokens[-size:] if from_end else tokens[:size]
            if not _person_shaped(part):
                continue
            if size == len(tokens) or size == 3:
                return part          # the whole capture, or First M Last
            # a bare two-word window is only trusted when what it cut off is
            # the form's own vocabulary: "ROCKY MOUNTAIN POWER" is a company
            cut = tokens[-size - 1] if from_end else tokens[size]
            if cut.strip(".,:").casefold() in (_FORM_WORDS | _TRAILING | ner._NEVER_IN_A_NAME):
                return part
        return []

    def put(raw: str, from_end: bool = False) -> None:
        pieces = [piece.strip(" ,.").split() for piece in _JOINT.split(" ".join(raw.split()))]
        people = [window(tokens, from_end) for tokens in pieces]
        # "Colby & Jess Morgan" - the first borrows the second's surname, once
        # the second has been cut down to a name ("Jess Morgan Lease end")
        if len(pieces) == 2 and len(pieces[0]) == 1 and people[1]:
            people[0] = [pieces[0][0], people[1][-1]]
        for tokens in people:
            name = " ".join(tokens)
            if not tokens or not _acceptable(name):
                continue
            bare = [t.strip(".,").casefold() for t in tokens]
            if any(t in ner._NEVER_IN_A_NAME or t in _FORM_WORDS for t in bare):
                continue
            found.setdefault(name.casefold(), Party(name, source, "person"))

    for match in LABELLED_PERSON.finditer(text):
        put(match.group(1))
    for match in AFTER_NAME.finditer(text):
        put(match.group(1), from_end=True)
    for match in CARD_TAIL.finditer(text):
        put(match.group(1), from_end=True)
    if _LAST_FIRST_CUE.search(text):
        for match in LAST_FIRST.finditer(text):
            last, first, initial = match.group(1), match.group(2), match.group(3)
            put(f"{first} {initial} {last}" if initial else f"{first} {last}")
    return list(found.values())


def receivers(text: str, source: str = "") -> list[Party]:
    """The account holder, as the ACH record names them on a deposit line."""
    found: dict[str, Party] = {}
    for pattern in RECEIVER:
        for match in pattern.finditer(text):
            name = " ".join(match.group(1).split()).strip(" ,.")
            if len(name.split()) >= 2 and _acceptable(name):
                found.setdefault(name.casefold(), Party(name, source, "person"))
    for match in RECEIVER_LAST_FIRST.finditer(text):
        last, first, initial = match.groups()
        name = f"{first} {initial} {last}" if initial else f"{first} {last}"
        if _acceptable(name):
            found.setdefault(name.casefold(), Party(name, source, "person"))
    return list(found.values())


# A payment app's activity screen, photographed or screenshotted: each row a
# person, under them what the payment was - the app's word for it or the
# payer's own note - and a signed amount at the right. No table, no labels, no
# app name to key on (a real app's or an invented one's), so the shape is the
# rule: a feed of signed amounts, and a person-shaped line whose next line is
# a payment verb or a lowercase note. A merchant's row says "Purchase", the
# app's own rows say "Add Money" or repeat their own name - neither passes.
# The sign may be glued to the name - OCR read "Tanner Jensen  -$92.76" as
# "Tanner Jensen-$92.76" - so only a digit or a dollar sign before it means it
# is not a sign. A hyphenated name ("Anne-Marie") has no "$" after its hyphen.
_SIGNED_AMOUNT = re.compile(r"(?<![\d$])[+\-−]\s?\$\s?\d[\d,]*\.?\d*")
_FEED_VERB = re.compile(
    r"(?i)^(?:sent|received|request(?:ed)?|paid(?:\s+you)?|you\s+paid|"
    r"charged?|split|money\s+(?:sent|received))\b")
# the payment's kind, as a wallet app prints it under the payee: "Mobile
# Payment", "Express Checkout Payment", "PreApproved Payment Bill User"
_FEED_KIND = re.compile(r"(?i)^(?:[A-Za-z]+\s+){0,3}payment\b")
# a given name may carry one inner capital - McKenzie, DeShawn, LaToya - or be
# two joined by a hyphen, Anne-Marie
_FEED_PERSON = (r"[A-Z][a-z]{0,3}[A-Z]?[a-z'’\-]+(?:-[A-Z][a-z'’]+)?(?:\s+[A-Z]\.?)?"
                r"(?:\s+[A-Z][A-Za-z'’\-]+){1,2}")
_FEED_NAME = re.compile(rf"^{_FEED_PERSON}$")
# Venmo's layout: who paid whom on the row itself, the note under it - "Tanner
# Jensen paid You", "You paid Dallin Cox", "Rachel Pratt requested $20.00 from
# You". The verb is on the name's own line, so the next-line test never fired.
_FEED_PAIR = re.compile(
    rf"^(You|{_FEED_PERSON})\s+(?:paid|charged|requested|sent)\s+"
    rf"(?:\$[\d,]+(?:\.\d+)?\s+)?(?:(?:from|to)\s+)?(You|{_FEED_PERSON})\b")
# A merchant row is the same shape as a person's on a wallet app - "Home Depot
# -$147.57" over "Mobile Payment" is "Ashley Hansen -$147.57" over "Mobile
# Payment" - so a trade's own vocabulary decides it. Generic words for what a
# shop sells or is, never a brand, and none a common surname: "Baker", "Cook",
# "Kitchen" and "Bell" are people often enough that they stay out.
# Trade words only - never one that is also a surname a payee could carry
# ("Garden", "Tree", "Baker", "Cook", "Bell" are left out): missing a person
# costs more than proposing a merchant the operator can untick.
_MERCHANT_WORDS = frozenset({
    "depot", "food", "foods", "grocery", "groceries", "wholesale", "express",
    "market", "mart", "supermarket", "outlet", "outlets", "pharmacy", "drug",
    "drugs", "grill", "cafe", "café", "pizza", "pizzeria",
    "taco", "tacos", "burger", "burgers", "coffee", "bakery", "dollar", "buy",
    "supply", "supplies", "hardware", "auto", "automotive", "motors",
    "fitness", "salon", "liquor", "pet", "pets", "furniture", "station", "gas",
    "fuel", "pub", "diner", "restaurant", "deli", "sushi", "bbq", "steakhouse",
    "cinema", "cinemas", "theater", "theatre", "toys", "sports", "wireless",
    "warehouse", "boutique", "chicken", "donuts", "bagels", "cleaners", "laundry",
    "parking", "airlines", "airline", "hotel", "hotels", "motel", "resort",
    "tire", "tires", "nails", "spa", "beauty", "books", "bookstore", "electronics",
})
# what a feed prints under a merchant's row, which marks that name as one
# wherever else on the screen it appears
_FEED_PURCHASE = re.compile(r"(?i)^(?:card\s+)?(?:purchase|refund|return|pos)\b")
FEED_MIN_SIGNED = 2


def _feed_person(name: str, merchants: set[str]) -> bool:
    if name.casefold() in merchants:
        return False
    # "Rewards Summary" over "earned this period" on a card statement
    if any(t.strip(".,'’").casefold() in _MERCHANT_WORDS | _FORM_WORDS for t in name.split()):
        return False
    return _acceptable(name) and caption.plausible_name(name)


# A business wallet prints the date before the payment's kind - "Jun 8 -
# Website Payment" - and OCR turns the dash into "+" or ":" ("Jun 6+:").
# A screenshot separates them with a middle dot ("May 8·General Payment"),
# and OCR reads an 8 as "&" ("May & - General Payment") - either way the
# payer above it shipped.
_FEED_DATE = re.compile(
    r"(?i)^(?:(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?[^\S\n]*[\d&]{1,2}"
    r"|\d{1,2}/\d{1,2}(?:/\d{2,4})?)[^\S\n]*[-–—+:,.·•]*[^\S\n]*")
# A wallet draws each row's avatar as the payer's initial, and a screenshot's
# row reading puts it in front of the name: "B   Brynlee Long   +$226.00".
_AVATAR = re.compile(r"^([A-Z])\s+(?=\1[a-z])")
_ONLY_AMOUNT = re.compile(r"^[+\-−]?\s?\$\s?\d[\d,]*(?:\.\d{2})?$")


def _feed_below(lines: list[str], index: int) -> str:
    """What the row says the payment was: the next line, past an amount that
    wrapped onto a line of its own, and past a leading date."""
    for below in lines[index + 1:index + 3]:
        if not below:
            return ""               # a blank line ends the row
        if not _ONLY_AMOUNT.match(below):
            return _FEED_DATE.sub("", below, count=1)
    return ""


def activity_feed(text: str, source: str = "") -> list[Party]:
    """The people on a payment app's activity screen - see above."""
    if len(_SIGNED_AMOUNT.findall(text)) < FEED_MIN_SIGNED:
        return []
    lines = [" ".join(line.split()) for line in text.splitlines()]
    # the name's own cell: a row reading carries the amount beside it
    cells = [_AVATAR.sub("", _SIGNED_AMOUNT.split(line)[0].strip()) for line in lines]
    merchants = {cells[i].casefold() for i in range(len(lines) - 1)
                 if _FEED_PURCHASE.match(lines[i + 1])}
    # an entry whose kind is its own name is the app's own line - "Daily Cash"
    # over "Daily Cash", the cash-back row - never a person
    merchants |= {cells[i].casefold() for i in range(len(lines) - 1)
                  if cells[i] and cells[i] == lines[i + 1]}
    found: dict[str, Party] = {}
    for index, line in enumerate(lines):
        pair = _FEED_PAIR.match(cells[index])
        if pair:
            for name in pair.groups():
                if name != "You" and _feed_person(name, merchants):
                    found.setdefault(name.casefold(), Party(name, source, "person"))
            continue
        if index + 1 >= len(lines):
            continue
        name = cells[index]
        below = _feed_below(lines, index)
        if not _FEED_NAME.match(name) or not below:
            continue
        if not (_FEED_VERB.match(below) or _FEED_KIND.match(below) or below[:1].islower()):
            continue
        if _feed_person(name, merchants):
            found.setdefault(name.casefold(), Party(name, source, "person"))
    return list(found.values())


# A payment app's statement is a ledger with a From and a To column, one end
# of every row the account holder and the other the person paid. A scanned
# one loses its table - no column comes back as a column, so the payee
# heading never applies - and each row is read across instead:
#   ID   Datetime   Type   Status   Note   From   To   Amount
#   9008223054395262090   Leilani Fonoti   Anthony Garcia   +$138.50
#   Payment   Complete   utilities split   Leilani Fonoti   Jennifer Hernandez
# Under that heading, two person-shaped cells side by side are the two ends
# of a transfer. Side by side, not one alone: a merchant is a single cell
# beside the amount - "Leilani Fonoti   Etsy.Com- Vintagefinds" - and a store
# with a two-word name would pass a lone-cell test.
_LEDGER_HEAD = re.compile(r"(?im)(?:^|[^\S\n]{2,})from[^\S\n]{2,}to(?:[^\S\n]{2,}|[^\S\n]*$)")
_CELL_GAP = re.compile(r"[^\S\n]{2,}")
# what a scan leaves at a cell's edges: a stray quote, a rule read as "~~"
_CELL_JUNK = " \t\"'“”‘’~_=—–-|"


def ledger_parties(text: str, source: str = "") -> list[Party]:
    """Both ends of each row of a From / To ledger read across - see above."""
    if not _LEDGER_HEAD.search(text):
        return []
    found: dict[str, Party] = {}
    folded = " ".join(text.split()).casefold()

    def opened(cell: str) -> str:
        # OCR glues the holder's name shut in one cell as often as not -
        # "Anthony Walker   AlyssaValdez" - and the pair was lost with it. A
        # glued cell is read apart only when the page prints the same name
        # with its space somewhere else, which the holder's always is; a
        # brand written as one word ("DoorDash") never is.
        if " " in cell:
            return cell
        apart = " ".join(_unglued([cell]))
        return apart if apart != cell and apart.casefold() in folded else cell

    for line in text.splitlines():
        cells = [opened(_SIGNED_AMOUNT.split(cell)[0].strip(_CELL_JUNK))
                 for cell in _CELL_GAP.split(line.strip())]
        # the Type column's own words sit beside the From column too:
        # "Standard Transfer   Emily Thompson"
        people = [bool(_FEED_NAME.match(cell)) and _feed_person(cell, set())
                  and not {t.casefold() for t in cell.split()} & _TRAILING
                  for cell in cells]
        for at in range(len(cells) - 1):
            if people[at] and people[at + 1]:
                for name in cells[at:at + 2]:
                    found.setdefault(name.casefold(), Party(name, source, "person"))
    return list(found.values())


# A bank's Zelle activity prints the direction as a cell of its own and the
# other party in the next one: "06/20/2025   Received   KELSEY AOKI
# (801)555-0110   babysitting   Completed   $58.54". There is no "from" for
# the descriptor patterns to anchor on. OCR misreads the verb ("Recewed") and,
# off a phone photo, loses the space inside an all-capitals name -
# "KELSEYAOKI" - where no rule can tell the given name from the surname. The
# run is proposed as printed: ticked, it is the word on the page, which is
# what gets covered.
_P2P_DIRECTION = re.compile(
    r"(?i)^(?:sent|rec[eiaow]{1,3}[vwu]?e?d|request(?:ed)?|paid|you\s+(?:sent|received))$")
GLUED_NAME_LETTERS = 8
_MONEY = re.compile(r"\$[^\S\n]?\d")


def direction_rows(text: str, source: str = "") -> list[Party]:
    """The party in the cell after a Sent / Received cell - see above."""
    found: dict[str, Party] = {}
    for line in text.splitlines():
        if not _MONEY.search(line):
            continue
        cells = [cell.strip(_CELL_JUNK) for cell in _CELL_GAP.split(line.strip())]
        for at in range(len(cells) - 1):
            if not _P2P_DIRECTION.match(cells[at]):
                continue
            name = cells[at + 1]
            tokens = _unglued(name.split())
            if re.fullmatch(_NAME, name) and _person_shaped(tokens):
                name = " ".join(tokens)
            elif not (name.isalpha() and name.isupper() and len(name) >= GLUED_NAME_LETTERS):
                continue
            bare = {t.casefold() for t in name.split()}
            if not _acceptable(name) or bare & (_TRAILING | _FORM_WORDS | ner._NEVER_IN_A_NAME):
                continue
            found.setdefault(name.casefold(), Party(name, source, "person"))
    return list(found.values())


def harvest_documents(regions: dict[str, str],
                      tables: dict[str, list] | None = None,
                      avoid: frozenset[str] = frozenset(),
                      feeds: dict[str, str] | None = None) -> list[Party]:
    """Counterparties across the batch, minus anybody already spoken for.

    ``feeds`` is the page's own readings, without the column readings: an
    activity feed pairs a name with the line under it, and down a table column
    the line under a cell is the next row's.
    """
    tables = tables or {}
    merged: dict[str, Party] = {}
    for source, text in regions.items():
        page = feeds.get(source, "") if feeds is not None else text
        for party in [*harvest(text, source),
                      *from_tables(tables.get(source, ()), source),
                      *employers_from_tables(tables.get(source, ()), source),
                      *employers(text, source),
                      *account_holders(text, source),
                      *receivers(text, source),
                      *labelled_people(text, source),
                      *activity_feed(page, source),
                      *ledger_parties(page, source),
                      *direction_rows(page, source),
                      *businesses(text, source)]:
            if party.key in avoid:
                continue
            merged.setdefault(party.key, party)
    return list(merged.values())
