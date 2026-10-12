"""Aggregate a corpus run into one comparable summary.

    ~/.venvs/anonymizer-redactor/bin/python -m eval.report RUN_DIR [--mode operator]
        [--examples 3] [--category counterparty] [--compare OTHER_RUN_DIR]

A leak is counted once per (file, category, value): the same account number
surviving twice on one page is one leak, surviving on twelve monthly
statements is twelve.
"""

from __future__ import annotations

import argparse
import json
from collections import Counter, defaultdict
from pathlib import Path

# Categories that are soft by construction: a first name that is also an
# everyday word, a town on a merchant line. Reported, never totalled as leaks.
SOFT = {"household_first_name"}


def load(run: Path, mode: str) -> list[dict]:
    return [json.loads(p.read_text()) for p in sorted((run / mode).glob("*.json"))]


def summarise(records: list[dict]) -> dict:
    files = [f for r in records for f in r["files"]]
    by_cat: Counter = Counter()
    files_by_cat: dict[str, set] = defaultdict(set)
    values_by_cat: dict[str, set] = defaultdict(set)
    examples: dict[str, list] = defaultdict(list)
    by_type: dict[str, Counter] = defaultdict(Counter)
    type_files: Counter = Counter()
    status: Counter = Counter()
    money = 0
    money_files = 0
    merchants: Counter = Counter()
    errors = [r["batch"] + ": " + r["error"].splitlines()[0] for r in records if r["error"]]
    for f in files:
        status[f["status"]] += 1
        type_files[f["doc_type"]] += 1
        if f["status"] == "error":
            errors.append(f"{f['file']}: {f['error'][:160]}")
        for leak in f["leaks"]:
            cat = leak["category"]
            by_cat[cat] += 1
            files_by_cat[cat].add(f["file"])
            values_by_cat[cat].add(leak["value"].casefold())
            by_type[f["doc_type"]][cat] += 1
            examples[cat].append((f["file"], leak))
        lost = sum(f["destroyed_money"].values())
        money += lost
        money_files += bool(lost)
        merchants.update(f["lost_merchants"])
    fallbacks = sum(1 for f in files for w in f.get("warnings", [])
                    if "added on top of the page" in w)
    hard = sum(n for c, n in by_cat.items() if c not in SOFT)
    leaky = sum(1 for f in files if any(l["category"] not in SOFT for l in f["leaks"]))
    return {"files": len(files), "status": status, "hard": hard, "leaky_files": leaky,
            "by_cat": by_cat, "files_by_cat": files_by_cat, "values_by_cat": values_by_cat,
            "examples": examples, "by_type": by_type, "type_files": type_files,
            "money": money, "money_files": money_files, "merchants": merchants,
            "errors": errors, "fallbacks": fallbacks}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    parser.add_argument("--mode", default="operator")
    parser.add_argument("--examples", type=int, default=2)
    parser.add_argument("--category", default=None, help="show examples for one category only")
    parser.add_argument("--types", action="store_true", help="per document type breakdown")
    parser.add_argument("--compare", type=Path, default=None)
    args = parser.parse_args(argv)

    s = summarise(load(args.run, args.mode))
    base = summarise(load(args.compare, args.mode)) if args.compare else None

    def delta(now, then):
        return "" if base is None else f"  (was {then})"

    print(f"{args.run.name} [{args.mode}]  files {s['files']}  {dict(s['status'])}")
    print(f"hard leaks {s['hard']} in {s['leaky_files']} files"
          + delta(0, base and f"{base['hard']} in {base['leaky_files']}"))
    print(f"destroyed dollar figures {s['money']} in {s['money_files']} files"
          + delta(0, base and f"{base['money']} in {base['money_files']}"))
    if s["fallbacks"]:
        print(f"anonymized pages that fell back to appended text: {s['fallbacks']}")
    print(f"merchant names lost {sum(s['merchants'].values())} ({len(s['merchants'])} distinct)"
          + delta(0, base and sum(base['merchants'].values())))
    print(f"\n{'category':28} {'leaks':>7} {'files':>6} {'values':>7}" + ("   baseline" if base else ""))
    cats = sorted(set(s["by_cat"]) | set(base["by_cat"] if base else ()),
                  key=lambda c: -s["by_cat"].get(c, 0))
    for cat in cats:
        line = (f"{cat:28} {s['by_cat'].get(cat, 0):7} {len(s['files_by_cat'].get(cat, ())):6} "
                f"{len(s['values_by_cat'].get(cat, ())):7}")
        if base:
            line += f"   {base['by_cat'].get(cat, 0):7}"
        print(line + ("   (soft)" if cat in SOFT else ""))
        if args.examples and (args.category in (None, cat)):
            shown = set()
            for file, leak in s["examples"].get(cat, []):
                key = leak["value"].casefold()
                if key in shown:
                    continue
                shown.add(key)
                print(f"      {Path(file).name[:48]:48} {leak['value'][:28]!r:30} {leak['context'][:90]!r}")
                if len(shown) >= args.examples:
                    break
    if s["merchants"]:
        print("\nmost-lost merchants:", ", ".join(f"{m} x{n}" for m, n in s["merchants"].most_common(12)))
    if args.types:
        print(f"\n{'doc type':32} {'files':>6} {'leaks':>6}  top categories")
        for dt, n in sorted(s["type_files"].items(), key=lambda x: -sum(s["by_type"][x[0]].values())):
            c = s["by_type"][dt]
            total = sum(v for k, v in c.items() if k not in SOFT)
            print(f"{dt:32} {n:6} {total:6}  " + ", ".join(f"{k} {v}" for k, v in c.most_common(4)))
    if s["errors"]:
        print(f"\nERRORS {len(s['errors'])}")
        for e in s["errors"][:15]:
            print("  ", e)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
