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
        # every delivered page read back for the ticked names, and none flagged
        assert (doc_set.results / "final-check.txt").exists()
        audit = next(s for s in done["sets"] if s["id"] == set_id)["run"]["audit"]
        assert audit["pages"] == 2 and audit["flagged"] == 0, audit
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


def test_a_read_set_has_its_name_screen_worked_out_ahead(client, monkeypatch):
    """Eleven sets read overnight still had minutes each of captions, bench and
    suggestions to work out at the keyboard; worked out ahead, the name screen
    opens at once - and says what a fresh scan would."""
    if not ocr.usable_engines():
        pytest.skip("no OCR engine")
    monkeypatch.setattr(sets.Preparer, "SETTLE", 1.0)
    set_id = client.get("/api/sets").json()["sets"][0]["id"]
    letter = ("IN THE THIRD JUDICIAL DISTRICT COURT\nJANE ELIZABETH SMITH, Petitioner,\n"
              "v.\nJOHN MICHAEL SMITH, Respondent.\nCase No. 224900871\n"
              "Judge Amber M. Cordova\nPaid to Tanner Jensen on 03/02/2024 $120.00")
    doc = pymupdf.open()
    page = doc.new_page()
    for number, line in enumerate(letter.splitlines()):
        page.insert_text((72, 72 + 18 * number), line, fontsize=11)
    client.post("/api/files", headers={"X-Set": set_id},
                files=[("files", ("petition.pdf", doc.tobytes(), "application/pdf")),
                       ("files", ("scan.pdf", _scan(["Account holder Jane Smith"]),
                                  "application/pdf"))])
    states = set()
    _wait(client, lambda p: states.add(p["sets"][0]["state"]) or p["sets"][0]["state"] == "ready")
    assert "preparing" in states
    doc_set = webapp.SESSION.sets[set_id]
    assert doc_set.ready_prepared() is not None

    captions = client.post("/api/captions", headers={"X-Set": set_id})
    result = _job(client, captions, set_id)
    state = client.get(f"/api/jobs/{captions.json()['job']}", headers={"X-Set": set_id}).json()
    assert state["pages_total"] == 0                    # nothing left to read
    assert any("SMITH" in c["name"].upper() for c in result["captions"])
    body = {"names": [{"name": "Jane Elizabeth Smith", "category": "person"}],
            "options": {}}
    prepared = _job(client, client.post("/api/suggestions", headers={"X-Set": set_id},
                                        json=body), set_id)
    # the same request with nothing prepared: a fresh scan
    doc_set.prepared = None
    fresh = _job(client, client.post("/api/suggestions", headers={"X-Set": set_id},
                                     json=body), set_id)
    assert sorted(s["text"] for s in prepared["suggestions"]) == \
        sorted(s["text"] for s in fresh["suggestions"])
    assert "Jane Elizabeth Smith" not in {s["text"] for s in prepared["suggestions"]}


def test_a_set_given_more_files_is_prepared_again(tmp_path):
    doc_set = sets.DocSet(id="a", name="A", rank=1, folder=tmp_path / "a")
    doc_set.prepare_wanted = True
    doc_set.files = [tmp_path / "one.pdf"]
    doc_set.prepared, doc_set.prepared_for = {"x": 1}, doc_set._file_set()
    assert doc_set.ready_prepared() is not None and not doc_set.should_prepare(0)
    doc_set.files.append(tmp_path / "two.pdf")
    assert doc_set.ready_prepared() is None
    doc_set.last_added = time.time()
    assert not doc_set.should_prepare(10)              # still uploading: wait
    assert doc_set.should_prepare(0)
    assert doc_set.summary()["state"] == "preparing"


# --------------------------------------------------------------------------
# a set is never handed another set's work
# --------------------------------------------------------------------------


@pytest.fixture()
def quiet(monkeypatch):
    """The window's server with nothing read ahead: the steps alone."""
    monkeypatch.setenv("REDACTOR_OCR_WORKERS", "0")
    webapp.SESSION = webapp.Session()
    with TestClient(webapp.app) as test_client:
        test_client.cookies.set(webapp.COOKIE, webapp.TOKEN)
        yield test_client


