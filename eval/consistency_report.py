"""One case at a time: one stand-in per person, and what the run took that it should not.

    ~/.venvs/anonymizer-redactor/bin/python -m eval.consistency_report RUN_DIR [--compare OTHER]
        [--show 10]

Reads a run made with ``eval.corpus_eval --whole-case`` (see
:mod:`eval.consistency`). The party scores (see :mod:`eval.sheet`) are printed
for every run, parties first or not, so a legacy run and a ``--parties`` run of
the same files compare line for line.
"""

from __future__ import annotations

import argparse
import json
import re
from collections import Counter
from pathlib import Path

from . import consistency


def _load(run: Path, mode: str) -> dict[str, dict]:
    return {json.loads(p.read_text())["case"]: json.loads(p.read_text())
            for p in sorted((run / mode).glob("*.json"))}


def summary(record: dict) -> dict:
    c = record.get("consistency") or {}
    people = c.get("people", {})
    split = {n: p for n, p in people.items() if p["stand_ins"] > 1}
    mixed = {n: p for n, p in people.items() if p["mixed"]}
    drift = {s: v for s, v in c.get("surnames", {}).items() if len([k for k in v if k]) > 1}
    biz = {b: v for b, v in c.get("businesses", {}).items() if v["stand_ins"] > 1}
    addr = {a: v for a, v in c.get("addresses", {}).items() if v["stand_ins"] > 1}
    files = record.get("files", [])
    words = sum(f.get("words", 0) for f in files)
    lost = Counter()
    dates = 0
    for f in files:
        lost.update(f.get("lost_words", {}))
        dates += sum(f.get("lost_dates", {}).values())
    return {
        "people": len(people), "split": split, "mixed": mixed, "drift": drift, "biz": biz,
        "addr": addr, "addresses": len(c.get("addresses", {})),
        "leaks": sum(len(f["leaks"]) for f in files if f.get("leaks")),
        "hard_leaks": sum(1 for f in files for l in f.get("leaks", [])
                          if l["category"] != "household_first_name"),
        "money": sum(sum(f.get("destroyed_money", {}).values()) for f in files),
        "scan_money": sum(sum(f.get("destroyed_money_ocr", {}).values()) for f in files),
        "words": words, "lost_words": sum(lost.values()), "top_lost": lost.most_common(25),
        "lost_dates": dates, "files": len(files), "error": record.get("error", "")[:200],
        **party_summary(record),
    }


def party_summary(record: dict) -> dict:
    """The parties-first scores of one case (see eval.sheet and eval.truth)."""
    files = record.get("files", [])
    party_leaks = [(f["file"], l) for f in files for l in f.get("party_leaks", [])]
    hard = [x for x in party_leaks if x[1]["category"] != "household_first_name"]
    third = Counter()
    third_examples = Counter()
    numbers = {"count": 0, "of": 0}
    took = Counter()
    known = Counter()
    number_examples = []
    phones = Counter()
    phone_took = Counter()
    phone_examples = []
    accounts = []
    for f in files:
        t = f.get("third_replaced") or {}
        for kind in ("names", "phones", "emails", "handles", "addresses"):
            third[kind] += t.get(kind, 0)
        for ex in t.get("examples", []):
            third_examples[(ex["kind"], ex["value"])] += ex["lost"]
        n = f.get("numbers_replaced") or {}
        numbers["count"] += n.get("count", 0)
        numbers["of"] += n.get("of", 0)
        took.update(n.get("by_took", {}))
        known.update(n.get("by_known", {}))
        number_examples += [ex | {"file": f["file"]} for ex in n.get("examples", [])]
        p = f.get("phones") or {}
        for key in ("mistyped", "party_as_phone", "other_as_phone", "of"):
            phones[key] += p.get(key, 0)
        phone_took.update(p.get("by_took", {}))
        phone_examples += [ex | {"file": f["file"]} for ex in p.get("examples", [])]
        accounts += [a | {"file": f["file"]} for a in f.get("accounts_left", [])]
    # one routing number on twelve statements is one example, counted twelve times
    merged: dict[tuple[str, str], dict] = {}
    for example in number_examples:
        key = (example["took"], re.sub(r"\D", "", example["written"]))
        if key in merged:
            merged[key]["count"] += example["count"]
        else:
            merged[key] = dict(example)
    number_examples = list(merged.values())
    rows = record.get("review_rows") or {}
    return {
        "parties_first": bool(record.get("parties_first")),
        "roles": record.get("roles", {}),
        "party_leaks": len(party_leaks), "party_hard_leaks": len(hard),
        "party_leak_cats": Counter(l["category"] for _f, l in hard),
        "party_leak_examples": hard[:40],
        "third_total": sum(third.values()), "third": dict(third),
        "third_examples": third_examples.most_common(40),
        "accounts_left": len(accounts), "accounts_left_list": accounts,
        "numbers_replaced": numbers["count"], "numbers_seen": numbers["of"],
        "numbers_took": took, "numbers_known": known,
        "number_examples": sorted(number_examples, key=lambda e: -e["count"]),
        "phones_mistyped": phones["mistyped"], "phones_party": phones["party_as_phone"],
        "phones_other": phones["other_as_phone"], "phones_seen": phones["of"],
        "phone_took": phone_took, "phone_examples": phone_examples,
        "party_splits": consistency.party_splits(record.get("party_consistency")),
        "review_rows": rows.get("rows", 0), "review_listed": rows.get("listed", 0),
        "review_found": rows.get("found", 0),
        "review_kinds": Counter(rows.get("by_kind", {})), "review_source": rows.get("source", ""),
    }


def _top(counter: Counter, n: int = 8) -> str:
    return ", ".join(f"{k} {v}" for k, v in counter.most_common(n)) or "-"


