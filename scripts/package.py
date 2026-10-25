#!/usr/bin/env python3
"""Build a self-contained, reproducible archive of the RISTER framework.

What goes in
------------
The archive mirrors the repository layout, so every relative link inside it keeps
working (``rister/README.md`` links to ``../docs/architecture.html`` and
``../THIRD-PARTY-NOTICES.md``)::

    rister-<version>/
      MANIFEST.txt          every file, its size and its SHA-256
      THIRD-PARTY-NOTICES.md
      docs/
        architecture.html   the architecture page, generated from the source
        07-rister.md        the design document
        08-debranding.md    the provenance audit
      rister/               the project: package, tests, examples, prompts
        README.md
        pyproject.toml
        rister/             the importable package (tests included)
        examples/
        prompts/

The zip is byte-identical across runs: entries are sorted, timestamps are fixed
at 1980-01-01, permissions are 0644, and no build residue (``__pycache__``,
``*.pyc``, ``*.egg-info``, virtualenvs, editor droppings) is ever included.

Usage
-----
    python3 scripts/package.py                  # write ./rister-<version>.zip
    python3 scripts/package.py --out DIR        # write into DIR
    python3 scripts/package.py --verify         # build, then test the archive
    python3 scripts/package.py --check          # build twice, prove determinism

``--verify`` extracts the archive to a temporary directory and, from inside the
extracted copy, runs the full test suite, the demo and the CLI. An archive that
cannot prove itself is not a release.
"""
from __future__ import annotations

import argparse
import hashlib
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PROJECT = ROOT / "rister"

#: Directories anywhere in the tree that never ship.
SKIP_DIRS = {
    "__pycache__", ".git", ".venv", "venv", ".mypy_cache", ".ruff_cache",
    ".pytest_cache", "build", "dist", "node_modules", ".idea", ".vscode",
}
#: File suffixes that are always build residue.
SKIP_SUFFIXES = {".pyc", ".pyo", ".pyd", ".zip", ".swp", ".orig", ".rej"}
#: Editor and OS droppings.
SKIP_NAMES = {".DS_Store", "Thumbs.db", ".gitignore"}

#: Extra files, as paths relative to the project directory.
EXTRAS = [
    "docs/architecture.html",
    "docs/07-rister.md",
    "docs/08-debranding.md",
    "THIRD-PARTY-NOTICES.md",
]

#: Fixed timestamp for every entry, so two builds hash identically.
EPOCH = (1980, 1, 1, 0, 0, 0)


def version() -> str:
    """Read the single source of truth for the version."""
    text = (PROJECT / "rister" / "core" / "meta.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if match is None:  # pragma: no cover - guarded by test_architecture
        raise SystemExit("package.py: could not read __version__ from core/meta.py")
    return match.group(1)


def _wanted(directory: Path) -> bool:
    return not any(part in SKIP_DIRS for part in directory.parts)


def _files() -> list[Path]:
    """Every file that belongs in the archive, sorted and absolute."""
    chosen: list[Path] = []
    for path in sorted(PROJECT.rglob("*")):
        if not path.is_file() or not _wanted(path.relative_to(PROJECT)):
            continue
        if path.suffix in SKIP_SUFFIXES or path.name in SKIP_NAMES:
            continue
        chosen.append(path)
    for extra in EXTRAS:
        source = ROOT / extra
        if not source.is_file():  # pragma: no cover - tracked in the repo
            raise SystemExit(f"package.py: missing {extra}")
        chosen.append(source)
    return sorted(set(chosen))


def _arcname(path: Path, root: str) -> str:
    """Where a file lands inside the archive."""
    return f"{root}/{path.relative_to(ROOT).as_posix()}" if path.is_relative_to(ROOT) \
        else f"{root}/{path.name}"


def manifest(files: list[Path], root: str, name: str) -> str:
    lines = [
        f"{name}",
        "",
        f"version   {version()}",
        f"files     {len(files)}",
        f"python    >=3.10",
        f"runtime   zero third-party dependencies",
        "",
        "sha256                                                            bytes  path",
        "-" * 78,
    ]
    for path in files:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.stat().st_size:>7}  {_arcname(path, root)}")
    total = sum(path.stat().st_size for path in files)
    lines += ["-" * 78, f"{len(files)} files, {total:,} bytes uncompressed", ""]
    return "\n".join(lines)


