"""Score the party-PII skill's deterministic stage against the labelled corpus.

For each household: the party truth is read off the labels (``eval.sheet``),
the skill's script is run over the household's files with the parties named,
and every identifier the truth holds - and the case's text actually prints -
is looked for among what the script hands the model to review. A value missing
there can never reach the CSV, so this is the recall ceiling of the skill.

    ~/.venvs/anonymizer-redactor/bin/python -m eval.party_pii_eval CORPUS --out RUN [--workers 5]

CORPUS is the folder holding HH01_... HH10_. Pages the script queues for a
model are transcribed here by Tesseract at any confidence - a stand-in, so a
scan's recall is a floor.

Reported per household and in total: recall by field (in review / covered by a
rule the app applies itself / missing), how often the proposed owner was the
right party, how many review items are a known non-party's, and the review's
size in tokens (characters / 4).
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import pickle
import re
import sys
from collections import Counter, defaultdict
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "skills" / "party-pii-extractor" / "scripts" / "extract_party_pii.py"
EXCLUDE = ["*.json", "index*.csv", "master_index*", "README.txt"]
FIELDS = {"names": "name", "ssn": "ssn", "tin": "tin", "dob": "dob", "addresses": "address",
          "phones": "phone", "emails": "email", "employers": "employer",
          "businesses": "business", "accounts": "account", "cards": "card",
          "handles": "handle", "vehicles": "vehicle", "parcels": "parcel", "dl": "dl",
          "passport": "passport"}


def _script():
    spec = importlib.util.spec_from_file_location("extract_party_pii", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    sys.modules["extract_party_pii"] = module
    spec.loader.exec_module(module)
    return module


def _raw(v) -> str:
    if isinstance(v, dict):
        return str(v.get("number") or v.get("vin") or v.get("plate") or "")
    return str(v)


def key(field: str, v) -> str:
    v = _raw(v)
    d = re.sub(r"\D", "", v)
    if field in ("ssn", "tin"):
        return d
    if field == "phones":
        return d[-10:]
    if field in ("accounts", "cards"):
        return d[-4:] if d else v.casefold()
    if field == "addresses":
        return re.sub(r"\W", "", v.split(",")[0]).casefold()[:12]
    if field == "dob":
        return d
    return re.sub(r"\W", "", v).casefold()


class Text:
    """The case's lines, to ask whether a truth value is printed at all."""

    MASK = re.compile(r"(?i)[x*•#]{2,}|\.{2,}|ending|last\s*4|acct|account\s*(?:no|number|#)")

    def __init__(self, pages):
        self.lines = [line for p in pages for line in p.text.split("\n")]
        self.digits = [re.sub(r"\D", "", line) for line in self.lines]
        self.lower = re.sub(r"\s+", " ", "\n".join(self.lines).casefold())

    def printed(self, field: str, v) -> bool:
        raw = _raw(v)
        d = re.sub(r"\D", "", raw)
        if field in ("accounts", "cards") and d:
            # the whole number as its own token, or its last four right after a mask
            sep = r"[\s-]?"
            whole = re.compile(r"(?<!\d)" + sep.join(d) + r"(?!\d)")
            tail = re.compile(rf"(?i)(?:[x*•#]{{2,}}|\.{{2,}}|ending\s+in)[\s-]?{d[-4:]}(?!\d)")
            return any((len(d) >= 6 and whole.search(line)) or tail.search(line)
                       for line in self.lines)
        if field in ("ssn", "tin", "phones") and d:
            want = d[-9:] if field != "phones" else d[-10:]
            return any(want in x for x in self.digits)
        if field == "addresses":
            return " ".join(raw.split(",")[0].split()).casefold() in self.lower
        return " ".join(raw.split()).casefold()[:24] in self.lower