def print_party(s: dict, was, show: int) -> None:
    flow = "parties first" if s["parties_first"] else "today's flow"
    print(f"   -- party scores ({flow}); sheet: " +
          ", ".join(f"{k} {v[0] if v else '?'}" for k, v in s["roles"].items()))
    print(f"   party leaks (hard): {s['party_hard_leaks']}{was('party_hard_leaks')}"
          f"   all: {s['party_leaks']}{was('party_leaks')}   by kind: {_top(s['party_leak_cats'])}")
    print(f"   third parties replaced: {s['third_total']}{was('third_total')}   "
          + ", ".join(f"{k} {v}" for k, v in s["third"].items()))
    print(f"   party accounts left showing: {s['accounts_left']}{was('accounts_left')}")
    print(f"   numbers replaced that are not the parties' accounts: {s['numbers_replaced']} "
          f"of {s['numbers_seen']}{was('numbers_replaced')}")
    print(f"        taken as: {_top(s['numbers_took'])}")
    print(f"        what they were: {_top(s['numbers_known'])}")
    print(f"   phones replaced as something else: {s['phones_mistyped']} of {s['phones_seen']}"
          f"{was('phones_mistyped')}   taken as: {_top(s['phone_took'])}")
    print(f"        parties' phones replaced as phones: {s['phones_party']}   "
          f"others' phones replaced as phones: {s['phones_other']}{was('phones_other')}")
    print(f"   one party value, several stand-ins: {len(s['party_splits'])}{was('party_splits')}")
    print(f"   review rows: {s['review_rows']}{was('review_rows')} "
          f"(listed {s['review_listed']}, with places in the documents {s['review_found']}, "
          f"from {s['review_source'] or '-'})   "
          f"by kind: {_top(s['review_kinds'], 10)}")
    for line in s["party_splits"][:show]:
        print(f"   [party split] {line}")
    for file, leak in s["party_leak_examples"][:show]:
        print(f"   [party leak] {leak['category']} {leak['value'][:30]!r} "
              f"{leak['context'][:70]!r}  ({Path(file).name})")
    for account in s["accounts_left_list"][:show]:
        print(f"   [account showing] {account['party']} {account['kind']} {account['written']} "
              f"({account['institution']}) as {'/'.join(account['forms'])}: "
              f"{account['context'][:70]!r}")
    by_took: dict[str, list] = {}
    for example in s["number_examples"]:
        by_took.setdefault(example["took"], []).append(example)
    for category, examples in sorted(by_took.items(), key=lambda kv: -sum(e["count"] for e in kv[1])):
        print(f"   [numbers taken as {category}] " + "; ".join(
            f"{e['written']} x{e['count']} ({e['known']}) {e['context'][:40]!r}"
            for e in examples[:max(1, show // 2)]))
    for example in s["phone_examples"][:show]:
        print(f"   [phone taken as {example['took']}] {example['written']} "
              f"{example['context'][:60]!r}")
    for (kind, value), lost in s["third_examples"][:show]:
        print(f"   [third party replaced] {kind} {value} x{lost}")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    parser.add_argument("--mode", default="operator")
    parser.add_argument("--compare", type=Path, default=None)
    parser.add_argument("--show", type=int, default=6)
    args = parser.parse_args(argv)
    now = _load(args.run, args.mode)
    before = _load(args.compare, args.mode) if args.compare else {}
    for case, record in now.items():
        s = summary(record)
        b = summary(before[case]) if case in before else None

        def was(key):
            if b is None:
                return ""
            value = b[key]
            return f"  (was {len(value) if isinstance(value, (dict, list)) else value})"
        print(f"== {case}: {s['files']} files {s['error']}")
        print(f"   people tied to labels {s['people']}")
        print(f"   one person, several invented names: {len(s['split'])}{was('split')}")
        print(f"   one person, invented name and bracket: {len(s['mixed'])}{was('mixed')}")
        print(f"   one surname, several invented surnames: {len(s['drift'])}{was('drift')}")
        print(f"   one business, several stand-ins: {len(s['biz'])}{was('biz')}")
        print(f"   one address, several placeholders: {len(s['addr'])} of {s['addresses']}{was('addr')}")
        print(f"   leaks (hard): {s['hard_leaks']}{was('hard_leaks')}   all: {s['leaks']}{was('leaks')}"
              + ("   (party leaks: parties-first run)" if s["parties_first"] else ""))
        print(f"   figures lost, typed: {s['money']}{was('money')}   scans painted: "
              f"{s['scan_money']}{was('scan_money')}")
        rate = 100 * s["lost_words"] / max(s["words"], 1)
        print(f"   ordinary words lost, typed: {s['lost_words']} of {s['words']} ({rate:.2f}%)"
              f"{was('lost_words')}   dates lost: {s['lost_dates']}{was('lost_dates')}")
        print("   most lost words:", ", ".join(f"{w} {n}" for w, n in s["top_lost"][:20]))
        for title, key in (("several names", "split"), ("name and bracket", "mixed")):
            for name, p in list(s[key].items())[:args.show]:
                print(f"   [{title}] {name}:")
                for form in p["forms"][:10]:
                    print(f"        {form}")
        for surname, fakes in list(s["drift"].items())[:args.show]:
            print(f"   [surname drift] {surname}: {fakes}")
        for name, p in list(s["addr"].items())[:args.show]:
            print(f"   [address] {name}:")
            for form in p["forms"][:8]:
                print(f"        {form}")
        for name, p in list(s["biz"].items())[:args.show]:
            print(f"   [business] {name}:")
            for form in p["forms"][:8]:
                print(f"        {form}")
        print_party(s, was, args.show)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
