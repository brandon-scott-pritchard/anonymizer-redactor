# PyInstaller spec for Anonymizer / Redactor.
#
# Built from a staging directory, never from the project folder directly: the
# project path contains a colon, which PyInstaller cannot handle.
# Run it through build_macos.sh or build_windows.bat rather than by hand.

import os
import platform
import sys
from pathlib import Path

APP_NAME = "Document Redactions & Anonymization"

from PyInstaller.utils.hooks import collect_all

datas, binaries, hiddenimports = [], [], []

# spaCy and its compiled friends need their data and metadata carried along,
# and the language model is a package in its own right.
for package in (
    "spacy", "en_core_web_sm", "thinc", "blis", "cymem", "preshed", "murmurhash",
    "srsly", "catalogue", "wasabi", "confection", "weasel", "cloudpathlib",
    "spacy_legacy", "spacy_loggers", "langcodes",
):
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    except Exception:
        continue
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# The OCR fallback ships its models inside the package, so collect_all carries
# both the code and the .onnx files.
for package in ("rapidocr_onnxruntime", "onnxruntime", "rapidocr"):
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    except Exception:
        continue
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden

# The vendored Tesseract, shipped as a tarball rather than a loose tree.
# PyInstaller inspects every Mach-O file it is given: it rewrites load commands
# and deduplicates libraries by basename across the whole build, which silently
# replaced the vendored libtesseract with a different version. A .tar.gz is
# opaque to all of that, so exactly what was vendored is what ships. ocr.py
# unpacks it once, on first use.
# Only the archive matching this build's architecture: a frozen app is already
# arm64-or-Intel, so carrying the other one is dead weight.
_machine = platform.machine().lower()
_machine = {"amd64": "x86_64", "x64": "x86_64", "aarch64": "arm64"}.get(_machine, _machine)
if sys.platform == "darwin":
    _folder = f"macos-{_machine}"
elif sys.platform.startswith("win"):
    _folder = f"windows-{_machine}"
else:
    _folder = f"linux-{_machine}"

_vendor = Path(SPECPATH) / "vendor"
if _vendor.is_dir():
    _archive = _vendor / f"tesseract-{_folder}.tar.gz"
    if _archive.is_file():
        datas.append((str(_archive), "vendor"))
        print(f"spec: bundling {_archive.name}")
    else:
        print(f"spec: no vendored Tesseract for {_folder}; RapidOCR will cover OCR")

hiddenimports += [
    "pymupdf", "fitz", "lxml.etree", "lxml._elementpath",
    "cryptography.hazmat.primitives.kdf.pbkdf2", "pytesseract", "PIL.Image",
    "numpy",
]

# The web front end: static assets plus the pieces uvicorn and fastapi load
# dynamically, which static analysis cannot see.
_webstatic = Path(SPECPATH) / "webapp" / "static"
if _webstatic.is_dir():
    datas.append((str(_webstatic), "webapp/static"))
    print("spec: bundling the web front end")
hiddenimports += [
    "uvicorn.logging", "uvicorn.loops", "uvicorn.loops.auto",
    "uvicorn.loops.asyncio", "uvicorn.protocols", "uvicorn.protocols.http",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets", "uvicorn.protocols.websockets.auto",
    "uvicorn.lifespan", "uvicorn.lifespan.on", "uvicorn.lifespan.off",
    "anyio._backends._asyncio", "multipart", "python_multipart",
]

# The native window: pywebview loads its platform back end dynamically, and on
# macOS reaches AppKit/WebKit through pyobjc.
for package in ("webview", "objc", "Foundation", "AppKit", "WebKit", "Quartz"):
    try:
        pkg_datas, pkg_binaries, pkg_hidden = collect_all(package)
    except Exception:
        continue
    datas += pkg_datas
    binaries += pkg_binaries
    hiddenimports += pkg_hidden
hiddenimports += ["webview.platforms.cocoa", "webview.platforms.winforms"]

a = Analysis(
    ["launcher.py"],
    pathex=["."],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["matplotlib", "pandas", "scipy", "notebook", "IPython", "pytest"],
    noarchive=False,
)
pyz = PYZ(a.pure)

# Set CODESIGN_IDENTITY to a "Developer ID Application: ..." identity to sign
# during the build; unset, PyInstaller ad-hoc signs on Apple Silicon.
exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Anonymizer-Redactor",
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    codesign_identity=os.environ.get("CODESIGN_IDENTITY"),
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False,
    name="Anonymizer-Redactor",
)
app = BUNDLE(
    coll,
    name=f"{APP_NAME}.app",
    bundle_identifier="family.thepritchard.anonymizer-redactor",
    info_plist={
        "CFBundleName": APP_NAME,
        "CFBundleDisplayName": APP_NAME,
        "CFBundleShortVersionString": "1.0.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
    },
)
