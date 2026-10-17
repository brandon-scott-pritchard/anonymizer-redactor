"""How likely is a written name to be a form of a main name? Spelling, not meaning.

A name misread by OCR, cut off, glued, reversed or run on with a line's words
is the same name a few letters out. Scored here with the standard measures:

- RapidFuzz's token-sort and token-set ratios (word order and extra words do
  not count against it) and partial ratio over the letters alone (a cut-off or
  glued form inside the full name), case and punctuation folded first;
- Jaro-Winkler on the surname, which weighs the first letters most - where a
  misread is least likely;
- Jellyfish's sound-alike codes for the surname (NYSIIS, Metaphone, Match
  Rating), for a respelling the letters miss.

The guards stand whatever the score: two different first names (not an
initial, a nickname or a misreading of the other) are two people; a Jr and a
Sr are two people. ``score`` is 0 when a guard holds.
"""

from __future__ import annotations

import re
from functools import lru_cache

import jellyfish
from rapidfuzz import fuzz
from rapidfuzz.distance import JaroWinkler

from . import names

# a form scoring at least HIGH attaches; between LOW and HIGH the local
# classifier decides; under LOW it does not - see eval.name_pairs for the tuning
HIGH = 0.90
LOW = 0.75
FIRST_SAME = 0.85              # Jaro-Winkler two first names need to be one
_SUFFIX = {"jr", "sr", "ii", "iii", "iv"}


def _norm(text: str) -> str:
    return " ".join(re.sub(r"[^a-z\s]", " ", text.casefold()).split())


_OCR_DIGITS = str.maketrans({"0": "o", "1": "l", "5": "s"})


def _letters(text: str) -> str:
    """Letters only - with the digits OCR reads for letters read back as
    them: "H0WARD" is "howard", not "hward" or nothing."""
    return re.sub(r"[^a-z]", "", text.casefold().translate(_OCR_DIGITS))


@lru_cache(maxsize=65536)
def _sounds(word: str) -> frozenset[str]:
    word = _letters(word)
    if len(word) < 3:
        return frozenset()
    return frozenset({jellyfish.nysiis(word), jellyfish.metaphone(word),
                      jellyfish.match_rating_codex(word)})


def _same_part(w: str, part: str) -> bool:
    """One word of a name another form of one part of it: equal, its initial,
    its nickname, a misreading, or a piece of it cut off at either end."""
    if not w or not part:
        return False
    if len(w) == 1:
        return part[0] == w
    if names.same_given(w, part) or names.alike(w, part):
        return True
    if len(w) >= 3 and len(w) < len(part) and (part.startswith(w) or part.endswith(w)):
        return True
    # a short name is a different name a letter or two out: Mary and Mark,
    # Dana and Dan, Joan and Juan are four people, and Jaro-Winkler scores
    # each pair above FIRST_SAME - only an OCR misreading (names.alike) or a
    # nickname makes two short first names one
    if min(len(w), len(part)) < 6:
        return False
    return JaroWinkler.similarity(w, part) >= FIRST_SAME


def _reshaped(form: list[str], parts: list[str]) -> list[str]:
    """The main name written in the form's shape: where a form word is an
    initial, nickname or cut-off piece of a part, that part as the form wrote
    it - so "K Harrington" is scored against "k harrington", not "kyle d harrington"."""
    out = list(parts)
    for w in form:
        for i, part in enumerate(out):
            if part and w != part and (len(w) == 1 and part[0] == w
                                       or names.same_given(w, part)
                                       or (len(w) >= 3 and (part.startswith(w) or part.endswith(w)))):
                out[i] = w
                break
    return out


def score(form_words: list[str], main_first: str, main_middles: list[str], main_last: str,
          form_text: str = "", main_text: str = "") -> float:
    """0..1: how likely the form is a written form of the main name."""
    form = [_letters(w) for w in form_words if _letters(w)]
    first, last = _letters(main_first), _letters(main_last)
    middles = [_letters(m) for m in main_middles if _letters(m)]
    if not form or not last:
        return 0.0
    parts = [w for w in (first, *middles, last) if w]
    la, lb = "".join(form), "".join(parts)
    # one word holding the whole name run together - with a payment line's
    # words, or glued - is scored on its letters
    if len(form) == 1 and len(la) > len(last) + 2:
        # what is left once the surname and a payment line's "to"/"from" are
        # taken off must be this person's first name: "CARSONSTEWART" is not
        # Steven Stewart because "stewart" starts with "ste"
        rest = la.replace(last, " ", 1).split(" ")[0] if last in la else ""
        for lead in ("from", "to"):
            if rest.startswith(lead) and len(rest) > len(lead) + 1:
                rest = rest[len(lead):]
                break
        # the whole first name, a misreading or nickname of it, or its
        # initial - with a middle initial glued on or not: "MARYHALL" is not
        # Mark Hall for sharing "mar"
        given_ok = bool(first) and bool(rest) and (
            (len(rest) <= 2 and rest[:1] == first[:1])
            or rest == first or rest[:-1] == first                  # a middle initial glued on
            or (len(rest) == len(first) and names.alike(rest, first))
            or names.same_given(rest, first))
        if last in la and given_ok:
            return 0.97
        if not first or not rest:
            given_ok = fuzz.partial_ratio(first, la) >= 90 if first else False
        if given_ok and fuzz.partial_ratio(lb, la) >= 92 and len(la) >= len(lb) - 2:
            # the surname misread inside the run, not another surname that
            # starts alike: "MARYCOOK" is not Mary Cox
            at = la.find(first) if first else -1
            tail = la[at + len(first):] if at >= 0 else la
            if any(JaroWinkler.similarity(t, last) >= HIGH or (_sounds(t) & _sounds(last))
                   for t in (tail, tail[1:]) if t):
                return 0.92
    # the surname: the form's word closest to it, wherever it stands - but
    # never the word that is the first name: "MICHAEL H0WARD" is not Michael
    # Mitchell because "michael" sounds like "mitchell"
    candidates = form
    if first:
        candidates = [w for w in form if not _same_part(w, first)]
        if not candidates:
            return 0.0              # the first name alone: the family's rule, not a score
    surname_sim = max(JaroWinkler.similarity(w, last) for w in candidates)
    if any(_sounds(w) & _sounds(last) for w in candidates):
        surname_sim = max(surname_sim, 0.95)
    if any(len(w) >= 4 and len(w) < len(last) and (last.startswith(w) or last.endswith(w))
           for w in candidates):
        surname_sim = max(surname_sim, 0.93)              # the surname cut off
    # the rest: a different first name is a different person
    others = [w for w in form if JaroWinkler.similarity(w, last) < 0.85 and not
              (len(w) >= 4 and (last.startswith(w) or last.endswith(w)))]
    if others:
        given = [first, *middles]
        lead = others[0]
        if not any(_same_part(lead, g) for g in given if g):
            return 0.0
    shaped = _reshaped(form, parts)
    a, b = " ".join(form), " ".join(shaped)
    words_sim = max(fuzz.token_sort_ratio(a, b), fuzz.token_set_ratio(a, b)) / 100
    return round(min(words_sim, surname_sim), 3)
