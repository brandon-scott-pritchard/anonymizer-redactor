"""Can Kev tell, from an item's context, whether it is what the run says it is?

    ~/.venvs/anonymizer-redactor/bin/python -m eval.kev_check_test KEV_URL RUN_DIR [RUN_DIR ...]

The answer key: items the labels confirm, and patterns read in context and
found right or wrong (a store number after a merchant taken for a card; a
bank's toll-free line or P.O. box taken for a party's; a payment ID for a VIN;
"Premium" for a handle; a title company's "File #" for a court case). Kev is
asked the question the app needs - is this a private person's phone, a
person's account, ... - not merely "is this a phone number". Reports AUC and
precision/recall at a threshold, per kind.
"""

from __future__ import annotations

import argparse
import json
import random
import re
import urllib.request
from collections import defaultdict
from pathlib import Path

QUESTION = {
    "phone": ("Is the marked text a private person's own phone number - not a business's, "
              "bank's, store's, utility's or customer-service number?"),
    "address": ("Is the marked text a private person's home or mailing address - not a "
                "business's or bank's address or a company's P.O. box?"),
    "account": ("Is the marked text a person's bank, card, credit, loan or investment account "
                "number, or its masked last digits - not a store number, check number, "
                "transaction reference, routing number, date or amount?"),
    "vehicle": ("Is the marked text a vehicle identification number (VIN) or a licence plate - "
                "not a transaction, payment or order ID?"),
    "handle": ("Is the marked text a person's username or payment or social-media handle - not "
               "an ordinary word, a product, a payment method or a heading?"),
    "case": "Is the marked text a court case number - not a file, order, title or reference number?",
    "title": "Is the marked text a vehicle title number - not a heading or a word?",
    "ssn": "Is the marked text a Social Security number or its masked form?",
    "name": "Is the marked text a person's name?",
}
KIND = {"phone": "phone", "fax": "phone", "street_address": "address", "bank_account": "account",
        "masked_account": "account", "credit_card": "account", "investment_account": "account",
        "vin": "vehicle", "license_plate": "vehicle", "social_handle": "handle",
        "payment_handle": "handle", "case_number": "case", "vehicle_title": "title",
        "ssn": "ssn", "person": "name", "minor": "name"}
_TOLL_FREE = re.compile(r"^(?:\+?1[\s.\-]?)?\(?8(?:00|33|44|55|66|77|88)\)?")


def verdict_by_pattern(kind: str, h: dict) -> bool | None:
    """True / False where reading the context settles it; None where it does not."""
    text, before = h["text"].strip(), h["before"]
    if kind == "account":
        if text.startswith("#"):
            return False                                   # a store number
        if re.search(r"(?i)CHECKCARD\s*$", before) and re.fullmatch(r"\d{4}", text):
            return False                                   # the MMDD date
        if re.search(r"(?i)(transfer (to|from)|withdrawal to|deposit from|added cash from|"
                     r"account (number|ending in|id)|ending in|debit card|visa|mastercard)"
                     r"[\s:.#]*$", before) and re.search(r"[*x•.]{2,}|\d{4}$", text):
            return True
    if kind == "phone":
        if _TOLL_FREE.match(text):
            return False
        if re.search(r"Line \d - [A-Z][a-z]+\s*$", before):
            return True                                    # a family plan's line
    if kind == "address":
        if re.match(r"(?i)p\.?\s*o\.?\s*box", text) and re.search(
                r"(?i)(bank|card|services|company|n\.a\.|credit union|trail|crest)", before[-60:]):
            return False
    if kind == "vehicle" and re.search(r"ID:\s*$", before):
        return False
    if kind == "vehicle" and re.search(r"(?i)\b(VIN|Plate)\s*$", before):
        return True
    if kind == "handle":
        if text.startswith(("@", "$")):
            return True
        if text.isalpha() and text[:1].isupper() and text[1:].islower():
            return False                                   # "Premium", "Visa"
    if kind == "case" and re.search(r"(?i)file\s*#\s*$", before):
        return False
    if kind == "title" and text.isalpha():
        return False
    return None


def ask(url: str, h: dict, kind: str) -> float:
    state = f"...{h['before'][-120:]} [[{h['text']}]] {h['after'][:120]}..."
    body = {"state": state, "model": "kev-latest", "questions": {"q": {
        "type": "noul", "instructions": QUESTION[kind] + " The marked text is between [[ and ]].",
        "criteria": {"true": "Yes, given its shape and the words around it.",
                     "false": "No - it is something else, as the words around it show."}}}}
    request = urllib.request.Request(f"{url}/v1/systemone", data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
    with urllib.request.urlopen(request, timeout=120) as response:
        return float(json.loads(response.read())["answers"]["q"]["noul"])


def main(argv=None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("kev")
    parser.add_argument("runs", nargs="+", type=Path)
    parser.add_argument("--per-kind", type=int, default=30)
    parser.add_argument("--threshold", type=float, default=0.5)
    args = parser.parse_args(argv)
    random.seed(5)
    pools: dict[str, dict[bool, dict]] = defaultdict(lambda: {True: {}, False: {}})
    for run in args.runs:
        for f in (run / "operator").glob("*.json"):
            for x in json.loads(f.read_text())["files"]:
                for h in x.get("hit_audit", []):
                    kind = KIND.get(h["category"])
                    if kind is None:
                        continue
                    truth = True if h["verdict"] == "confirmed" else (
                        False if h["verdict"] == "wrong_kind" else verdict_by_pattern(kind, h))
                    if truth is None:
                        continue
                    pools[kind][truth].setdefault(h["text"].casefold(), h)
    print(f"{'kind':8} {'pos':>4} {'neg':>4} {'AUC':>5} {'prec@t':>7} {'rec@t':>6} {'s/item':>6}")
    import time
    allpos, allneg = [], []
    for kind, pool in sorted(pools.items()):
        pos = random.sample(list(pool[True].values()), min(args.per_kind, len(pool[True])))
        neg = random.sample(list(pool[False].values()), min(args.per_kind, len(pool[False])))
        started = time.time()
        sp = [ask(args.kev, h, kind) for h in pos]
        sn = [ask(args.kev, h, kind) for h in neg]
        seconds = (time.time() - started) / max(len(pos) + len(neg), 1)
        allpos += sp; allneg += sn
        auc = (sum((a > b) + 0.5 * (a == b) for a in sp for b in sn) / (len(sp) * len(sn))
               if sp and sn else float("nan"))
        tp = sum(p >= args.threshold for p in sp); fp = sum(p >= args.threshold for p in sn)
        prec = tp / (tp + fp) if tp + fp else float("nan")
        rec = tp / len(sp) if sp else float("nan")
        print(f"{kind:8} {len(sp):4} {len(sn):4} {auc:5.2f} {prec:7.0%} {rec:6.0%} {seconds:6.2f}")
        worst = sorted(zip(sn, neg), key=lambda t: -t[0])[:3] + sorted(zip(sp, pos), key=lambda t: t[0])[:3]
        for p, h in worst:
            print(f"      p={p:.2f} {h['text']!r}: ..{h['before'][-40:]} [*] {h['after'][:30]}..")
    if allpos and allneg:
        auc = sum((a > b) + 0.5 * (a == b) for a in allpos for b in allneg) / (len(allpos) * len(allneg))
        print(f"ALL      {len(allpos):4} {len(allneg):4} {auc:5.2f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
