"""The town gazetteer from the Anonymizer's ``redactor/places.py`` - all the
skill needs of it: ``known()``, which keeps "LAYTON UT" in a statement header
from being taken for a person."""

from __future__ import annotations

import gzip
from functools import lru_cache
from pathlib import Path

DATA = Path(__file__).resolve().parent / "data" / "us_places.txt.gz"


@lru_cache(maxsize=1)
def gazetteer() -> dict[str, str]:
    """{casefolded: canonical spelling}. Empty if the data file is missing."""
    try:
        with gzip.open(DATA, "rt", encoding="utf-8") as fh:
            return {line.strip().casefold(): line.strip() for line in fh
                    if line.strip() and not line.startswith("#")}
    except OSError:
        return {}


def _data_file() -> Path:
    return DATA


def known(name: str) -> bool:
    """Is this the name of a US town, city or community?"""
    return " ".join(name.split()).casefold() in gazetteer()
