"""Find the towns and cities a document names.

A client's home town identifies them almost as well as their name does. Across
a real sample of pleadings, "South Jordan", "West Jordan", "Pleasant Grove",
"Provo", "Roosevelt" and "Salt Lake City" all shipped in the delivered file,
because nothing in the tool knew they were places.

The obvious fix is the wrong one. There are 101,160 populated place names in
the United States and 5,478 of them are also US surnames, carried by about
101.8 million people - Hull, Fowler and Falcon are all towns, and all three
were the surname of a real client in that same sample. A tool that redacts
every word appearing in a gazetteer would rename the petitioner to
``[LOCATION-1]``. An earlier attempt at this went the other way and put a
handful of Utah city names into ``caption.BOILERPLATE``; the result was that
eight real party names stopped being proposed at all, and it was reverted.

So the gazetteer never triggers anything. It only ever *confirms*:

    a context pattern proposes  ->  the gazetteer confirms  ->  the
    allowlist and the do-not-change list get a veto

That ordering is the whole design, and it is worth stating why both halves are
needed. Measured across eleven real pleadings:

  * Context alone produced 14 hits, 8 of them wrong - "Uniform Parentage Act",
    "Enforcement Act", "Salt Lake County", and worst, "Cody Miles", which is a
    party's name. The gazetteer rejects all 8.
  * The gazetteer alone would flag "Rule" (a town in Texas) inside every
    statute citation, "Telephone", "Holiday", "Golden", and the tool's own
    invented surrogate names - Chesterton, Underhill and Zephyr are all real
    US towns.

Neither half is usable by itself. Together they were right 6 times out of 6.

Everything here proposes; nothing is applied without a tick.
"""

from __future__ import annotations

import gzip
import re
import sys
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path

from .caption import _LETTER, _UPPER


def _data_file() -> Path:
    """The gazetteer, from the source tree or from inside the frozen app."""
    beside = Path(__file__).resolve().parent / "data" / "us_places.txt.gz"
    if beside.is_file():
        return beside
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen:
        bundled = Path(frozen) / "redactor" / "data" / "us_places.txt.gz"
        if bundled.is_file():
            return bundled
    return beside


DATA = _data_file()

# Postal codes for the 50 states, DC and the territories that have courts.
STATE_CODES = frozenset("""
    AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS
    MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV
    WI WY DC PR VI GU AS MP
""".split())

STATE_NAMES = frozenset("""
    alabama alaska arizona arkansas california colorado connecticut delaware
    florida georgia hawaii idaho illinois indiana iowa kansas kentucky
    louisiana maine maryland massachusetts michigan minnesota mississippi
    missouri montana nebraska nevada new-hampshire new-jersey new-mexico
    new-york north-carolina north-dakota ohio oklahoma oregon pennsylvania
    rhode-island south-carolina south-dakota tennessee texas utah vermont
    virginia washington west-virginia wisconsin wyoming
""".split())
# spelled-out states, as they appear in text ("New Hampshire", not the slug)
_STATE_NAME_RE = "|".join(
    sorted((s.replace("-", r"\s+") for s in STATE_NAMES), key=len, reverse=True)
)

# "Truth or Consequences" is three words; "Village of Grosse Pointe Shores"
# is the longest shape worth chasing.
MAX_PLACE_WORDS = 4

# A place-name token. ALL CAPS is not a stylistic nicety here: bank statement
# headers and pleading address blocks both print "SOUTH JORDAN UT 84095", and
# a pattern that only accepted title case would miss every one of them.
_TOKEN = rf"(?:[{_UPPER}][{_LETTER}'’.\-]{{0,20}}|[{_UPPER}]{{2,20}})"

# Places carry lower-case joining words the way people's names carry particles:
# Coeur d'Alene, Truth or Consequences, Isle of Palms, Lake in the Hills. A run
# that only accepted capitalised tokens stopped at "Coeur".
_PARTICLE = (r"(?:de|del|des|du|da|la|le|los|las|of|the|or|on|upon|in|by"
             r"|en|y|van|von|al|au|aux)")
# "d'Alene", "l'Anse" - one whitespace-delimited word that begins with the
# particle, so neither _TOKEN (needs a capital first) nor _PARTICLE (stops at
# the apostrophe) matches it whole.
_PARTICLE_TOKEN = rf"(?:[dlo]'[{_UPPER}][{_LETTER}'’.\-]{{0,20}})"
_WORD = rf"(?:{_TOKEN}|{_PARTICLE_TOKEN}|{_PARTICLE})"
# Horizontal whitespace only. A plain \s+ crosses the line break, and in a
# two-line address block -
#
#     17775 W. 7000 N.
#     Altonah, Utah  84001
#
# it fused the street's trailing "N." onto the city, giving a run of
# "N. Altonah" that the single-word guard below then threw away. The town a
# real client lives in was lost to a character class.
_H = r"[^\S\n]+"
_RUN = rf"{_TOKEN}(?:{_H}{_WORD}){{0,{MAX_PLACE_WORDS - 1}}}"

