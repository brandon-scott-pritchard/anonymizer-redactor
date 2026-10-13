"""Why did a word disappear? Name the rule whose box covered it.

    ~/.venvs/anonymizer-redactor/bin/python -m eval.why BATCH_DIR FILE.pdf "111,274.97" \\
        [--name "Nathan C Blackburn" ...]

Runs the app's own steps over every PDF in BATCH_DIR (captions, suggestions,
prescan) so the store is what a real run would have, then redacts FILE.pdf and
reports, for every occurrence of the text on its pages, each box that touched
it: the hit's category, the text the hit matched, and the entity behind it -
or "address block" / "image" for the geometric covers.
"""

from __future__ import annotations

import argparse
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


def main(argv=None) -> int:
    import pymupdf

    from redactor import officials, pdf_processor, pipeline, review
    from redactor.engine import Settings

    parser = argparse.ArgumentParser()
    parser.add_argument("batch", type=Path)
    parser.add_argument("file")
    parser.add_argument("needle")
    parser.add_argument("--name", action="append", default=[], help="a name the operator types")
    args = parser.parse_args(argv)

    files = sorted(args.batch.glob("*.pdf"))
    caps = pipeline.collect_caption_names(files)
    offs = pipeline.collect_officials(files)
    entries = [(c.name, c.category) for c in caps if c.confidence == "high"]
    entries += [(n, "person") for n in args.name]
    settings = Settings(use_ner=False, protected_names=list(
        officials.protected_terms(offs, avoid=[e[0] for e in entries]).terms))
    store, _ = review.build_store(entries, {})
    found, _ = pipeline.collect_suggestions(files, store, settings, caption_names=caps)
    entries += [(s.text, s.category) for s in found]
    store, _ = review.build_store(entries, {})
    pipeline.prescan(files, store, settings)

    causes: dict[tuple, str] = {}
    original_rects = pdf_processor._rects_for_hit
    original_blocks = pdf_processor.address_blocks

    def rects_for_hit(hit, spans, padding=pdf_processor.BOX_PADDING, **options):
        out = original_rects(hit, spans, padding, **options)
        entity = store.entities.get(hit.entity_key)
        for rect in out:
            causes[tuple(round(v, 1) for v in rect)] = (
                f"{hit.category}: {hit.text!r} (entity {entity.canonical!r})" if entity
                else f"{hit.category}: {hit.text!r}")
        return out

    def blocks(words, ocr=False):
        out = original_blocks(words, ocr=ocr)
        for rect in out:
            causes[tuple(round(v, 1) for v in rect)] = "address block"
        return out

    seen: list[tuple[int, pymupdf.Rect, str]] = []
    original_annot = pymupdf.Page.add_redact_annot

    def add_redact_annot(self, quad, *a, **k):
        rect = pymupdf.Rect(quad)
        seen.append((self.number, rect, causes.get(tuple(round(v, 1) for v in rect), "image")))
        return original_annot(self, quad, *a, **k)

    pdf_processor._rects_for_hit = rects_for_hit
    pdf_processor.address_blocks = blocks
    pymupdf.Page.add_redact_annot = add_redact_annot
    try:
        with tempfile.TemporaryDirectory() as tmp:
            pdf_processor.process(args.batch / args.file, Path(tmp) / "out.pdf", store, settings)
    finally:
        pymupdf.Page.add_redact_annot = original_annot
        pdf_processor._rects_for_hit = original_rects
        pdf_processor.address_blocks = original_blocks

    first = args.needle.split()[0]
    with pymupdf.open(args.batch / args.file) as doc:
        reported = 0
        for page in doc:
            for word in page.get_text("words"):
                if first not in word[4]:
                    continue
                box = pymupdf.Rect(word[:4])
                hits = [cause for number, rect, cause in seen
                        if number == page.number and rect.intersects(box)]
                print(f"page {page.number + 1} {word[4]!r} at {tuple(round(v) for v in box)}: "
                      + ("; ".join(dict.fromkeys(hits)) if hits else "untouched"))
                reported += 1
        if not reported:
            print(f"{args.needle!r} is not on any page's text layer")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
