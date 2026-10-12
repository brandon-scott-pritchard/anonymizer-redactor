"""The web front end's API: auth, the wizard flow, and downloads."""

import time

import pytest

fastapi = pytest.importorskip("fastapi")
from fastapi.testclient import TestClient

from redactor import webapp


@pytest.fixture()
def client():
    # a fresh session per test - the module keeps one global session
    webapp.SESSION = webapp.Session()
    with TestClient(webapp.app) as test_client:
        test_client.cookies.set(webapp.COOKIE, webapp.TOKEN)
        yield test_client


def wait_for(client, job_id, timeout=60):
    deadline = time.time() + timeout
    while time.time() < deadline:
        state = client.get(f"/api/jobs/{job_id}").json()
        if state["status"] == "done":
            return state["result"]
        if state["status"] == "error":
            raise AssertionError(state["error"])
        time.sleep(0.05)
    raise AssertionError("job never finished")


def test_requests_without_the_token_are_refused():
    webapp.SESSION = webapp.Session()
    with TestClient(webapp.app) as bare:
        assert bare.get("/api/state").status_code == 401
        assert bare.get("/", follow_redirects=False).status_code == 401


def test_the_launcher_url_sets_the_cookie():
    webapp.SESSION = webapp.Session()
    with TestClient(webapp.app) as bare:
        response = bare.get(f"/?token={webapp.TOKEN}")
        assert response.status_code == 200
        assert webapp.COOKIE in response.cookies


def test_state_reports_the_essentials(client):
    state = client.get("/api/state").json()
    assert state["files"] == []
    assert any(c["key"] == "person" for c in state["categories"])
    assert state["error_types"][0].startswith("(no error")


def test_upload_review_edit_and_run(client, sample_docx):
    with open(sample_docx, "rb") as handle:
        uploaded = client.post("/api/files", files=[
            ("files", (sample_docx.name, handle,
                       "application/vnd.openxmlformats-officedocument.wordprocessingml.document")),
        ]).json()
    assert uploaded["added"] == 1

    names = [{"name": "Jane Elizabeth Smith", "category": "person"},
             {"name": "John Michael Smith", "category": "person"}]
    options = {"docx_mode": "anonymize", "ner": False, "ocr": False}

    review = wait_for(client, client.post(
        "/api/review", json={"options": options, "names": names}).json()["job"])
    assert review["entities"], "the prescan must find the sample's values"
    jane = next(e for e in review["entities"] if "Jane" in e["canonical"])
    assert jane["occurrences"] > 0

    # untick a row, then rescan - the decision must survive
    ssn = next(e for e in review["entities"] if e["category"] == "ssn")
    edited = client.patch("/api/entities",
                          json={"key": ssn["key"], "enabled": False}).json()
    assert not next(e for e in edited["entities"] if e["key"] == ssn["key"])["enabled"]
    review = wait_for(client, client.post(
        "/api/review", json={"options": options, "names": names}).json()["job"])
    again = next(e for e in review["entities"] if e["category"] == "ssn")
    assert not again["enabled"], "an untick must survive a rescan"

    # a digit string cannot become a person
    refused = client.patch("/api/entities",
                           json={"key": again["key"], "category": "person"})
    assert refused.status_code == 422

    result = wait_for(client, client.post(
        "/api/run", json={"options": options, "password": "correct horse"}).json()["job"])
    assert result["archive"] and result["key"] and result["report"]
    assert result["failed"] == 0

    archive = client.get("/api/download/archive")
    assert archive.status_code == 200
    assert archive.content[:2] == b"PK"
    assert client.get("/api/download/key").status_code == 200


def test_running_without_a_password_is_refused(client):
    assert client.post("/api/run",
                       json={"options": {}, "password": " "}).status_code == 422


def test_feedback_logs_and_builds_the_mailto(client, tmp_path, monkeypatch):
    from redactor import feedback as feedback_module

    logged = []
    monkeypatch.setattr(feedback_module, "log_report",
                        lambda report: logged.append(report) or tmp_path / "feedback.jsonl")
    response = client.post("/api/feedback", json={
        "error_type": "Person flagged as organization",
        "text": "JANE ELLEN SMITH",
        "predicted_category": "organization",
        "corrected_category": "person",
        "origin": "review",
    }).json()
    assert logged and logged[0]["corrected_category"] == "person"
    assert response["mailto"].startswith("mailto:bots@pritchard.works")