def build(out_dir: Path) -> Path:
    """Write the archive and return its path."""
    import io

    name = "rister"
    ver = version()
    root = f"{name}-{ver}"
    files = _files()
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{root}.zip"

    # The manifest describes the payload, so it is not part of its own listing.
    payload = manifest(files, root, root)

    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        def add(arcname: str, data: bytes) -> None:
            info = zipfile.ZipInfo(arcname, date_time=EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.create_system = 3  # unix, so permissions survive extraction
            archive.writestr(info, data)

        for path in files:
            add(_arcname(path, root), path.read_bytes())
        add(f"{root}/MANIFEST.txt", payload.encode("utf-8"))

    return target


def verify(target: Path) -> int:
    """Prove the archive works: extract it and run from the extracted copy."""
    print(f"\nverifying {target.name}")
    with tempfile.TemporaryDirectory() as directory:
        with zipfile.ZipFile(target) as archive:
            names = archive.namelist()
            bad = [n for n in names if "__pycache__" in n or n.endswith((".pyc", ".pyo"))]
            if bad:  # pragma: no cover - defensive
                print(f"FAIL archive carries build residue: {bad[:3]}")
                return 1
            archive.extractall(directory)

        root = Path(directory) / target.stem
        project = root / "rister"
        if not (project / "rister" / "__init__.py").is_file():
            print("FAIL the extracted archive has no importable package")
            return 1
        if not (root / "docs" / "architecture.html").is_file():
            print("FAIL the architecture page is missing")
            return 1
        if not (project / "rister" / "py.typed").is_file():
            print("FAIL py.typed is missing (typing marker declared in pyproject)")
            return 1

        env = {"PYTHONPATH": ".", "PATH": "/usr/bin:/bin:/usr/local/bin",
               "PYTHONDONTWRITEBYTECODE": "1", "HOME": directory}
        checks = [
            ("test suite", [sys.executable, "-m", "rister.tests"]),
            ("demo", [sys.executable, "-m", "rister", "demo", "--quiet"]),
            ("cli version", [sys.executable, "-m", "rister", "version"]),
            ("self-scan", [sys.executable, "-m", "rister", "scan", "rister",
                           "--fail-on-severity", "high"]),
        ]
        for label, command in checks:
            result = subprocess.run(command, cwd=project, env=env,
                                    capture_output=True, text=True)
            tail = (result.stdout or result.stderr).strip().splitlines()
            summary = tail[-1] if tail else "(no output)"
            if label == "test suite":
                print(f"  {label:12} {summary}")
            elif label == "self-scan":
                print(f"  {label:12} {'no findings' if result.returncode == 0 else summary}")
            else:
                print(f"  {label:12} exit {result.returncode}"
                      + (f" · {summary}" if label == "cli version" else ""))
            if result.returncode != 0:
                print(f"\nFAIL {label} failed inside the extracted archive")
                print(result.stdout[-2000:] + result.stderr[-2000:])
                return 1

    print("  archive is complete, importable and green")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Pack the RISTER framework into a zip.")
    parser.add_argument("--out", default=".", help="output directory (default: .)")
    parser.add_argument("--verify", action="store_true", help="test the archive after building")
    parser.add_argument("--check", action="store_true", help="build twice and prove determinism")
    args = parser.parse_args(argv)

    target = build(Path(args.out))
    data = target.read_bytes()
    print(f"{target}  {len(data):,} bytes  sha256 {hashlib.sha256(data).hexdigest()}")

    if args.check:
        with tempfile.TemporaryDirectory() as directory:
            again = build(Path(directory))
            if again.read_bytes() != data:
                print("FAIL two builds of the same tree differ — the archive is not reproducible")
                return 1
        print("  reproducible: a second build is byte-identical")

    if args.verify:
        return verify(target)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