_ZIP = r"\d{5}(?:-\d{4})?"

# ---------------------------------------------------------------------------
# the three context patterns
# ---------------------------------------------------------------------------

# "South Jordan, UT 84095" - a state code with a ZIP behind it. The strongest
# signal there is, and the one an address block always carries.
CITY_STATE_ZIP = re.compile(
    rf"({_RUN})\s*,\s*(?:{'|'.join(sorted(STATE_CODES))})\.?\s+{_ZIP}"
)

# "SOUTH JORDAN UT 84095" - the same thing with the comma dropped, which is how
# a statement header and a mailing label print it.
CITY_STATE_ZIP_BARE = re.compile(
    rf"({_RUN})\s+(?:{'|'.join(sorted(STATE_CODES))})\.?\s+{_ZIP}"
)

# "Provo, Utah" - the state spelled out. No ZIP needed: nothing abbreviates to
# a whole state name by accident.
CITY_STATE_NAME = re.compile(
    rf"({_RUN})\s*,\s*(?i:{_STATE_NAME_RE})\b"
)

# "moved to Roosevelt, UT" - a state code with no ZIP. Weaker, because two
# letters after a comma is also how a professional credential is written, so
# this one carries an extra guard below.
CITY_STATE_ONLY = re.compile(
    rf"({_RUN})\s*,\s*({'|'.join(sorted(STATE_CODES))})\.?(?![\w-])"
)

# "resides in Provo", "was born in Altonah", "relocated to Orem". The cue is
# case-insensitive; the name after it is not, or the capitalised-token
# requirement stops meaning anything and the match runs into the sentence.
RESIDENCE = re.compile(
    r"(?i:\b(?:resides?|resided|residing|lives?|lived|living|located|situated)"
    r"\s+(?:in|at)\s+"
    r"|\b(?:moved|relocated|returned)\s+(?:to|back\s+to)\s+"
    r"|\b(?:a\s+)?resident\s+of\s+"
    r"|\bborn\s+in\s+"
    r"|\bcity\s+of\s+)"
    rf"({_RUN})"
)

# A month after "born in" is a date, not a town, and May, June, August, March
# and Mai are all populated places. This is the one blocklist here, and it is
# closed: there are twelve months and there will not be a thirteenth.
_MONTHS = frozenset("""
    january february march april may june july august september october
    november december jan feb mar apr jun jul aug sep sept oct nov dec
""".split())

# A county is usually named after its largest city, and every pleading opens by
# reciting one: "residents of Salt Lake County, Utah" reads as "<city>, <state>"
# and yielded Salt Lake, which is a real place and the wrong answer - redacting
# it would mangle Salt Lake County and Salt Lake City everywhere else in the
# document. The venue recital is not a client's address.
#
# Only the word directly after the match counts. Testing the whole run instead
# threw away "resides in Provo in Utah County", where the county is a separate
# clause and Provo is exactly the town we want.
_REGION_WORDS = frozenset({"county", "parish", "borough", "township", "district"})


@dataclass(frozen=True)
class Place:
    """A place name a context pattern proposed and the gazetteer confirmed."""

    name: str
    source: str = ""
    evidence: str = ""      # which pattern found it, for the report

    @property
    def key(self) -> str:
        return " ".join(self.name.split()).casefold()

    @property
    def category(self) -> str:
        return "location"

    @property
    def role(self) -> str:
        return "Place name"


# ---------------------------------------------------------------------------
# the gazetteer
# ---------------------------------------------------------------------------


@lru_cache(maxsize=1)
def gazetteer() -> dict[str, str]:
    """{casefolded: canonical spelling}. Empty if the data file is missing.

    The canonical spelling is the gazetteer's own, which is why a statement
    header shouting "MCKINNEY TX 75070" puts *McKinney* on the name list
    instead of the "Mckinney" that title-casing would produce.
    """
    try:
        with gzip.open(DATA, "rt", encoding="utf-8") as fh:
            return {line.strip().casefold(): line.strip() for line in fh
                    if line.strip() and not line.startswith("#")}
    except OSError:
        return {}


def known(name: str) -> bool:
    """Is this the name of a US town, city or community?"""
    return " ".join(name.split()).casefold() in gazetteer()