# ------------------------------------------------------ the app window --

def test_the_window_shell_serves_the_app_before_it_opens():
    """desktop.main starts the server and waits; the window never opens blank."""
    from redactor import desktop

    port = desktop._free_port()
    desktop._serve(port)
    assert desktop._wait_until_up(port), "the server never answered"

    import urllib.request
    request = urllib.request.Request(f"http://127.0.0.1:{port}/?token={desktop.TOKEN}")
    with urllib.request.urlopen(request, timeout=5) as response:
        body = response.read().decode()
    assert response.status == 200
    assert "Document Redactions & Anonymization" in body
    assert "/assets/js/app.js" in body


def test_the_window_falls_back_to_the_browser_without_a_web_view(monkeypatch):
    """A machine with no web view still gets the app, just in a browser."""
    import builtins

    from redactor import desktop

    real_import = builtins.__import__

    def no_webview(name, *args, **kwargs):
        if name == "webview":
            raise ImportError("no web view here")
        return real_import(name, *args, **kwargs)

    called = []
    monkeypatch.setattr(builtins, "__import__", no_webview)
    monkeypatch.setattr("redactor.webapp.main", lambda: called.append(1) or 0)
    assert desktop.main() == 0
    assert called, "it must fall back to the browser rather than exiting"


def test_the_app_window_is_allowed_to_save_files():
    """Every download button is a plain <a href>, which a web view may refuse.

    pywebview defaults ALLOW_DOWNLOADS to False. With it off the results screen
    looks completely normal and its three buttons do nothing - in the app
    window only, so it survives every browser-based test in this file.
    """
    webview = pytest.importorskip("webview")
    from redactor import desktop

    webview.settings["ALLOW_DOWNLOADS"] = False       # whatever the default is
    assert desktop.enable_downloads(webview) is True
    assert webview.settings["ALLOW_DOWNLOADS"] is True


def test_a_download_works_without_the_cookie(client, sample_docx):
    """The app window's download task does not reliably carry the cookie.

    It saved the 401 body under the archive's own filename, so the file looked
    like it downloaded and then opened as "unsupported format". Every other
    test here holds the cookie, which is exactly why none of them caught it.
    """
    with open(sample_docx, "rb") as handle:
        client.post("/api/files", files=[
            ("files", (sample_docx.name, handle,
                       "application/vnd.openxmlformats-officedocument.wordprocessingml.document")),
        ])
    options = {"docx_mode": "anonymize", "ner": False, "ocr": False}
    names = [{"name": "Jane Elizabeth Smith", "category": "person"}]
    wait_for(client, client.post(
        "/api/review", json={"options": options, "names": names}).json()["job"])
    wait_for(client, client.post(
        "/api/run", json={"options": options, "password": "correct horse"}).json()["job"])

    token = client.get("/api/state").json()["download_token"]
    client.cookies.clear()

    assert client.get("/api/download/archive").status_code == 401
    naked = client.get(f"/api/download/archive?token={token}")
    assert naked.status_code == 200
    assert naked.content[:4] == b"PK\x03\x04", "not a zip - this is the bug"

    # the query token opens nothing else
    assert client.get(f"/api/state?token={token}").status_code == 401


def test_the_review_list_groups_forms_and_splits_one_out(client):
    from redactor import grouping, review
    store, _ = review.build_store([("BRETT H. PRITCHARD", "person"), ("BRETT H", "person")])
    for text, count in (("123 N Main St", 3), ("123 North Main Street", 1)):
        store.add_value("street_address", text).occurrences = count
    grouping.group(store)
    webapp.SESSION.store = store
    payload = webapp._entities_payload()
    assert "WILL BE CHANGED" in payload["instructions"]
    street = next(r for r in payload["entities"] if r["canonical"] == "123 N Main St")
    assert [f["canonical"] for f in street["forms"]] == ["123 North Main Street"]
    assert street["occurrences"] == 4
    # unticking the group unticks the form; the count of active rows follows
    after = client.patch("/api/entities", json={"key": street["key"], "enabled": False}).json()
    head = next(r for r in after["entities"] if r["key"] == street["key"])
    assert not head["forms"][0]["enabled"]
    form_key = head["forms"][0]["key"]
    split = client.post("/api/entities/split", json={"key": form_key}).json()
    alone = next(r for r in split["entities"] if r["key"] == form_key)
    assert alone["parent"] is None and alone["replacement"] != head["replacement"]


