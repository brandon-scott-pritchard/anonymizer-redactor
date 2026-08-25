#!/usr/bin/env python3
"""Copy Tesseract and everything it links against into ``vendor/``.

Homebrew binaries point at absolute paths under ``/opt/homebrew``, which do not
exist on a machine that has never installed Homebrew. This walks the dylib
graph, copies each non-system library next to the binary, rewrites the load
commands to relative paths, and re-signs everything - because editing a Mach-O
header invalidates its signature and macOS will refuse to load it.

    python3 vendor_tesseract_macos.py

Run it once on a Mac that has ``brew install tesseract``. The result is
committed-free and rebuildable; PyInstaller picks ``vendor/`` up automatically.
"""

from __future__ import annotations

import platform
import shutil
import tarfile
import subprocess
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
TARGET = HERE / "vendor" / "tesseract" / "macos"

# Libraries the operating system always provides. Copying these would be both
# pointless and, for the frameworks, wrong.
SYSTEM_PREFIXES = ("/usr/lib/", "/System/")

TESSDATA_WANTED = ("eng.traineddata", "osd.traineddata", "pdf.ttf", "configs")


def run(*args: str) -> str:
    result = subprocess.run(args, capture_output=True, text=True)
    if result.returncode != 0:
        raise SystemExit(f"command failed: {' '.join(args)}\n{result.stderr}")
    return result.stdout


# Homebrew libraries reference some of their own dependencies through @rpath
# rather than an absolute path (libwebpmux -> libsharpyuv is one). Those must be
# resolved and carried too, or the bundle dies at load time on a clean machine.
SEARCH_DIRS: list[Path] = []


def dependencies(binary: Path, keep_relative: bool = False) -> list[str]:
    """Dylibs ``binary`` links against, excluding the ones macOS provides."""
    out = run("otool", "-L", str(binary))
    found = []
    for line in out.splitlines()[1:]:
        path = line.strip().split(" ", 1)[0]
        if not path or path.startswith(SYSTEM_PREFIXES):
            continue
        if path.startswith(("@loader_path", "@executable_path")):
            continue
        if path.startswith("@rpath") and not keep_relative:
            continue
        found.append(path)
    return found


def note_search_dir(path: Path) -> None:
    parent = path.parent
    if parent not in SEARCH_DIRS:
        SEARCH_DIRS.append(parent)


def resolve(path: str) -> Path | None:
    """Turn a load-command entry into a real file, following @rpath by search."""
    if path.startswith("@rpath/"):
        name = path.split("/", 1)[1]
        for directory in SEARCH_DIRS:
            candidate = directory / name
            if candidate.exists():
                return candidate.resolve()
        return None
    real = Path(path)
    return real.resolve() if real.exists() else None


def collect(binary: Path, seen: dict[str, Path]) -> None:
    """Walk the dylib graph depth-first, recording every library reached."""
    for dep in dependencies(binary, keep_relative=True):
        name = Path(dep).name
        if name in seen:
            continue
        resolved = resolve(dep)
        if resolved is None:
            print(f"    ! could not resolve {dep}", file=sys.stderr)
            continue
        seen[name] = resolved
        note_search_dir(resolved)
        collect(resolved, seen)


def sign(path: Path) -> None:
    subprocess.run(["codesign", "--force", "--sign", "-", "--timestamp=none", str(path)],
                   capture_output=True)


