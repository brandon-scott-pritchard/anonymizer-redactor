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
