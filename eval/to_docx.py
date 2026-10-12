"""Render a labelled PDF as the DOCX an office would have made of it.

The corpus is all PDF, and the DOCX reader has to meet the same documents. A
statement or a pay stub typed up in Word is a letterhead in paragraphs, the
columns in a real ``w:tbl``, and the remittance address as a short run of
paragraphs - so that is what this writes: every visual row with two or more
columns joins a table, every other row is a paragraph.

Deliberately independent of ``redactor.pdf_processor``. It re-derives rows and
columns from word geometry with its own few lines of code, so a bug in the
redactor's PDF table reader cannot quietly produce the DOCX that hides it.
"""

from __future__ import annotations

from pathlib import Path


def _rows(page) -> list[list[tuple[str, float, float]]]:
    words = sorted((w for w in page.get_text("words") if w[4].strip()),
                   key=lambda w: ((w[1] + w[3]) / 2, w[0]))
    grouped: list[list] = []
    anchor = 0.0
    for w in words:
        mid = (w[1] + w[3]) / 2
        if grouped and abs(mid - anchor) <= (w[3] - w[1]) * 0.5:
            grouped[-1].append(w)
        else:
            grouped.append([w])
            anchor = mid
    rows = []
    for row in grouped:
        row.sort(key=lambda w: w[0])
        cells: list[list] = []
        for w in row:
            width = (w[2] - w[0]) / max(len(w[4]), 1)
            if cells and w[0] - cells[-1][2] < max(width, 1.0) * 1.6:
                cells[-1][0] += " " + w[4]
                cells[-1][2] = w[2]
            else:
                cells.append([w[4], w[0], w[2]])
        rows.append([tuple(c) for c in cells])
    return rows


def _table(rows):
    widest = max(rows, key=len)
    columns = [(a, b) for _t, a, b in widest]
    grid = []
    for row in rows:
        cells = [""] * len(columns)
        for text, a, b in row:
            best = max(range(len(columns)),
                       key=lambda k: (min(b, columns[k][1]) - max(a, columns[k][0]),
                                      -abs((a + b) / 2 - sum(columns[k]) / 2)))
            cells[best] = (cells[best] + " " + text).strip()
        grid.append(cells)
    return grid


def convert(pdf: Path, target: Path) -> bool:
    """Write ``target``; False when the PDF has no text layer to render."""
    import docx
    import pymupdf

    document = docx.Document()
    wrote = False
    with pymupdf.open(pdf) as doc:
        for number, page in enumerate(doc):
            if number:
                document.add_page_break()
            rows = _rows(page)
            run: list = []

            def flush():
                nonlocal run
                if len(run) >= 2:
                    grid = _table(run)
                    table = document.add_table(rows=len(grid), cols=len(grid[0]))
                    for r, cells in enumerate(grid):
                        for c, text in enumerate(cells):
                            table.cell(r, c).text = text
                else:
                    for row in run:
                        document.add_paragraph("    ".join(t for t, _a, _b in row))
                run = []

            for row in rows:
                wrote = True
                texts = [t for t, _a, _b in row]
                heading = len(texts) >= 3 and not any(ch.isdigit() for t in texts for ch in t)
                figure = len(row) == 1 and not any(ch.isalpha() for ch in texts[0])
                if heading and run:
                    flush()               # a heading row starts its own table
                if len(row) >= 2 or (figure and run):
                    run.append(row)
                    continue
                flush()
                document.add_paragraph(row[0][0])
            flush()
    if not wrote:
        return False
    target.parent.mkdir(parents=True, exist_ok=True)
    document.save(target)
    return True
