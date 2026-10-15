"""Run the redactor over a labelled corpus and score every output.

    ~/.venvs/anonymizer-redactor/bin/python -m eval.corpus_eval CORPUS_ROOT \\
        --out /tmp/corpus-run --mode operator --workers 14

Each directory of PDFs is one batch, run through the same four steps the app
takes (captions, suggestions, review, run). Two operator models:

``auto``
    Only what the tool proposes. High-confidence caption names pre-ticked, as
    the app does, and every suggestion ticked - the operator who presses
    "tick all" and trusts it. Measures what the rules find on their own.
``operator``
    ``auto`` plus the household's own names typed in, because an attorney
    always knows who the client, the spouse and the children are. This is the
    realistic run, and a leak here is a defect, full stop.

``--docx`` renders every PDF as a DOCX first (see :mod:`eval.to_docx`) and runs
the DOCX path in the chosen ``--docx-mode``, so the two readers are held to the
same corpus.

Results land as one JSON file per batch under ``--out``; ``eval.report``
aggregates them.
"""

from __future__ import annotations

import argparse
import json
import os
import pickle
import re
import sys
import tempfile
import time
import traceback
import zipfile
from collections import Counter
from concurrent.futures import ProcessPoolExecutor, as_completed
from dataclasses import asdict
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from eval import truth  # noqa: E402

# --------------------------------------------------------------------------
# discovering the corpus
# --------------------------------------------------------------------------


def case_of(path: Path, root: Path) -> str:
    """Which case a document belongs to - the unit secrets are pooled over."""
    rel = path.relative_to(root).parts
    for index, part in enumerate(rel):
        if re.match(r"HH\d+_", part):
            return "/".join(rel[:index + 1])
        if part == "synthetic_household_discovery_2025":
            return part
        if part == "statements" and index + 2 < len(rel):
            return "/".join(rel[:index + 3])
    return "/".join(rel)                       # a lone file is its own case


def discover(root: Path, limit_batches: int | None = None,
             match: str | None = None, photos: bool = False,
             whole_case: bool = False) -> tuple[dict, dict]:
    """``{case: [pdf,...]}`` and ``{batch_id: [pdf,...]}``.

    ``photos`` adds the corpus's phone photos and screenshots, which the app
    makes into PDFs on the way in. ``whole_case`` makes each case one batch -
    every folder of a household together, as an attorney's set is - so that
    one person keeping one stand-in across all of it is tested at all.
    """
    from redactor import photo
    cases: dict[str, list[Path]] = {}
    batches: dict[str, list[Path]] = {}
    found = [p for p in root.rglob("*") if p.is_file() and (
        p.suffix.lower() == ".pdf" or (photos and photo.is_photo(p)))]
    for pdf in sorted(found):
        # secrets pool over the whole case even when only part of it is run
        cases.setdefault(case_of(pdf, root), []).append(pdf)
        if match and not re.search(match, str(pdf.relative_to(root))):
            continue
        batch = case_of(pdf, root) if whole_case else str(pdf.parent.relative_to(root))
        batches.setdefault(batch, []).append(pdf)
    if limit_batches:
        batches = dict(list(batches.items())[:limit_batches])
    return cases, batches


# --------------------------------------------------------------------------
# phase 1: the secrets for each case
# --------------------------------------------------------------------------


def _source_text(pdf: Path) -> tuple[str, bool]:
    import pymupdf
    with pymupdf.open(pdf) as doc:
        text = "\n".join(page.get_text("text") for page in doc)
    return text, len(text.strip()) < 20


def build_case(case: str, pdfs: list[Path], root: Path) -> tuple[str, truth.Secrets, set[str]]:
    secrets = truth.Secrets()
    merchants: set[str] = set()
    # every label in the case directory, not just those beside a PDF: the CSV
    # exports and phone screenshots carry the same people
    base = root / case
    labels = sorted(base.rglob("*.json")) if base.is_dir() else [p.with_suffix(".json") for p in pdfs]
    for sidecar in labels:
        try:
            truth.collect(json.loads(sidecar.read_text()), secrets, merchants)
        except (OSError, ValueError):
            continue
    for pdf in pdfs:
        try:
            text, _scanned = _source_text(pdf)
        except Exception:
            continue
        truth.collect_text(text, secrets)
    truth.finalize(secrets, merchants)
    return case, secrets, merchants


# --------------------------------------------------------------------------
# phase 2: one batch through the app's own steps
# --------------------------------------------------------------------------


