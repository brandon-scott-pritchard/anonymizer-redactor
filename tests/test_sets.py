"""Several sets in one window: each read, reviewed and queued, run unattended."""

import io
import time
import zipfile

import pytest

fastapi = pytest.importorskip("fastapi")
pymupdf = pytest.importorskip("pymupdf")
from fastapi.testclient import TestClient  # noqa: E402

from redactor import ocr, ocr_pool, pdf_processor, sets, webapp  # noqa: E402


@pytest.fixture()
def client(monkeypatch):
    monkeypatch.setenv("REDACTOR_OCR_WORKERS", "2")
    ocr_pool.shutdown()
    webapp.SESSION = webapp.Session()
    with TestClient(webapp.app) as test_client:
        test_client.cookies.set(webapp.COOKIE, webapp.TOKEN)
        yield test_client
    ocr_pool.shutdown()


def _scan(lines) -> bytes:
    """A one-page scan: a picture of the lines, no text layer."""
    text = pymupdf.open()
    page = text.new_page()
    for number, line in enumerate(lines):
        page.insert_text((72, 100 + 30 * number), line, fontsize=16)
    scan = pymupdf.open()
    scan.new_page().insert_image(pymupdf.Rect(0, 0, 595, 842),
                                 stream=io.BytesIO(page.get_pixmap(dpi=150).tobytes("png")))
    return scan.tobytes()


def _typed(line) -> bytes:
    doc = pymupdf.open()
    doc.new_page().insert_text((72, 72), line, fontsize=12)
    return doc.tobytes()


def _wait(client, test, timeout=180):
    deadline = time.time() + timeout
    while time.time() < deadline:
        payload = client.get("/api/sets").json()
        if test(payload):
            return payload
        time.sleep(0.5)
    raise AssertionError(f"timed out: {payload}")


def _job(client, response, set_id):
    job_id = response.json()["job"]
    deadline = time.time() + 180
    while time.time() < deadline:
        state = client.get(f"/api/jobs/{job_id}", headers={"X-Set": set_id}).json()
        if state["status"] == "done":
            return state["result"]
        if state["status"] == "error":
            raise AssertionError(state["error"])
        time.sleep(0.2)
    raise AssertionError("job never finished")


def test_two_sets_are_read_reviewed_and_run_unattended(client, tmp_path):
    if not ocr.usable_engines():
        pytest.skip("no OCR engine")
    first = client.get("/api/sets").json()["sets"][0]["id"]
    second = client.post("/api/sets", json={"name": "Second / case"}).json()["selected"]
    client.patch(f"/api/sets/{first}", json={"name": "First case"})
    cases = {first: ("Kyle D Harrington", "4417220193"), second: ("Rosa M Valdez", "8812004471")}
    for set_id, (name, account) in cases.items():
        client.post("/api/files", headers={"X-Set": set_id}, files=[
            ("files", ("scan.pdf", _scan([f"Account holder {name}", f"Account {account}"]),
                       "application/pdf")),
            ("files", ("letter.pdf", _typed(f"Dear {name}, about account {account}."),
                       "application/pdf"))])

    # read in the background, each set counting its own scanned pages
    ready = _wait(client, lambda p: all(s["state"] == "ready" for s in p["sets"]))
    assert all(s["reading"] == {"done": 1, "total": 1} for s in ready["sets"])

    root = tmp_path / "Results"
    root.mkdir()
    refused = client.post("/api/queue", headers={"X-Set": first},
                          json={"options": {"ner": False}, "password": "pw-1"})
    assert refused.status_code == 422                     # no results folder yet
    client.post("/api/results-folder", json={"path": str(root)})

    for set_id, (name, _account) in cases.items():
        body = {"names": [{"name": name, "category": "person"}], "options": {"ner": False}}
        result = _job(client, client.post("/api/review", headers={"X-Set": set_id}, json=body),
                      set_id)
        assert any(row["canonical"] == name for row in result["entities"])
        queued = client.post("/api/queue", headers={"X-Set": set_id},
                             json={"options": {"ner": False, "pdf_mode": "redact"},
                                   "password": f"pw-{set_id}"})
        assert queued.status_code == 200, queued.text

    done = _wait(client, lambda p: all(s["state"] in ("done", "failed") for s in p["sets"]),
                 timeout=300)
    for info in done["sets"]:
        assert info["state"] == "done", info
        assert info["run"]["failed"] == 0 and info["run"]["files"] == 2
    folders = sorted(p.name for p in root.iterdir())
    assert folders == ["First case", "Second case"]      # the slash is not a folder
    for set_id, (name, account) in cases.items():
        doc_set = webapp.SESSION.sets[set_id]
        produced = {p.suffix for p in doc_set.results.iterdir()}
        assert {".zip", ".json", ".txt"} <= produced
        archive = next(doc_set.results.glob("*.zip"))
        with zipfile.ZipFile(archive) as zf:
            for member in zf.namelist():
                with pymupdf.open(stream=zf.read(member), filetype="pdf") as out:
                    assert account not in "".join(page.get_text() for page in out)
        # the set's copies of the documents, and what was read off them, are gone
        assert not doc_set.uploads.exists()
        names = [engine.name for engine in ocr.usable_engines()]
        for path in doc_set.files:
            base = pdf_processor._ocr_key(path, 0)
            assert base is None or not pdf_processor.ocr_cached(base, names)
        # and its results can still be downloaded
        got = client.get(f"/api/download/archive?set={set_id}")
        assert got.status_code == 200 and got.content == archive.read_bytes()


def test_a_waiting_set_can_be_taken_out_of_the_queue(tmp_path):
    started = []
    queue = sets.RunQueue(tmp_path)
    queue._run = lambda *args: started.append(args) or time.sleep(1.5)
    first = sets.DocSet(id="a", name="A", rank=1, folder=tmp_path / "a")
    second = sets.DocSet(id="b", name="B", rank=2, folder=tmp_path / "b")
    queue.add(first, None, "pw", tmp_path)
    queue.add(second, None, "pw", tmp_path)
    time.sleep(0.3)
    assert queue.cancel(second) and second.run_state == ""
    assert not queue.cancel(first)                       # already running
    time.sleep(1.6)
    assert [args[0].id for args in started] == ["a"]


def test_set_folders_are_safe_and_never_overwrite(tmp_path):
    assert sets.folder_name('Harrington v. Harrington: 2024/03 "draft"') == \
        "Harrington v. Harrington 2024 03 draft"
    (tmp_path / "A").mkdir()
    assert sets._unique_folder(tmp_path, "A").name == "A (2)"


def test_the_sets_are_read_in_the_order_they_were_made(client):
    first = webapp.SESSION.current
    second = webapp.SESSION.new_set()
    assert (first.rank, second.rank) == (1, 2)
    assert ocr_pool._ahead(first.rank, 9) < ocr_pool._ahead(second.rank, 0)
    assert ocr_pool.NEEDED_NOW < ocr_pool._ahead(first.rank, 0)