def main() -> int:
    if sys.platform != "darwin":
        raise SystemExit("this script vendors the macOS build; run it on a Mac")

    source = shutil.which("tesseract")
    if not source:
        raise SystemExit(
            "tesseract was not found on PATH.\n"
            "Install it first:  brew install tesseract"
        )
    source = Path(source).resolve()
    print(f"==> Vendoring {source}")
    print(f"    architecture: {platform.machine()}")

    bin_dir, lib_dir, data_dir = TARGET / "bin", TARGET / "lib", TARGET / "tessdata"
    for directory in (bin_dir, lib_dir, data_dir):
        directory.mkdir(parents=True, exist_ok=True)

    # --- the executable ---------------------------------------------------
    binary = bin_dir / "tesseract"
    shutil.copy2(source, binary)
    binary.chmod(0o755)

    # --- its libraries, transitively --------------------------------------
    for seed in (source.parent.parent / "lib", Path("/opt/homebrew/lib"),
                 Path("/usr/local/lib"), Path("/opt/homebrew/opt")):
        if seed.is_dir():
            SEARCH_DIRS.append(seed)

    libraries: dict[str, Path] = {}
    collect(source, libraries)
    print(f"==> {len(libraries)} bundled libraries")
    for name, path in sorted(libraries.items()):
        shutil.copy2(path, lib_dir / name)
        (lib_dir / name).chmod(0o755)
        print(f"    {name}")

    # --- rewrite the load commands ----------------------------------------
    for dep in dependencies(binary, keep_relative=True):
        name = Path(dep).name
        if name in libraries:
            run("install_name_tool", "-change", dep,
                f"@executable_path/../lib/{name}", str(binary))

    for name in libraries:
        target = lib_dir / name
        run("install_name_tool", "-id", f"@loader_path/{name}", str(target))
        for dep in dependencies(target, keep_relative=True):
            dep_name = Path(dep).name
            if dep_name in libraries:
                run("install_name_tool", "-change", dep,
                    f"@loader_path/{dep_name}", str(target))

    # --- re-sign, since editing the header broke the signature -------------
    for path in list(lib_dir.iterdir()) + [binary]:
        sign(path)

    # --- language data ----------------------------------------------------
    candidates = [
        source.parent.parent / "share" / "tessdata",
        Path("/opt/homebrew/share/tessdata"),
        Path("/usr/local/share/tessdata"),
    ]
    tessdata = next((c for c in candidates if c.is_dir()), None)
    if tessdata is None:
        raise SystemExit("could not find tessdata; is tesseract installed properly?")
    print(f"==> Language data from {tessdata}")
    for name in TESSDATA_WANTED:
        item = tessdata / name
        if not item.exists():
            continue
        destination = data_dir / name
        if item.is_dir():
            shutil.copytree(item, destination, dirs_exist_ok=True)
        else:
            shutil.copy2(item, destination)
        print(f"    {name}")

    # --- prove it runs detached from Homebrew ------------------------------
    print("==> Verifying")
    check = subprocess.run([str(binary), "--version"], capture_output=True, text=True,
                           env={"TESSDATA_PREFIX": str(data_dir), "PATH": "/usr/bin:/bin"})
    if check.returncode != 0:
        raise SystemExit(f"the vendored binary does not run:\n{check.stderr}")
    print("    " + check.stdout.splitlines()[0])

    remaining = [d for d in dependencies(binary, keep_relative=True)
                 if not d.startswith("@executable_path")]
    if remaining:
        raise SystemExit(f"still linking against absolute paths: {remaining}")

    # Also pack it. PyInstaller inspects any Mach-O file it is handed, rewrites
    # its load commands and deduplicates libraries by basename across the whole
    # build - which silently swapped these for a different Tesseract version.
    # A tarball is opaque to that machinery, so what ships is exactly what was
    # vendored here. ocr.py unpacks it once on first use.
    archive = TARGET.parent.parent / "tesseract-macos.tar.gz"
    print(f"==> Packing {archive.name}")
    with tarfile.open(archive, "w:gz") as tar:
        tar.add(TARGET, arcname="tesseract")

    size = sum(f.stat().st_size for f in TARGET.rglob("*") if f.is_file()) / 1e6
    print(f"==> Done: {TARGET.relative_to(HERE)} ({size:.0f} MB), "
          f"archive {archive.stat().st_size / 1e6:.0f} MB")
    print("    Rebuild the app with ./build_macos.sh to include it.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
