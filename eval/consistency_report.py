"""One case at a time: one stand-in per person, and what the run took that it should not.

    ~/.venvs/anonymizer-redactor/bin/python -m eval.consistency_report RUN_DIR [--compare OTHER]
        [--show 10]

Reads a run made with ``eval.corpus_eval --whole-case`` (see
:mod:`eval.consistency`).
"""

from __future__ import annotations

import argparse
import json
from collections import Counter
from pathlib import Path


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
    files = record.get("files", [])
    words = sum(f.get("words", 0) for f in files)
    lost = Counter()
    dates = 0
    for f in files:
        lost.update(f.get("lost_words", {}))
        dates += sum(f.get("lost_dates", {}).values())
    return {
        "people": len(people), "split": split, "mixed": mixed, "drift": drift, "biz": biz,
        "leaks": sum(len(f["leaks"]) for f in files if f.get("leaks")),
        "hard_leaks": sum(1 for f in files for l in f.get("leaks", [])
                          if l["category"] != "household_first_name"),
        "money": sum(sum(f.get("destroyed_money", {}).values()) for f in files),
        "scan_money": sum(sum(f.get("destroyed_money_ocr", {}).values()) for f in files),
        "words": words, "lost_words": sum(lost.values()), "top_lost": lost.most_common(25),
        "lost_dates": dates, "files": len(files), "error": record.get("error", "")[:200],
    }


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
            return f"  (was {len(value) if isinstance(value, dict) else value})"
        print(f"== {case}: {s['files']} files {s['error']}")
        print(f"   people tied to labels {s['people']}")
        print(f"   one person, several invented names: {len(s['split'])}{was('split')}")
        print(f"   one person, invented name and bracket: {len(s['mixed'])}{was('mixed')}")
        print(f"   one surname, several invented surnames: {len(s['drift'])}{was('drift')}")
        print(f"   one business, several stand-ins: {len(s['biz'])}{was('biz')}")
        print(f"   leaks (hard): {s['hard_leaks']}{was('hard_leaks')}   all: {s['leaks']}{was('leaks')}")
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
        for name, p in list(s["biz"].items())[:args.show]:
            print(f"   [business] {name}:")
            for form in p["forms"][:8]:
                print(f"        {form}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