def run_flow(files: list[Path], out_dir: Path, mode: str, operator_names: list[str],
             docx_mode: str = "redact", use_ner: bool = False,
             pdf_mode: str = "redact") -> dict:
    from redactor import officials, pipeline, review
    from redactor.engine import Settings

    timings = {}
    t0 = time.time()
    caps = pipeline.collect_caption_names(files)
    offs = pipeline.collect_officials(files)
    timings["captions"] = time.time() - t0

    entries = [(c.name, c.category) for c in caps if c.confidence == "high"]
    if mode == "operator":
        have = {name.casefold() for name, _c in entries}
        entries += [(name, "person") for name in operator_names if name.casefold() not in have]
    roles = {c.name.casefold(): c.role for c in caps}

    def settings_for(names):
        protection = officials.protected_terms(offs, avoid=[n for n, _c in names])
        return Settings(docx_mode=docx_mode, pdf_mode=pdf_mode, use_ner=use_ner,
                        protected_names=list(protection.terms))

    settings = settings_for(entries)
    store, _overlaps = review.build_store(entries, roles)
    t0 = time.time()
    suggestions, notes = pipeline.collect_suggestions(files, store, settings,
                                                      caption_names=caps)
    timings["suggestions"] = time.time() - t0
    have = {name.casefold() for name, _c in entries}
    # "Tick all", as the name screen does it: the suggestions it lists as
    # likely names, not the headings and merchants it lists apart
    from redactor import ner
    known = frozenset(w for name, _c in entries for w in (ner._plain(t) for t in name.split())
                      if len(w) > 2)
    # the local model's sort, as the name screen asks it, from one server the
    # whole run shares (EVAL_LLM_URL) - each worker loading its own would not fit
    labels = {}
    if os.environ.get("EVAL_LLM_URL") and suggestions:
        from redactor import llm_names
        try:
            labels = llm_names.classify([s.text for s in suggestions],
                                        [name for name, _c in entries], os.environ["EVAL_LLM_URL"])
        except Exception:
            labels = {}
    for s in suggestions:
        if s.nickname_for or any(ner._plain(t) in known for t in s.text.split()):
            pass
        elif s.text in labels:
            if labels[s.text] == "other":
                continue
        elif not ner.likely_name(s.text, s.category, known):
            continue
        if s.text.casefold() not in have:
            entries.append((s.text, s.category))
            have.add(s.text.casefold())

    settings = settings_for(entries)
    store, _overlaps = review.build_store(entries, roles)
    t0 = time.time()
    pipeline.prescan(files, store, settings)
    timings["prescan"] = time.time() - t0
    t0 = time.time()
    result = pipeline.run_job(files, store, settings, out_dir, "corpus-eval")
    timings["run"] = time.time() - t0
    return {
        "result": result,
        "captions": [(c.name, c.category, c.confidence) for c in caps],
        "suggestions": [(s.text, s.category) for s in suggestions],
        "entities": [(e.category, e.canonical) for e in store.entities.values()],
        "timings": timings,
        "store": store,
    }


def _output_text(data: bytes, suffix: str, ocr_it: bool) -> str:
    with tempfile.NamedTemporaryFile(suffix=suffix, delete=False) as handle:
        handle.write(data)
        name = handle.name
    try:
        if suffix == ".docx":
            from redactor import docx_processor
            return "\n".join(docx_processor.extract_text(Path(name)).values())
        import pymupdf
        with pymupdf.open(name) as doc:
            if not ocr_it:
                return "\n".join(page.get_text("text") for page in doc)
            from redactor import pdf_processor
            lines = []
            for page in doc:
                # what is visible on the picture, and what a parser reads from
                # the text layer an anonymized scan now carries - both count
                words = pdf_processor._ocr_words(page) or []
                text, _spans = pdf_processor._words_to_text(words)
                lines.append(text)
                lines.append(page.get_text("text"))
            return "\n".join(lines)
    finally:
        os.unlink(name)


# A dollar figure as one OCR word: "$1,204.50", "1,204.50", "-49.21".
_FIGURE_WORD = re.compile(r"[-+(]?\$?\d[\d,]*\.\d{2}\)?")
# A pixel counts as changed when it moved this far in grey (0-255): a black
# box or a white-out moves the page's pixels by well over a hundred, and
# re-encoding the picture by a few.
_CHANGED_GREY = 60
# A figure was touched when this share of its box changed.
_TOUCHED_SHARE = 0.25
_FIGURE_DPI = 100
# a whited-out figure: most of what was ink before is paper after
_INK_GREY = 110
_PAPER_GREY = 200
_INK_GONE_SHARE = 0.5


