"""Tune the name matcher on pairs whose answer is known.

    ~/.venvs/anonymizer-redactor/bin/python -m eval.name_pairs RUN_DIR [--kev URL] [--hold-out REGEX]

From every labelled person in a corpus run's cases, the ways a scan writes a
name - misread letters, a letter dropped or added, the start or end cut off,
glued, reversed, initialled, a nickname, a payment line's word run on - each
scored by redactor.name_match against its own person (should attach) and
against the hard look-alikes (should not): a family member with the same
surname, the same first name under another surname, a near-identical surname.
Reports, per threshold, how many true forms attach and how many wrong ones do,
for the tuning cases and the held-out ones apart.
"""

from __future__ import annotations

import argparse
import pickle
import random
import re
from collections import defaultdict
from pathlib import Path

from redactor import name_match, names

_OCR = [("l", "1"), ("o", "0"), ("i", "l"), ("rn", "m"), ("m", "rn"), ("e", "c"), ("c", "e"),
        ("h", "b"), ("n", "h"), ("a", "o"), ("u", "v"), ("t", "f"), ("s", "5"), ("g", "q")]
_LINE = ["Conf", "Cont", "Ret", "Received", "Sent", "POS", "ID", "Acct"]


def variants(first: str, middle: str, last: str, rng: random.Random) -> list[tuple[str, str]]:
    """(kind, written form) for one person."""
    out = []
    f, m, l = first, middle, last

    def misread(word: str) -> str:
        for _ in range(3):
            a, b = rng.choice(_OCR)
            if a in word.casefold():
                i = word.casefold().index(a)
                return word[:i] + (b.upper() if word.isupper() else b) + word[i + len(a):]
        return word
    out.append(("full", " ".join(p for p in (f, m, l) if p)))
    out.append(("first last", f"{f} {l}"))
    out.append(("caps", f"{f} {l}".upper()))
    out.append(("reversed", f"{l.upper()}, {f.upper()}" + (f" {m[:1].upper()}" if m else "")))
    out.append(("initial", f"{f[:1]}. {l}"))
    out.append(("misread surname", f"{f} {misread(l)}"))
    out.append(("misread twice", f"{f} {misread(misread(l))}"))
    if len(l) > 5:
        out.append(("letter dropped", f"{f} {l[:3] + l[4:]}"))
        out.append(("cut off end", f"{f} {l[:-2]}"))
        out.append(("cut off start", f"{f[2:]} {l}".strip()))
    out.append(("glued", f"{f}{m[:1]}{l}".upper()))
    out.append(("glued payment", f"to{f}{l}".upper()))
    out.append(("line word", f"{f} {l} {rng.choice(_LINE)}"))
    out.append(("glued initial", f"{f} {m[:1]}{l}".upper() if m else f"{f}{l[:1]} {l}"))
    nick = names.NICKNAMES.get(f.casefold())
    if nick:
        short = nick if isinstance(nick, str) else nick[0]
        out.append(("nickname", f"{short.title()} {l}"))
    return out


def _words(text: str) -> list[str]:
    from redactor import name_clusters
    # every word, a misread one with an OCR digit in it too - name_match
    # reads the digit back as its letter
    return name_clusters._words(text)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    parser.add_argument("--hold-out", default=r"HH0[258]|HH09|discovery",
                        help="regex on the case: these are scored apart, never tuned on")
    parser.add_argument("--kev", default=None)
    args = parser.parse_args(argv)
    rng = random.Random(9)
    cases = pickle.load(open(args.run / "secrets.pkl", "rb"))
    people_by_case = {}
    for case, value in cases.items():
        secrets = value[0]
        people = []
        for p in secrets.people.values():
            parsed = names.parse(p.full)
            if parsed.first and parsed.last and len(parsed.first) > 1 and len(parsed.last) > 2:
                people.append((parsed.first, parsed.middles[0] if parsed.middles else "",
                               parsed.last, p.household))
        if people:
            people_by_case[case] = people
    pairs = {"tune": [], "hold": []}
    for case, people in people_by_case.items():
        split = "hold" if re.search(args.hold_out, case) else "tune"
        for i, (f, m, l, _hh) in enumerate(people):
            for kind, written in variants(f, m, l, rng):
                pairs[split].append((written, (f, m, l), True, kind))
                # the hard look-alikes in the same case
                for j, (f2, m2, l2, _h2) in enumerate(people):
                    if j == i:
                        continue
                    same_surname = l2.casefold() == l.casefold() and f2.casefold() != f.casefold()
                    same_first = f2.casefold() == f.casefold() and l2.casefold() != l.casefold()
                    near = (l2.casefold() != l.casefold() and
                            name_match.JaroWinkler.similarity(l.casefold(), l2.casefold()) >= 0.85)
                    # an initial and a surname two family members share is
                    # either of them - the family's, never counted as wrong
                    if kind in ("initial",) and same_surname and f2[:1].casefold() == f[:1].casefold():
                        continue
                    if same_surname or same_first or near:
                        tag = "same surname" if same_surname else "same first" if same_first else "near surname"
                        pairs[split].append((written, (f2, m2, l2), False, tag))
    for split, items in pairs.items():
        scored = [(name_match.score(_words(w), f, [m] if m else [], l, w, f"{f} {m} {l}"), y, kind)
                  for w, (f, m, l), y, kind in items]
        pos = [s for s, y, _k in scored if y]
        neg = [s for s, y, _k in scored if not y]
        print(f"\n== {split}: {len(pos)} true forms, {len(neg)} look-alikes")
        print(f"   {'cutoff':>6} {'true attach':>11} {'wrong attach':>12}")
        for cut in (0.70, 0.75, 0.80, 0.85, 0.90, 0.95):
            tp = sum(s >= cut for s in pos); fp = sum(s >= cut for s in neg)
            print(f"   {cut:6.2f} {tp / len(pos):11.1%} {fp / max(len(neg), 1):12.2%}  ({fp} wrong)")
        by_kind = defaultdict(list)
        for s, y, k in scored:
            if y:
                by_kind[k].append(s)
        print("   true forms attaching at HIGH (%.2f) by kind:" % name_match.HIGH,
              ", ".join(f"{k} {sum(x >= name_match.HIGH for x in v) / len(v):.0%}"
                        for k, v in sorted(by_kind.items())))
        wrong = defaultdict(int)
        for s, y, k in scored:
            if not y and s >= name_match.LOW:
                wrong[k] += 1
        print(f"   look-alikes scoring {name_match.LOW:.2f} or more, by kind: {dict(wrong)}")
        examples = [(w, f"{f} {l}", s) for (w, (f, m, l), y, k), (s, _y, _k) in zip(items, scored)
                    if not y and s >= name_match.HIGH][:6]
        if examples:
            print("   wrong attachments at HIGH:", examples)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
