"""The final check, and the Kev service started and stopped with the app.

A stand-in Kev - a few lines of HTTP server answering as Kev does - stands in
for the real model, so these run anywhere and fast.
"""

import json
import os
import subprocess
import sys
import textwrap
import time
import zipfile
from pathlib import Path

import pytest

pymupdf = pytest.importorskip("pymupdf")

from redactor import audit, review  # noqa: E402

FAKE_SERVE = textwrap.dedent('''
    """Answers as Kev does: yes when a glued or plain ticked surname is there."""
    import json, re, sys
    from http.server import BaseHTTPRequestHandler, HTTPServer

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def do_GET(self):
            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps({"models": [{"name": "kev-latest"}]}).encode())

        def do_POST(self):
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            text = body["state"].upper()
            answers = {}
            for key, q in body["questions"].items():
                names = q["instructions"].split("invented ones: ")[1].split(". Is any")[0]
                surnames = [n.split()[-1].upper() for n in names.split("; ")]
                answers[key] = {"type": "noul",
                                "noul": 0.9 if any(s in text for s in surnames) else 0.05}
            self.send_response(200); self.end_headers()
            self.wfile.write(json.dumps({"answers": answers}).encode())

    port = int(sys.argv[sys.argv.index("--port") + 1])
    HTTPServer(("127.0.0.1", port), Handler).serve_forever()
''')


@pytest.fixture
def fake_kev(tmp_path, monkeypatch):
    home = tmp_path / "kev"
    (home / "kev").mkdir(parents=True)
    (home / "kev" / "__init__.py").write_text("")
    (home / "kev" / "serve.py").write_text(FAKE_SERVE)
    bin_dir = home / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").symlink_to(sys.executable)
    monkeypatch.setenv("REDACTOR_KEV_DIR", str(home))
    monkeypatch.delenv("REDACTOR_KEV", raising=False)
    service = audit.KevService()
    yield service
    service.stop()


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


def test_kev_starts_offline_and_is_stopped_with_the_app(fake_kev):
    assert fake_kev.start()
    assert fake_kev.ready.wait(30), fake_kev.note
    assert fake_kev.url.startswith("http://127.0.0.1:")
    assert audit.ask_kev(fake_kev.url, "Paid to HARRINGTONENTERPRISES", ["Kyle D Harrington"]) > 0.5
    pid = fake_kev.process.pid
    fake_kev.stop()
    deadline = time.time() + 10
    while _alive(pid) and time.time() < deadline:
        time.sleep(0.2)
    assert not _alive(pid)


def test_kev_is_stopped_even_when_the_app_dies_without_closing(fake_kev, tmp_path):
    """An app that crashed must not leave a model holding ten gigabytes."""
    script = textwrap.dedent(f'''
        import os, sys, time
        sys.path.insert(0, {str(Path(__file__).resolve().parents[1])!r})
        from redactor import audit
        audit.KEV.start()
        audit.KEV.ready.wait(30)
        print(audit.KEV.process.pid, flush=True)
        os._exit(1)                                  # no atexit, no clean-up
    ''')
    out = subprocess.run([sys.executable, "-c", script], capture_output=True, text=True,
                         env=os.environ, timeout=60)
    pid = int(out.stdout.strip().splitlines()[-1])
    deadline = time.time() + 20
    while _alive(pid) and time.time() < deadline:
        time.sleep(0.5)
    assert not _alive(pid), "Kev outlived the app"


def test_without_kev_it_says_so_and_does_not_start(monkeypatch, tmp_path):
    monkeypatch.setenv("REDACTOR_KEV_DIR", str(tmp_path / "nowhere"))
    service = audit.KevService()
    assert not service.start() and "not installed" in service.note
    monkeypatch.setenv("REDACTOR_KEV", "0")
    assert not audit.KevService().start()


def _archive(tmp_path) -> Path:
    pages = {
        "clean.pdf": "Payment from Rory Vacek to [PERSON-2] of $120.00",
        "glued.pdf": "Deposit 4417IntuPymdHARRINGTONENTERPRISES 2,485.81",
        "plain.pdf": "Letter to Kyle Harrington about the house",
    }
    archive = tmp_path / "anonymized.zip"
    with zipfile.ZipFile(archive, "w") as zf:
        for name, line in pages.items():
            doc = pymupdf.open()
            doc.new_page().insert_text((72, 72), line, fontsize=11)
            zf.writestr(f"Case/{name}", doc.tobytes())
    return archive


def test_the_final_check_flags_what_either_reader_sees(fake_kev, tmp_path):
    assert fake_kev.start() and fake_kev.ready.wait(30)
    store, _ = review.build_store([("Kyle D Harrington", "person")])
    out = tmp_path / "results"
    out.mkdir()
    check = audit.check_archive(_archive(tmp_path), store, None, out, fake_kev.url)
    flagged = {(f.file, f.page) for f in check.flags}
    # the glued name only Kev sees; the plain one the matcher sees too;
    # the clean page, invented names and all, is left alone
    assert flagged == {("Case/glued.pdf", 1), ("Case/plain.pdf", 1)}
    glued = next(f for f in check.flags if f.file == "Case/glued.pdf")
    assert glued.kev and glued.kev >= audit.KEV_THRESHOLD and not glued.matched
    written = (out / "final-check.txt").read_text()
    assert "2 page(s) to look at" in written and "Case/glued.pdf, page 1" in written
    assert check.summary()["flagged"] == 2


def test_without_kev_the_matcher_still_checks_and_the_report_says_so(tmp_path):
    store, _ = review.build_store([("Kyle D Harrington", "person")])
    out = tmp_path / "results"
    out.mkdir()
    check = audit.check_archive(_archive(tmp_path), store, None, out, None)
    assert {(f.file, f.page) for f in check.flags} == {("Case/plain.pdf", 1)}
    assert "Kev was not running" in (out / "final-check.txt").read_text()
