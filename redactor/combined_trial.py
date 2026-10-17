"""The review list with everything applied together - counts only, as a trial.

Called by redactor.counts_report with --combined. Our own rules, the
libraries and Kev, each doing the part it measured best at:

Names
  - who is a person: Kev's person-name check and probablepeople together. A
    row both call not a person is left off the list (left as written); a row
    one of them doubts stays on the list, marked for a look.
  - main names: a full name, first and last, in ten documents or more, that
    Kev and probablepeople both call a person.
  - forms of a main name: redactor.name_match (RapidFuzz and Jellyfish, with
    the OCR digits read back as letters) at HIGH attaches; between LOW and HIGH
    Kev is asked whether the two are one person. The guards stand whatever
    either says: two different first names, a child and an adult, a Jr and a
    Sr. A form that fits two people of one surname goes to the family; two
    people of two surnames, to neither.
  - a surname alone goes to the family; a first name alone to the one main
    name that has it.
  - run together: SymSpell, given the main names' words, beside our own pass.
  - misread surnames: regex with one or two letters wrong, dictionary words
    left out, and counted apart where the first name stands just before.
Addresses: our parser and usaddress together - two rows are one address when
  either says so, so a street alone folds into the street with its city and ZIP.
Accounts: the title rule (in the scan itself); a masked tail folds into the one
  full account that ends in its digits.
Everything else as the scan found it.

Nothing from the documents is returned: every value is a count.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict

from . import name_clusters as NC
from . import name_match as M

PEOPLE = ("person", "minor")
ACCOUNT_KINDS = ("bank_account", "masked_account", "credit_card", "investment_account")
KEV_ASK = 2                      # main names Kev is asked about per form, best scored first
MAIN_DOCUMENTS = 10              # a full name found this widely is a main name (Brandon, 2026-10-16)


# --------------------------------------------------------------------------
# names
# --------------------------------------------------------------------------

def _screen(rows, kev_scores: dict, kinds: dict) -> dict[str, str]:
    """person / flagged / not, per row."""
    out = {}
    for e in rows:
        lib_person = kinds.get(e.key) != "business"
        kev = kev_scores.get(e.key)
        kev_person = True if kev is None else kev >= NC.KEV_NAME
        if kev_person and lib_person:
            out[e.key] = "person"
        elif kev_person or lib_person:
            out[e.key] = "flagged"
        else:
            out[e.key] = "not"
    return out


def _parts(form: NC.Form) -> tuple[str, list[str], str, str]:
    words = [w for w in NC._alpha(form.words) if w.casefold() not in NC._SUFFIX]
    return words[0], words[1:-1], words[-1], NC._suffix(form.words)


def _guards_ok(form: NC.Form, main: NC.Main) -> bool:
    if form.minor != main.form.minor and len(NC._alpha(form.words)) > 1:
        return False
    suffix = NC._suffix(form.words)
    if suffix and main.suffix and suffix != main.suffix:
        return False
    return not NC._first_names_differ(form, main)


def _score(form: NC.Form, main: NC.Main) -> float:
    return M.score(form.words, main.first, main.middles, main.last, form.text, main.form.text)


def names(rows, kev_scores: dict, kinds: dict, kev_same=None, progress=print) -> dict:
    screen = _screen(rows, kev_scores, kinds)
    forms = {}
    for e in rows:
        f = NC.Form(e.canonical, set(e.documents), e.category == "minor")
        f.words = NC._words(e.canonical)
        forms[e.key] = f
    out = Counter()
    out["rows"] = len(rows)
    out["left_off_not_a_person"] = sum(v == "not" for v in screen.values())
    out["kept_marked_for_a_look"] = sum(v == "flagged" for v in screen.values())
    kept = [e for e in rows if screen[e.key] != "not"]

    # main names, the most widely found first
    full = [e for e in kept if screen[e.key] == "person"
            and len([w for w in NC._alpha(forms[e.key].words) if len(w) > 1]) >= 2
            and NC.w_count_ok(forms[e.key])
            and len(e.documents) >= MAIN_DOCUMENTS]
    full.sort(key=lambda e: (-len(e.documents), -len(e.canonical)))
    mains: list[NC.Main] = []
    placed = set()
    for e in full:
        f = forms[e.key]
        first, middles, last, suffix = _parts(f)
        home = next((m for m in mains if _guards_ok(f, m) and _score(f, m) >= M.HIGH), None)
        if home is not None:
            home.forms.append((f, "fuzzy"))
        else:
            mains.append(NC.Main(f, first, last, middles, suffix))
        placed.add(e.key)
    progress(f"combined: {len(mains)} main names; attaching the other forms")

    family: dict[str, list] = defaultdict(list)
    unattached = []
    asked = 0
    closest = Counter()
    for e in kept:
        if e.key in placed:
            continue
        f = forms[e.key]
        words = [w for w in NC._alpha(f.words) if w.casefold() not in NC._SUFFIX]
        scored = sorted(((_score(f, m), m) for m in mains if _guards_ok(f, m)),
                        key=lambda t: -t[0])
        if not scored and mains:
            closest["blocked_by_a_guard"] += 1
        elif scored:
            top = scored[0][0]
            closest["0.90_plus" if top >= M.HIGH else "0.75_to_0.90" if top >= M.LOW
                    else "0.50_to_0.75" if top >= 0.5 else "under_0.50"] += 1
        sure = [(s, m) for s, m in scored if s >= M.HIGH]
        if sure:
            surnames = {m.last.casefold() for _s, m in sure}
            if len(sure) == 1:
                sure[0][1].forms.append((f, "fuzzy"))
            elif len(surnames) == 1:
                family[next(iter(surnames))].append(f)
            else:
                out["fits_two_surnames_left_alone"] += 1
                unattached.append(f)
            continue
        band = [(s, m) for s, m in scored if M.LOW <= s < M.HIGH][:KEV_ASK]
        if band and kev_same is not None:
            asked += len(band)
            answers = sorted(((kev_same(f.text, m.form.text), m) for _s, m in band),
                             key=lambda t: -t[0])
            if answers[0][0] >= NC.KEV_SAME:
                answers[0][1].forms.append((f, "kev"))
                continue
        if len(words) == 1:
            w = words[0].casefold()
            by_surname = [m for m in mains if M._letters(m.last) == M._letters(w)
                          or (len(w) >= 7 and M.names.alike(M._letters(w), M._letters(m.last)))]
            if by_surname:
                family[by_surname[0].last.casefold()].append(f)
                continue
            by_first = [m for m in mains if M._letters(m.first) == M._letters(w)]
            if len(by_first) == 1:
                by_first[0].forms.append((f, "first name"))
                continue
        unattached.append(f)
    how = Counter(h for m in mains for _f, h in m.forms)
    out["kev_questions"] = asked
    for k, v in closest.items():
        out[f"closest_{k}"] = v
    out["main_names"] = len(mains)
    out["main_names_in_10_plus"] = sum(len(m.documents) >= 10 for m in mains)
    out["attached_fuzzy"] = how.get("fuzzy", 0)
    out["attached_kev"] = how.get("kev", 0)
    out["attached_first_name"] = how.get("first name", 0)
    out["family_surnames"] = len(family)
    out["family_forms"] = sum(len(v) for v in family.values())
    out["unattached"] = len(unattached)
    out["unattached_seen_once"] = sum(len(u.documents) == 1 for u in unattached)
    out["unattached_marked_for_a_look"] = sum(
        screen.get(e.key) == "flagged" for e in kept if forms[e.key] in unattached)
    shapes = Counter(NC.shape_of(u) for u in unattached)
    out["unattached_one_word"] = shapes.get("one word", 0)
    out["unattached_two_words"] = shapes.get("two words", 0)
    out["unattached_three_plus"] = shapes.get("three words or more", 0)
    # the joins: every form to its main name, every family form to the others
    key_of = {id(f): k for k, f in forms.items()}
    links = []
    for m in mains:
        links += [(key_of[id(m.form)], key_of[id(f)]) for f, _h in m.forms]
    for forms_of in family.values():
        links += [(key_of[id(forms_of[0])], key_of[id(f)]) for f in forms_of[1:]]
    dropped = {k for k, v in screen.items() if v == "not"}
    return {"counts": dict(out), "links": links, "dropped": dropped, "mains": mains,
            "screen": screen}


# --------------------------------------------------------------------------
# run together, and misread surnames
# --------------------------------------------------------------------------

def run_together(texts: dict[str, str], mains, store) -> dict:
    from symspellpy import Verbosity

    from . import engine, grouping, library_trial as LT

    people = [(M._letters(m.first), M._letters(m.last)) for m in mains]
    people = [(f, l) for f, l in people if len(f) >= 2 and len(l) >= 2]
    name_words = {w for pair in people for w in pair}
    sym = LT._symspell(name_words)
    tokens = set()
    for text in texts.values():
        tokens.update(m.group(0) for m in LT._LONG_WORD.finditer(text))
    by_symspell = set()
    for token in tokens:
        low = token.casefold()
        if low in name_words or sym.lookup(low, Verbosity.TOP, max_edit_distance=0):
            continue
        pieces = sym.word_segmentation(low, max_edit_distance=1).corrected_string.split()
        if len(pieces) >= 2 and any(f in pieces and l in pieces for f, l in people):
            by_symspell.add(low)
    for e in store.persons():
        if e.enabled and e.person is not None and e.surrogate is None \
                and grouping.head_of(store, e) is e:
            e.surrogate = store._unique_person_surrogate(e.person, misread=False)
    by_ours = set()
    for text in texts.values():
        try:
            by_ours.update(h.text.casefold() for h in engine.run_together_hits(text, store, redact=False))
        except Exception:
            break
    return {"symspell": len(by_symspell), "ours": len(by_ours),
            "both": len(by_symspell & by_ours), "either": len(by_symspell | by_ours)}


def misread_surnames(texts: dict[str, str], mains, listed_words: set[str]) -> dict:
    import regex
    from symspellpy import Verbosity

    from . import library_trial as LT

    sym = LT._symspell(set())
    words = Counter()
    for text in texts.values():
        words.update(re.findall(r"[A-Za-z0-9]{4,}", text))
    found: dict[str, set[str]] = defaultdict(set)          # misread word -> first names
    for m in mains:
        surname = M._letters(m.last)
        if len(surname) < 5:
            continue
        errors = 2 if len(surname) >= 7 else 1
        fuzzy = regex.compile(rf"(?i)(?:{regex.escape(surname)}){{e<={errors}}}")
        for word in words:
            low = M._letters(word)
            if low == surname or not fuzzy.fullmatch(low):
                continue
            if sym.lookup(low, Verbosity.TOP, max_edit_distance=0):
                continue
            found[word].add(M._letters(m.first))
    beside = set()
    if found:
        pattern = re.compile(r"([A-Za-z]+)\.?\s+(" + "|".join(re.escape(w) for w in found) + r")\b")
        for text in texts.values():
            for mt in pattern.finditer(text):
                before = M._letters(mt.group(1))
                if any(before == f or (len(before) == 1 and before == f[:1])
                       for f in found[mt.group(2)]):
                    beside.add(mt.group(2))
    return {"distinct": len(found), "occurrences": sum(words[w] for w in found),
            "beside_the_first_name": len(beside),
            "already_on_the_list": sum(w.casefold() in listed_words for w in found)}


# --------------------------------------------------------------------------
# addresses and accounts
# --------------------------------------------------------------------------

def _union(n: int, pairs) -> list[int]:
    parent = list(range(n))

    def find(i):
        while parent[i] != i:
            parent[i] = parent[parent[i]]
            i = parent[i]
        return i
    for a, b in pairs:
        parent[find(a)] = find(b)
    return [find(i) for i in range(n)]


def addresses(rows) -> list[tuple[str, str]]:
    """Pairs of rows our parser or usaddress calls one address."""
    import usaddress

    from . import grouping

    keys = defaultdict(list)
    for e in rows:
        parsed = grouping.parse_address(e.canonical)
        if parsed is not None and parsed.street:
            keys["o" + f"{parsed.number}|{'-'.join(parsed.street)}|{parsed.unit or ''}"].append(e.key)
        try:
            parts, kind = usaddress.tag(e.canonical)
        except usaddress.RepeatedLabelError:
            continue
        if kind == "Street Address" and "AddressNumber" in parts and "StreetName" in parts:
            key = "|".join(re.sub(r"[^a-z0-9]", "", parts.get(p, "").casefold()) for p in
                           ("AddressNumber", "StreetNamePreDirectional", "StreetName",
                            "StreetNamePostDirectional", "OccupancyIdentifier"))
            keys["u" + key].append(e.key)
    return [(ids[0], j) for ids in keys.values() for j in ids[1:]]


def accounts(rows) -> list[tuple[str, str]]:
    """A masked tail with the one full account that ends in its digits."""
    tails = defaultdict(list)
    for e in rows:
        d = re.sub(r"\D", "", e.canonical)
        if e.category != "masked_account" and len(d) >= 6:
            tails[d[-4:]].append(e.key)
    links = []
    for e in rows:
        d = re.sub(r"\D", "", e.canonical)
        if e.category == "masked_account" and len(d) >= 4:
            owners = tails.get(d[-4:], [])
            if len(owners) == 1:
                links.append((owners[0], e.key))
    return links


def _components(keys: list[str], links) -> dict[str, str]:
    parent = {k: k for k in keys}

    def find(k):
        while parent[k] != k:
            parent[k] = parent[parent[k]]
            k = parent[k]
        return k
    for a, b in links:
        if a in parent and b in parent:
            parent[find(a)] = find(b)
    return {k: find(k) for k in keys}


# --------------------------------------------------------------------------
# whose an address is: the name printed with it
# --------------------------------------------------------------------------

TIE_LINES = 3                     # lines above an address a mailing block puts its name on
# an employer or organisation on the list is the party's - where they work
# identifies them as their home does - and an address under it is kept with
# the parties'; "a business" is one the list does not hold
#
# An appraisal's comparable sales - the neighbours' houses, within a mile of
# the party's home - once read as a business's, through the column headings
# above them; reading stops at another entry or a heading now (_context),
# and they come out "nothing", which is kept.
TIES = ("a main name or the family", "an employer or organisation on the list",
        "another person", "a business not on the list", "nothing")


def _street_pattern(text: str):
    """The address's first three words, as written with any spacing."""
    tokens = re.findall(r"[A-Za-z0-9#]+", text)[:3]
    if len(tokens) < 2:
        return None
    return re.compile(r"(?<![A-Za-z0-9])" + r"[\s,.]+".join(re.escape(t) for t in tokens)
                      + r"(?![A-Za-z0-9])", re.IGNORECASE)