def test_a_set_cannot_be_removed_while_its_step_works(quiet, monkeypatch):
    from redactor import pipeline

    first = quiet.get("/api/sets").json()["sets"][0]["id"]
    quiet.post("/api/files", headers={"X-Set": first},
               files=[("files", ("a.pdf", _typed("Kyle D Harrington"), "application/pdf"))])
    second = quiet.post("/api/sets", json={"name": "B"}).json()["selected"]
    real = pipeline.prescan
    monkeypatch.setattr(pipeline, "prescan",
                        lambda *a, **k: (time.sleep(1.0), real(*a, **k))[1])
    body = {"names": [{"name": "Kyle D Harrington", "category": "person"}],
            "options": {"ner": False}}
    started = quiet.post("/api/review", headers={"X-Set": first}, json=body)
    refused = quiet.delete(f"/api/sets/{first}")
    assert refused.status_code == 409
    _job(quiet, started, first)
    assert not webapp.SESSION.sets[second].store.entities     # B never got A's names
    assert quiet.delete(f"/api/sets/{first}").status_code == 200


def test_a_removed_set_is_refused_not_swapped_for_the_selected_one(quiet):
    first = quiet.get("/api/sets").json()["sets"][0]["id"]
    quiet.post("/api/files", headers={"X-Set": first},
               files=[("files", ("a.pdf", _typed("Kyle D Harrington"), "application/pdf"))])
    gone = quiet.post("/api/sets", json={"name": "Gone"}).json()["selected"]
    quiet.post(f"/api/sets/{first}/select")
    assert quiet.delete(f"/api/sets/{gone}").status_code == 200
    stale = quiet.delete("/api/files/0", headers={"X-Set": gone})
    assert stale.status_code == 404
    assert len(webapp.SESSION.sets[first].files) == 1          # the selected set's file stays
    # a job's thread still naming the removed set cannot write anywhere
    marker = webapp._current_set.set(gone)
    try:
        with pytest.raises(webapp.HTTPException):
            webapp.SESSION.store = None
    finally:
        webapp._current_set.reset(marker)


def test_a_running_or_finished_set_is_not_changed_under_it(quiet):
    set_id = quiet.get("/api/sets").json()["sets"][0]["id"]
    head = {"X-Set": set_id}
    quiet.post("/api/files", headers=head,
               files=[("files", ("a.pdf", _typed("Kyle D Harrington"), "application/pdf"))])
    doc_set = webapp.SESSION.sets[set_id]
    for state in ("running", "done"):
        doc_set.run_state = state
        assert quiet.post("/api/files", headers=head, files=[
            ("files", ("b.pdf", _typed("x"), "application/pdf"))]).status_code == 409
        assert quiet.delete("/api/files/0", headers=head).status_code == 409
        assert quiet.delete("/api/files", headers=head).status_code == 409
        assert quiet.patch("/api/entities", headers=head,
                           json={"key": "x", "enabled": False}).status_code == 409
        assert quiet.post("/api/entities/all", headers=head,
                          json={"enabled": False}).status_code == 409
        assert quiet.post("/api/review", headers=head, json={"names": []}).status_code == 409
    assert len(doc_set.files) == 1
    doc_set.run_state = "queued"                 # the run takes it as it is when it starts
    assert quiet.delete("/api/files/0", headers=head).status_code == 200


def test_the_results_folder_must_be_one_that_can_be_written(quiet, tmp_path):
    locked = tmp_path / "Locked"
    locked.mkdir()
    locked.chmod(0o500)
    try:
        refused = quiet.post("/api/results-folder", json={"path": str(locked)})
        assert refused.status_code == 422 and "Cannot write" in refused.json()["detail"]
    finally:
        locked.chmod(0o700)
    for given in ("", ".", "Results"):
        assert quiet.post("/api/results-folder", json={"path": given}).status_code == 422
    assert quiet.post("/api/results-folder", json={"path": str(tmp_path)}).status_code == 200
    assert list(tmp_path.iterdir()) == [locked]               # the probe left nothing