def _figures_covered(before: bytes, after: bytes, output_text: str = "") -> Counter:
    """Dollar figures on a scan or photo that the run painted over.

    Comparing OCR of the page before and after counted every misread on
    either side as a lost figure - thousands on a corpus where checking by
    eye found none. Only the page as received is read here, to find where
    its figures are; whether each was touched is then measured on the
    pixels under it, which OCR cannot misread. A page whose size changed is
    not measured.

    A figure whited out - anonymize mode clears a word before writing over
    it - changes only its ink, too few pixels for the share above: it counts
    when most of its ink is gone and ``output_text`` (what the delivered page
    reads as) no longer has it, so a figure written back in place is kept.
    """
    import numpy as np
    import pymupdf

    from redactor import ocr, pdf_processor

    engines = ocr.usable_engines()
    touched: Counter = Counter()
    if not engines:
        return touched
    with pymupdf.open(stream=before, filetype="pdf") as src, \
            pymupdf.open(stream=after, filetype="pdf") as out:
        for index, page in enumerate(src):
            if index >= len(out) or page.rect != out[index].rect:
                continue
            words = pdf_processor._ocr_words(page, engines[0]) or []
            figures = [w for w in words if _FIGURE_WORD.fullmatch(w[4].strip())]
            if not figures:
                continue
            grey = []
            for doc_page in (page, out[index]):
                pix = doc_page.get_pixmap(dpi=_FIGURE_DPI, colorspace=pymupdf.csGRAY)
                grey.append(np.frombuffer(pix.samples, np.uint8)
                            .reshape(pix.height, pix.stride)[:, :pix.width].astype(int))
            if grey[0].shape != grey[1].shape:
                continue
            scale = _FIGURE_DPI / 72.0
            for x0, y0, x1, y1, word, *_rest in figures:
                box = (slice(max(0, int(y0 * scale)), int(y1 * scale) + 1),
                       slice(max(0, int(x0 * scale)), int(x1 * scale) + 1))
                region = np.abs(grey[0][box] - grey[1][box])
                if not region.size:
                    continue
                figure = re.sub(r"[^\d,.]", "", word)
                if (region > _CHANGED_GREY).mean() >= _TOUCHED_SHARE:
                    touched[figure] += 1
                    continue
                ink = grey[0][box] < _INK_GREY
                if ink.sum() and (grey[1][box][ink] > _PAPER_GREY).mean() >= _INK_GONE_SHARE \
                        and figure not in re.sub(r"[^\d,.]", "", output_text):
                    touched[figure] += 1
    return touched


def _doc_type(label: dict | None, pdf: Path) -> str:
    if not label:
        return "unlabelled"
    if label.get("doc_type"):
        return str(label["doc_type"])
    if label.get("layout"):
        return f"statement_{str(label['layout'])[:1]}"
    return "unlabelled"


def pipeline_classify(path: Path) -> str | None:
    from redactor import pipeline
    return pipeline.classify(path)


