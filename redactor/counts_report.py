"""A counts-only report on a real set - numbers, never content.

    ~/.venvs/anonymizer-redactor/bin/python -m redactor.counts_report FOLDER --out REPORT.txt

Runs the scan the app runs today when it works everything out itself
(captions, the bench, suggestions with "Tick all" on the likely ones, the
review scan), then writes only numbers under fixed labels: how many rows each
kind of thing produced, how many documents each row was found in, how many
rows would fold into another by shape alone, how many fall in the kinds the
attorney leaves as written.

Nothing from the documents is written: no name, value, word, file name or
folder name. Before writing, every word of the report is checked against the
report's own fixed vocabulary; anything else and the report is not written.
"""

from __future__ import annotations

import argparse
import re
import sys
import tempfile
import time
from collections import Counter, defaultdict
from pathlib import Path

# the kinds the attorney leaves as written (not publicly searchable)
LEAVE = {"check_number", "order_number", "tracking_number", "routing_number", "policy_number",
         "claim_number", "employee_id", "health_plan", "student_id", "loan_number"}
BUCKETS = ((1, 1, "1"), (2, 2, "2"), (3, 3, "3"), (4, 9, "4-9"), (10, 10 ** 9, "10+"))


def _bucket(n: int) -> str:
    return next(label for lo, hi, label in BUCKETS if lo <= n <= hi)


def _shape_key(category: str, text: str) -> str | None:
    """What two rows share when they are one thing by shape alone."""
    from . import grouping, patterns

    digits = re.sub(r"\D", "", text)
    if category in ("phone", "fax") and len(digits) >= 10:
        return "p" + digits[-10:]
    if category in ("bank_account", "masked_account", "credit_card", "investment_account") \
            and len(digits) >= 4:
        return "a" + digits[-4:]
    if category == "ssn" and len(digits) >= 4:
        return "s" + digits[-4:]
    if category == "street_address":
        parsed = grouping.parse_address(text)
        if parsed is not None and parsed.street:
            return f"r{parsed.number}|{'-'.join(parsed.street)}|{parsed.unit or ''}"
    if category == "dob" and patterns.looks_like_date(text):
        return "d" + digits
    if category in ("person", "minor"):
        words = re.findall(r"[a-z]{2,}", text.casefold())
        return ("n" + words[-1]) if words else None
    return None