def test_runs_stop_cleanly_where_there_are_no_process_groups(monkeypatch):
    """Windows: no setpgrp, no killpg, no SIGKILL - a queued run still runs."""
    import os
    import signal

    for module, name in ((os, "setpgrp"), (os, "killpg"), (signal, "SIGKILL")):
        monkeypatch.delattr(module, name, raising=False)
    sets._own_group()

    class Child:
        pid, alive, stopped = 4242, True, False

        def join(self, timeout=None):
            pass

        def is_alive(self):
            return self.alive

        def terminate(self):
            self.alive, self.stopped = False, True

    child = Child()
    sets._stop(child)
    assert child.stopped


def test_the_last_word_of_a_child_that_has_just_exited_is_still_heard():
    """A finished run was marked failed when its "done" came in just after
    the wait for it gave up and just before the child was seen gone."""
    import queue as queue_module

    class Late:
        def __init__(self):
            self.calls = 0

        def get(self, timeout=None):
            self.calls += 1
            if self.calls == 1:
                raise queue_module.Empty
            return ("done", "result")

    class Gone:
        def is_alive(self):
            return False

    assert sets._next_event(Late(), Gone()) == ("done", "result")

    class Silent:
        def get(self, timeout=None):
            raise queue_module.Empty

    assert sets._next_event(Silent(), Gone()) is None


def test_a_file_replaced_under_the_same_name_is_prepared_again(tmp_path):
    doc_set = sets.DocSet(id="a", name="A", rank=1, folder=tmp_path / "a")
    path = tmp_path / "statement.pdf"
    path.write_bytes(b"first")
    doc_set.files = [path]
    doc_set.prepared, doc_set.prepared_for = {"x": 1}, doc_set._file_set()
    assert doc_set.ready_prepared() is not None
    path.write_bytes(b"a different statement")
    assert doc_set.ready_prepared() is None


def test_quitting_mid_run_stops_it_and_says_so_in_its_folder(tmp_path):
    queue = sets.RunQueue(tmp_path)
    doc_set = sets.DocSet(id="a", name="A", rank=1, folder=tmp_path / "a")
    doc_set.results = tmp_path / "Results" / "A"
    (doc_set.results / "_work-1" / "Case").mkdir(parents=True)
    (doc_set.results / "_work-1" / "Case" / "half.pdf").write_bytes(b"half")
    doc_set.run_state = "running"

    class Child:
        pid, alive = 999999, True

        def is_alive(self):
            return self.alive

        def join(self, timeout=None):
            self.alive = False

    queue.running, queue.child = doc_set, Child()
    queue.stop_now()
    assert not queue.child.alive
    left = sorted(p.name for p in doc_set.results.iterdir())
    assert left == [sets.RunQueue.INTERRUPTED]
    later = sets.DocSet(id="b", name="B", rank=2, folder=tmp_path / "b")
    queue.add(later, None, "pw", tmp_path)
    time.sleep(0.5)
    assert later.run_state == "queued"                   # nothing more starts


def test_a_set_queued_while_still_reading_waits_for_it(tmp_path, monkeypatch):
    from redactor import audit

    queue = sets.RunQueue(tmp_path)
    queue.READ_STALL = 1.5
    doc_set = sets.DocSet(id="a", name="A", rank=1, folder=tmp_path / "a")
    doc_set.scans = {"/x.pdf": 3}
    messages = []

    class Started(Exception):
        pass

    def kev(seconds):
        messages.append(doc_set.run_message)
        raise Started

    monkeypatch.setattr(audit, "wait_for_kev", kev)
    began = time.time()
    with pytest.raises(Started):
        queue._run(doc_set, None, "pw", tmp_path)
    assert time.time() - began >= 1.5                    # waited, then went on
    assert messages == ["Starting"]


# --------------------------------------------------------------------------
# the window's stages: one set of options, every set taken through together
# --------------------------------------------------------------------------


