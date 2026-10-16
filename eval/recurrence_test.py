"""What would pre-ticking only what recurs - 2x, 3x - do to the review list?

    ~/.venvs/anonymizer-redactor/bin/python -m eval.recurrence_test RUN_DIR [--match REGEX]

Reads a run made with the app deriving everything (no party sheet), whose
records carry every row and the documents it was found in. A row's group is
its head with every form the run folded under it; a group recurs Nx when it
was found in N documents. Per case and threshold: the rows the operator would
see pre-ticked, and - since an unticked row is left as written - the labelled
secrets that would be left showing because their group fell below it.
"""

from __future__ import annotations

import argparse
import json
import pickle
import re
from collections import Counter, defaultdict
from pathlib import Path

from eval import precision

THRESHOLDS = (1, 2, 3)


def groups(record: dict) -> dict[str, dict]:
    rows = {r["key"]: r for r in record.get("rows", [])}
    out: dict[str, dict] = {}
    for row in rows.values():
        head = row
        seen = set()
        while head["parent"] and head["parent"] in rows and head["key"] not in seen:
            seen.add(head["key"])
            head = rows[head["parent"]]
        g = out.setdefault(head["key"], {"head": head, "forms": [], "documents": set()})
        g["forms"].append(row)
        g["documents"].update(row["documents"])
    return out


def report(record: dict, secrets) -> list[str]:
    gs = groups(record)
    lines = [f"== {record['case']}: {len(record.get('files', []))} files, {len(gs)} rows"]
    verdicts = {}
    for key, g in gs.items():
        head = g["head"]
        verdict = "unlabelled"
        for form in g["forms"]:
            v, _said = precision.classify(form["category"], form["canonical"], secrets)
            if v in ("confirmed", "third_party", "wrong_kind"):
                verdict = v if verdict == "unlabelled" else verdict
                if v in ("confirmed", "third_party"):
                    verdict = v
        verdicts[key] = verdict
    for need in THRESHOLDS:
        kept = {k for k, g in gs.items() if len(g["documents"]) >= need}
        by = Counter(gs[k]["head"]["category"] for k in kept)
        real = {k for k, v in verdicts.items() if v in ("confirmed", "third_party")}
        unticked_real = [gs[k] for k in real - kept]
        examples = sorted({g["head"]["canonical"] for g in unticked_real})[:6]
        top = ", ".join(f"{c} {n}" for c, n in by.most_common(6))
        lines.append(f"   {need}x: {len(kept):5} rows pre-ticked ({top})")
        if need > 1:
            lines.append(f"       labelled values left unticked: {len(unticked_real)} of {len(real)}"
                         + (f"  e.g. {examples}" if examples else ""))
    singles = Counter(gs[k]["head"]["category"] for k, g in gs.items() if len(g["documents"]) < 2)
    lines.append("   seen in one document only, by kind: "
                 + ", ".join(f"{c} {n}" for c, n in singles.most_common(8)))
    files = record.get("files", [])
    leaks = sum(len(f.get("leaks", [])) for f in files)
    words = sum(f.get("words", 0) for f in files)
    lost = sum(sum(f.get("lost_words", {}).values()) for f in files)
    lines.append(f"   as run (every row ticked): leaks {leaks}, ordinary words lost "
                 f"{100 * lost / max(words, 1):.1f}%")
    return lines


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    parser.add_argument("--mode", default="operator")
    parser.add_argument("--match", default=None)
    args = parser.parse_args(argv)
    cases = pickle.load(open(args.run / "secrets.pkl", "rb"))
    for path in sorted((args.run / args.mode).glob("*.json")):
        record = json.loads(path.read_text())
        if args.match and not re.search(args.match, record["case"]):
            continue
        secrets = cases[record["case"]][0]
        print("\n".join(report(record, secrets)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
