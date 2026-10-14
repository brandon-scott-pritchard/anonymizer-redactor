"""The frozen app's entry point, run as plain Python."""

import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _launcher():
    spec = importlib.util.spec_from_file_location("launcher", ROOT / "build" / "launcher.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_the_selftest_report_never_goes_inside_the_mac_bundle(monkeypatch):
    """A file written into Contents/MacOS broke the app's code seal."""
    launcher = _launcher()
    monkeypatch.setattr(sys, "platform", "darwin")
    targets = launcher._selftest_targets()
    assert targets == [Path.home() / "Library" / "Logs" / "Anonymizer-Redactor" / "selftest.txt"]
    assert not any(Path(sys.executable).resolve().parent in t.parents for t in targets)


def test_on_windows_it_goes_beside_the_exe_then_to_local_app_data(monkeypatch, tmp_path):
    launcher = _launcher()
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setenv("LOCALAPPDATA", str(tmp_path))
    targets = launcher._selftest_targets()
    assert targets[0].parent == Path(sys.executable).resolve().parent
    assert targets[1] == tmp_path / "Anonymizer-Redactor" / "selftest.txt"


def test_the_self_test_reads_a_page_in_the_worker_processes(monkeypatch):
    """A frozen build that cannot start its workers quietly reads one page at
    a time; the self-test is where that has to show."""
    import pytest

    from redactor import ocr, ocr_pool
    if not ocr.usable_engines():
        pytest.skip("no OCR engine")
    launcher = _launcher()
    monkeypatch.setenv("REDACTOR_OCR_WORKERS", "2")
    for name, value in (("_pool", None), ("_broken", False), ("_pending", {}), ("_surveys", {})):
        monkeypatch.setattr(ocr_pool, name, value)
    ok, note = launcher._ocr_workers()
    assert ok, note
    assert note.startswith("2 worker(s)")
