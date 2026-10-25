#!/usr/bin/env python3
"""Build reproducible, self-verifying archives of the merged agent framework.

Two scopes, because they answer different questions
---------------------------------------------------
``framework`` (default)
    The RISTER package on its own: the importable package with its tests, the
    examples, the prompts, and the three documents that describe it. This is
    what you hand to someone who wants to *use* the framework.

``repo``
    The whole repository: the merged framework **plus** every upstream pin
    (``MANIFEST.json``, ``.gitmodules``), the seven per-framework design docs,
    the debranding audit, the verification notes, the CI workflow, the smoke
    tests for the pinned packages, and the build scripts. This is what you hand
    to someone who wants to *understand or rebuild* the merge.

Both archives
-------------
* mirror the repository layout, so every relative link inside keeps resolving;
* are byte-identical across builds (sorted entries, fixed timestamps, 0644);
* carry no build residue (``__pycache__``, ``*.pyc``, ``*.egg-info``, venvs);
* contain ``MANIFEST.txt`` — every file, its size and its SHA-256;
* contain ``ARCHIVE.md`` — what is inside and how to prove it yourself.

Usage
-----
    python3 scripts/package.py                    # rister-<version>.zip
    python3 scripts/package.py --scope repo       # agent-frameworks-merged-<version>.zip
    python3 scripts/package.py --all              # both
    python3 scripts/package.py --all --verify     # build, then test each archive
    python3 scripts/package.py --all --check      # build twice, prove determinism
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
SKIP_NAMES = {".DS_Store", "Thumbs.db"}

#: Fixed timestamp for every entry, so two builds hash identically.
EPOCH = (1980, 1, 1, 0, 0, 0)

#: The six upstream projects this repository pins. They are git submodules, so
#: their sources live in their own repositories; an archive carries the pins and
#: the one-line command that materialises them, never a stale copy.
UPSTREAMS = [
    ("langgraph", "python", "graph orchestration"),
    ("mastra", "typescript", "agent/workflow runtime"),
    ("beeai-framework", "python", "agent platform"),
    ("voltagent", "typescript", "agent framework"),
    ("agent-governance-toolkit", "python", "agent governance"),
    ("codex-security", "typescript", "security scanning"),
]


def version() -> str:
    """Read the single source of truth for the version."""
    text = (PROJECT / "rister" / "core" / "meta.py").read_text(encoding="utf-8")
    match = re.search(r'__version__\s*=\s*"([^"]+)"', text)
    if match is None:  # pragma: no cover - guarded by test_architecture
        raise SystemExit("package.py: could not read __version__ from core/meta.py")
    return match.group(1)


def _keep(path: Path) -> bool:
    rel = path.relative_to(ROOT)
    if any(part in SKIP_DIRS for part in rel.parts):
        return False
    return path.suffix not in SKIP_SUFFIXES and path.name not in SKIP_NAMES


def _tracked() -> list[Path]:
    """Files git tracks — the authoritative definition of "the repository"."""
    result = subprocess.run(
        ["git", "-C", str(ROOT), "ls-files", "-z"], capture_output=True, text=True
    )
    if result.returncode != 0:  # pragma: no cover - no git, e.g. inside an archive
        return []
    found = []
    for name in result.stdout.split("\0"):
        if not name:
            continue
        path = ROOT / name
        if path.is_file() and _keep(path):
            found.append(path)
    return sorted(found)


def _framework_files() -> list[Path]:
    """The standalone package: project tree plus the docs that describe it."""
    chosen: list[Path] = []
    for path in sorted(PROJECT.rglob("*")):
        if path.is_file() and _keep(path):
            chosen.append(path)
    for extra in ("docs/architecture.html", "docs/07-rister.md",
                  "docs/08-debranding.md", "THIRD-PARTY-NOTICES.md"):
        source = ROOT / extra
        if not source.is_file():  # pragma: no cover - tracked in the repo
            raise SystemExit(f"package.py: missing {extra}")
        chosen.append(source)
    return sorted(set(chosen))


def _repo_files() -> list[Path]:
    """Everything: the merged framework and the record of what it merged."""
    files = _tracked()
    if not files:  # pragma: no cover - fallback when git is unavailable
        files = sorted(p for p in ROOT.rglob("*") if p.is_file() and _keep(p))
    # A release archive never carries a previously built archive.
    return [f for f in files if f.suffix not in SKIP_SUFFIXES]


def payload(scope: str) -> tuple[str, list[Path]]:
    """Archive name and the files that belong in it."""
    if scope == "framework":
        return f"rister-{version()}", _framework_files()
    return f"agent-frameworks-merged-{version()}", _repo_files()


def archive_note(scope: str, root: str, files: list[Path]) -> str:
    """The human-readable summary that ships as ARCHIVE.md."""
    total = sum(p.stat().st_size for p in files)
    ver = version()
    lines = [f"# {root}", ""]
    if scope == "framework":
        lines += [
            f"RISTER **{ver}** — one framework for governed AI agents, packed as a",
            "standalone Python project. No dependencies beyond the standard library.",
            "",
            "## Layout",
            "",
            "```",
            f"{root}/",
            "├── ARCHIVE.md            this file",
            "├── MANIFEST.txt          every file, its size and its SHA-256",
            "├── THIRD-PARTY-NOTICES.md",
            "├── docs/                 architecture page, design doc, debranding audit",
            "└── rister/               the project",
            "    ├── pyproject.toml    pip-installable, zero dependencies",
            "    ├── rister/           the importable package (incl. its tests, py.typed)",
            "    ├── examples/         four runnable, offline examples",
            "    └── prompts/          a hardened system prompt",
            "```",
            "",
            "## Prove it",
            "",
            "```bash",
            "cd rister",
            "PYTHONPATH=. python3 -m rister.tests   # the suite, no test runner needed",
            "PYTHONPATH=. python3 -m rister demo    # offline end-to-end walkthrough",
            "pip install -e .                       # then: rister --help",
            "```",
        ]
    else:
        lines += [
            f"The **merged agent framework**, complete: RISTER {ver} — the single",
            "native framework synthesised from six upstream projects — together with",
            "the pinned record of what it merged, the per-framework design notes, the",
            "debranding audit, the verification log, CI and build tooling.",
            "",
            "## Layout",
            "",
            "```",
            f"{root}/",
            "├── ARCHIVE.md                 this file",
            "├── MANIFEST.txt               every file, its size and its SHA-256",
            "├── README.md                  the repository overview",
            "├── MANIFEST.json              the six upstream pins (repo, tag, commit)",
            "├── .gitmodules                where each upstream is mounted",
            "├── THIRD-PARTY-NOTICES.md     attribution for the six",
            "├── Makefile                   bootstrap · verify · package · docs-check",
            "├── .github/workflows/         CI: verification on every push",
            "├── docs/                      architecture + 00–08 design notes",
            "│   ├── architecture.html      generated from the source tree",
            "│   ├── 01–06                  what each upstream contributed",
            "│   ├── 07-rister.md           how the seven layers became one",
            "│   ├── 08-debranding.md       the brand-neutrality audit",
            "│   └── verification.md        measured results",
            "├── rister/                    the merged framework (package + tests + examples)",
            "├── scripts/                   bootstrap, verify, pins, docs, package",
            "├── smoke/                     install-and-call smoke tests for the pins",
            "└── vendor/                    the six upstream mount points (see below)",
            "```",
            "",
            "## The six upstreams are pinned, not copied",
            "",
            "`vendor/*` are git submodules: the sources stay in their own repositories,",
            "which keeps this project honest about what it did not write, and keeps a",
            "release archive small. The pins are exact commits, recorded in",
            "`MANIFEST.json` and `.gitmodules`.",
            "",
            "| Upstream | Language | Role |",
            "|---|---|---|",
        ]
        lines += [f"| `{name}` | {lang} | {role} |" for name, lang, role in UPSTREAMS]
        lines += [
            "",
            "To materialise all six from a checkout of this project:",
            "",
            "```bash",
            "make bootstrap-fast     # depth-1 clones (~526 MB)",
            "make bootstrap          # full history (~2.3 GB)",
            "make status             # pinned vs checked-out commits",
            "make verify             # install the pins and run their smoke tests",
            "```",
            "",
            "## Prove it",
            "",
            "```bash",
            "cd rister",
            "PYTHONPATH=. python3 -m rister.tests    # 173 tests, no runner needed",
            "PYTHONPATH=. python3 -m rister demo     # offline end-to-end walkthrough",
            "PYTHONPATH=. python3 -m rister scan rister --fail-on-severity high",
            "cd .. && python3 scripts/docs_check.py  # metadata and docs consistency",
            "```",
        ]
    lines += [
        "",
        "## Integrity",
        "",
        f"{len(files)} files, {total:,} bytes uncompressed. `MANIFEST.txt` lists a",
        "SHA-256 for each one; compare a file with:",
        "",
        "```bash",
        "sha256sum -c <(awk 'NR>9 && NF==3 {print $1\"  \"substr($0, index($0,$3))}' MANIFEST.txt)",
        "```",
        "",
        "Built by `scripts/package.py`: sorted entries, fixed timestamps and uniform",
        "permissions, so two builds of the same source are byte-identical.",
        "",
    ]
    return "\n".join(lines)


def manifest(files: list[Path], root: str) -> str:
    lines = [
        root,
        "",
        f"version   {version()}",
        f"files     {len(files)}",
        "python    >=3.10",
        "runtime   zero third-party dependencies",
        "",
        "sha256                                                            bytes  path",
        "-" * 78,
    ]
    for path in files:
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append(f"{digest}  {path.stat().st_size:>7}  {root}/{path.relative_to(ROOT).as_posix()}")
    total = sum(path.stat().st_size for path in files)
    lines += ["-" * 78, f"{len(files)} files, {total:,} bytes uncompressed", ""]
    return "\n".join(lines)


def build(out_dir: Path, scope: str) -> Path:
    """Write one archive and return its path."""
    root, files = payload(scope)
    out_dir.mkdir(parents=True, exist_ok=True)
    target = out_dir / f"{root}.zip"

    with zipfile.ZipFile(target, "w", compression=zipfile.ZIP_DEFLATED,
                         compresslevel=9) as archive:
        def add(arcname: str, data: bytes) -> None:
            info = zipfile.ZipInfo(arcname, date_time=EPOCH)
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            info.create_system = 3  # unix, so permissions survive extraction
            archive.writestr(info, data)

        for path in files:
            add(f"{root}/{path.relative_to(ROOT).as_posix()}", path.read_bytes())

        if scope == "repo":
            # Keep the submodule mount points visible after extraction.
            for name, _lang, _role in UPSTREAMS:
                add(f"{root}/vendor/{name}/.gitkeep", b"")
        add(f"{root}/ARCHIVE.md", archive_note(scope, root, files).encode("utf-8"))
        add(f"{root}/MANIFEST.txt", manifest(files, root).encode("utf-8"))

    return target


def verify(target: Path, scope: str) -> int:
    """Extract the archive and prove it from the inside."""
    print(f"\nverifying {target.name}")
    with tempfile.TemporaryDirectory() as directory:
        with zipfile.ZipFile(target) as archive:
            residue = [
                n for n in archive.namelist()
                if "__pycache__" in n or n.endswith((".pyc", ".pyo", ".egg-info"))
            ]
            if residue:  # pragma: no cover - defensive
                print(f"FAIL archive carries build residue: {residue[:3]}")
                return 1
            archive.extractall(directory)

        root = Path(directory) / target.stem
        project = root / "rister"
        required = [
            project / "rister" / "__init__.py",
            project / "rister" / "py.typed",
            root / "docs" / "architecture.html",
            root / "ARCHIVE.md",
            root / "MANIFEST.txt",
        ]
        if scope == "repo":
            required += [
                root / "MANIFEST.json",
                root / ".gitmodules",
                root / "docs" / "07-rister.md",
                root / "docs" / "08-debranding.md",
                root / "scripts" / "docs_check.py",
                root / ".github" / "workflows",
            ]
        missing = [str(p.relative_to(root)) for p in required if not p.exists()]
        if missing:
            print(f"FAIL archive is incomplete, missing: {missing}")
            return 1

        env = {
            "PYTHONPATH": ".", "PATH": "/usr/bin:/bin:/usr/local/bin",
            "PYTHONDONTWRITEBYTECODE": "1", "HOME": directory,
        }
        checks: list[tuple[str, list[str], Path]] = [
            ("test suite", [sys.executable, "-m", "rister.tests"], project),
            ("demo", [sys.executable, "-m", "rister", "demo", "--quiet"], project),
            ("cli", [sys.executable, "-m", "rister", "version"], project),
            ("self-scan", [sys.executable, "-m", "rister", "scan", "rister",
                           "--fail-on-severity", "high"], project),
        ]
        if scope == "repo":
            checks.append(("docs-check", [sys.executable, "scripts/docs_check.py"], root))

        for label, command, cwd in checks:
            result = subprocess.run(command, cwd=cwd, env=env, capture_output=True, text=True)
            output = (result.stdout or "").strip().splitlines() or (result.stderr or "").strip().splitlines()
            summary = output[-1] if output else "(no output)"
            if label == "self-scan":
                summary = "no findings" if result.returncode == 0 else summary
            elif label == "cli":
                summary = f"exit {result.returncode} · {summary}"
            print(f"  {label:11} {summary}")
            if result.returncode != 0:
                print(f"\nFAIL {label} failed inside the extracted archive")
                print((result.stdout or "")[-2000:] + (result.stderr or "")[-2000:])
                return 1

    print("  archive is complete, importable and green")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="Pack the merged agent framework.")
    parser.add_argument("--out", default=".", help="output directory (default: .)")
    parser.add_argument("--scope", choices=("framework", "repo"), default="framework",
                        help="framework = RISTER alone (default); repo = everything")
    parser.add_argument("--all", action="store_true", help="build both archives")
    parser.add_argument("--verify", action="store_true", help="test each archive after building")
    parser.add_argument("--check", action="store_true", help="build twice, prove determinism")
    args = parser.parse_args(argv)

    scopes = ["framework", "repo"] if args.all else [args.scope]
    failed = False
    for scope in scopes:
        target = build(Path(args.out), scope)
        data = target.read_bytes()
        root, files = payload(scope)
        print(f"{target}\n  {len(data):,} bytes · {len(files)} files · "
              f"sha256 {hashlib.sha256(data).hexdigest()}")

        if args.check:
            with tempfile.TemporaryDirectory() as directory:
                again = build(Path(directory), scope).read_bytes()
            if again != data:
                print("  FAIL two builds of the same source differ")
                failed = True
            else:
                print("  reproducible: a second build is byte-identical")

        if args.verify and verify(target, scope) != 0:
            failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
