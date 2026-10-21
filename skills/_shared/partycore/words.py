"""Word lists from the Anonymizer's ``redactor/ner.py`` - the only part of
that module the skill needs (its spaCy model is never used here)."""

from __future__ import annotations

# Tokens that mean a string really is an organization, however person-shaped
# the rest of it looks.
_ORG_MARKERS = frozenset({
    "llc", "llp", "pllc", "pc", "inc", "corp", "co", "company", "ltd",
    "bank", "credit", "union", "church", "school", "district", "university",
    "college", "hospital", "clinic", "court", "county", "city", "state",
    "department", "dept", "agency", "bureau", "associates", "group", "firm",
    "partners", "partnership", "trust", "foundation", "services", "service",
    "customer", "insurance",
    "realty", "properties", "enterprises", "industries", "&",
    # medical and dental practices, which the model reads as people: a real
    # decree proposed "Cascade Pediatric Dentistry" and "Wasatch Valley Pediatrics"
    # as persons, which would have given each an invented human name
    "dentistry", "dental", "pediatrics", "pediatric", "orthodontics",
    "orthodontia", "medical", "medicine", "health", "healthcare", "therapy",
    "counseling", "psychiatry", "psychology", "surgery", "care", "center",
    "centre", "institute", "academy", "credit union",
    # insurers, which a statement and a declaration are both full of.
    # "Silverline Casualty" was proposed as a PERSON and would have been given
    # an invented human name in the middle of a policy line.
    "casualty", "mutual", "assurance", "indemnity", "underwriters", "surety",
    "reinsurance", "annuity", "fidelity", "utilities", "utility", "energy",
    "logistics", "freight", "servicing",
    # how a credit union and a savings bank abbreviate themselves on a
    # statement's letterhead: "OQUIRRH MOUNTAIN FCU" was proposed as a person
    "fcu", "cu", "fsb", "bancorp", "n.a.",
})

# Kept deliberately narrow, because this rejects a whole candidate on one
# token: a real surname Winter or Parent has to survive, so those live in
# _NOT_A_NAME (single-word only) instead.
_NEVER_IN_A_NAME = frozenset({
    "account", "accounts", "address", "tutoring", "neither", "either",
    "signature", "print", "card", "balance", "deposit", "withdrawal",
    "expense", "expenses", "reimbursement", "premium", "deductible",
    "statement", "invoice", "receipt", "subtotal", "total",
    # Column headings from financial statements. On a statement the model
    # routinely runs a real party's name together with the heading of the next
    # column - "Douglas W. Vandenbrook Secured", "Karis Elaine Vandenbrook
    # Beneficiary", "Theo Wray Vandenbrook Fund" - and ticking one of those
    # registers an entity that matches nothing while costing the operator
    # attention on a list that is already too long. These are document
    # structure words, not institutions, so they carry no overfitting risk.
    "beneficiary", "participant", "subscriber", "employee", "employer",
    "borrower", "payee", "payer", "payor", "recipient", "plan", "fund",
    "loan", "secured", "unsecured", "trustee", "custodian", "holder",
    # the same thing on an explanation of benefits
    "patient", "dob", "insured", "guarantor", "provider", "claimant",
    # and on an order record, whose product titles the model reads as people
    # and runs into the next column: "Franklin Sports Qty", "Scotch Heavy Duty
    # Shipping", "Order ID", "Sales Tax"
    "qty", "quantity", "sku", "seller", "shipping", "billing", "order", "tax",
})

# Transaction-type abbreviations, rejected for *every* category rather than
# only for people. A statement's description column is mostly these, and the
# model reads them both ways: "DIRECT DEP CASCADIA FREIGHT PAYROLL" came back
# as a PERSON, and accepting it renamed the deposit type to an invented human
# being, so the line read as though somebody called Giselle Eastcott had paid
# the wages. "SVC CHG - OVERDRAFT" came back as an ORG and became "[ORG-1]",
# which deletes the reason for the fee.
#
# The point of redacting a statement is that it can still be analysed
# afterwards. A description column that has been renamed or blanked out fails
# at the only thing the exhibit is for.
_TRANSACTION_WORDS = frozenset({
    "dep", "chg", "svc", "trn", "trns", "xfer", "pos", "atm", "ach", "eft",
    "nsf", "overdraft", "payroll", "autopay", "recur", "recurring", "memo",
    "adj", "adjustment", "reversal", "direct", "pending", "misc", "debit",
    "credit", "withdrawal", "deposit", "purchase", "refund", "fee", "charge",
    "debits", "credits", "withdrawals", "deposits", "purchases", "refunds",
    "fees", "charges", "payments", "transfers",
    # a payment app's status and type columns
    "merchant", "transacti", "transaction", "complete", "completed", "declined",
    "canceled", "cancelled",
})

_NOT_A_NAME = frozenset({
    "order", "orders", "debt", "debts", "title", "titles", "asset", "assets",
    "time", "times", "date", "dates", "day", "days", "eve", "week", "weeks",
    "month", "months", "year", "years", "vaccine", "vaccines", "mother", "father",
    "parent", "parents", "child", "children", "spouse", "husband", "wife",
    "childcare", "custody", "support", "alimony", "income", "expense",
    "expenses", "insurance", "vehicle", "vehicles", "account", "accounts",
    "balance", "payment", "payments", "interest", "clerk", "notary",
    "stipulation", "decree", "judgment", "judgement", "motion", "petition",
    "exhibit", "schedule", "attachment", "holiday", "holidays", "vacation",
    "birthday", "religion", "travel", "transportation", "education", "school",
    "medical", "dental", "vision", "extra-curricular", "extracurricular",
    "tutoring", "venue", "grounds", "jurisdiction", "residency", "marriage",
    "divorce", "parent-time", "parenting", "visitation", "arrears", "arrearages",
    "gift", "gifts",
})
