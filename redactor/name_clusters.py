"""The main names of a set, and every other written form attached to one.

A real case set has some twenty people, and the scan proposed 590 name rows:
each person's name misread, glued, shortened, reversed, initialled, with a
line's word run on - and each a row. Here the MAIN NAMES are picked first - a
full name (first and last) written in several documents - and every other
form is attached to the one main name it is a form of. An attached form is no
longer a row of its own but is still replaced, as that person's form.

What attaches, in order:

1. by rule - the form's words are the main name's, or its initials, nicknames
   or misreadings (names.same_given / names.alike), in its order or reversed;
   or the main name run together (glued, with a payment line's words);
2. a surname alone - to the family (every main name sharing that surname);
   a first name alone - to the one main name with that first name;
3. by the local classifier (Kev), for what the rules cannot settle: a form
   sharing a near-surname with a main name, asked whether the two are one
   person - but never two different first names, a child and an adult, or a
   Jr and a Sr, whatever it says.

Anything still unattached is reported by shape, so the missing rule can be
found without anyone reading a name.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

from . import names

MAIN_MIN_DOCUMENTS = 3          # a full name written this often is a person, not a misread
KEV_SAME = 0.6                  # Kev's "same person" score to attach by
_SUFFIX = {"jr", "sr", "ii", "iii", "iv"}


@dataclass
class Form:
    text: str
    documents: set[str]
    minor: bool = False
    words: list[str] = field(default_factory=list)
    junk: bool = False


@dataclass
class Main:
    form: Form
    first: str
    last: str
    middles: list[str]
    suffix: str
    forms: list[tuple[Form, str]] = field(default_factory=list)    # (form, how)

    @property
    def documents(self) -> set[str]:
        out = set(self.form.documents)
        for f, _how in self.forms:
            out |= f.documents
        return out


def _not_a_name_word(word: str) -> bool:
    """A word a table or a payment line ran onto a name: "Received", "POS",
    "Etsy", "DueDate", "Inquiries", "Activity"."""
    from . import ner

    w = re.sub(r"[^a-z]", "", word.casefold())
    if not w:
        return True
    _common, given = ner._lists()
    if w in given or w in names._BY_NICKNAME or w in names.NICKNAMES:
        return False
    return (w in ner.HEADING_WORDS or w in names.NOT_A_NAME_TAIL or w in names.FUNCTION_WORDS
            or w in ner._RAILS_AND_BRANDS or names.ordinary_word(w)
            or (len(w) > 6 and re.search(r"[a-z][A-Z]", word) is not None))


def _split_glued_initial(word: str) -> list[str]:
    """"TysonJ" -> Tyson J; "MFonoti" -> M Fonoti."""
    m = re.fullmatch(r"([A-Z][a-z]{2,})([A-Z])", word)
    if m:
        return [m.group(1), m.group(2)]
    m = re.fullmatch(r"([A-Z])([A-Z][a-z]{2,})", word)
    if m:
        return [m.group(1), m.group(2)]
    return [word]


def _words(text: str) -> list[str]:
    text = names.without_tail(" ".join(text.split()))
    text = re.sub(r"[,;:+$‘’'\"()]", " ", text)
    words = [w for w in (t.strip(".") for t in text.split()) if w]
    words = [piece for w in words for piece in _split_glued_initial(w)]
    # trim what was run onto either end, keeping two words at least
    while len(words) > 2 and _not_a_name_word(words[-1]):
        words.pop()
    while len(words) > 2 and _not_a_name_word(words[0]):
        words.pop(0)
    return words


def _alpha(words: list[str]) -> list[str]:
    return [w for w in words if re.fullmatch(r"[A-Za-z][A-Za-z'’\-]*", w)]


def _fits(word: str, part: str, loose: bool = False) -> bool:
    w, p = word.casefold(), part.casefold()
    if not w or not p:
        return False
    if len(w) == 1:
        return p[0] == w
    if names.same_given(w, p) or names.alike(w, p):
        return True
    # cut off at either end by the scan: "Okaf", "RALD" (for Gerald)
    if len(w) >= 4 and len(w) < len(p) and (p.startswith(w) or p.endswith(w)):
        return True
    # two letters out, where the rest of the name already agrees
    if loose and min(len(w), len(p)) >= 5 and w[0] == p[0]:
        fw, fp = names._ocr_folded(w), names._ocr_folded(p)
        return _edits(fw, fp) <= 2
    return False


def _edits(a: str, b: str) -> int:
    previous = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        current = [i]
        for j, cb in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (ca != cb)))
        previous = current
    return previous[-1]


def _by_rule(form: Form, main: Main) -> bool:
    words = [w for w in _alpha(form.words) if w.casefold() not in _SUFFIX]
    if not words:
        return False
    parts = [main.first, *main.middles, main.last]
    parts = [p for p in parts if p]
    if not any(len(w) > 1 for w in words):
        return False
    for order in (words, words[1:] + words[:1]):          # as written, or surname first
        used, ok = -1, True
        # the surname may be two letters out when the first name is exactly the main's
        loose = bool(order) and order[0].casefold() == main.first.casefold() and len(order) > 1
        for w in order:
            j = next((j for j in range(used + 1, len(parts))
                      if _fits(w, parts[j], loose and parts[j] == main.last)), None)
            if j is None:
                ok = False
                break
            used = j
        if ok and (len(words) > 1 or len(words[0]) > 1):
            # a word that is the surname must be there unless the form is one word
            if len(words) == 1 or any(_fits(w, main.last, loose) for w in words):
                return True
    return False


def _glued(form: Form, main: Main) -> bool:
    letters = re.sub(r"[^a-z]", "", form.text.casefold())
    first = re.sub(r"[^a-z]", "", main.first.casefold())
    last = re.sub(r"[^a-z]", "", main.last.casefold())
    if len(letters) < 6 or not last or last not in letters:
        return False
    if " " in form.text.strip() and len(_alpha(form.words)) > 1:
        return False
    return first[:3] in letters or letters.startswith(("to", "from"))


def _first_names_differ(form: Form, main: Main) -> bool:
    words = _alpha(form.words)
    if len(words) < 2:
        return False
    first = words[0]
    if _fits(words[-1], main.last) and len(first) > 1:
        return not _fits(first, main.first) and not any(_fits(first, m) for m in main.middles)
    return False


def _suffix(words: list[str]) -> str:
    return next((w.casefold() for w in words if w.casefold() in _SUFFIX), "")


def cluster(forms: list[Form], kev_same=None) -> tuple[list[Main], dict[str, list[Form]],
                                                         list[Form]]:
    """(main names with their forms, family surname -> forms, unattached forms).
    ``kev_same(a, b) -> float`` is asked only for what the rules cannot settle."""
    for f in forms:
        f.words = _words(f.text)
    junk = [f for f in forms if not _a_name(f)]
    forms = [f for f in forms if _a_name(f)]
    full = [f for f in forms if len([w for w in _alpha(f.words) if len(w) > 1]) >= 2
            and w_count_ok(f)]
    full.sort(key=lambda f: (-len(f.documents), -len(f.text)))
    mains: list[Main] = []
    rest: list[Form] = []
    for f in full:
        words = [w for w in _alpha(f.words) if w.casefold() not in _SUFFIX]
        candidate = Main(f, words[0], words[-1], words[1:-1], _suffix(f.words))
        home = next((m for m in mains if m.suffix == candidate.suffix
                     and m.form.minor == f.minor and _by_rule(f, m)
                     and not _first_names_differ(f, m)), None)
        if home is not None:
            home.forms.append((f, "rule"))
        elif len(f.documents) >= MAIN_MIN_DOCUMENTS:
            mains.append(candidate)
        else:
            rest.append(f)
    rest += [f for f in forms if f not in full]
    family: dict[str, list[Form]] = {}
    unattached: list[Form] = []
    for f in rest:
        words = [w for w in _alpha(f.words) if w.casefold() not in _SUFFIX]
        fitting = [m for m in mains if m.form.minor == f.minor or len(words) == 1]
        fitting = [m for m in fitting if (_by_rule(f, m) or _glued(f, m))
                   and not _first_names_differ(f, m)
                   and (m.suffix == _suffix(f.words) or not _suffix(f.words))]
        if len(fitting) == 1:
            fitting[0].forms.append((f, "rule" if not _glued(f, fitting[0]) else "glued"))
            continue
        if len(fitting) > 1:
            surnames = {m.last.casefold() for m in fitting}
            if len(words) == 1 and len(surnames) == 1:
                family.setdefault(fitting[0].last.casefold(), []).append(f)
                continue
            # two people the form fits equally: the family if one surname, else alone
            if len(surnames) == 1:
                family.setdefault(fitting[0].last.casefold(), []).append(f)
                continue
        if kev_same is not None and words:
            near = [m for m in mains if not _first_names_differ(f, m)
                    and any(names._ocr_folded(w)[:3] == names._ocr_folded(m.last)[:3]
                            or names._ocr_folded(m.last) in names._ocr_folded(w)
                            for w in words)]
            scored = sorted(((kev_same(f.text, m.form.text), m) for m in near),
                            key=lambda t: -t[0])
            if scored and scored[0][0] >= KEV_SAME:
                scored[0][1].forms.append((f, "kev"))
                continue
        unattached.append(f)
    unattached += junk
    for f in junk:
        f.junk = True
    return mains, family, unattached


def _a_name(f: Form) -> bool:
    """Not a store, a product or a heading: some word is not an everyday one."""
    from . import ner

    if any(not _not_a_name_word(w) for w in _alpha(f.words)):
        return ner.likely_name(" ".join(f.words), "person") or len(_alpha(f.words)) == 1
    return False


def w_count_ok(f: Form) -> bool:
    """A full name has at most five words; more is a line read across."""
    return len(_alpha(f.words)) <= 5


def shape_of(f: Form) -> str:
    if f.junk:
        return "not a name"
    words = _alpha(f.words)
    if not words:
        return "no letters"
    if len(words) == 1:
        return "one word"
    if len(words) == 2:
        return "two words"
    return "three words or more"


# --------------------------------------------------------------------------
# asking the local classifier
# --------------------------------------------------------------------------

def _kev(url: str, state: str, instructions: str, true: str, false: str) -> float:
    import json
    import urllib.request

    body = {"state": state, "model": "kev-latest", "questions": {"q": {
        "type": "noul", "instructions": instructions,
        "criteria": {"true": true, "false": false}}}}
    request = urllib.request.Request(f"{url}/v1/systemone", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return float(json.loads(response.read())["answers"]["q"]["noul"])


def kev_same(url: str):
    """Kev's score that two written names are one person."""
    def ask(a: str, b: str) -> float:
        return _kev(url, f"A: {a}\nB: {b}",
                    "Do A and B refer to the same person (one may be a shorter, initialled, "
                    "misspelled or run-together form of the other)?",
                    "Yes, the same person.", "No, two different people or not a person.")
    return ask


KEV_NAME = 0.5


def kev_is_name(url: str):
    """Kev's score that a string is a person's name - not a store, product or heading."""
    def ask(text: str) -> float:
        return _kev(url, text, "Is this text a person's name - not a store, brand, product, "
                               "company, place or heading?",
                    "Yes, a person's name.", "No - a store, brand, product, company, place or heading.")
    return ask