def _longest_known(run: str, prefer: str = "right") -> tuple[str, str]:
    """The longest run of words inside ``run`` that is a real place, or "".

    A captured run is greedy on purpose, so it picks up whatever capitalised
    word happened to sit in front of the city - "Lane South Jordan" off the end
    of a street address, "North Orem" where the line above wrapped - and, now
    that joining words are allowed, whatever prose trailed off the back of it.

    Longest wins. Among equal lengths the tie breaks toward whichever end the
    context pattern anchored on, which is not the same end in both cases: in
    "1234 Maple Lane South Jordan, UT" the city is against the comma on the
    right, but in "resides in Provo in Utah" it is against the cue on the left,
    and picking the wrong end there returns the state instead of the town.

    Returns the canonical name and the word sitting directly after it inside
    the run, which is what the county guard needs.
    """
    # the run swallows the sentence's full stop - "resides in Pleasant Grove."
    # captures "Grove." and no gazetteer entry ends in a period
    tokens = run.rstrip(".,;:").split()
    table = gazetteer()
    for size in range(len(tokens), 0, -1):
        starts = range(len(tokens) - size, -1, -1) if prefer == "right" \
            else range(0, len(tokens) - size + 1)
        for start in starts:
            canonical = table.get(" ".join(tokens[start:start + size]).casefold())
            if canonical:
                after = tokens[start + size] if start + size < len(tokens) else ""
                return canonical, after
    return "", ""


def _preceded_by_capital(text: str, start: int) -> bool:
    """Is there a capitalised word immediately before ``start`` on this line?"""
    head = text[:start]
    line = head.rsplit("\n", 1)[-1]
    match = re.search(rf"([{_UPPER}][{_LETTER}'’.\-]*)[\s,]*$", line)
    return bool(match)


def _accept(text: str, match: re.Match, run: str, evidence: str) -> str:
    """The confirmed place name inside ``run``, or "" if this hit is rejected."""
    name, after = _longest_known(
        run, "left" if evidence == "residence" else "right")
    if not name:
        return ""
    if name.casefold() in _MONTHS:
        return ""
    # the run named a county and the gazetteer matched the city inside it
    if after.strip(".,").casefold() in _REGION_WORDS:
        return ""
    # "Jane Miller, MD" is a doctor, and MD is also Maryland. A bare state code
    # with no ZIP, on a single-word place, directly after another capitalised
    # word, is far more often a credential or a surname than a town. Structural
    # rather than a list of credentials, because the list would miss the next
    # one and this catches all of them.
    #
    # The capitalised word is either inside the run - the run captured "Jane
    # Miller" and only "Miller" survived the gazetteer - or just before it on
    # the same line.
    if evidence == "city-state" and " " not in name:
        trimmed = len(run.rstrip(".,;:").split()) > len(name.split())
        if trimmed or _preceded_by_capital(text, match.start(1)):
            return ""
    return name


_PATTERNS = (
    (CITY_STATE_ZIP, "address"),
    (CITY_STATE_ZIP_BARE, "address"),
    (CITY_STATE_NAME, "city-state"),
    (CITY_STATE_ONLY, "city-state"),
    (RESIDENCE, "residence"),
)


def harvest(text: str, source: str = "",
            protected: tuple[tuple[int, int], ...] = (),
            avoid: frozenset[str] = frozenset()) -> list[Place]:
    """Places this document names, with every veto already applied.

    ``protected`` are allowlist spans - a case citation or a statute reference
    that must survive untouched. ``avoid`` are the casefolded names already
    spoken for: parties, children, judicial officers. A town that shares a
    surname with somebody in this document is that person's name here,
    whatever the gazetteer says.
    """
    found: dict[str, Place] = {}
    for pattern, evidence in _PATTERNS:
        for match in pattern.finditer(text):
            if any(match.start() < end and start < match.end()
                   for start, end in protected):
                continue
            name = _accept(text, match, match.group(1), evidence)
            if not name:
                continue
            folded = name.casefold()
            if folded in avoid:
                continue
            # a place whose name is one of the tokens of somebody's name here
            if any(folded == token for term in avoid for token in term.split()):
                continue
            found.setdefault(folded, Place(name, source, evidence))
    return list(found.values())


def harvest_documents(regions: dict[str, str],
                      protected: dict[str, list[tuple[int, int]]] | None = None,
                      avoid: frozenset[str] = frozenset()) -> list[Place]:
    """Places named anywhere in the batch, merged by name."""
    protected = protected or {}
    merged: dict[str, Place] = {}
    for source, text in regions.items():
        spans = tuple(protected.get(source, ()))
        for place in harvest(text, source, spans, avoid):
            merged.setdefault(place.key, place)
    return list(merged.values())
