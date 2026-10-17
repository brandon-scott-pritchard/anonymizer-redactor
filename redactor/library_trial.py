"""What established libraries make of a set's rows - counts only, as a trial.

Called by redactor.counts_report with --libraries. Each library is run with
its own defaults, nothing tuned on the synthetic corpus; where a library needs
a cutoff, the counts are given at several so the cutoff can be chosen on a
real set. Beside each, the count the app's own rules give today.

- probablepeople (datamade, MIT): is each name row a person, a household or a
  business?
- RapidFuzz (MIT) with SciPy's complete-linkage clustering: how many people
  the name rows fall into. Complete linkage joins two groups only when every
  pair across them is close, so one loose pair cannot chain two people.
- SymSpell (symspellpy, MIT): long words run together - "toABIGAILOKAFOR" -
  split by English word frequencies, with the set's own name words added.
- usaddress (datamade, MIT): each street address split into its parts, and
  rows folded when number, direction and street agree - so a street alone
  folds into the street with its city, state and ZIP.
- regex (mrab, Apache-2.0): each main surname searched with one or two
  letters wrong, over every word the documents hold.

Nothing here writes or returns text from the documents: every value is a count.
"""

from __future__ import annotations

import re
from collections import Counter

PEOPLE = ("person", "minor")
CUTOFFS = (80, 85, 90, 95)
SCORERS = ("WRatio", "token_sort_ratio")


# --------------------------------------------------------------------------
# probablepeople: person, household or business
# --------------------------------------------------------------------------

def person_or_business(rows) -> dict:
    import probablepeople

    from . import name_clusters as NC

    out = Counter()
    kinds = {}
    for e in rows:
        try:
            _parts, kind = probablepeople.tag(e.canonical)
        except probablepeople.RepeatedLabelError:
            kind = "unclear"
        kind = {"Person": "person", "Household": "household",
                "Corporation": "business"}.get(kind, "unclear")
        kinds[e.key] = kind
        form = NC.Form(e.canonical, set(e.documents))
        form.words = NC._words(e.canonical)
        ours = "name" if NC._a_name(form) else "not"
        out[kind] += 1
        out[f"{kind}|{ours}"] += 1
    return {"counts": dict(out), "kinds": kinds}


# --------------------------------------------------------------------------
# RapidFuzz + SciPy: how many people
# --------------------------------------------------------------------------

def _groups(texts: list[str], scorer_name: str, cutoff: int):
    import numpy as np
    from rapidfuzz import fuzz, process, utils
    from scipy.cluster.hierarchy import fcluster, linkage
    from scipy.spatial.distance import squareform

    if len(texts) < 2:
        return list(range(len(texts)))
    scorer = getattr(fuzz, scorer_name)
    similar = process.cdist(texts, texts, scorer=scorer, processor=utils.default_process,
                            workers=-1, dtype=np.float32)
    distance = 100.0 - np.minimum(similar, similar.T)
    np.fill_diagonal(distance, 0.0)
    tree = linkage(squareform(distance, checks=False), method="complete")
    return list(fcluster(tree, t=100 - cutoff, criterion="distance"))


def name_groups(rows, only=None) -> dict:
    """Per scorer and cutoff: groups, and how many documents each spans."""
    rows = [e for e in rows if only is None or only(e)]
    texts = [e.canonical for e in rows]
    out = {}
    for scorer in SCORERS:
        for cutoff in CUTOFFS:
            labels = _groups(texts, scorer, cutoff)
            docs: dict[int, set] = {}
            size = Counter(labels)
            for e, label in zip(rows, labels):
                docs.setdefault(label, set()).update(e.documents)
            out[f"{scorer}|{cutoff}"] = {
                "rows": len(rows), "groups": len(size),
                "in_3_plus": sum(len(d) >= 3 for d in docs.values()),
                "in_10_plus": sum(len(d) >= 10 for d in docs.values()),
                "one_row": sum(n == 1 for n in size.values()),
                "largest": max(size.values(), default=0),
            }
    return out


