"""Score a finished party-PII CSV against a household's party truth.

    ~/.venvs/anonymizer-redactor/bin/python -m eval.party_pii_score TRUTH.pkl CSV WORKDIR

TRUTH.pkl is what ``eval.party_pii_eval`` caches per household; WORKDIR is the
run's ``.work/<case>`` folder (for the page text and the rule drops). Reports,
for every truth value the pages print: kept in the CSV (any row), kept under
the right party, covered by a rule the app applies itself, or lost - and the
known non-party values that reached the CSV.
"""

from __future__ import annotations

import csv
import json
import pickle
import re
import sys
from collections import Counter
from pathlib import Path

from eval.party_pii_eval import FIELDS, Text, _raw, _script, key

CSV_FIELD = {"Names": "names", "Date of birth": "dob", "SSN": "ssn", "Addresses": "addresses",
             "Phones": "phones", "Emails": "emails", "TIN / EIN": "tin",
             "Driver's licence": "dl", "Passport": "passport", "Employers": "employers",
             "Businesses": "businesses", "Accounts": "accounts", "Cards": "cards",
             "Handles": "handles", "Vehicles": "vehicles", "Parcels": "parcels",
             "Case numbers": "case_numbers", "Other identifiers": "other"}


def main(argv=None) -> int:
    argv = argv or sys.argv[1:]
    pt = pickle.loads(Path(argv[0]).read_bytes())
    rows = list(csv.DictReader(open(argv[1], encoding="utf-8-sig")))
    work = Path(argv[2])
    e = _script()
    text = Text(e.load_pages(work))
    labels = dict(e.party_labels(json.loads((work / "parties.json").read_text())))
    have: dict[tuple[str, str], set[str]] = {}
    for r in rows:
        f = CSV_FIELD.get(r["Field"])
        if not f:
            continue
        roles = have.setdefault((f, key(f, r["Value"])), set())
        roles.add(r["Role"])
        d = re.sub(r"\D", "", r["Value"])
        if len(d) >= 4 and f in ("accounts", "cards", "handles", "other"):
            for g in ("accounts", "cards"):
                have.setdefault((g, d[-4:]), set()).add(r["Role"])
        if f == "names":
            for v in e.names.variants(e.names.parse(r["Value"])):
                have.setdefault(("names", key("names", v.text)), set()).add(r["Role"])
    ruled = {re.sub(r"\D", "", d["value"])[-4:]
             for d in json.loads((work / "not_proposed.json").read_text())
             if d["reason"] in ("transfer tail", "tail of full")} \
        if (work / "not_proposed.json").exists() else set()
    role_of = {}
    for party in pt.parties:
        for label, entry in labels.items():
            for n in entry.get("names", []):
                p = e.names.parse(n)
                if p.first.casefold() == party.first.casefold() and \
                        p.last.casefold() == party.last.casefold():
                    role_of[party.key] = label
    tally, lost = Counter(), []
    for party in pt.parties:
        role = role_of.get(party.key)
        for field, values in party.sheet.items():
            if field not in FIELDS or not values:
                continue
            for v in values:
                if not text.printed(field, v):
                    continue
                k = key(field, v)
                roles = have.get((field, k), set())
                if roles:
                    tally["kept"] += 1
                    if role in roles or "Both parties" in roles:
                        tally["right party"] += 1
                    elif "Unassigned" in roles:
                        tally["unassigned"] += 1
                elif field in ("accounts", "cards") and k in ruled:
                    tally["app rule"] += 1
                else:
                    tally["lost"] += 1
                    lost.append((party.key, field, _raw(v)))
    third = pt.third_values
    csv_phones = {re.sub(r"\D", "", r["Value"])[-10:] for r in rows if r["Field"] == "Phones"}
    leaks = [p for p in third["phones"] if p[-10:] in csv_phones]
    total = tally["kept"] + tally["app rule"] + tally["lost"]
    print(f"printed truth values: {total}; kept {tally['kept']} (right party "
          f"{tally['right party']}, unassigned {tally['unassigned']}); covered by an app rule "
          f"{tally['app rule']}; LOST {tally['lost']}")
    for item in lost:
        print("   lost", item)
    print(f"known non-party phones in the CSV: {len(leaks)}; CSV rows {len(rows)}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
