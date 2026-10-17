"""How sure is each kind of replacement? Per kind: confirmed, wrong kind, third
party, unlabelled. (A local-model judge of the unlabelled items was taken out
with that model on 2026-10-16; its calibration had failed anyway.)

    ~/.venvs/anonymizer-redactor/bin/python -m eval.precision_report RUN_DIR
        [--show 8] [--match REGEX]

Counted once per distinct (kind, text) in a case; ``places`` counts every hit.
"""

from __future__ import annotations

import argparse
import json
import random
import re
from collections import Counter, defaultdict
from pathlib import Path

KIND_WORDS = {
    "ssn": "a Social Security number", "bank_account": "a bank account number",
    "masked_account": "the masked tail of a bank or card account number",
    "credit_card": "a credit or debit card number", "investment_account": "an investment "
    "account number", "loan_number": "a loan number", "phone": "a telephone number",
    "fax": "a fax number", "email": "an email address", "street_address": "a street address",
    "person": "a person's name", "minor": "a child's name", "party_value": "an identifier of "
    "one of the parties", "routing_number": "a bank routing number", "check_number":
    "a check, invoice or wire number", "order_number": "an order number", "tracking_number":
    "a shipping tracking number", "dob": "a date of birth", "employer": "an employer's name",
    "organization": "a business's name", "payment_handle": "a payment-app handle",
    "case_number": "a court case number", "drivers_license": "a driver's licence number",
}


def _load(run: Path, mode: str, match: str | None):
    for path in sorted((run / mode).glob("*.json")):
        record = json.loads(path.read_text())
        if match and not re.search(match, record.get("case", "")):
            continue
        yield record


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    parser.add_argument("--mode", default="operator")
    parser.add_argument("--show", type=int, default=8)
    parser.add_argument("--match", default=None, help="regex on the case name")
    args = parser.parse_args(argv)

    distinct: dict[str, dict[tuple, dict]] = defaultdict(dict)
    places: Counter = Counter()
    for record in _load(args.run, args.mode, args.match):
        for f in record.get("files", []):
            for hit in f.get("hit_audit", []):
                key = (record["case"], hit["text"].casefold())
                places[hit["category"]] += 1
                distinct[hit["category"]].setdefault(key, hit)
    random.seed(7)
    print(f"{'kind':20} {'distinct':>8} {'places':>7} {'confirmed':>9} {'wrong':>6} "
          f"{'3rd pty':>7} {'unlabld':>7} {'judged p':>8}  precision floor..estimate")
    report = {}
    for category, items in sorted(distinct.items(), key=lambda kv: -len(kv[1])):
        hits = list(items.values())
        tally = Counter(h["verdict"] for h in hits)
        unl = [h for h in hits if h["verdict"] == "unlabelled"]
        judged = []
        ps = [j["p"] for j in judged if j["p"] is not None]
        mean_p = sum(ps) / len(ps) if ps else None
        n = len(hits)
        floor = tally["confirmed"] / n
        estimate = (tally["confirmed"] + (mean_p or 0) * len(unl)) / n if mean_p is not None \
            else None
        print(f"{category:20} {n:8} {places[category]:7} {tally['confirmed']:9} "
              f"{tally['wrong_kind']:6} {tally['third_party']:7} {len(unl):7} "
              f"{'' if mean_p is None else f'{mean_p:.2f}':>8}  {floor:.0%}.."
              f"{'?' if estimate is None else f'{estimate:.0%}'}")
        report[category] = {"tally": tally, "judged": judged,
                            "wrong": [h for h in hits if h["verdict"] == "wrong_kind"],
                            "third": [h for h in hits if h["verdict"] == "third_party"]}
    for category, r in report.items():
        worst = sorted((j for j in r["judged"] if j["p"] is not None), key=lambda j: j["p"])
        lines = [f"   wrong kind ({h['labels_say']}): {h['text']!r}  ..{h['before'][-35:]} [*] "
                 f"{h['after'][:35]}.." for h in r["wrong"][:args.show]]
        lines += [f"   p={j['p']:.2f} is {j['judged_is']!r}: {j['text']!r}  ..{j['before'][-35:]}"
                  f" [*] {j['after'][:35]}.." for j in worst[:args.show] if j["p"] < 0.8]
        lines += [f"   third party: {h['text']!r}" for h in r["third"][:3]]
        if lines:
            print(f"\n== {category}")
            print("\n".join(lines))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
