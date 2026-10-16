"""Would gathering by attribution and recurrence cut the flood? Measured, not built.

    ~/.venvs/anonymizer-redactor/bin/python -m eval.discovery_test RUN_DIR CORPUS [--match REGEX]

``RUN_DIR`` is a ``--parties`` corpus run (its secrets.pkl holds each case's party
truth). For each case, over its typed PDFs:

``flood``
    every distinct value today's detectors flag, per kind - what the review
    list grows from.
``candidates``
    only values tied to a party: in a labelled field of a document addressed
    to a party, or in the lines under a written form of a party's name -
    counted in how many documents they were so tied.
``strong``
    candidates tied in two documents or more (pre-ticked).

Each scored against the labels' list of the parties' own identifiers:
precision (share of candidates that are a party's) and recall (share of the
parties' identifiers present in the documents that were gathered).
"""

from __future__ import annotations

import argparse
import pickle
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

# what a field's label says it holds, read in the words just before the value
LABELS = {
    "ssn": r"\b(?:ssn|s\.s\.n|social\s+security(?:\s+(?:no|number|#))?|tin|taxpayer)\b",
    "dob": r"\b(?:date\s+of\s+birth|birth\s*date|dob|d\.o\.b)\b",
    "phone": r"\b(?:phone|tel|telephone|mobile|cell|home|work|contact)\b",
    "email": r"\b(?:e-?mail)\b",
    "account": r"\b(?:account|acct|a/c|card|ending(?:\s+in)?|member|policy\s+holder|"
               r"account\s+(?:no|number|#|id))\b",
    "address": r"\b(?:address|residence|mailing|home|service\s+address|garaging)\b",
    "dl": r"\b(?:driver'?s?\s+licen[cs]e|dl\s*(?:no|#)?|license\s+(?:no|number))\b",
    "vehicle": r"\b(?:vin|plate|license\s+plate)\b",
    "case": r"\b(?:case\s*(?:no|number|#)|civil\s+no)\b",
    "handle": r"\b(?:username|user\s*name|handle|@)\b",
}
KIND_OF = {
    "ssn": "ssn", "dob": "dob", "phone": "phone", "fax": "phone", "email": "email",
    "bank_account": "account", "masked_account": "account", "credit_card": "account",
    "investment_account": "account", "street_address": "address",
    "drivers_license": "dl", "vin": "vehicle", "license_plate": "vehicle",
    "case_number": "case", "payment_handle": "handle", "social_handle": "handle",
}
HOLDER_LINES = 5            # the block under a name: address, phone, email, account


def key_of(kind: str, value: str) -> str:
    digits = re.sub(r"\D", "", value)
    if kind == "phone":
        return digits[-10:]
    if kind == "account":
        return digits[-4:]
    if kind in ("ssn",):
        return digits[-4:]
    if kind == "email":
        return value.strip().casefold()
    if kind == "address":
        from redactor import grouping
        parsed = grouping.parse_address(value)
        if parsed is None or not parsed.street:
            return value.casefold()
        return f"{parsed.number} {' '.join(parsed.street)} {parsed.unit or ''}".strip()
    if kind == "dob":
        from eval import sheet
        return sheet._iso_date(value) or value
    return re.sub(r"[^0-9a-z]", "", value.casefold())


def truth_keys(parties) -> dict[str, set[str]]:
    out: dict[str, set[str]] = defaultdict(set)
    for party in parties.parties:
        s = party.sheet
        for v in s.get("ssn", []) + s.get("tin", []):
            out["ssn"].add(key_of("ssn", str(v)))
        for v in s.get("dob", []):
            out["dob"].add(key_of("dob", str(v)))
        for v in s.get("phones", []):
            out["phone"].add(key_of("phone", str(v)))
        for v in s.get("emails", []):
            out["email"].add(key_of("email", str(v)))
        for v in s.get("addresses", []):
            out["address"].add(key_of("address", str(v)))
        for a in s.get("accounts", []) + s.get("cards", []):
            out["account"].add(key_of("account", str(a.get("number", "") if isinstance(a, dict) else a)))
        for v in s.get("dl", []):
            out["dl"].add(key_of("dl", str(v.get("number", "") if isinstance(v, dict) else v)))
        for v in s.get("vehicles", []):
            for part in ((v.get("vin"), v.get("plate")) if isinstance(v, dict) else (v,)):
                if part:
                    out["vehicle"].add(key_of("vehicle", str(part)))
        for v in s.get("case_numbers", []):
            out["case"].add(key_of("case", str(v)))
        for v in s.get("handles", []):
            out["handle"].add(key_of("handle", str(v)))
    return out