# a line holding another street address starts another entry - the row
# above in a list of addresses - and a column heading names no one
_ADDRESS_LINE = re.compile(r"\b\d{1,6}\s+(?:[NSEW]\.?\s+)?[A-Za-z0-9]+(?:\s+[A-Za-z]+)?\s+"
                           r"(?:st|street|ave|avenue|rd|road|dr|drive|ln|lane|cir|circle|blvd|"
                           r"way|ct|court|pl|place|pkwy|hwy|ter|trl)\b", re.IGNORECASE)
_HEADING_LINE = re.compile(r"\baddress(?:es)?\b", re.IGNORECASE)


def _context(text: str, start: int) -> list[str]:
    """The line up to the address, and the lines above it - up to the line
    that starts another entry or heads a column."""
    head = text[:start]
    lines = head.split("\n")
    out = [lines[-1].strip()] if lines[-1].strip() else []
    for line in reversed(lines[:-1]):
        line = line.strip()
        if not line:
            continue
        if len(out) >= TIE_LINES + 1 or _ADDRESS_LINE.search(line) or _HEADING_LINE.search(line):
            break
        out.append(line)
    return out


def address_ties(texts: dict[str, str], groups: list[dict], by_key: dict, mains, screen: dict,
                 orgs: list[str]) -> dict:
    import probablepeople

    people = []
    for m in mains:
        first, last = M._letters(m.first), M._letters(m.last)
        nicks = set(M.names.NICKNAMES.get(first, ()))
        people.append((first, last, nicks))
    family = {last for _f, last, _n in people if len(last) >= 4}
    others = []
    for key, verdict in screen.items():
        e = by_key.get(key)
        if e is None or verdict != "person":
            continue
        words = [M._letters(w) for w in e.canonical.split()]
        words = [w for w in words if len(w) >= 2]
        if len(words) >= 2:
            others.append(set(words))
    org_texts = [o.casefold() for o in orgs if len(o) >= 4]
    corp: dict[str, bool] = {}

    def on_list(line: str) -> bool:
        low = line.casefold()
        return any(o in low for o in org_texts)

    def business(line: str) -> bool:
        # a line of words: "0.25 mi", "$211.68" or a column of figures is
        # not a business, whatever the tagger makes of what letters it has
        visible = [ch for ch in line if not ch.isspace()]
        if not visible or sum(ch.isalpha() for ch in visible) / len(visible) < 0.6:
            return False
        if not any(len(w) >= 3 for w in re.findall(r"[A-Za-z]+", line)):
            return False
        clean = " ".join(re.findall(r"[A-Za-z&'.-]+", line))[:80]
        if len(clean) < 3:
            return False
        if clean not in corp:
            try:
                corp[clean] = probablepeople.tag(clean)[1] == "Corporation"
            except probablepeople.RepeatedLabelError:
                corp[clean] = False
        return corp[clean]

    def tie(lines: list[str]) -> str:
        words = set()
        for line in lines:
            words.update(M._letters(w) for w in re.findall(r"[A-Za-z0-9'’.-]+", line))
        for first, last, nicks in people:
            if last in words and (first in words or first[:1] in words or nicks & words):
                return TIES[0]
        if family & words:
            return TIES[0]
        if any(on_list(line) for line in lines):
            return TIES[1]
        if any(o <= words for o in others):
            return TIES[2]
        if any(business(line) for line in lines):
            return TIES[3]
        return TIES[4]

    rank = {t: i for i, t in enumerate(TIES)}
    decided = []
    for g in groups:
        if g["category"] != "street_address":
            continue
        patterns_ = [p for p in (_street_pattern(by_key[k].canonical) for k in g["keys"]
                                 if k in by_key) if p is not None]
        best = TIES[-1]
        for text in texts.values():
            for pattern in patterns_:
                for mt in pattern.finditer(text):
                    t = tie(_context(text, mt.start()))
                    if rank[t] < rank[best]:
                        best = t
                    if best == TIES[0]:
                        break
                if best == TIES[0]:
                    break
            if best == TIES[0]:
                break
        decided.append((g, best))
    out = {t: Counter() for t in TIES}
    for g, best in decided:
        docs = len(g["documents"])
        out[best]["groups"] += 1
        out[best]["in_2_plus"] += docs >= 2
        out[best]["in_3_plus"] += docs >= 3
        out[best]["in_1"] += docs == 1
    return {t: dict(c) for t, c in out.items()}