def _docx_bytes(*lines):
    import io
    docx = pytest.importorskip("docx")
    document = docx.Document()
    for line in lines:
        document.add_paragraph(line)
    buffer = io.BytesIO()
    document.save(buffer)
    return buffer.getvalue()


DOCX_TYPE = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def test_a_dropped_folder_comes_back_as_the_same_tree(client):
    import io
    import zipfile
    body = _docx_bytes("Petitioner Kyle D Harrington")
    sent = client.post("/api/files", files=[
        ("files", ("Petition.docx", body, DOCX_TYPE)),
        ("files", ("Jan.docx", body, DOCX_TYPE)),
        ("files", ("Jan.docx", body, DOCX_TYPE)),
    ], data={"paths": ["Harrington Case/Pleadings/Petition.docx",
                       "Harrington Case/2024 Statements/Jan.docx",
                       "Harrington Case/2023 Statements/Jan.docx"],
             "skipped": ["Harrington Case/2024 Statements/check.jpg", "Harrington Case/.DS_Store"]}
    ).json()
    assert sent["added"] == 3
    assert [f["name"] for f in sent["files"]] == [
        "Harrington Case/Pleadings/Petition.docx", "Harrington Case/2024 Statements/Jan.docx",
        "Harrington Case/2023 Statements/Jan.docx"]
    assert sent["skipped"] == ["Harrington Case/2024 Statements/check.jpg"]
    options = {"docx_mode": "anonymize", "ner": False, "ocr": False}
    names = [{"name": "Kyle D Harrington", "category": "person"}]
    wait_for(client, client.post("/api/review", json={"options": options, "names": names}).json()["job"])
    result = wait_for(client, client.post("/api/run", json={"options": options, "password": "pw-123456"}).json()["job"])
    assert result["failed"] == 0 and result["skipped"] == 1
    with zipfile.ZipFile(io.BytesIO(client.get("/api/download/archive").content)) as archive:
        delivered = sorted(archive.namelist())
    assert len(delivered) == 3 and len({n.split("/")[0] for n in delivered}) == 1
    assert not any("harrington" in n.casefold() for n in delivered)
    assert [n.split("/", 1)[1] for n in delivered] == [
        "2023 Statements/Jan.docx", "2024 Statements/Jan.docx", "Pleadings/Petition.docx"]
    report = client.get("/api/download/report").text
    assert "Harrington Case/2024 Statements/check.jpg" in report


@pytest.mark.parametrize("path", ["../../escape.docx", "a/../../escape.docx"])
def test_a_path_that_climbs_out_lands_flat_in_the_upload_area(client, path):
    sent = client.post("/api/files", files=[("files", ("escape.docx", _docx_bytes("x"), DOCX_TYPE))],
                       data={"paths": [path]}).json()
    assert [f["name"] for f in sent["files"]] == ["escape.docx"]
    assert all(webapp.SESSION.uploads in p.parents for p in webapp.SESSION.files)


def test_remove_all_clears_files_and_the_left_out_list(client):
    client.post("/api/files", files=[("files", ("a.docx", _docx_bytes("x"), DOCX_TYPE))],
                data={"paths": ["Case/a.docx"], "skipped": ["Case/b.png"]})
    cleared = client.delete("/api/files").json()
    assert cleared == {"files": [], "skipped": []}


def test_the_app_windows_folder_button_reads_the_folder_from_disk(client, tmp_path):
    root = tmp_path / "Case"
    (root / "2024").mkdir(parents=True)
    (root / "2024" / "a.docx").write_bytes(_docx_bytes("x"))
    (root / "2024" / "photo.jpg").write_bytes(b"\xff\xd8")
    added = webapp.add_paths([root])
    assert added["added"] == 1
    assert [f["name"] for f in added["files"]] == ["Case/2024/a.docx"]
    assert added["skipped"] == ["Case/2024/photo.jpg"]
    # copied in: the original stays where it was, untouched
    assert (root / "2024" / "a.docx").exists()
    assert webapp.SESSION.uploads in webapp.SESSION.files[0].parents