def name_forms(parties) -> list[re.Pattern]:
    from redactor import names
    forms = set()
    for party in parties.parties:
        for person in party.people:
            parsed = names.parse(person.full)
            for variant in names.variants(parsed):
                if variant.token_count >= 2 or (variant.layout == "plain" and len(variant.text) > 3
                                                and party.role != "child"):
                    forms.add(variant.text)
    return [re.compile(rf"(?i)\b{re.escape(f)}\b") for f in sorted(forms, key=len, reverse=True)]


def gather(text: str, forms: list[re.Pattern]):
    """(kind, key, value, tied) for every detection in one document."""
    from redactor import patterns

    lines = text.split("\n")
    starts, at = [], 0
    for line in lines:
        starts.append(at)
        at += len(line) + 1
    named = [i for i, line in enumerate(lines) if any(f.search(line) for f in forms)]
    addressed = any(i < 30 for i in named)            # a party's name near the top
    under: set[int] = set()
    for i in named:
        under.update(range(i, i + HOLDER_LINES + 1))
    for m in patterns.scan(text):
        kind = KIND_OF.get(m.category)
        if kind is None:
            continue
        line = max(0, sum(1 for s in starts if s <= m.start) - 1)
        before = text[max(0, m.start - 45):m.start].casefold()
        labelled = bool(re.search(LABELS[kind], before)) if kind in LABELS else False
        tied = (line in under) or (labelled and addressed)
        yield kind, key_of(kind, m.text), m.text, tied


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("run", type=Path)
    parser.add_argument("corpus", type=Path)
    parser.add_argument("--match", default=None)
    parser.add_argument("--show", type=int, default=6)
    args = parser.parse_args(argv)
    import pymupdf

    cases = pickle.load(open(args.run / "secrets.pkl", "rb"))
    for case, value in cases.items():
        if args.match and not re.search(args.match, case):
            continue
        parties = value[2]
        truth = truth_keys(parties)
        forms = name_forms(parties)
        flood: dict[str, set] = defaultdict(set)
        tied_docs: dict[str, dict[str, set]] = defaultdict(lambda: defaultdict(set))
        seen_keys: dict[str, set] = defaultdict(set)
        sample: dict[tuple, str] = {}
        pdfs = [p for p in sorted((args.corpus / case).rglob("*.pdf"))
                if "variants_" not in str(p)]
        for pdf in pdfs:
            try:
                with pymupdf.open(pdf) as doc:
                    text = "\n".join(page.get_text("text") for page in doc)
            except Exception:
                continue
            if len(text.strip()) < 20:
                continue                                  # a scan: not in this test
            for kind, key, raw, tied in gather(text, forms):
                flood[kind].add(key)
                seen_keys[kind].add(key)
                if tied:
                    tied_docs[kind][key].add(pdf.name + str(pdf.parent))
                    sample.setdefault((kind, key), raw)
            # the parties' identifiers present in this document at all
        print(f"\n== {case}: {len(pdfs)} typed PDFs")
        print(f"   {'kind':9} {'flood':>6} {'cands':>6} {'strong':>6} {'prec':>6} {'s.prec':>6} "
              f"{'recall':>6} {'s.recall':>8}   (party identifiers present: n)")
        total_flood = total_c = total_s = 0
        for kind in sorted(set(flood) | set(truth)):
            cands = tied_docs[kind]
            strong = {k for k, d in cands.items() if len(d) >= 2}
            present = truth[kind] & seen_keys[kind]
            hit = {k for k in cands if k in truth[kind]}
            shit = {k for k in strong if k in truth[kind]}
            prec = len(hit) / len(cands) if cands else float("nan")
            sprec = len(shit) / len(strong) if strong else float("nan")
            rec = len(present & set(cands)) / len(present) if present else float("nan")
            srec = len(present & strong) / len(present) if present else float("nan")
            total_flood += len(flood[kind]); total_c += len(cands); total_s += len(strong)
            print(f"   {kind:9} {len(flood[kind]):6} {len(cands):6} {len(strong):6} {prec:6.0%} "
                  f"{sprec:6.0%} {rec:6.0%} {srec:8.0%}   ({len(present)})")
            wrong = [sample[(kind, k)] for k in sorted(strong - truth[kind],
                                                       key=lambda k: -len(cands[k]))][:args.show]
            if wrong:
                print(f"      strong but not a party's: {wrong}")
            missed = sorted(present - set(cands))[:args.show]
            if missed:
                print(f"      party's, not gathered (keys): {missed}")
        print(f"   TOTAL     flood {total_flood}, candidates {total_c}, strong {total_s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
