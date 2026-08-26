"""OCR back ends, and finding the one that travels with the app.

Two engines, tried in order:

1.  **Tesseract** - preferred. A copy is vendored into ``vendor/tesseract`` and
    bundled into the frozen app, so it works on a machine that has never seen
    Homebrew. A system install on PATH is used if the bundled copy is missing.
2.  **RapidOCR** - fallback. An ordinary pip package with its ONNX models
    inside, so it is always present wherever the Python environment is, with no
    binary to locate and no architecture to match.

If neither is usable the caller refuses image-only pages rather than passing
them through looking redacted.
"""

from __future__ import annotations

import hashlib
import os
import platform
import shutil
import sys
import tarfile
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class OcrWord:
    """One recognised word, in image pixel coordinates."""

    x0: float
    y0: float
    x1: float
    y1: float
    text: str
    block: int = 0
    line: int = 0
    word: int = 0


# --------------------------------------------------------------------------
# locating the bundled Tesseract
# --------------------------------------------------------------------------


def _platform_folder() -> str:
    """Platform *and* architecture.

    An Intel Mac and an Apple Silicon Mac need different binaries, so the
    architecture is part of the identity - shipping one to the other produces a
    file that simply will not execute.
    """
    machine = platform.machine().lower()
    if machine in {"amd64", "x64"}:
        machine = "x86_64"
    elif machine in {"aarch64"}:
        machine = "arm64"
    if sys.platform == "darwin":
        return f"macos-{machine}"
    if sys.platform.startswith("win"):
        return f"windows-{machine}"
    return f"linux-{machine}"


def _vendor_roots() -> list[Path]:
    """Places the vendored copy might live, frozen or from source."""
    roots: list[Path] = []
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen:
        roots.append(Path(frozen) / "vendor" / "tesseract" / _platform_folder())
    package_root = Path(__file__).resolve().parent.parent
    roots.append(package_root / "vendor" / "tesseract" / _platform_folder())
    # inside a macOS .app the data lands beside the executable
    roots.append(Path(sys.executable).resolve().parent / "vendor" / "tesseract" / _platform_folder())
    return roots


def _binary_name() -> str:
    return "tesseract.exe" if sys.platform.startswith("win") else "tesseract"


def _usable(root: Path) -> tuple[Path, Path] | None:
    binary = root / "bin" / _binary_name()
    tessdata = root / "tessdata"
    if binary.exists() and tessdata.is_dir():
        return binary, tessdata
    return None


def cache_dir() -> Path:
    """Where the unpacked copy lives, per user."""
    if sys.platform == "darwin":
        base = Path.home() / "Library" / "Application Support"
    elif sys.platform.startswith("win"):
        base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData" / "Local"))
    else:
        base = Path(os.environ.get("XDG_CACHE_HOME", Path.home() / ".cache"))
    return base / "Anonymizer-Redactor" / "ocr"


def _bundled_archive() -> Path | None:
    """The vendored tarball, wherever this is running from."""
    name = f"tesseract-{_platform_folder()}.tar.gz"
    candidates = []
    frozen = getattr(sys, "_MEIPASS", None)
    if frozen:
        candidates.append(Path(frozen) / "vendor" / name)
    package_root = Path(__file__).resolve().parent.parent
    candidates.append(package_root / "vendor" / name)
    candidates.append(Path(sys.executable).resolve().parent / "vendor" / name)
    return next((c for c in candidates if c.is_file()), None)


def _stamp_for(archive: Path) -> str:
    digest = hashlib.sha256()
    with archive.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()[:16]


def _unpack(archive: Path) -> tuple[Path, Path] | None:
    """Extract the vendored copy once, into the per-user cache.

    The tarball exists because PyInstaller rewrites and deduplicates any Mach-O
    file it is handed, which swapped the vendored libraries for a different
    build. Unpacking at runtime keeps the vendored set exactly intact.
    """
    target = cache_dir() / _platform_folder()
    stamp_file = target.parent / f"{_platform_folder()}.stamp"
    stamp = _stamp_for(archive)

    existing = _usable(target)
    if existing and stamp_file.is_file():
        try:
            if stamp_file.read_text(encoding="utf-8").strip() == stamp:
                return existing
        except OSError:
            pass

    if target.exists():
        shutil.rmtree(target, ignore_errors=True)
    target.parent.mkdir(parents=True, exist_ok=True)

    staging = target.parent / f".{_platform_folder()}-unpacking"
    if staging.exists():
        shutil.rmtree(staging, ignore_errors=True)
    staging.mkdir(parents=True)
    try:
        with tarfile.open(archive, "r:gz") as tar:
            _safe_extract(tar, staging)
        unpacked = staging / "tesseract"
        if not _usable(unpacked):
            return None
        unpacked.rename(target)
    except (OSError, tarfile.TarError):
        return None
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    result = _usable(target)
    if result is None:
        return None

    binary = result[0]
    binary.chmod(0o755)
    for lib in (target / "lib").glob("*") if (target / "lib").is_dir() else []:
        try:
            lib.chmod(0o755)
        except OSError:
            pass
    try:
        stamp_file.write_text(stamp, encoding="utf-8")
    except OSError:
        pass
    return result


def _safe_extract(tar: "tarfile.TarFile", destination: Path) -> None:
    """Extract, refusing any member that would escape the destination.

    The stdlib "data" filter rejects absolute paths, parent traversal, and
    symlink tricks a lexical prefix check misses (a "<root>-evil" sibling, or
    writing through a symlink member extracted a moment earlier).
    """
    try:
        tar.extractall(destination, filter="data")
    except TypeError:                # pragma: no cover - Python < 3.12
        import os
        root = destination.resolve()
        for member in tar.getmembers():
            if member.issym() or member.islnk():
                raise tarfile.TarError(f"link member in archive: {member.name}")
            target = (root / member.name).resolve()
            if os.path.commonpath([str(root), str(target)]) != str(root):
                raise tarfile.TarError(f"unsafe path in archive: {member.name}")
        tar.extractall(destination)


