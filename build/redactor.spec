# PyInstaller spec for Anonymizer / Redactor.
#
# Built from a staging directory, never from the project folder directly: the
# project path contains a colon, which PyInstaller cannot handle.
# Run it through build_macos.sh or build_windows.bat rather than by hand.

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

hiddenimports += [
    "pymupdf", "fitz", "lxml.etree", "lxml._elementpath",
    "cryptography.hazmat.primitives.kdf.pbkdf2", "pytesseract", "PIL.Image",
]

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

exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Anonymizer-Redactor",
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
)
coll = COLLECT(
    exe, a.binaries, a.datas,
    strip=False, upx=False,
    name="Anonymizer-Redactor",
)
app = BUNDLE(
    coll,
    name="Anonymizer-Redactor.app",
    bundle_identifier="family.thepritchard.anonymizer-redactor",
    info_plist={
        "CFBundleShortVersionString": "1.0.0",
        "NSHighResolutionCapable": True,
        "LSMinimumSystemVersion": "11.0",
    },
)