def run(folder: Path, quick: bool = False, progress=print) -> dict:
    from . import grouping, ner, officials, patterns, pipeline, review
    from .engine import Settings

    started = time.time()
    gathered = pipeline.gather([folder])
    work = Path(tempfile.mkdtemp(prefix="counts-"))
    files, kinds = [], Counter()
    for source, _relative in gathered.files:
        kind = pipeline.classify(source)
        if kind == "image":
            try:
                source = pipeline.as_document(source, work)
                kinds["photo"] += 1
            except Exception:
                kinds["unreadable"] += 1
                continue
        elif kind:
            kinds[kind] += 1
        files.append(source)
    if quick:
        import pymupdf
        typed = []
        for f in files:
            if f.suffix.lower() != ".pdf":
                typed.append(f)
                continue
            try:
                with pymupdf.open(f) as doc:
                    if len("".join(page.get_text() for page in doc).strip()) >= 20:
                        typed.append(f)
            except Exception:
                pass
        files = typed
    progress(f"{len(files)} documents to read")
    pages = pipeline.count_pages(files)

    progress("reading captions and the bench")
    caps = pipeline.collect_caption_names(files)
    bench = pipeline.collect_officials(files)
    entries = [(c.name, c.category) for c in caps if c.confidence == "high"]
    protection = officials.protected_terms(bench, avoid=[n for n, _c in entries])
    settings = Settings(pdf_mode="anonymize", use_ner=True, protected_names=list(protection.terms))
    store, _ = review.build_store(entries)
    progress("collecting suggestions")
    suggestions, _notes = pipeline.collect_suggestions(files, store, settings, caption_names=caps)
    known = frozenset(w for name, _c in entries for w in (ner._plain(t) for t in name.split())
                      if len(w) > 2)
    likely = [s for s in suggestions if s.nickname_for or ner.likely_name(s.text, s.category, known)]
    have = {n.casefold() for n, _c in entries}
    for s in likely:
        if s.text.casefold() not in have:
            entries.append((s.text, s.category))
            have.add(s.text.casefold())
    store, _ = review.build_store(entries)
    progress("the review scan")
    pipeline.prescan(files, store, settings)

    toll_free = 0
    for text in pipeline.document_texts(files).values():
        toll_free += len(patterns.toll_free_spans(text))

    # groups: a head with every form folded under it
    rows = [e for e in store.entities.values() if e.enabled]
    by_key = {e.key: e for e in rows}
    groups: dict[str, dict] = {}
    for e in rows:
        head = grouping.head_of(store, e)
        g = groups.setdefault(head.key, {"category": head.category, "forms": 0, "documents": set(),
                                         "texts": []})
        g["forms"] += 1
        g["documents"].update(e.documents)
        g["texts"].append(e.canonical)
    seen_rows = [g for g in groups.values() if g["documents"]]

    stats = {
        "documents": len(files), "by_type": dict(kinds), "pages": pages,
        "skipped_files": len(gathered.skipped), "seconds": int(time.time() - started),
        "captions": len(caps), "bench": len(bench), "suggestions": len(suggestions),
        "suggestions_likely": len(likely), "toll_free_skipped": toll_free,
        "rows_listed": len(rows), "groups": len(groups), "groups_found": len(seen_rows),
        "by_kind": {}, "fold_by_shape": {},
    }
    per_kind: dict[str, dict] = defaultdict(lambda: {"groups": 0, "forms": 0, "buckets": Counter(),
                                                     "single_word": 0})
    shapes: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for g in seen_rows:
        k = per_kind[g["category"]]
        k["groups"] += 1
        k["forms"] += g["forms"]
        k["buckets"][_bucket(len(g["documents"]))] += 1
        if g["category"] in ("person", "minor") and all(len(t.split()) == 1 for t in g["texts"]):
            k["single_word"] += 1
        key = _shape_key(g["category"], g["texts"][0])
        if key:
            shapes[g["category"]][key] += 1
    for kind, k in per_kind.items():
        stats["by_kind"][kind] = {"groups": k["groups"], "forms": k["forms"],
                                  "buckets": dict(k["buckets"]), "single_word": k["single_word"],
                                  "leave": kind in LEAVE}
        stats["fold_by_shape"][kind] = sum(n - 1 for n in shapes[kind].values() if n > 1)
    import shutil
    shutil.rmtree(work, ignore_errors=True)
    return stats


# --------------------------------------------------------------------------
# the report, and the check that it says nothing but numbers
# --------------------------------------------------------------------------