def run_household(args) -> dict:
    case_dir, out_root = Path(args[0]), Path(args[1])
    sys.path.insert(0, str(ROOT))
    from eval import corpus_eval
    e = _script()
    name = case_dir.name
    out = out_root / name
    truth_file = out_root / f"{name}.truth.pkl"
    if truth_file.exists():
        pt = pickle.loads(truth_file.read_bytes())
    else:
        pdfs = sorted(case_dir.rglob("*.pdf"))
        pt = corpus_eval.build_case(name, pdfs, case_dir.parent)[3]
        truth_file.write_bytes(pickle.dumps(pt))
    adults = [p for p in pt.parties if p.role in ("petitioner", "respondent")]
    argv = ["run", str(case_dir), "--out", str(out), "--single-case", "--no-sweep"]
    for flag, party in zip(("--petitioner", "--respondent"), adults):
        argv += [flag, party.people[0].full]
    for pat in EXCLUDE:
        argv += ["--exclude", pat]
    import contextlib
    import io
    log = io.StringIO()
    with contextlib.redirect_stdout(log):
        code = e.main(argv)
        work = out / ".work" / e.safe_name(name)
        if code == 2:                                   # stand in for the transcriber
            (work / "transcripts").mkdir(exist_ok=True)
            for item in json.loads((work / "queue.json").read_text()):
                text = e.ocr_image(Path(item["image"]))[0] if item.get("image") else ""
                (work / "transcripts" / f"{item['id']}.txt").write_text(text)
            code = e.main(argv)
    review = json.loads((work / "review.json").read_text()) if (work / "review.json").exists() \
        else {"items": []}
    items = review["items"]
    parties_json = json.loads((work / "parties.json").read_text())
    labels = dict(e.party_labels(parties_json))
    pages = e.load_pages(work)
    text = Text(pages)
    # which CSV role is which truth party: by first name, then surname
    role_of: dict[str, str] = {}
    for party in pt.parties:
        first, last = party.first.casefold(), party.last.casefold()
        for label, entry in labels.items():
            for n in entry.get("names", []):
                parsed = e.names.parse(n)
                if parsed.first.casefold() == first and parsed.last.casefold() == last:
                    role_of[party.key] = label
    # what reaches the model, and what the CSV gets without review (name forms)
    offered: dict[tuple[str, str], set[str]] = defaultdict(set)    # (field, key) -> proposals
    for item in items:
        f = next((k for k, v in FIELDS.items() if v == item["kind"]), item["kind"])
        offered[(f, key(f, item["value"]))].add(item["proposed"] or "?")
    for label, entry in labels.items():
        for n in entry.get("names", []):
            offered[("names", key("names", n))].add(label)
            # every form the app derives from an entered name is covered by it
            for v in e.names.variants(e.names.parse(n)):
                offered[("names", key("names", v.text))].add(label)
        if entry.get("dob"):
            offered[("dob", key("dob", entry["dob"]))].add(label)
    # what a rule left out because the app replaces or derives it by itself
    by_rule = set()
    for d in json.loads((work / "not_proposed.json").read_text()) \
            if (work / "not_proposed.json").exists() else []:
        if d["reason"] in ("transfer tail", "tail of full"):
            by_rule.add(re.sub(r"\D", "", d["value"])[-4:])
    # an id a detector filed under another field (a crypto "User ID" as a handle)
    for item in items:
        dd = re.sub(r"\D", "", item["value"])
        if len(dd) >= 8:
            offered[("accounts", dd[-4:])].add(item["proposed"] or "?")
    # card and account tails are the same thing to the sheet
    for (f, k), owners in list(offered.items()):
        if f == "cards":
            offered[("accounts", k)] |= owners
        if f == "accounts":
            offered[("cards", k)] |= owners
    stats = {"case": name, "exit": code, "items": len(items),
             "tokens": len((work / "review.txt").read_text()) // 4
             if (work / "review.txt").exists() else 0,
             "fields": defaultdict(Counter), "missing": [], "wrong_owner": 0, "right_owner": 0,
             "parties_found": sorted(labels), "truth_parties": [p.key for p in pt.parties]}
    for party in pt.parties:
        role = role_of.get(party.key)
        for field, values in party.sheet.items():
            if field not in FIELDS or not values:
                continue
            for v in values:
                if not text.printed(field, v):
                    stats["fields"][field]["unprinted"] += 1
                    continue
                k = key(field, v)
                owners = offered.get((field, k))
                if not owners and field == "businesses":
                    # "WHITFIELD ENTERPRISES LLC IOLTA TRUST ACCOUNT" is the business
                    # "Whitfield Enterprises LLC" with its account's title after it
                    owners = next((o for (f, kk), o in offered.items()
                                   if f == "businesses" and len(kk) >= 8
                                   and (k.startswith(kk) or kk.startswith(k))), None)
                if owners:
                    stats["fields"][field]["offered"] += 1
                    if role and role in owners:
                        stats["right_owner"] += 1
                    elif owners - {"?", "Both parties", "Case"}:
                        stats["wrong_owner"] += 1
                        stats.setdefault("wrong", []).append((party.key, role, field, _raw(v),
                                                              sorted(owners)))
                elif field in ("accounts", "cards") and k in by_rule:
                    stats["fields"][field]["by_rule"] += 1    # the app replaces it itself
                else:
                    stats["fields"][field]["missing"] += 1
                    stats["missing"].append((party.key, field, _raw(v)))
    third = pt.third_values
    stats["third_offered"] = sum(1 for (f, k) in offered
                                 if (f == "phones" and k in {p[-10:] for p in third["phones"]})
                                 or (f == "addresses" and k in {key("addresses", a)
                                                                for a in third["addresses"]}))
    stats["fields"] = {f: dict(c) for f, c in stats["fields"].items()}
    return stats


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("corpus", type=Path)
    ap.add_argument("--out", type=Path, required=True)
    ap.add_argument("--workers", type=int, default=5)
    ap.add_argument("--match", default="HH", help="household folder prefix to include")
    args = ap.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    cases = sorted(p for p in args.corpus.iterdir() if p.is_dir() and p.name.startswith(args.match))
    with ProcessPoolExecutor(max_workers=args.workers) as pool:
        results = list(pool.map(run_household, [(str(c), str(args.out)) for c in cases]))
    (args.out / "results.json").write_text(json.dumps(results, indent=1))
    total = Counter()
    for r in results:
        offered = sum(c.get("offered", 0) for c in r["fields"].values())
        missing = sum(c.get("missing", 0) for c in r["fields"].values())
        total.update(offered=offered, missing=missing, items=r["items"], tokens=r["tokens"],
                     right=r["right_owner"], wrong=r["wrong_owner"], third=r["third_offered"])
        print(f"{r['case']}: recall {offered}/{offered + missing}; review {r['items']} items "
              f"~{r['tokens']} tokens; owner right {r['right_owner']} wrong {r['wrong_owner']}; "
              f"parties {r['parties_found']} (truth {r['truth_parties']})")
        for miss in r["missing"][:6]:
            print(f"    missing {miss}")
    by_field = defaultdict(Counter)
    for r in results:
        for f, c in r["fields"].items():
            by_field[f].update(c)
    print("\nBy field:", {f: dict(c) for f, c in sorted(by_field.items())})
    print(f"TOTAL recall {total['offered']}/{total['offered'] + total['missing']}; "
          f"review items {total['items']}, ~{total['tokens']} tokens; "
          f"proposals right {total['right']} wrong {total['wrong']}; "
          f"known third-party values offered {total['third']}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