# --------------------------------------------------------------------------
# SymSpell: words run together
# --------------------------------------------------------------------------

_LONG_WORD = re.compile(r"(?<![A-Za-z0-9@./])[A-Za-z]{8,}(?![A-Za-z0-9@])")


def _symspell(name_words: set[str]):
    from importlib.resources import files

    from symspellpy import SymSpell

    sym = SymSpell(max_dictionary_edit_distance=2, prefix_length=7)
    sym.load_dictionary(str(files("symspellpy") / "frequency_dictionary_en_82_765.txt"),
                        term_index=0, count_index=1)
    # the main names' words at the dictionary's median frequency - an
    # ordinary word, not a favoured one, so a split lands on a name only where
    # the letters call for it
    counts = sorted(sym.words.values())
    median = counts[len(counts) // 2] if counts else 1
    for w in name_words:
        if w not in sym.words:
            sym.create_dictionary_entry(w, median)
    return sym


def run_together(texts: dict[str, str], mains: list[tuple[str, str]], store) -> dict:
    """Long words SymSpell splits into a main name's words."""
    from symspellpy import Verbosity

    from . import engine

    people = [(_letters(f), _letters(l)) for f, l in mains]
    people = [(f, l) for f, l in people if len(f) >= 2 and len(l) >= 2]
    name_words = {w for pair in people for w in pair}
    sym = _symspell(name_words)
    tokens = Counter()
    for text in texts.values():
        for m in _LONG_WORD.finditer(text):
            tokens[m.group(0)] += 1
    out = Counter()
    for token, n in tokens.items():
        low = token.casefold()
        if low in name_words or sym.lookup(low, Verbosity.TOP, max_edit_distance=0):
            continue                           # a word or a main name word as it stands
        out["long_words_not_in_dictionary"] += 1
        for edits, label in ((0, "split"), (1, "split_and_corrected")):
            pieces = sym.word_segmentation(low, max_edit_distance=edits).corrected_string.split()
            if len(pieces) < 2:
                continue
            out[f"{label}_into_words"] += 1
            if any(p in name_words for p in pieces):
                out[f"{label}_holding_a_name_word"] += 1
            if any(f in pieces and l in pieces for f, l in people):
                out[f"{label}_holding_a_whole_name"] += 1
    # what the app's own run-together pass finds in the same texts; it reads
    # only heads that have a stand-in, which the run gives them - given here
    from . import grouping
    for e in store.persons():
        if e.enabled and e.person is not None and e.surrogate is None \
                and grouping.head_of(store, e) is e:
            e.surrogate = store._unique_person_surrogate(e.person, misread=False)
    found = set()
    for text in texts.values():
        try:
            for hit in engine.run_together_hits(text, store, redact=False):
                found.add(hit.text.casefold())
        except Exception:
            out["app_pass_errors"] += 1
            break
    out["app_pass_distinct_words"] = len(found)
    out["main_names_used"] = len(people)
    return dict(out)


def _letters(text: str) -> str:
    return re.sub(r"[^a-z]", "", text.casefold())


# --------------------------------------------------------------------------
# usaddress: street addresses folded by their parts
# --------------------------------------------------------------------------

def addresses(rows) -> dict:
    import usaddress

    from . import grouping

    out = Counter()
    keys = Counter()
    ours = Counter()
    for e in rows:
        out["rows"] += 1
        try:
            parts, kind = usaddress.tag(e.canonical)
        except usaddress.RepeatedLabelError:
            out["could_not_parse"] += 1
            continue
        if kind != "Street Address" or "AddressNumber" not in parts or "StreetName" not in parts:
            out[f"parsed_as_{'po_box' if kind == 'PO Box' else 'other'}"] += 1
            continue
        out["parsed_as_street"] += 1
        if "ZipCode" in parts or "PlaceName" in parts:
            out["with_city_or_zip"] += 1
        key = "|".join(re.sub(r"[^a-z0-9]", "", parts.get(p, "").casefold()) for p in
                       ("AddressNumber", "StreetNamePreDirectional", "StreetName",
                        "StreetNamePostDirectional", "OccupancyIdentifier"))
        keys[key] += 1
        parsed = grouping.parse_address(e.canonical)
        if parsed is not None and parsed.street:
            ours[f"{parsed.number}|{'-'.join(parsed.street)}|{parsed.unit or ''}"] += 1
    out["distinct_by_library"] = len(keys)
    out["folded_by_library"] = sum(n - 1 for n in keys.values())
    out["distinct_by_app"] = len(ours)
    out["folded_by_app"] = sum(n - 1 for n in ours.values())
    return dict(out)


# --------------------------------------------------------------------------
# regex: each main surname with letters wrong
# --------------------------------------------------------------------------

def surname_search(texts: dict[str, str], surnames: set[str], sym, listed_words: set[str]) -> dict:
    import regex

    from symspellpy import Verbosity

    words = Counter()
    for text in texts.values():
        words.update(w for w in re.findall(r"[A-Za-z]{4,}", text))
    out = Counter()
    out["surnames_searched"] = len(surnames)
    for surname in surnames:
        exact = regex.compile(rf"(?i){regex.escape(surname)}")
        for errors in (1, 2):
            if errors == 2 and len(surname) < 7:
                continue
            fuzzy = regex.compile(rf"(?i)(?:{regex.escape(surname)}){{e<={errors}}}")
            for word, n in words.items():
                if exact.fullmatch(word):
                    if errors == 1:
                        out["exact_occurrences"] += n
                    continue
                if not fuzzy.fullmatch(word):
                    continue
                low = word.casefold()
                if sym.lookup(low, Verbosity.TOP, max_edit_distance=0):
                    out[f"e{errors}_dictionary_words_left_out"] += 1
                    continue
                out[f"e{errors}_distinct_words"] += 1
                out[f"e{errors}_occurrences"] += n
                if low in listed_words:
                    out[f"e{errors}_distinct_already_on_the_list"] += 1
    return dict(out)


def together(rows, kinds: dict, kev_scores: dict) -> dict:
    """Kev's person-name check and probablepeople's call, row by row."""
    from . import name_clusters as NC

    out = Counter()
    for e in rows:
        if e.key not in kev_scores:
            continue
        kev = kev_scores[e.key] >= NC.KEV_NAME
        lib = kinds.get(e.key) != "business"           # household and unclear count as people
        out[{(True, True): "both_person", (True, False): "kev_person_library_business",
             (False, True): "kev_not_library_person", (False, False): "both_not"}[(kev, lib)]] += 1
    return dict(out)


def mains_without(rows, drop) -> dict:
    """Main names by our rules once the dropped rows are gone."""
    from . import name_clusters as NC

    kept = [e for e in rows if not drop(e)]
    forms = [NC.Form(e.canonical, set(e.documents), e.category == "minor") for e in kept]
    mains, _family, unattached = NC.cluster(forms)
    return {"rows": len(kept), "main_names": len(mains),
            "in_10_plus": sum(len(m.documents) >= 10 for m in mains),
            "unattached": len(unattached)}


# --------------------------------------------------------------------------

def trial(store, texts: dict[str, str], progress=print, kev_scores: dict | None = None,
          mains: dict | None = None) -> dict:
    from . import name_clusters as NC

    kev_scores = kev_scores or {}
    mains = mains or {}
    rows = [e for e in store.entities.values() if e.enabled and e.category in PEOPLE
            and e.documents]
    progress("libraries: person or business")
    pb = person_or_business(rows)
    kinds = pb["kinds"]
    both = {}
    if kev_scores:
        both = together(rows, kinds, kev_scores)
        not_kev = lambda e: kev_scores.get(e.key, 1.0) < NC.KEV_NAME
        not_lib = lambda e: kinds.get(e.key) == "business"
        both["drop_both"] = mains_without(rows, lambda e: not_kev(e) and not_lib(e))
        both["drop_either"] = mains_without(rows, lambda e: not_kev(e) or not_lib(e))
        both["drop_kev"] = mains_without(rows, not_kev)
    both["drop_library"] = mains_without(rows, lambda e: kinds.get(e.key) == "business")
    progress("libraries: grouping names")
    groups_all = name_groups(rows)
    groups_people = name_groups(rows, only=lambda e: kinds.get(e.key) in ("person", "household"))
    # the main names the rest is measured on: Kev's when it ran, else ours
    # less the rows the library calls businesses
    confirmed = mains.get("kev")
    if confirmed is None:
        kept = [e for e in rows if kinds.get(e.key) != "business"]
        forms = [NC.Form(e.canonical, set(e.documents), e.category == "minor") for e in kept]
        confirmed = [(m.first, m.last) for m in NC.cluster(forms)[0]]
    progress("libraries: words run together")
    glued = run_together(texts, confirmed, store)
    glued["confirmed_by_kev"] = 1 if "kev" in mains else 0
    progress("libraries: addresses")
    addr = addresses([e for e in store.entities.values() if e.enabled
                      and e.category == "street_address" and e.documents])
    progress("libraries: surnames with letters wrong")
    surnames = {_letters(l) for _f, l in confirmed}
    surnames = {x for x in surnames if len(x) >= 5}
    name_words = {w.casefold() for e in rows for w in re.findall(r"[A-Za-z]{2,}", e.canonical)}
    search = surname_search(texts, surnames, _symspell(set()), name_words)
    return {"person_or_business": pb["counts"], "together": both, "groups_all": groups_all,
            "groups_people": groups_people, "run_together": glued, "addresses": addr,
            "surname_search": search, "rules_main_names": len(mains.get("rules", []))}


# --------------------------------------------------------------------------
# the report section - fixed labels and numbers only
# --------------------------------------------------------------------------

def render(lib: dict) -> list[str]:
    pb = lib["person_or_business"]
    lines = ["LIBRARY TRIAL", "",
             "NAME ROWS, PERSON OR BUSINESS (probablepeople)",
             "  library says | rows | of those our screen calls a name | our screen calls not a name"]
    for kind in ("person", "household", "business", "unclear"):
        lines.append(f"  {kind} | {pb.get(kind, 0)} | {pb.get(kind + '|name', 0)} | "
                     f"{pb.get(kind + '|not', 0)}")
    lines += ["", f"  main names by our rules today: {lib['rules_main_names']}", ""]
    t = lib.get("together", {})
    lines += ["NAME ROWS, KEV AND probablepeople TOGETHER",
              f"  both say a person: {t.get('both_person', 0)}",
              f"  Kev says a person, the library a business: {t.get('kev_person_library_business', 0)}",
              f"  Kev says not a person, the library a person: {t.get('kev_not_library_person', 0)}",
              f"  both say not a person: {t.get('both_not', 0)}",
              "  rows dropped | rows kept | main names by our rules | in 10 or more documents | unattached"]
    for key, label in (("drop_both", "where both say not a person"),
                       ("drop_kev", "where Kev says not a person"),
                       ("drop_library", "where the library says a business"),
                       ("drop_either", "where either says not a person")):
        g = t.get(key, {})
        lines.append(f"  {label} | {g.get('rows', 0)} | {g.get('main_names', 0)} | "
                     f"{g.get('in_10_plus', 0)} | {g.get('unattached', 0)}")
    lines.append("")
    for title, key in (("NAME GROUPS, EVERY NAME ROW (RapidFuzz with complete linkage)", "groups_all"),
                       ("NAME GROUPS, ONLY ROWS THE LIBRARY CALLS PEOPLE", "groups_people")):
        lines += [title,
                  "  scorer | cutoff | rows | groups | groups in 3 or more documents | "
                  "in 10 or more | groups of one row | largest group"]
        for scorer in SCORERS:
            for cutoff in CUTOFFS:
                g = lib[key][f"{scorer}|{cutoff}"]
                lines.append(f"  {scorer} | {cutoff} | {g['rows']} | {g['groups']} | {g['in_3_plus']} | "
                             f"{g['in_10_plus']} | {g['one_row']} | {g['largest']}")
        lines.append("")
    r = lib["run_together"]
    lines += ["WORDS RUN TOGETHER (SymSpell), MAIN NAMES ONLY",
              f"  main names used: {r.get('main_names_used', 0)} "
              f"(confirmed by Kev: {'yes' if r.get('confirmed_by_kev') else 'no'})",
              f"  long words not in the dictionary or the main names: {r.get('long_words_not_in_dictionary', 0)}",
              f"  split into words: {r.get('split_into_words', 0)}",
              f"  split, holding a main name word: {r.get('split_holding_a_name_word', 0)}",
              f"  split, holding a whole main name: {r.get('split_holding_a_whole_name', 0)}",
              f"  split and corrected into words: {r.get('split_and_corrected_into_words', 0)}",
              f"  split and corrected, holding a main name word: "
              f"{r.get('split_and_corrected_holding_a_name_word', 0)}",
              f"  split and corrected, holding a whole main name: "
              f"{r.get('split_and_corrected_holding_a_whole_name', 0)}",
              f"  distinct words our run together pass replaces today: {r.get('app_pass_distinct_words', 0)}",
              f"  our pass errors: {r.get('app_pass_errors', 0)}", ""]
    a = lib["addresses"]
    lines += ["STREET ADDRESSES (usaddress)",
              f"  rows: {a.get('rows', 0)}",
              f"  parsed as a street address: {a.get('parsed_as_street', 0)}",
              f"  of those with a city or ZIP: {a.get('with_city_or_zip', 0)}",
              f"  parsed as a post office box: {a.get('parsed_as_po_box', 0)}",
              f"  parsed as something else: {a.get('parsed_as_other', 0)}",
              f"  could not parse: {a.get('could_not_parse', 0)}",
              f"  distinct addresses by the library: {a.get('distinct_by_library', 0)}",
              f"  rows folded by the library: {a.get('folded_by_library', 0)}",
              f"  distinct addresses by our parser: {a.get('distinct_by_app', 0)}",
              f"  rows folded by our parser: {a.get('folded_by_app', 0)}", ""]
    s = lib["surname_search"]
    lines += ["MAIN SURNAMES WITH LETTERS WRONG (regex), DICTIONARY WORDS LEFT OUT",
              f"  surnames searched, five letters or more: {s.get('surnames_searched', 0)}",
              f"  exact occurrences: {s.get('exact_occurrences', 0)}"]
    for e in (1, 2):
        lines += [f"  {e} letter wrong, distinct words: {s.get(f'e{e}_distinct_words', 0)}"
                  + (" (surnames of seven letters or more)" if e == 2 else ""),
                  f"  {e} letter wrong, occurrences: {s.get(f'e{e}_occurrences', 0)}",
                  f"  {e} letter wrong, distinct words already on the list: "
                  f"{s.get(f'e{e}_distinct_already_on_the_list', 0)}",
                  f"  {e} letter wrong, dictionary words left out: "
                  f"{s.get(f'e{e}_dictionary_words_left_out', 0)}"]
    lines.append("")
    return lines


def empty() -> dict:
    """Every label at zero - the report's vocabulary is read from this."""
    groups = {f"{s}|{c}": {"rows": 0, "groups": 0, "in_3_plus": 0, "in_10_plus": 0, "one_row": 0,
                           "largest": 0} for s in SCORERS for c in CUTOFFS}
    return {"person_or_business": {}, "together": {}, "groups_all": groups, "groups_people": groups,
            "run_together": {}, "addresses": {}, "surname_search": {}, "rules_main_names": 0}