def bundled_tesseract() -> tuple[Path, Path] | None:
    """(binary, tessdata) for the vendored copy, unpacking it if necessary."""
    for root in _vendor_roots():
        found = _usable(root)
        if found:
            return found
    archive = _bundled_archive()
    if archive is not None:
        return _unpack(archive)
    return None


# --------------------------------------------------------------------------
# engines
# --------------------------------------------------------------------------


class TesseractEngine:
    name = "Tesseract"

    def __init__(self) -> None:
        self._configured = False
        self._description = ""

    def available(self) -> tuple[bool, str]:
        # Same lock the model loader uses. The web front end answers /state
        # from a request thread and lands here, while a run thread may be
        # importing spaCy; two background threads importing packages this
        # large at once is a shape worth not having.
        from .ner import IMPORT_LOCK
        with IMPORT_LOCK:
            return self._available_locked()

    def _available_locked(self) -> tuple[bool, str]:
        try:
            import pytesseract
        except Exception as exc:                    # pragma: no cover - env dependent
            return False, f"pytesseract is not installed ({exc})"

        bundled = bundled_tesseract()
        if bundled:
            binary, tessdata = bundled
            pytesseract.pytesseract.tesseract_cmd = str(binary)
            os.environ["TESSDATA_PREFIX"] = str(tessdata)
            origin = "bundled"
        else:
            found = shutil.which("tesseract")
            if not found:
                return False, "no bundled copy and nothing named tesseract on PATH"
            pytesseract.pytesseract.tesseract_cmd = found
            origin = "system"

        try:
            version = pytesseract.get_tesseract_version()
        except Exception as exc:                    # pragma: no cover - env dependent
            return False, f"the Tesseract binary would not run ({exc})"

        self._configured = True
        self._description = f"Tesseract {version} ({origin})"
        return True, self._description

    def read(self, image) -> list[OcrWord]:
        import pytesseract

        if not self._configured:
            self.available()
        data = pytesseract.image_to_data(image, output_type=pytesseract.Output.DICT)
        words: list[OcrWord] = []
        for i, raw in enumerate(data["text"]):
            text = (raw or "").strip()
            if not text:
                continue
            try:
                confidence = float(data["conf"][i])
            except (TypeError, ValueError):
                confidence = -1.0
            if confidence < 0:
                continue
            left, top = data["left"][i], data["top"][i]
            width, height = data["width"][i], data["height"][i]
            words.append(OcrWord(left, top, left + width, top + height, text,
                                 data["block_num"][i], data["line_num"][i],
                                 data["word_num"][i]))
        return words


class RapidOcrEngine:
    """ONNX-based fallback. Detects whole lines, which are split into words.

    Word boxes are apportioned across the line by character offset. That is an
    approximation, so the boxes are padded generously by the caller - a box a
    little too wide costs nothing, a box too narrow leaves text on the page.
    """

    name = "RapidOCR"

    def __init__(self) -> None:
        self._engine = None

    def available(self) -> tuple[bool, str]:
        try:
            from rapidocr_onnxruntime import RapidOCR
        except Exception:
            try:
                from rapidocr import RapidOCR       # newer package name
            except Exception as exc:                # pragma: no cover - env dependent
                return False, f"RapidOCR is not installed ({exc})"
        try:
            if self._engine is None:
                self._engine = RapidOCR()
        except Exception as exc:                    # pragma: no cover - env dependent
            return False, f"RapidOCR would not start ({exc})"
        return True, "RapidOCR (bundled models)"

    def read(self, image) -> list[OcrWord]:
        import numpy as np

        if self._engine is None:
            ok, _note = self.available()
            if not ok:
                return []

        result = self._engine(np.array(image))
        detections = result[0] if isinstance(result, tuple) else result
        if not detections:
            return []

        words: list[OcrWord] = []
        for line_index, detection in enumerate(detections):
            try:
                box, text = detection[0], detection[1]
            except (TypeError, IndexError):
                continue
            text = (text or "").strip()
            if not text:
                continue
            xs = [float(point[0]) for point in box]
            ys = [float(point[1]) for point in box]
            x0, x1 = min(xs), max(xs)
            y0, y1 = min(ys), max(ys)
            span = max(x1 - x0, 1.0)
            total = max(len(text), 1)

            cursor = 0
            for word_index, token in enumerate(text.split()):
                start = text.find(token, cursor)
                if start < 0:
                    start = cursor
                cursor = start + len(token)
                words.append(OcrWord(
                    x0 + span * (start / total),
                    y0,
                    x0 + span * (cursor / total),
                    y1,
                    token,
                    block=0,
                    line=line_index,
                    word=word_index,
                ))
        return words


# --------------------------------------------------------------------------
# selection
# --------------------------------------------------------------------------

_ENGINES: list = [TesseractEngine(), RapidOcrEngine()]


def select_engine():
    """The first usable engine, with a note explaining the choice."""
    notes: list[str] = []
    for engine in _ENGINES:
        ok, note = engine.available()
        if ok:
            return engine, note
        notes.append(f"{engine.name}: {note}")
    return None, "; ".join(notes)


def describe() -> tuple[bool, str]:
    engine, note = select_engine()
    return engine is not None, note
