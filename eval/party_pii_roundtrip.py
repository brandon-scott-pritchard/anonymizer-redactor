"""The whole round trip on one case, as the operator runs it, without a model.

    ~/.venvs/anonymizer-redactor/bin/python -m eval.party_pii_roundtrip CASE PARTIES.csv OUT
        [--pdf-mode anonymize|redact] [--docx-mode anonymize|redact]

1. PARTIES.csv - the party-PII extractor's CSV - becomes the app's party sheet,
   as the operator would type it (``csv_to_sheet``). A value the sheet refuses
   is moved to "other" and said so, as the operator would have to.
2. The app runs on CASE with that sheet, step for step as the window drives it
   (``eval.corpus_eval.run_flow``), and writes its archive into OUT.
3. The leak checker runs on the archive with the same CSV (deterministic
   search only: ``--no-readthrough``) and writes OUT/leak_report.csv.

Synthetic cases only - this is a test harness.
"""

from __future__ import annotations

import argparse
import csv
import importlib.util
import json
import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
CHECKER = ROOT / "skills" / "party-pii-leak-checker" / "scripts" / "check_leaks.py"

KEY = {"names": "names", "date of birth": "dob", "ssn": "ssn", "addresses": "addresses",
       "phones": "phones", "emails": "emails", "tin / ein": "tin", "driver's licence": "dl",
       "passport": "passport", "employers": "employers", "businesses": "businesses",
       "accounts": "accounts", "cards": "cards", "handles": "handles", "vehicles": "vehicles",
       "case numbers": "case_numbers", "parcels": "parcels", "other identifiers": "other"}


def csv_to_sheet(path: Path) -> tuple[dict, list[str]]:
    """The extractor's CSV as the app's party sheet. Rows for both parties, the
    case or nobody in particular go on the petitioner's sheet: every value on
    the sheet is replaced wherever it is found, whoever it is filed under."""
    parties: dict[str, dict] = {}
    order: list[str] = []

    def party(role: str) -> dict:
        label = role if re.match(r"(Petitioner|Respondent|Child \d+|Other party \d+)$", role) \
            else "Petitioner"
        if label not in parties:
            kind = label.split()[0].casefold()
            kind = {"other": "other", "child": "child"}.get(kind, kind)
            parties[label] = {"role": kind}
            order.append(label)
        return parties[label]

    with open(path, encoding="utf-8-sig", newline="") as fh:
        for row in csv.DictReader(fh):
            value = (row.get("Value") or "").strip().lstrip("'").strip()
            key = KEY.get((row.get("Field") or "").strip().casefold())
            if not value or key is None:
                continue
            p = party((row.get("Role") or "").strip())
            if key == "dl":
                item = {"number": value}
            elif key in ("accounts", "cards"):
                item = {"number": value.replace(" ", "") if re.fullmatch(r"[\d ]+", value)
                        else value}
            elif key == "vehicles":
                item = {"vin": value} if len(re.sub(r"\W", "", value)) == 17 else {"plate": value}
            else:
                item = value
            p.setdefault(key, []).append(item)
    # the sheet wants a petitioner or a respondent first
    order.sort(key=lambda l: (0 if l == "Petitioner" else 1 if l == "Respondent" else 2, l))
    return {"parties": [parties[l] for l in order]}, []


def accepted_sheet(sheet: dict) -> tuple[dict, list[str]]:
    """Move each value the app's sheet refuses to "other", as an operator would."""
    from redactor import parties as core
    notes = []
    for _ in range(200):
        try:
            core.PartySheet.from_json(sheet)
            return sheet, notes
        except core.PartyError as exc:
            m = re.match(r"parties\[(\d+)\]\.(\w+)(?:\[(\d+)\])?", exc.where or "")
            if not m or m.group(3) is None:
                raise
            item = sheet["parties"][int(m.group(1))]
            value = item[m.group(2)].pop(int(m.group(3)))
            text = value if isinstance(value, str) else next(iter(value.values()))
            item.setdefault("other", []).append(text)
            notes.append(f"{m.group(2)} value moved to other: {exc}")
    raise RuntimeError("the sheet would not settle")


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("case", type=Path)
    ap.add_argument("csv", type=Path)
    ap.add_argument("out", type=Path)
    ap.add_argument("--pdf-mode", default="anonymize", choices=("anonymize", "redact"))
    ap.add_argument("--docx-mode", default="anonymize", choices=("anonymize", "redact"))
    args = ap.parse_args(argv)
    sys.path.insert(0, str(ROOT))
    from eval import corpus_eval
    sheet, _ = csv_to_sheet(args.csv)
    sheet, notes = accepted_sheet(sheet)
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "sheet.json").write_text(json.dumps(sheet, indent=1))
    for note in notes:
        print("SHEET:", note)
    from redactor import pipeline
    # every type the app takes; a photo becomes the scanned page the window makes of it
    photos = args.out / "photos"
    photos.mkdir(parents=True, exist_ok=True)
    files = [pipeline.as_document(p, photos) for p in sorted(args.case.rglob("*"))
             if p.is_file() and pipeline.classify(p) is not None]
    result = corpus_eval.run_flow(files, args.out / "app", "operator", [],
                                  docx_mode=args.docx_mode, pdf_mode=args.pdf_mode, sheet=sheet)
    archive = next((args.out / "app").glob("*.zip"), None)
    print("APP:", archive, "| files:", len(files))
    spec = importlib.util.spec_from_file_location("check_leaks", CHECKER)
    checker = importlib.util.module_from_spec(spec)
    sys.modules["check_leaks"] = checker
    spec.loader.exec_module(checker)
    return checker.main(["run", str(archive), "--csv", str(args.csv), "--out",
                         str(args.out / "check"), "--no-readthrough"])


if __name__ == "__main__":
    sys.exit(main())