# --------------------------------------------------------------------------

def trial(store, texts: dict[str, str], groups: list[dict], kev_url: str | None,
          kev_scores: dict, progress=print, leave=frozenset()) -> dict:
    from . import library_trial as LT

    rows = [e for e in store.entities.values() if e.enabled and e.documents]
    people = [e for e in rows if e.category in PEOPLE]
    progress("combined: person or business")
    kinds = LT.person_or_business(people)["kinds"]
    kev_same = NC.kev_same(kev_url) if kev_url else None
    progress("combined: main names and their forms")
    named = names(people, kev_scores, kinds, kev_same, progress)
    progress("combined: words run together")
    glued = run_together(texts, named["mains"], store)
    progress("combined: misread surnames")
    listed = {w.casefold() for e in people for w in re.findall(r"[A-Za-z0-9]{2,}", e.canonical)}
    misread = misread_surnames(texts, named["mains"], listed)
    progress("combined: whose each address is")
    by_key_all = {e.key: e for e in store.entities.values()}
    orgs = [e.canonical for e in rows if e.category in ("organization", "employer")]
    ties = address_ties(texts, groups, by_key_all, named["mains"], named["screen"], orgs)
    progress("combined: addresses and accounts")
    links = list(named["links"])
    links += addresses([e for e in rows if e.category == "street_address"])
    links += accounts([e for e in rows if e.category in ACCOUNT_KINDS])
    # the scan's own groups stand: every form joined to its group's first
    for g in groups:
        links += [(g["keys"][0], k) for k in g["keys"][1:]]
    by_key = {e.key: e for e in rows}
    kind_of = {}
    for g in groups:
        kind = g["category"]
        kind = "person" if kind in PEOPLE else "bank_account" if kind in ACCOUNT_KINDS else kind
        for k in g["keys"]:
            kind_of[k] = kind
    keys = [k for k in kind_of if k in by_key]
    roots = _components(keys, links)
    after = defaultdict(dict)                     # kind -> root -> documents
    for k in keys:
        if k in named["dropped"]:
            continue
        after[kind_of[roots[k]]].setdefault(roots[k], set()).update(by_key[k].documents)
    before = Counter()
    for g in groups:
        kind = g["category"]
        before["person" if kind in PEOPLE else "bank_account" if kind in ACCOUNT_KINDS
               else kind] += 1
    table = {}
    for kind in sorted(set(before) | set(after)):
        a = list(after.get(kind, {}).values())
        table[kind] = {"before": before.get(kind, 0), "after": len(a),
                       "after_2_plus": sum(len(d) >= 2 for d in a),
                       "after_3_plus": sum(len(d) >= 3 for d in a), "leave": kind in leave}
    return {"names": named["counts"], "run_together": glued, "misread": misread,
            "kev": 1 if kev_url else 0, "table": table, "address_ties": ties}