def run_batch(batch_id: str, pdfs: list[str], root: str, out: str, mode: str,
              secrets_path: str, docx: bool, docx_mode: str, use_ner: bool,
              keep_outputs: bool, pdf_mode: str = "redact", scan_money: bool = False) -> dict:
    root_p, out_p = Path(root), Path(out)
    files = [Path(p) for p in pdfs]
    with open(secrets_path, "rb") as handle:
        cases = pickle.load(handle)
    case = case_of(files[0], root_p)
    secrets, merchants = cases[case]
    import redactor
    anonymized = (docx_mode == "anonymize") if docx else (pdf_mode == "anonymize")
    record = {"batch": batch_id, "case": case, "mode": mode, "docx": docx, "pdf_mode": pdf_mode,
              "code": redactor.__file__, "files": [], "error": ""}
    work = Path(tempfile.mkdtemp(prefix="corpus-", dir=out_p / "_tmp"))
    try:
        inputs = files
        if any(pipeline_classify(p) == "image" for p in files):
            # as the app does on the way in: each photo becomes a PDF
            from redactor import pipeline
            inputs = [pipeline.as_document(p, work / "photos") for p in files]
        if docx:
            from eval import to_docx
            inputs = []
            for pdf in files:
                target = work / "in" / (pdf.stem + ".docx")
                target.parent.mkdir(parents=True, exist_ok=True)
                if to_docx.convert(pdf, target):
                    inputs.append(target)
        flow = run_flow(inputs, work / "run", mode, secrets.household_names(),
                        docx_mode=docx_mode, use_ner=use_ner, pdf_mode=pdf_mode)
        result = flow["result"]
        record.update({k: flow[k] for k in ("captions", "suggestions", "entities", "timings")})
        from eval import consistency
        record["consistency"] = consistency.score(flow["store"], secrets)
        record["review"] = [(e.category, e.canonical, e.replacement, e.parent or "")
                            for e in flow["store"].entities.values() if e.enabled]
        archive = zipfile.ZipFile(result.archive) if result.archive else None
        by_source = {o.source.stem: o for o in result.outcomes}
        # a whole case repeats file names across its folders: match on the path
        by_path = {str(Path(o.source)): o for o in result.outcomes}
        input_of = dict(zip(files, inputs)) if not docx else {}
        given = {Path(p).stem: Path(p) for p in inputs}
        for pdf in files:
            label = truth.load_label(pdf)
            source_text, scanned = _source_text(pdf)
            if docx and scanned:
                continue                    # nothing to render; PDF-only case
            outcome = by_path.get(str(input_of.get(pdf, ""))) or by_source.get(pdf.stem)
            entry = {"file": str(pdf.relative_to(root_p)), "doc_type": _doc_type(label, pdf),
                     "scanned": scanned, "status": outcome.status if outcome else "missing",
                     "error": outcome.error if outcome else "not converted",
                     "hits": outcome.hits if outcome else 0,
                     "delivered": outcome.delivered_name if outcome else "",
                     "leaks": [], "destroyed_money": {}, "lost_merchants": [],
                     "warnings": [w.splitlines()[0][:200] for w in (outcome.warnings if outcome else [])]}
            if outcome and outcome.status == "processed" and archive is not None:
                data = archive.read(outcome.delivered_name)
                if keep_outputs:
                    keep = out_p / "outputs" / mode / batch_id
                    keep.mkdir(parents=True, exist_ok=True)
                    (keep / outcome.delivered_name).write_bytes(data)
                suffix = ".docx" if docx else ".pdf"
                text = _output_text(data, suffix, ocr_it=scanned and not docx)
                entry["leaks"] = [asdict(f) for f in truth.leaks(
                    text, secrets, ocr=scanned, anonymized=anonymized)]
                # the filename is delivered too
                entry["leaks"] += [asdict(f) | {"category": "filename:" + f.category}
                                   for f in truth.leaks(outcome.delivered_name.replace("_", " "), secrets)
                                   if f.category not in {"masked_tail", "ssn_shape"}]
                if not scanned:
                    from eval import consistency
                    entry["words"] = consistency.word_count(source_text)
                    entry["lost_words"] = dict(consistency.lost_words(source_text, text, secrets))
                    entry["lost_dates"] = dict(consistency.lost_dates(source_text, text))
                    entry["destroyed_money"] = dict(truth.destroyed_money(source_text, text))
                    entry["lost_merchants"] = truth.lost_merchants(source_text, text, merchants, secrets)
                elif scan_money and not docx and pdf in input_of:
                    entry["destroyed_money_ocr"] = dict(
                        _figures_covered(input_of[pdf].read_bytes(), data, text))
            record["files"].append(entry)
    except Exception as exc:
        record["error"] = f"{type(exc).__name__}: {exc}\n{traceback.format_exc(limit=6)}"
    finally:
        import shutil
        shutil.rmtree(work, ignore_errors=True)
    target = out_p / mode / (re.sub(r"[^\w.-]+", "__", batch_id) + ".json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(record, indent=1, default=str))
    return {"batch": batch_id, "files": len(files), "error": bool(record["error"]),
            "leaks": sum(len(f["leaks"]) for f in record["files"])}


def _snapshot_code(out: Path) -> None:
    """Freeze the code under test for the whole run.

    Workers are recycled every 20 batches and re-import the package, so an
    edit made while a run is going used to split the run silently between two
    versions of the redactor. The workers import from this copy instead, and
    the copy stays beside the results as a record of what produced them.
    """
    import shutil
    frozen = out / "_code"
    if frozen.exists():
        shutil.rmtree(frozen)
    frozen.mkdir(parents=True)
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for package in ("redactor", "eval"):
        shutil.copytree(ROOT / package, frozen / package, ignore=ignore)
    vendor = ROOT / "vendor"
    if vendor.exists():                     # the bundled OCR engine, by reference
        (frozen / "vendor").symlink_to(vendor)
    # spawned workers inherit the parent's sys.path, so this is what they import
    sys.path.insert(0, str(frozen))


def _run_star(job) -> dict:
    try:
        return run_batch(*job)
    except Exception as exc:                          # never lose the pool
        return {"batch": job[0], "files": len(job[1]), "error": repr(exc), "leaks": 0}


# --------------------------------------------------------------------------


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("root", type=Path)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--mode", choices=["auto", "operator", "both"], default="both")
    parser.add_argument("--workers", type=int, default=max(1, (os.cpu_count() or 2) - 2))
    parser.add_argument("--limit", type=int, default=None, help="first N batches only")
    parser.add_argument("--match", default=None, help="regex on the relative PDF path")
    parser.add_argument("--docx", action="store_true", help="render each PDF as DOCX first")
    parser.add_argument("--docx-mode", choices=["redact", "anonymize"], default="anonymize")
    parser.add_argument("--pdf-mode", choices=["redact", "anonymize"], default="redact")
    parser.add_argument("--ner", action="store_true", help="ask the spaCy model too")
    parser.add_argument("--keep-outputs", action="store_true")
    parser.add_argument("--scan-money", action="store_true",
                        help="also find the figures on each scan or photo as received and count those painted over")
    parser.add_argument("--whole-case", action="store_true",
                        help="one batch per case (a household's every folder together), as a set is")
    parser.add_argument("--photos", action="store_true",
                        help="also run the phone photos and screenshots (made into PDFs)")
    args = parser.parse_args(argv)

    root = args.root.resolve()
    # One thread per worker. Tesseract and onnxruntime each start a thread per
    # core, and fourteen workers doing that put the load average near 200 on a
    # 16-core machine. The app runs one document at a time and keeps them.
    for knob in ("OMP_NUM_THREADS", "OMP_THREAD_LIMIT", "OPENBLAS_NUM_THREADS",
                 "MKL_NUM_THREADS", "VECLIB_MAXIMUM_THREADS", "NUMEXPR_NUM_THREADS"):
        os.environ.setdefault(knob, "1")
    os.environ.setdefault("REDACTOR_OCR_THREADS", "1")
    # the harness is its own pool of processes; each reading ahead with a pool
    # of its own would put four OCR processes behind every worker
    os.environ.setdefault("REDACTOR_OCR_WORKERS", "0")
    args.out.mkdir(parents=True, exist_ok=True)
    (args.out / "_tmp").mkdir(exist_ok=True)
    _snapshot_code(args.out)
    cases, batches = discover(root, args.limit, args.match, photos=args.photos,
                              whole_case=args.whole_case)
    print(f"{sum(len(v) for v in batches.values())} PDFs in {len(batches)} batches, "
          f"{len(cases)} cases", flush=True)

    t0 = time.time()
    built = {}
    with ProcessPoolExecutor(args.workers) as pool:
        wanted = {case_of(pdfs[0], root) for pdfs in batches.values()}
        futures = [pool.submit(build_case, case, pdfs, root)
                   for case, pdfs in cases.items() if case in wanted]
        for future in as_completed(futures):
            case, secrets, merchants = future.result()
            built[case] = (secrets, merchants)
    secrets_path = args.out / "secrets.pkl"
    secrets_path.write_bytes(pickle.dumps(built))
    (args.out / "secrets.json").write_text(json.dumps(
        {case: {"people": [asdict(p) for p in s.people.values()], "ids": s.ids,
                "orgs": s.orgs, "addresses": sorted(s.addresses),
                "handles": sorted(s.handles), "phones": sorted(s.phones),
                "emails": sorted(s.emails), "merchants": len(m)}
         for case, (s, m) in built.items()}, indent=1))
    print(f"secrets for {len(built)} cases in {time.time() - t0:.0f}s", flush=True)

    modes = ["auto", "operator"] if args.mode == "both" else [args.mode]
    jobs = [(b, [str(p) for p in pdfs], str(root), str(args.out), mode, str(secrets_path),
             args.docx, args.docx_mode, args.ner, args.keep_outputs, args.pdf_mode,
             args.scan_money)
            for mode in modes for b, pdfs in batches.items()]
    done = 0
    t0 = time.time()
    # multiprocessing.Pool rather than ProcessPoolExecutor: the executor's
    # max_tasks_per_child deadlocks on 3.11 when it replaces a worker, and the
    # workers do need recycling - PyMuPDF and the OCR engine both hold memory.
    import multiprocessing
    with multiprocessing.get_context("spawn").Pool(args.workers, maxtasksperchild=20) as pool:
        for info in pool.imap_unordered(_run_star, jobs):
            done += 1
            if done % 25 == 0 or info["error"]:
                print(f"[{done}/{len(jobs)} {time.time() - t0:.0f}s] {info}", flush=True)
    print(f"done in {time.time() - t0:.0f}s", flush=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
