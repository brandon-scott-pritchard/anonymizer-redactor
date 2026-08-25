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

FIRST_NAMES = (
    "Adrian", "Alana", "Alec", "Alina", "Amara", "Ansel", "Arden", "Ariel", "Armand",
    "Asher", "Aurora", "Averil", "Beatrix", "Bennett", "Bianca", "Blaise", "Bridget",
    "Callum", "Camila", "Carmen", "Cassian", "Cecilia", "Cedric", "Celeste", "Clara",
    "Colette", "Conrad", "Cormac", "Damaris", "Darius", "Delia", "Desmond", "Dorian",
    "Edith", "Elena", "Elias", "Eloise", "Emeric", "Esme", "Evander", "Everett",
    "Fabian", "Felicity", "Fenwick", "Fiona", "Florian", "Frances", "Gareth", "Genevieve",
    "Gideon", "Giselle", "Gordon", "Greta", "Hadley", "Harriet", "Helena", "Hollis",
    "Horace", "Ignatius", "Imogen", "Ingrid", "Isadora", "Ivo", "Jacinta", "Jasper",
    "Jerome", "Josephine", "Juliet", "Julian", "Junia", "Kendra", "Killian", "Lacey",
    "Lamont", "Larissa", "Leander", "Leonie", "Lorcan", "Lucinda", "Ludovic", "Lyra",
    "Mabel", "Magnus", "Malachy", "Marguerite", "Marcus", "Mathilde", "Maureen", "Merrick",
    "Mireille", "Mortimer", "Nadia", "Nathanial", "Nerissa", "Nicolo", "Noelle", "Octavia",
    "Odette", "Orson", "Oswin", "Paloma", "Pascal", "Perpetua", "Phineas", "Priscilla",
    "Quentin", "Rafaela", "Ramona", "Reginald", "Rhiannon", "Roderick", "Rosalind",
    "Rowena", "Rupert", "Sabine", "Salvatore", "Seraphina", "Silas", "Solveig", "Sylvia",
    "Tamsin", "Thaddeus", "Theodora", "Tobias", "Ulric", "Ursula", "Valentina", "Vaughn",
    "Verity", "Vivienne", "Wendell", "Wilhelmina", "Xavier", "Yolanda", "Yves", "Zelda",
    "Zephyr", "Zora",
)

LAST_NAMES = (
    "Abernathy", "Ainsworth", "Alderidge", "Ashcombe", "Balfour", "Barlowe", "Beaumont",
    "Bellweather", "Blackwood", "Braddock", "Bramhall", "Calloway", "Carrington",
    "Chadwick", "Chesterton", "Clairborne", "Colefax", "Cranfield", "Crowhurst",
    "Danforth", "Darlington", "Deveraux", "Dunmore", "Eastcott", "Edgeworth", "Ellingham",
    "Fairbanks", "Fallowell", "Farnsworth", "Featherstone", "Fenmore", "Fitzgibbon",
    "Galbraith", "Garrowick", "Glendower", "Granville", "Greaves", "Halloran",
    "Hargreaves", "Harrowgate", "Havelock", "Hawthorne", "Heathcote", "Hollingsworth",
    "Huxley", "Inglewood", "Ivorson", "Kettering", "Kingsleigh", "Lambourne", "Langford",
    "Larkspur", "Lattimore", "Leighton", "Lindquist", "Lockridge", "Maddox", "Marchetti",
    "Merriweather", "Middleton", "Montrose", "Mortlake", "Nettleton", "Norwood",
    "Oakhurst", "Ollivander", "Pemberton", "Pendergast", "Penhaligon", "Quarrier",
    "Radcliffe", "Ravensworth", "Redmayne", "Rothbury", "Saltonstall", "Sandringham",
    "Selwyn", "Sheffield", "Sinclair", "Somerville", "Stanhope", "Stratford", "Sutherland",
    "Thackeray", "Thorncroft", "Tillingham", "Underhill", "Vandermere", "Vasquez",
    "Verrinder", "Wadsworth", "Wallingford", "Westbrook", "Whitmore", "Wickersham",
    "Winterbourne", "Wolcott", "Wrenfield", "Yardley", "Zabriskie",
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
