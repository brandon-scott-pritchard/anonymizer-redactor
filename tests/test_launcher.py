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