def render(stats: dict) -> str:
    from . import categories

    lines = ["COUNTS ONLY REPORT", "",
             "Numbers only. No names, values, words, file names or folder names from the documents.",
             "", "DOCUMENTS",
             f"  documents read: {stats['documents']}",
             f"  pages: {stats['pages']}"]
    for kind, n in sorted(stats["by_type"].items()):
        lines.append(f"  type {kind}: {n}")
    lines += [f"  files left out, not documents: {stats['skipped_files']}",
              f"  seconds taken: {stats['seconds']}", "",
              "NAME SCREEN",
              f"  caption names: {stats['captions']}",
              f"  judicial officers: {stats['bench']}",
              f"  suggestions proposed: {stats['suggestions']}",
              f"  suggestions sorted as likely names: {stats['suggestions_likely']}",
              f"  toll free numbers skipped: {stats['toll_free_skipped']}", "",
              "REVIEW LIST",
              f"  rows listed (every form): {stats['rows_listed']}",
              f"  groups (one per thing, forms folded): {stats['groups']}",
              f"  groups found in at least one document: {stats['groups_found']}", "",
              "GROUPS BY KIND, AND HOW MANY DOCUMENTS EACH WAS FOUND IN",
              "  kind | groups | forms | in 1 doc | 2 | 3 | 4-9 | 10+ | would fold by shape | "
              "single word names | left as written by rule"]
    total = Counter()
    for kind, k in sorted(stats["by_kind"].items(), key=lambda kv: -kv[1]["groups"]):
        b = k["buckets"]
        label = categories.label_for(kind)
        fold = stats["fold_by_shape"].get(kind, 0)
        lines.append(f"  {label} | {k['groups']} | {k['forms']} | {b.get('1', 0)} | {b.get('2', 0)} | "
                     f"{b.get('3', 0)} | {b.get('4-9', 0)} | {b.get('10+', 0)} | {fold} | "
                     f"{k['single_word']} | {'yes' if k['leave'] else 'no'}")
        for name in ("1", "2", "3", "4-9", "10+"):
            total[name] += b.get(name, 0)
        total["leave"] += k["groups"] if k["leave"] else 0
        total["fold"] += fold
    lines += ["", "TOTALS",
              f"  groups found in 1 document: {total['1']}",
              f"  groups found in 2 documents: {total['2']}",
              f"  groups found in 3 documents: {total['3']}",
              f"  groups found in 4 to 9 documents: {total['4-9']}",
              f"  groups found in 10 or more documents: {total['10+']}",
              f"  groups in kinds left as written by rule: {total['leave']}",
              f"  groups that would fold into another by shape alone: {total['fold']}",
              f"  groups pre ticked at 2 or more documents: {total['2'] + total['3'] + total['4-9'] + total['10+']}",
              f"  groups pre ticked at 3 or more documents: {total['3'] + total['4-9'] + total['10+']}",
              ""]
    return "\n".join(lines) + "\n"


def _vocabulary() -> set[str]:
    from . import categories

    words = set()
    template = render({"documents": 0, "by_type": {"pdf": 0, "docx": 0, "photo": 0,
                                                   "unreadable": 0},
                       "pages": 0, "skipped_files": 0, "seconds": 0, "captions": 0, "bench": 0,
                       "suggestions": 0, "suggestions_likely": 0, "toll_free_skipped": 0,
                       "rows_listed": 0, "groups": 0, "groups_found": 0, "by_kind": {},
                       "fold_by_shape": {}})
    words.update(re.findall(r"[A-Za-z]+", template))
    for c in categories.CATEGORIES:
        words.update(re.findall(r"[A-Za-z]+", categories.label_for(c.key)))
    words.update({"yes", "no", "doc", "docs"})
    return {w.casefold() for w in words}


def safe(report: str) -> list[str]:
    """Every word not in the report's own vocabulary - must be none."""
    allowed = _vocabulary()
    return sorted({w for w in re.findall(r"[A-Za-z]+", report) if w.casefold() not in allowed})


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="A counts-only report on a real set.")
    parser.add_argument("folder", type=Path)
    parser.add_argument("--out", type=Path, required=True,
                        help="where to write the report (not inside the client folder)")
    parser.add_argument("--quick", action="store_true",
                        help="typed documents only - skips reading scans and photos")
    args = parser.parse_args(argv)
    folder = args.folder.expanduser().resolve()
    out = args.out.expanduser().resolve()
    if folder in out.parents:
        print("Write the report outside the client folder.", file=sys.stderr)
        return 2
    stats = run(folder, quick=args.quick, progress=lambda m: print(m, flush=True))
    report = render(stats)
    unexpected = safe(report)
    if unexpected:
        print(f"Not written: the report would contain {len(unexpected)} word(s) that are not "
              "its own labels.", file=sys.stderr)
        return 3
    out.write_text(report, encoding="utf-8")
    print(f"Written: {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
