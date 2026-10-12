"""Deterministic surrogate values.

Every fake name is chosen by an HMAC of the original string against a fixed
salt, so the same input always yields the same output - no RNG, no clock, no
counter that depends on how the files happened to be ordered on disk.  Re-run
the tool on the same documents with the same approved entity list and you get
byte-identical results.
"""

from __future__ import annotations

import hmac
import hashlib

from . import names as _names

# Fixed on purpose.  Changing this changes every fake name the tool has ever
# produced, which would break consistency with documents already produced.
SALT = b"anonymizer-redactor/v1/surrogate-salt"

# Short on purpose. A PDF cannot reflow, so an invented name has to fit in the
# space the real one occupied, and most real names are longer than these.
# Three to five letters, uncommon enough not to read as anybody in particular,
# and none of them an ordinary English word - "Rose", "Ash" and "May" would
# read as text to the person or the model reading the result.
FIRST_NAMES = (
    "Ada", "Alba", "Anke", "Anya", "Arlo", "Asa", "Ayla", "Bea", "Bram", "Cai",
    "Cleo", "Coby", "Dax", "Deni", "Dov", "Edda", "Elke", "Emil", "Enzo", "Esme",
    "Etta", "Ezra", "Faye", "Finn", "Fritz", "Gita", "Hana", "Hugo", "Ida", "Ilse",
    "Ines", "Ivo", "Jada", "Jory", "Juno", "Kai", "Kasia", "Lars", "Lena", "Lior",
    "Lise", "Liv", "Lotte", "Luca", "Mae", "Mika", "Milo", "Mira", "Nell", "Nico",
    "Nils", "Noor", "Ola", "Olek", "Orla", "Otto", "Pia", "Quin", "Rafe", "Remy",
    "Rhea", "Rory", "Rufus", "Sana", "Saul", "Seb", "Sian", "Suki", "Tali", "Tess",
    "Theo", "Tova", "Ugo", "Una", "Vera", "Vito", "Wren", "Xan", "Yara", "Yuri",
    "Zane", "Zia", "Zoe", "Abel", "Bodil", "Cosmo", "Dina", "Eero", "Fenna", "Greer",
    "Haldo", "Isak", "Jonna", "Kjell", "Lumi", "Maren", "Nadja", "Oskar", "Petra",
    "Rasmo", "Signe", "Tomas", "Ulla", "Vidar",
)

LAST_NAMES = (
    "Aune", "Baird", "Benn", "Brask", "Cade", "Coyle", "Dahl", "Dorn", "Duhr", "Eide",
    "Elam", "Engel", "Esch", "Falk", "Fenn", "Fogg", "Frey", "Gant", "Geer", "Gish",
    "Haag", "Hess", "Holm", "Hoyt", "Ilg", "Iver", "Jarl", "Juhl", "Kell", "Kerr",
    "Kist", "Kohl", "Kruse", "Lusk", "Lutz", "Mohr", "Nagy", "Noll", "Nye", "Oakes",
    "Orr", "Ott", "Pell", "Raab", "Roth", "Rusk", "Sato", "Saxe", "Senn", "Skov",
    "Stahl", "Toft", "Uhl", "Voss", "Wahl", "Wolk", "Yost", "Zahn", "Zell", "Aho",
    "Bjork", "Casse", "Dyck", "Ebel", "Fahy", "Gorse", "Hulme", "Imel", "Jost", "Kemp",
    "Lind", "Maag", "Nolt", "Oyen", "Pratz", "Quast", "Rehm", "Sund", "Tveit", "Unger",
    "Vahl", "Welk", "Yarr", "Zorn", "Abt", "Brede", "Crall", "Dekk", "Ernst", "Fiske",
    "Aasen", "Bakke", "Berge", "Birk", "Borg", "Brun", "Dahle", "Ekdal", "Foss", "Grieg",
    "Haug", "Hovde", "Kvam", "Lunde", "Moen", "Ruud", "Strom", "Tange", "Vik", "Aakre",
    "Bratt", "Hauge", "Thue", "Vaag", "Ekre", "Falck", "Hegg", "Jahr", "Kolb", "Lerch",
    "Mertz", "Nehr", "Obst", "Prinz", "Rauch", "Seitz", "Thiel", "Uhlig", "Vogt", "Wirth",
    "Abeln", "Bohr", "Dietz", "Eber", "Fuchs", "Gehl", "Hiltz", "Kranz", "Lenz", "Merk",
    "Pfaff", "Rapp", "Tesch", "Vehr", "Weig", "Ambro", "Biel", "Cerny", "Dvor", "Fiala",
    "Hajek", "Jelen", "Kral", "Liska", "Mraz", "Novak", "Pavek", "Rybar", "Tuma", "Vacek",
    "Zeman", "Daal", "Graaf", "Haan", "Jonk", "Laan", "Roos", "Sluis", "Veen", "Zijl",
    "Aalto", "Ikola", "Kivi", "Lahti", "Niemi", "Oja", "Palo", "Rinne", "Salo", "Vuori",
    "Arbo", "Brix", "Dyrby", "Friis", "Holst", "Juel", "Krogh", "Lykke", "Moll", "Riis",
    "Bekk", "Dalby", "Eklof", "Fjell", "Grund", "Hult", "Ivers", "Jarvi", "Kalm", "Lodin",
    "Mehl", "Nord", "Ohm", "Pahl", "Rask", "Sjo", "Tolk", "Unruh", "Vinje", "Wiik",
    "Axen", "Bolin", "Cedre", "Dufva", "Enok", "Frode", "Gyllo", "Hedin", "Insel", "Jessl",
    "Kopp", "Lauer", "Marth", "Nabb", "Opsal", "Peder", "Quist", "Rolf", "Sauer", "Treu",
    "Ulven", "Valt", "Wenz", "Zuber", "Aberg", "Bunde", "Dirks", "Edin", "Feld", "Gamm",
)


def _digest(*parts: str) -> int:
    msg = "\x1f".join(parts).encode("utf-8")
    return int.from_bytes(hmac.new(SALT, msg, hashlib.sha256).digest()[:8], "big")


def _pick(pool: tuple[str, ...], *parts: str) -> str:
    return pool[_digest(*parts) % len(pool)]


def person(canonical: str, want_middle: bool, attempt: int = 0) -> _names.PersonName:
    """A deterministic fake person for ``canonical``.

    ``attempt`` is bumped only to break a collision with an already-used
    surrogate; it keeps the result deterministic while guaranteeing that two
    different real people never share one fake name.
    """
    seed = canonical.casefold()
    tag = str(attempt)
    first = _pick(FIRST_NAMES, seed, "first", tag)
    last = _pick(LAST_NAMES, seed, "last", tag)
    middles: tuple[str, ...] = ()
    if want_middle:
        middle = _pick(FIRST_NAMES, seed, "middle", tag)
        if middle == first:
            middle = _pick(FIRST_NAMES, seed, "middle-alt", tag)
        middles = (middle,)
    return _names.PersonName(raw=f"{first} {last}", first=first, middles=middles, last=last)


def placeholder(tag: str, index: int) -> str:
    return f"[{tag}-{index}]"
