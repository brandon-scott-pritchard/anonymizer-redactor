"""Build the shareable skill files: ``dist/<skill>.skill``.

    ~/.venvs/anonymizer-redactor/bin/python skills/build_skills.py [SKILL ...]

With no names, every skill folder under ``skills/`` is built. Each skill is
self-contained once built: its own script and instructions, plus a copy of
``_shared/`` - ``partycore/``, the trimmed fork of the app's detectors, and
``partyread.py``, the file readers - so both skills read and match exactly
alike. Everything is standard-library Python, which is what lets a skill run
in a chat sandbox that cannot install anything.

Staged in a temporary folder: the project's path contains a colon.
"""

from __future__ import annotations

import ast
import re
import shutil
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SKILLS = ROOT / "skills"
SHARED = SKILLS / "_shared"
# imported only inside a try that falls back when they are missing
OPTIONAL = {"pymupdf", "fitz", "pdfplumber", "pypdf", "striprtf", "extract_msg", "PIL", "stdnum"}


def check_frontmatter(skill: Path) -> None:
    text = (skill / "SKILL.md").read_text(encoding="utf-8")
    m = re.match(r"^---\n(.*?)\n---\n", text, re.DOTALL)
    if not m:
        sys.exit(f"{skill.name}: SKILL.md has no frontmatter")
    fields = dict(re.findall(r"^(\w[\w-]*):\s*(.*)$", m.group(1), re.MULTILINE))
    if fields.get("name") != skill.name:
        sys.exit(f"{skill.name}: frontmatter name is {fields.get('name')!r}")
    if not 0 < len(fields.get("description", "")) <= 1024:
        sys.exit(f"{skill.name}: description is {len(fields.get('description', ''))} characters "
                 "(1-1024)")


def stage(skill: Path, into: Path) -> Path:
    folder = into / skill.name
    shutil.copytree(skill, folder, ignore=shutil.ignore_patterns("__pycache__", "*.pyc",
                                                                 ".DS_Store"))
    shutil.copytree(SHARED / "partycore", folder / "scripts" / "partycore",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    shutil.copy2(SHARED / "partyread.py", folder / "scripts" / "partyread.py")
    shutil.copy2(ROOT / "LICENSE", folder / "LICENSE.txt")
    return folder


def check_imports(folder: Path) -> None:
    """Every module the scripts import is standard library, local, or optional
    behind a fallback - a chat sandbox installs nothing."""
    local = {p.stem for p in (folder / "scripts").rglob("*.py")} | {"partycore"}
    bad = []
    for path in (folder / "scripts").rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            names = []
            if isinstance(node, ast.Import):
                names = [a.name.split(".")[0] for a in node.names]
            elif isinstance(node, ast.ImportFrom) and node.level == 0 and node.module:
                names = [node.module.split(".")[0]]
            for name in names:
                if name not in sys.stdlib_module_names and name not in local \
                        and name not in OPTIONAL:
                    bad.append(f"{path.name}: {name}")
    if bad:
        sys.exit("non-standard imports without a fallback: " + ", ".join(sorted(set(bad))))


def smoke_test(folder: Path) -> None:
    """The staged copy imports its own shared modules, not the source tree's."""
    mains = [p for p in (folder / "scripts").glob("*.py") if p.name != "partyread.py"]
    if not mains:
        sys.exit(f"{folder.name}: no script")
    script = mains[0]
    # every optional library blocked: the skill must import with the standard library alone
    block = "; ".join(f"sys.modules[{m!r}] = None" for m in sorted(OPTIONAL))
    probe = (f"import sys, importlib; {block}; sys.path.insert(0, sys.argv[1]); "
             f"m = importlib.import_module({script.stem!r}); "
             "import partycore, partyread; "
             "import os; here = os.path.realpath(sys.argv[1]); "
             "assert os.path.realpath(partycore.__file__).startswith(here), partycore.__file__; "
             "from partycore import names, places; "
             "assert names.ordinary_word('baker'), 'word list missing'; "
             "assert places.known('Layton'), 'gazetteer missing'; print('ok')")
    out = subprocess.run([sys.executable, "-I", "-c", probe, str(folder / "scripts")],
                         capture_output=True, text=True, cwd=folder)
    if out.returncode != 0 or not out.stdout.startswith("ok"):
        sys.exit(f"{folder.name}: staged skill failed its smoke test:\n{out.stdout}{out.stderr}")


def package(folder: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    tmp = target.with_suffix(".skill.tmp")
    with zipfile.ZipFile(tmp, "w", zipfile.ZIP_DEFLATED) as z:
        for path in sorted(folder.rglob("*")):
            if path.is_file() and "__pycache__" not in path.parts:
                z.write(path, path.relative_to(folder.parent).as_posix())
    tmp.replace(target)


def build(skill: Path) -> Path:
    check_frontmatter(skill)
    target = ROOT / "dist" / f"{skill.name}.skill"
    with tempfile.TemporaryDirectory() as tmp:
        folder = stage(skill, Path(tmp))
        check_imports(folder)
        smoke_test(folder)
        package(folder, target)
    with zipfile.ZipFile(target) as z:
        count = len(z.namelist())
    print(f"{target} - {count} files, {target.stat().st_size // 1024} KB")
    return target


def main(argv=None) -> int:
    names = (argv if argv is not None else sys.argv[1:]) or sorted(
        p.name for p in SKILLS.iterdir() if p.is_dir() and (p / "SKILL.md").exists())
    for name in names:
        build(SKILLS / name)
    return 0


if __name__ == "__main__":
    sys.exit(main())