# --------------------------------------------------------------------------
# the report section - fixed labels and numbers only
# --------------------------------------------------------------------------

def render(r: dict) -> list[str]:
    from . import categories

    n = r["names"]
    lines = ["COMBINED: KEV, THE LIBRARIES AND OUR RULES TOGETHER", "",
             f"  Kev used: {'yes' if r.get('kev') else 'no'}", "",
             "NAMES",
             f"  name rows: {n.get('rows', 0)}",
             f"  left off the list, Kev and probablepeople both say not a person: "
             f"{n.get('left_off_not_a_person', 0)}",
             f"  kept and marked for a look, one of them doubts it: {n.get('kept_marked_for_a_look', 0)}",
             f"  main names: {n.get('main_names', 0)}",
             f"  main names found in 10 or more documents: {n.get('main_names_in_10_plus', 0)}",
             f"  forms attached by spelling (RapidFuzz and Jellyfish): {n.get('attached_fuzzy', 0)}",
             f"  forms attached by Kev: {n.get('attached_kev', 0)} "
             f"(Kev asked {n.get('kev_questions', 0)} times)",
             f"  first names alone attached: {n.get('attached_first_name', 0)}",
             f"  family surnames: {n.get('family_surnames', 0)} holding {n.get('family_forms', 0)} forms",
             f"  forms fitting two surnames, left alone: {n.get('fits_two_surnames_left_alone', 0)}",
             f"  unattached forms: {n.get('unattached', 0)}",
             f"  unattached seen in one document: {n.get('unattached_seen_once', 0)}",
             f"  unattached marked for a look: {n.get('unattached_marked_for_a_look', 0)}",
             f"  unattached one word: {n.get('unattached_one_word', 0)}",
             f"  unattached two words: {n.get('unattached_two_words', 0)}",
             f"  unattached three words or more: {n.get('unattached_three_plus', 0)}",
             "  how close each form not attached at once came to its nearest main name:",
             f"    blocked by a guard: {n.get('closest_blocked_by_a_guard', 0)}",
             f"    0.75 to 0.90, asked of Kev: {n.get('closest_0.75_to_0.90', 0)}",
             f"    0.50 to 0.75: {n.get('closest_0.50_to_0.75', 0)}",
             f"    under 0.50: {n.get('closest_under_0.50', 0)}", ""]
    g = r["run_together"]
    lines += ["WORDS RUN TOGETHER INTO A MAIN NAME",
              f"  found by SymSpell: {g.get('symspell', 0)}",
              f"  found by our pass: {g.get('ours', 0)}",
              f"  found by both: {g.get('both', 0)}",
              f"  found by either: {g.get('either', 0)}", ""]
    m = r["misread"]
    lines += ["MAIN SURNAMES MISREAD (one or two letters wrong, dictionary words left out)",
              f"  distinct misread words: {m.get('distinct', 0)}",
              f"  occurrences: {m.get('occurrences', 0)}",
              f"  standing just after the person's first name or initial: "
              f"{m.get('beside_the_first_name', 0)}",
              f"  already on the list: {m.get('already_on_the_list', 0)}", ""]
    ties = r.get("address_ties", {})
    lines += ["STREET ADDRESSES, BY THE NAME PRINTED WITH THEM",
              "  (the line the address is on, and the three lines above it)",
              "  tied to | groups | in 1 document | in 2 or more | in 3 or more"]
    for t in TIES:
        c = ties.get(t, {})
        lines.append(f"  {t} | {c.get('groups', 0)} | {c.get('in_1', 0)} | {c.get('in_2_plus', 0)} | "
                     f"{c.get('in_3_plus', 0)}")
    lines.append("")
    lines += ["THE REVIEW LIST, KIND BY KIND",
              "  kind | groups before | groups after | after, in 2 or more documents | "
              "after, in 3 or more | left as written by rule"]
    total = Counter()
    for kind, t in sorted(r["table"].items(), key=lambda kv: -kv[1]["before"]):
        label = "Names (people and children)" if kind == "person" else \
            "Accounts and cards (all kinds)" if kind == "bank_account" else categories.label_for(kind)
        lines.append(f"  {label} | {t['before']} | {t['after']} | {t['after_2_plus']} | "
                     f"{t['after_3_plus']} | {'yes' if t['leave'] else 'no'}")
        if not t["leave"]:
            for k in ("before", "after", "after_2_plus", "after_3_plus"):
                total[k] += t[k]
    lines += ["",
              f"  review list before, kinds not left as written: {total['before']}",
              f"  review list after: {total['after']}",
              f"  after, in 2 or more documents: {total['after_2_plus']}",
              f"  after, in 3 or more documents: {total['after_3_plus']}", ""]
    return lines


def empty() -> dict:
    return {"names": {}, "run_together": {}, "misread": {}, "kev": 0, "address_ties": {},
            "table": {"person": {"before": 0, "after": 0, "after_2_plus": 0, "after_3_plus": 0,
                                 "leave": False},
                      "bank_account": {"before": 0, "after": 0, "after_2_plus": 0,
                                       "after_3_plus": 0, "leave": True}}}
