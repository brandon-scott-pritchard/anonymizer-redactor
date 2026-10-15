"""Scanned pages read ahead in worker processes: the same words, sooner."""

import io
from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf")

from redactor import ocr, ocr_pool, pdf_processor  # noqa: E402


@pytest.fixture
def pool(monkeypatch):
    if not ocr.usable_engines():
        pytest.skip("no OCR engine")
    monkeypatch.setenv("REDACTOR_OCR_WORKERS", "2")
    ocr_pool.shutdown()
    pdf_processor._ocr_cache.clear()
    yield
    ocr_pool.shutdown()
    pdf_processor._ocr_cache.clear()


def _scan(path, pages):
    """A PDF of pictures of text - no text layer, as a scanner makes."""
    text = pymupdf.open()
    for number in range(pages):
        text.new_page().insert_text((72, 100), f"Kyle D Harrington statement page {number + 1}",
                                    fontsize=16)
    scan = pymupdf.open()
    for page in text:
        picture = page.get_pixmap(dpi=150).tobytes("png")
        scan.new_page().insert_image(scan[-1].rect if len(scan) else pymupdf.Rect(0, 0, 612, 792),
                                     stream=io.BytesIO(picture))
    scan.save(path)
    return path


def _in_line(path):
    pdf_processor._ocr_cache.clear()
    with pymupdf.open(path) as doc:
        return [pdf_processor._page_words(page, path, index)[0] for index, page in enumerate(doc)]


def test_pages_read_ahead_are_the_pages_read_in_line(pool, tmp_path):
    path = _scan(tmp_path / "scan.pdf", 3)
    expected = _in_line(path)
    pdf_processor._ocr_cache.clear()
    seen = []
    ocr_pool.read_ahead([path], on_page=lambda p, i: seen.append(i))
    assert sorted(seen) == [0, 1, 2]
    names = [engine.name for engine in ocr.usable_engines()]
    for index in range(3):
        base = pdf_processor._ocr_key(path, index)
        assert pdf_processor.ocr_cached(base, names)
    # what every step then reads comes out of the cache, word for word
    with pymupdf.open(path) as doc:
        got = [pdf_processor._page_words(page, path, index)[0] for index, page in enumerate(doc)]
    assert got == expected
    assert any("HARRINGTON" in w[4].upper() for words in got[0] for w in words)


def test_reading_stops_at_the_pages_a_step_needs(pool, tmp_path):
    path = _scan(tmp_path / "scan.pdf", 4)
    seen = []
    ocr_pool.read_ahead([path], per_file=2, on_page=lambda p, i: seen.append(i))
    assert sorted(seen) == [0, 1]
    names = [engine.name for engine in ocr.usable_engines()]
    assert not pdf_processor.ocr_cached(pdf_processor._ocr_key(path, 3), names)


def test_a_page_read_in_the_background_is_not_read_twice(pool, tmp_path, monkeypatch):
    path = _scan(tmp_path / "scan.pdf", 2)
    ocr_pool.read_ahead([path], wait=False)
    scheduler = ocr_pool._get_scheduler()
    submitted = []
    real = scheduler.readers.submit
    monkeypatch.setattr(scheduler.readers, "submit",
                        lambda fn, *args: submitted.append(args) or real(fn, *args))
    seen = []
    ocr_pool.read_ahead([path], on_page=lambda p, i: seen.append(i))
    # the step waited on the readings already under way rather than starting its own
    assert len(submitted) == len(set(submitted)) and sorted(seen) == [0, 1]
    names = [engine.name for engine in ocr.usable_engines()]
    assert all(pdf_processor.ocr_cached(pdf_processor._ocr_key(path, i), names) for i in range(2))


def test_turned_off_it_reads_nothing(monkeypatch, tmp_path):
    monkeypatch.setenv("REDACTOR_OCR_WORKERS", "0")
    ocr_pool.shutdown()
    seen = []
    ocr_pool.read_ahead([tmp_path / "missing.pdf"], on_page=lambda p, i: seen.append(i))
    assert seen == [] and ocr_pool._scheduler is None


