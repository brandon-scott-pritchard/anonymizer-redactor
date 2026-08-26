"""Rebuild redactor/data/us_places.txt.gz from the two public-domain sources.

Run this by hand when the sources publish a new year; the tool itself never
downloads anything, and the shipped file is what it reads.

    python build/build_places.py

Sources, both public domain (works of the US federal government):

  * US Census Bureau national place gazetteer - every incorporated place and
    census-designated place in the country, about 21,000 names.
  * USGS Geographic Names Information System, "Populated Place" features -
    the unincorporated communities the Census list has no row for. Altonah,
    Utah is one of them, and it is where a real client lived.

The GNIS half is filtered before it lands, and the filter is the whole reason
this file is worth reading.

GNIS carries about 110,000 populated-place names, and taking them wholesale
would be a mistake that measures like this:

                     names    top-100 US surnames also a place
    Census          21,299    79
    GNIS           110,402    96
    Census + GNIS  111,824    97

Ninety-six of the hundred most common surnames in America are a populated
place somewhere in GNIS, because a hamlet gets named after the family that
settled it. The gazetteer's job in this tool is to *confirm* a location that a
context pattern already proposed, so every one of those names is a chance to
confirm a person as a city. Importing all of GNIS nearly doubles that risk to
buy 89,000 hamlets that a family law pleading will never mention.

So a GNIS-only name is admitted only when it is neither a US surname nor an
English word - the two ways a place name doubles as something else. What
survives is the long tail of genuinely place-shaped names, and the arithmetic
says the filter is exact:

                     names    top-100 surnames    Americans whose surname is a place
    Census          21,299    79                  101,820,782
    what we ship   101,208    79                  101,820,782

Same collision surface as the Census list alone, plus 79,909 unincorporated
communities. The residual 101 million is not a bug to fix here - Hull, Fowler
and Falcon are all real towns and all real client surnames, and no filter can
separate them. That is handled at match time, where the do-not-change list
knows who *this* document's parties are. See redactor/places.py.
"""

from __future__ import annotations

import csv
import gzip
import io
import sys
import urllib.request
import zipfile
from pathlib import Path

CENSUS_URL = (
    "https://www2.census.gov/geo/docs/maps-data/data/gazetteer/"
    "2024_Gazetteer/2024_Gaz_place_national.zip"
)
GNIS_URL = (
    "https://prd-tnm.s3.amazonaws.com/StagedProducts/GeographicNames/"
    "DomesticNames/DomesticNames_National_Text.zip"
)
OUT = Path(__file__).resolve().parent.parent / "redactor" / "data" / "us_places.txt.gz"
WORDS = Path("/usr/share/dict/words")

# The Census NAME column appends the legal/statistical description: "Abbeville
# city", "Abanda CDP", "Milford city (balance)". Strip it, and strip it
# case-sensitively - the appended word is always lower case, so "Carson City"
# and "Princeton" (which carry no description at all) come through whole.
LSAD_SUFFIXES = (
    "(balance)", "zona urbana", "metro township", "urban county",
    "unified government", "consolidated government", "metropolitan government",
    "CDP", "city", "town", "village", "borough", "municipality", "comunidad",
    "township", "corporation",
)


def strip_lsad(name: str) -> str:
    """"Salt Lake City city" -> "Salt Lake City"; "Carson City" unchanged."""
    result = name.strip()
    changed = True
    while changed:                      # "Milford city (balance)" needs two passes
        changed = False
        for suffix in LSAD_SUFFIXES:
            if result.endswith(" " + suffix):
                result = result[: -len(suffix) - 1].strip()
                changed = True
    return result


def fetch(url: str) -> bytes:
    print(f"    downloading {url.rsplit('/', 1)[-1]}")
    with urllib.request.urlopen(url) as response:      # noqa: S310 - fixed URLs
        return response.read()


def census_names() -> dict[str, str]:
    """{casefolded: canonical}. The canonical spelling is what gets displayed.

    Keeping the source's own casing matters: a bank statement prints
    "MCKINNEY TX 75070" in caps, and title-casing that ourselves would put
    "Mckinney" on the operator's name list. The gazetteer already knows how the
    place spells itself.
    """
    archive = zipfile.ZipFile(io.BytesIO(fetch(CENSUS_URL)))
    member = next(n for n in archive.namelist() if n.endswith(".txt"))
    text = archive.read(member).decode("utf-8")
    names: dict[str, str] = {}
    for row in csv.DictReader(io.StringIO(text), delimiter="\t"):
        raw = (row.get("NAME") or row.get("NAME ") or "").strip()
        cleaned = strip_lsad(raw)
        if cleaned:
            names.setdefault(cleaned.casefold(), cleaned)
    return names


def gnis_names() -> dict[str, str]:
    archive = zipfile.ZipFile(io.BytesIO(fetch(GNIS_URL)))
    member = next(n for n in archive.namelist()
                  if n.endswith(".txt") and "DomesticNames" in n)
    names: dict[str, str] = {}
    with archive.open(member) as raw:
        stream = io.TextIOWrapper(raw, encoding="utf-8-sig")
        for row in csv.DictReader(stream, delimiter="|"):
            if (row.get("feature_class") or "").strip() != "Populated Place":
                continue
            name = (row.get("feature_name") or "").strip()
            if name:
                names.setdefault(name.casefold(), name)
    return names


def surnames() -> set[str]:
    """Every surname the Census publishes, from the decennial surname file."""
    url = "https://www2.census.gov/topics/genealogy/2010surnames/names.zip"
    archive = zipfile.ZipFile(io.BytesIO(fetch(url)))
    member = next(n for n in archive.namelist() if n.lower().endswith(".csv"))
    text = archive.read(member).decode("utf-8", "replace")
    return {row["name"].casefold()
            for row in csv.DictReader(io.StringIO(text))
            if row.get("name")}


def main() -> int:
    print("==> Census places")
    census = census_names()
    print(f"    {len(census):,} names")

    print("==> GNIS populated places")
    gnis = gnis_names()
    print(f"    {len(gnis):,} names")

    print("==> filters")
    known_surnames = surnames()
    print(f"    {len(known_surnames):,} surnames")
    if not WORDS.is_file():
        print(f"    ERROR: {WORDS} not found; the English-word filter cannot run.")
        return 1
    words = {w.strip().casefold() for w in WORDS.read_text(
        encoding="utf-8", errors="replace").splitlines() if w.strip()}
    print(f"    {len(words):,} dictionary words")

    only_in_gnis = {k: v for k, v in gnis.items() if k not in census}
    extra = {k: v for k, v in only_in_gnis.items()
             if k not in known_surnames and k not in words}
    places = {**census, **extra}
    print(f"    GNIS contributed {len(extra):,} of its {len(only_in_gnis):,} "
          f"names not already in Census")

    body = "\n".join(v for _, v in sorted(places.items()))
    header = (
        "# US place names: Census Bureau national place gazetteer (2024) plus\n"
        "# USGS GNIS populated places that are neither a US surname nor an\n"
        "# English word. Both sources are public domain.\n"
        "# Rebuild with build/build_places.py - do not edit by hand.\n"
        f"# {len(places)} names\n"
    )
    OUT.parent.mkdir(parents=True, exist_ok=True)
    # mtime=0 so rebuilding from unchanged sources produces an identical file
    with gzip.GzipFile(OUT, "wb", compresslevel=9, mtime=0) as fh:
        fh.write((header + body + "\n").encode("utf-8"))

    size = OUT.stat().st_size / 1024
    print(f"==> wrote {OUT.relative_to(OUT.parent.parent.parent)}: "
          f"{len(places):,} names, {size:.0f} KB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
