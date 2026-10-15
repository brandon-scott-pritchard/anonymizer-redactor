"""Optional spaCy suggestions.

The model never edits anything.  It proposes people, organisations and places
that the pattern rules cannot see, and those proposals land on the review
screen for the operator to accept or reject.  If the model is unavailable the
tool degrades to rules plus the operator's own name list and says so.
"""

from __future__ import annotations

import re
import threading
from dataclasses import dataclass

MODEL_NAME = "en_core_web_sm"

# spaCy entity label -> our category.  NORP (nationalities, religions,
# political groups) is deliberately absent - "American" or "Catholic" is not an
# organization and only polluted the suggestion list.
LABEL_MAP = {
    "PERSON": "person",
    "ORG": "organization",
    "GPE": "location",
    "LOC": "location",
    "FAC": "location",
}

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

# Ordinary words the model keeps offering as people and organizations. One real
# divorce decree produced fifty suggestions, of which "Order" (eight times, as a
# PERSON), "Debts", "Titles", "Vaccines", "Time", "Mother" and "Spring Brake"
# were representative. A list this long is unreviewable, and an unreviewable
# list gets skipped - which is worse than a short one that missed something.
# Words that make a candidate a place rather than a person.
_PLACE_WORDS = frozenset({
    "north", "south", "east", "west", "valley", "heights", "springs", "creek",
    "canyon", "ridge", "hills", "park", "point", "view", "haven", "field",
    "grove", "glen", "hollow", "bluff", "mesa", "butte", "bay", "lake",
    "fork", "junction", "bench", "flat", "meadow", "meadows", "cove", "town",
    "village", "township", "borough", "island", "beach", "harbor", "harbour",
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

# The card networks. They are what a payment line is paid *with*, so they
# name nobody, and the model proposed "Mastercard" as a person on every
# invoice that was paid with one. The same four transactions._ACCOUNT_WORDS
# already treats as account types; no issuer or merchant belongs here.
_CARD_NETWORKS = frozenset({"visa", "mastercard", "amex", "discover"})

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

# The last word of a street. A candidate ending in one is a street, whatever
# the model labelled it - "Cedarline Avenue" and "Foundry Row" both arrived as
# people, and ticking one would have given a road an invented human name while
# the real address shipped.
_STREET_SUFFIX = frozenset("""
    street st avenue ave road rd boulevard blvd lane ln drive dr court ct
    circle cir way wy place pl terrace ter parkway pkwy highway hwy trail trl
    loop square sq plaza alley route rte bend row run crossing xing landing
    commons cove creek ridge hollow meadow meadows glen grove knoll bluff
    trace path walk point pass bay hill hills vista mews canyon valley summit
""".split())

# "XXXX" is a masked card number and "****" is a redaction bar; the model
# offered both as organizations. "EMP-044821" and "XXX-XX-1147" are identifiers
# the pattern layer already owns.
_ALL_MASK = re.compile(r"^[Xx*•#\-\s]+$")
_ID_FRAGMENT = re.compile(r"[A-Z]{2,}-\d|\d-\d")
# "Amazon.com" and "Retail.OrderHistory.1.csv" were both offered as people.
_DOTTED_WORD = re.compile(r"[A-Za-z]\.[A-Za-z]{2,}")

# Part-of-speech tags for a column heading: common nouns and a participle -
# "Order Placed", "Items Ordered", "Amount Paid", "Sold". A personal name is
# tagged a proper noun, even one that is also an English verb ("Drew", "Reed").
_HEADING_NOUNS = frozenset({"NN", "NNS"})
_PARTICIPLES = frozenset({"VBN", "VBD"})

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
# Seasons are deliberately absent. "Spring Brake" appeared in the sample - a
# typo for Spring Break - and adding "brake" and the four seasons would have
# been fitting the list to one document's spelling mistake at the cost of every
# real Winter, Summers and Spring on a name list.

_nlp = None
_load_error: str | None = None

# Shared by every lazy heavy import in the package - see load() and
# redactor.ocr. One lock, not one per module, because the point is that only
# one background thread is ever inside an import of this size.
IMPORT_LOCK = threading.RLock()
_IMPORT_LOCK = IMPORT_LOCK


def available() -> tuple[bool, str]:
    """(is the model usable, human-readable explanation)."""
    load()
    if _nlp is not None:
        return True, f"spaCy model '{MODEL_NAME}' loaded"
    return False, _load_error or "spaCy model not loaded"


def load():
    """Load the model once; remember the failure if it will not load.

    Guarded, because this import is heavy and lazy and does not always happen
    on the main thread. The web front end answers /state from its own thread
    and that path imports pytesseract, while a run thread can be in here
    importing spaCy - two background threads pulling large packages in at the
    same time. Serialising them costs nothing after the first call and takes a
    whole class of interleaving off the table.
    """
    global _nlp, _load_error
    if _nlp is not None or _load_error is not None:
        return _nlp
    with _IMPORT_LOCK:
        if _nlp is not None or _load_error is not None:
            return _nlp
        return _load_locked()


def _load_locked():
    global _nlp, _load_error
    try:
        import spacy
    except Exception as exc:                      # pragma: no cover - env dependent
        _load_error = f"spaCy is not installed ({exc})"
        return None
    try:
        _nlp = spacy.load(MODEL_NAME, disable=["lemmatizer", "textcat"])
    except Exception as exc:                      # pragma: no cover - env dependent
        _load_error = (
            f"spaCy model '{MODEL_NAME}' is not installed. "
            f"Install it with:  python -m spacy download {MODEL_NAME}   ({exc})"
        )
        return None
    return _nlp


@dataclass
class Suggestion:
    text: str
    category: str
    count: int = 1
    documents: set[str] = None      # type: ignore[assignment]
    # set when this is a diminutive of a party already on the name list, so the
    # screen can say whose it is instead of leaving the operator to spot it
    nickname_for: str = ""

    def __post_init__(self):
        if self.documents is None:
            self.documents = set()

    @property
    def key(self) -> str:
        return f"{self.category}:{' '.join(self.text.split()).casefold()}"


MAX_CHARS_PER_DOC = 400_000      # spaCy's default parser limit is 1_000_000

# One token of a personal name: capitalised word, ALL CAPS, initial, particle
# or generational suffix - the same shapes caption.NAME_RE accepts.
_PERSON_TOKEN = re.compile(
    r"(?:[A-Z][A-Za-z'’\-]{1,20}|[A-Z]{2,20}|[A-Z]\."
    r"|van|von|de|del|della|di|da|du|la|le|el|bin|ibn|al|st\.?|mc|mac|o'"
    r"|Jr\.?|Sr\.?|II|III|IV|V)$"
)


def refine_category(value: str, category: str) -> str:
    """Reclassify a person-shaped org/location suggestion as a person.

    The small spaCy model routinely labels ALL-CAPS caption names and
    surname-only mentions as ORG.  Accepting that would register the person as
    a value entity - an ``[ORG-n]`` placeholder with no surname or initial
    variant matching, which leaks.  A string of 2-4 name-shaped tokens with no
    digits and no organization marker is a person.
    """
    if category == "person":
        # the reverse leak: a place labelled PERSON gets an invented human name
        # and the real city ships. "Pleasant Grove" arrived this way.
        from .caption import BOILERPLATE
        tokens = [t.strip(".,").casefold() for t in value.split()]
        if tokens and all(t in BOILERPLATE or t in _PLACE_WORDS for t in tokens):
            return "location"
        if any(t in _ORG_MARKERS for t in tokens):
            return "organization"
        return category
    if category not in {"organization", "location"}:
        return category
    if any(ch.isdigit() for ch in value):
        return category
    tokens = value.split()
    if not 2 <= len(tokens) <= 4:
        return category
    from .caption import BOILERPLATE
    bare = [t.strip(".,").casefold() for t in tokens]
    # an explicit marker settles it first: "Salt Lake County" is every bit as
    # place-shaped as it is organization-shaped, and the marker is the signal
    if any(t in _ORG_MARKERS for t in bare):
        return category
    # The model already said this is a place. A town passes every person test
    # below - two capitalised words, no digits, no company marker - so
    # "Pleasant Grove" was being promoted to a person and would have been given
    # an invented human name while the real city shipped. Refusing to overrule
    # the model when a geographic word is present is safe in a way that
    # guessing at a bare PERSON label is not: here the label is corroborating
    # evidence, and "Park" or "Glen" as somebody's actual surname never reaches
    # this branch because the model would have labelled it PERSON.
    if any(t in _PLACE_WORDS for t in bare):
        return category
    for token in tokens:
        if not _PERSON_TOKEN.match(token.rstrip(",")):
            return category
    return "person"


def suggest(documents: dict[str, str], protected: dict[str, list[tuple[int, int]]] | None = None
            ) -> list[Suggestion]:
    """Propose entities across ``documents`` ({label: text})."""
    nlp = load()
    if nlp is None:
        return []
    protected = protected or {}
    merged: dict[str, Suggestion] = {}

    for label, text in documents.items():
        if not text:
            continue
        guard = protected.get(label, [])
        for chunk_start, chunk in _chunks(text):
            try:
                doc = nlp(chunk)
            except Exception:                     # pragma: no cover - defensive
                continue
            for ent in doc.ents:
                label_category = LABEL_MAP.get(ent.label_)
                if not label_category:
                    continue
                start = chunk_start + ent.start_char
                end = chunk_start + ent.end_char
                if any(start < e and s < end for s, e in guard):
                    continue
                # A line break inside an entity is two cells the model read
                # as one: a payment ledger prints its payer and payee on
                # consecutive lines, and "Elizabeth Kimball Jenna Harrington"
                # came back as one person. Each line is its own candidate.
                offset = ent.start_char
                for line in ent.text.split("\n"):
                    tags = [tok.tag_ for tok in ent
                            if offset <= tok.idx < offset + len(line) and tok.is_alpha]
                    offset += len(line) + 1
                    if _is_heading(tags):
                        continue
                    value = " ".join(line.split()).strip(" .,;:'\"+-–—*#")
                    value = re.sub(r"['\u2019]s$", "", value).strip()   # drop possessives
                    category = refine_category(value, label_category)
                    if not _plausible(value, category):
                        continue
                    key = f"{category}:{value.casefold()}"
                    item = merged.get(key)
                    if item is None:
                        merged[key] = item = Suggestion(value, category, 0)
                    item.count += 1
                    item.documents.add(label)

    return sorted(merged.values(), key=lambda s: (s.category, -s.count, s.text.casefold()))


def _is_heading(tags: list[str]) -> bool:
    """A column heading the model read as an entity: "Order Placed" (NN VBN).

    Only common nouns and at least one participle. One proper noun anywhere
    keeps the candidate, so a surname that is also a verb survives.
    """
    return (any(tag in _PARTICIPLES for tag in tags)
            and all(tag in _PARTICIPLES or tag in _HEADING_NOUNS for tag in tags))


def drop_bare_names(suggestions: list[Suggestion]) -> list[Suggestion]:
    """Drop a one-word person already inside a fuller name from the same documents.

    An order export proposed "DAVID" twelve times beside "DAVID PRATT". One
    name-list entry matches the first name and the surname on their own, so
    ticking the full name already covers the bare one, and listing both only
    lengthens a list the operator has to read. A bare name kept anywhere the
    full name was not proposed stays: there it is the only proposal.
    """
    parts: dict[str, set[str]] = {}
    for item in suggestions:
        tokens = item.text.split()
        if item.category != "person" or len(tokens) < 2:
            continue
        for token in (tokens[0], tokens[-1]):
            parts.setdefault(token.strip(".,").casefold(), set()).update(item.documents)
    return [item for item in suggestions
            if item.category != "person" or len(item.text.split()) != 1
            or not item.documents <= parts.get(item.text.strip(".,").casefold(), set())]


def _chunks(text: str):
    """Yield (offset, chunk) slices small enough for the model, split on blank lines."""
    if len(text) <= MAX_CHARS_PER_DOC:
        yield 0, text
        return
    offset = 0
    while offset < len(text):
        end = min(len(text), offset + MAX_CHARS_PER_DOC)
        if end < len(text):
            split = text.rfind("\n", offset + MAX_CHARS_PER_DOC // 2, end)
            if split > offset:
                end = split
        yield offset, text[offset:end]
        offset = end


_MIN_LEN = {"person": 4, "organization": 4, "location": 3}

_NOISE = {
    "the court", "court", "the state", "state", "county", "district court", "petitioner",
    "respondent", "plaintiff", "defendant", "exhibit", "appendix", "esq", "llc", "llp",
    "inc", "the parties", "parties", "usa", "u.s.", "united states", "america",
}


# Nobody is named with more words than this. A run of names printed one per
# line - a supervised-contact list - used to arrive as a single candidate
# ("Petra Vandermeer Rachel Bly Steven Cole"), and ticking it registered a
# three-person entity that matched none of them.
MAX_NAME_TOKENS = 5


def _plausible(value: str, category: str) -> bool:
    if len(value) < _MIN_LEN.get(category, 3):
        return False
    folded = value.casefold()
    if folded in _NOISE:
        return False
    if not any(ch.isalpha() for ch in value):
        return False
    if sum(ch.isdigit() for ch in value) > len(value) / 3:
        return False
    tokens = value.split()
    if len(tokens) > MAX_NAME_TOKENS:
        return False
    if _ALL_MASK.match(value) or _ID_FRAGMENT.search(value):
        return False
    # a card processor's star - "Uber *Eats", "SQ *BLUE MOON" - marks a merchant
    if "*" in value:
        return False
    # A spaced dash is a memo separator, never part of a name: a cheque
    # register writes "Shawna Kirkendall - childcare", and the model offered
    # the whole cell as a person. Accepted alongside the real name it made two
    # overlapping entities, and the line came out as
    # "Ansel Evander Saltonstall Saltonstall". A hyphenated surname carries no
    # spaces, so nothing real is lost here.
    if " - " in value or " – " in value or " — " in value:
        return False
    # "Mother's Day" must reduce to {mother, day}: a possessive inside the
    # candidate hid the junk word behind it
    bare = [re.sub(r"['’]s$", "", tok.strip(".,'’-").casefold())
            for tok in tokens]
    if any(tok in _TRANSACTION_WORDS or tok in _CARD_NETWORKS for tok in bare):
        return False
    # a lone word that is only an organization marker is a fragment of a name,
    # not a name: "Bank", "Credit", "Services"
    if len(tokens) == 1 and bare[0] in _ORG_MARKERS:
        return False
    # a candidate that ends in a street suffix is a street
    if category == "person" and len(bare) > 1 and bare[-1] in _STREET_SUFFIX:
        return False
    # a single ordinary word is not a name, whatever the model labelled it
    if len(tokens) == 1 and bare[0] in _NOT_A_NAME:
        return False
    if all(tok in _NOT_A_NAME for tok in bare):
        return False
    # one of these anywhere in a person candidate means it is a label the model
    # ran together with something else - "Venmo Account", "Marcus Vaughn Address",
    # "Mother Neither", "Print Name" were all offered as people
    if category == "person" and any(tok in _NEVER_IN_A_NAME for tok in bare):
        return False
    if category == "person":
        # Nobody's name has a digit or a domain in it; a product title has
        # both - "Mario Kart 8", "OXO Good Grips 3", "Amazon.com"
        if any(ch.isdigit() for ch in value) or _DOTTED_WORD.search(value):
            return False
        # a person suggestion worth reviewing has at least one capitalised word
        if not any(tok[:1].isupper() for tok in value.split()):
            return False
    return True


# --------------------------------------------------------------------------
# which suggestions are likely names
# --------------------------------------------------------------------------

# The model calls a column heading, a merchant or a city a person as readily
# as a person: on a household's statements it proposed "Statement", "Interest
# Rate", "Maturity Date", "Zelle", "Los Angeles" - and "Tick all" sent every
# one of them through the run, each a stand-in where a heading or a figure's
# label had been. Nothing is dropped here; a suggestion that is probably not
# a name is listed apart, unticked, for the operator to tick if it is one.
_RAILS_AND_BRANDS = frozenset("""
zelle venmo paypal cashapp fdic sipc ach usa us whse llc inc corp ltd co bank
""".split())


def _word_list(name: str) -> frozenset[str]:
    import gzip

    from .places import _data_file

    path = _data_file().with_name(name)
    try:
        return frozenset(gzip.decompress(path.read_bytes()).decode().split())
    except OSError:
        return frozenset()


_COMMON: frozenset[str] | None = None
_GIVEN: frozenset[str] | None = None


def _lists() -> tuple[frozenset[str], frozenset[str]]:
    global _COMMON, _GIVEN
    if _COMMON is None:
        _COMMON = _word_list("common_words.txt.gz")
        from . import names as _names
        _GIVEN = _word_list("given_names.txt.gz") | frozenset(_names.NICKNAMES) | frozenset(
            short for shorts in _names.NICKNAMES.values()
            for short in ((shorts,) if isinstance(shorts, str) else shorts))
    return _COMMON, _GIVEN


def _plain(word: str) -> str:
    return re.sub(r"(?i)['’]s$", "", word).strip(".,'’()").casefold()


def likely_name(text: str, category: str, known: frozenset[str] = frozenset()) -> bool:
    """Is this suggestion probably a name at all?

    ``known`` is the words of the names already on the list: a suggestion
    sharing one - the household's surname in a business, a party's first
    name - is always likely.
    """
    from . import places

    words = [_plain(w) for w in text.split() if _plain(w)]
    if not words:
        return False
    if any(w in known for w in words):
        return True
    if category == "employer":
        return True
    if category in ("organization", "location"):
        # a business or place is someone's only when it carries their name
        return False
    common, given = _lists()
    if places.known(text):
        return False
    if any(w in _RAILS_AND_BRANDS for w in words):
        return False
    if len(words) == 1 and text.strip(".").isupper() and len(words[0]) <= 4:
        return False                          # "FDIC", "SIPC"
    if any(w in given for w in words):
        return True
    def ordinary(word: str) -> bool:
        return word in common or (word.endswith("s") and word[:-1] in common) or \
            (word.endswith("es") and word[:-2] in common)
    return not all(ordinary(w) for w in words)