def test_text_pages_are_not_sent_for_reading(pool, tmp_path):
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "A typed page with a perfectly good text layer on it.")
    path = tmp_path / "typed.pdf"
    doc.save(path)
    seen = []
    ocr_pool.read_ahead([path], on_page=lambda p, i: seen.append(i))
    scheduler = ocr_pool._get_scheduler()
    assert seen == [] and not scheduler.pages


def test_the_pages_a_step_waits_for_go_ahead_of_a_dropped_batch(pool, tmp_path, monkeypatch):
    """A drop of a few hundred files queued every page of every file, and the
    caption step - wanting only the first pages - waited behind the lot with
    its counter at nought."""
    monkeypatch.setenv("REDACTOR_OCR_WORKERS", "1")
    ocr_pool.shutdown()
    batch, wanted = _scan(tmp_path / "batch.pdf", 6), _scan(tmp_path / "wanted.pdf", 2)
    ocr_pool.read_ahead([batch], wait=False)
    scheduler = ocr_pool._get_scheduler()
    order = []
    finished = scheduler._finished
    monkeypatch.setattr(scheduler, "_finished",
                        lambda base, task: order.append(Path(base[0]).name) or finished(base, task))
    ocr_pool.read_ahead([wanted], on_page=lambda p, i: None)
    # one worker and one page ready behind it: at most two of the batch's
    # pages can have been handed out before the step asked
    assert order.count("wanted.pdf") == 2
    assert order.index("wanted.pdf") <= 2 and order[:4].count("wanted.pdf") == 2


@pytest.mark.parametrize("step", ["collect_children", "collect_officials", "document_texts"])
def test_every_step_that_reads_whole_documents_reads_them_ahead(step, monkeypatch, tmp_path):
    """The caption step read six pages ahead, then built the children list and
    the bench page by page, one at a time, for hours, its counter already full."""
    from redactor import pipeline
    calls = []
    monkeypatch.setattr(ocr_pool, "read_ahead",
                        lambda files, per_file=None, **kw: calls.append((list(files), per_file)))
    path = tmp_path / "a.pdf"
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), "Nothing to see")
    doc.save(path)
    getattr(pipeline, step)([path])
    assert ([path], None) in calls


def test_readers_leave_when_the_app_dies_without_closing(tmp_path):
    """A force-quit app cannot stop its readers; they must notice and go."""
    import os
    import subprocess
    import sys
    import textwrap
    import time
    script = textwrap.dedent(f'''
        import os, sys
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
        os.environ["REDACTOR_OCR_WORKERS"] = "2"
        from redactor import ocr_pool
        if __name__ == "__main__":
            scheduler = ocr_pool._get_scheduler()
            pids = [p.pid for p in scheduler.readers._processes.values()] if \\
                scheduler.readers._processes else []
            future = scheduler.surveyor.submit(os.getpid)
            future.result(timeout=60)
            scheduler.readers.submit(os.getpid).result(timeout=60)
            pids = list(scheduler.readers._processes) + list(scheduler.surveyor._processes)
            print(" ".join(map(str, pids)), flush=True)
            os._exit(1)                              # no clean-up at all
    ''')
    path = tmp_path / "dies.py"
    path.write_text(script)
    out = subprocess.run([sys.executable, str(path)], capture_output=True, text=True, timeout=120)
    pids = [int(p) for p in out.stdout.split()]
    assert pids, out.stderr[-500:]
    deadline = time.time() + 20
    def alive(pid):
        try:
            os.kill(pid, 0)
            return True
        except OSError:
            return False
    while any(alive(p) for p in pids) and time.time() < deadline:
        time.sleep(0.5)
    assert not any(alive(p) for p in pids), "a reader outlived the app"