def test_the_options_are_set_once_for_every_set(quiet, tmp_path, monkeypatch):
    """Chosen once on the Documents screen, they are what every set's name
    lists are worked out with and what every queued run is given."""
    assert quiet.get("/api/state").json()["options"] == webapp.DEFAULT_OPTIONS
    saved = quiet.put("/api/options", json={"pdf_mode": "anonymize", "ner": 0,
                                            "allowlist": "Zions Bank", "typo": True}).json()
    assert saved["pdf_mode"] == "anonymize" and saved["ner"] is False
    assert saved["allowlist"] == "Zions Bank" and "typo" not in saved
    assert quiet.get("/api/options").json() == saved
    refused = quiet.put("/api/options", json={"ner": True, "docx_mode": "shred"})
    assert refused.status_code == 422
    assert quiet.get("/api/options").json() == saved           # nothing half applied

    # the name lists worked out ahead use them
    webapp.SESSION.run_queue()
    prepared_with = webapp.SESSION.preparer.settings_for(webapp.SESSION.current)
    assert prepared_with.pdf_mode == "anonymize" and prepared_with.use_ner is False

    # and a set queued without options of its own runs with them
    queued = []
    monkeypatch.setattr(sets.RunQueue, "add",
                        lambda self, doc_set, settings, password, root:
                        queued.append((doc_set.id, settings, password)))
    quiet.post("/api/results-folder", json={"path": str(tmp_path)})
    for name in ("Kyle D Harrington", "Rosa M Valdez"):
        set_id = quiet.post("/api/sets", json={"name": name}).json()["selected"]
        head = {"X-Set": set_id}
        quiet.post("/api/files", headers=head,
                   files=[("files", ("a.pdf", _typed(f"Dear {name}"), "application/pdf"))])
        body = {"names": [{"name": name, "category": "person"}]}
        _job(quiet, quiet.post("/api/review", headers=head, json=body), set_id)
        assert quiet.post("/api/queue", headers=head,
                          json={"password": f"pw-{set_id}"}).status_code == 200
    assert len(queued) == 2
    for set_id, settings, password in queued:
        assert settings.pdf_mode == "anonymize" and settings.use_ner is False
        assert "Zions Bank" in settings.extra_allowlist and password == f"pw-{set_id}"


def test_each_sets_documents_are_listed_for_a_window_opened_again(quiet):
    first = quiet.get("/api/sets").json()["sets"][0]["id"]
    second = quiet.post("/api/sets", json={"name": "B"}).json()["selected"]
    quiet.post("/api/files", headers={"X-Set": first},
               files=[("files", ("a.pdf", _typed("Kyle D Harrington"), "application/pdf"))],
               data={"skipped": ["Case/ledger.xlsx"]})
    assert quiet.get("/api/files", headers={"X-Set": first}).json() == {
        "files": [{"name": "a.pdf", "kind": "pdf"}], "skipped": ["Case/ledger.xlsx"]}
    assert quiet.get("/api/files", headers={"X-Set": second}).json() == \
        {"files": [], "skipped": []}


def test_a_cards_folder_button_fills_that_set_not_the_newest(quiet, tmp_path):
    """Every set's card has its own Choose folder button: the app window's
    folder panel names the set, and the folder goes to it."""
    case = tmp_path / "Case"
    case.mkdir()
    (case / "a.pdf").write_bytes(_typed("Kyle D Harrington"))
    first = quiet.get("/api/sets").json()["sets"][0]["id"]
    newest = quiet.post("/api/sets", json={"name": "Newest"}).json()["selected"]
    added = webapp.add_paths([case], first)
    assert [f["name"] for f in added["files"]] == ["Case/a.pdf"]
    assert len(webapp.SESSION.sets[first].files) == 1
    assert not webapp.SESSION.sets[newest].files
    quiet.delete(f"/api/sets/{first}")
    with pytest.raises(webapp.HTTPException) as refused:
        webapp.add_paths([case], first)
    assert refused.value.status_code == 404
    assert not webapp.SESSION.sets[newest].files
