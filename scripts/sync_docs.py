#!/usr/bin/env python3
"""Rewrite the measured numbers in docs/architecture.html from the source tree.

The page is a published claim about the code, so its counts are *checked* by
``rister/tests/test_architecture.py``. This script is the other half of that
contract: it recomputes every number the page states and patches it in place,
so the fix for a stale page is one command rather than an archaeology session.

It deliberately reuses the measuring function from the test module instead of
re-implementing it — two counters would eventually disagree.

    python3 scripts/sync_docs.py            # rewrite in place
    python3 scripts/sync_docs.py --check    # report drift, change nothing

Exit code: 0 = page matches the source, 1 = drift (with --check).
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PAGE = ROOT / "docs" / "architecture.html"


def _stats():
    sys.path.insert(0, str(ROOT / "rister"))
    from rister.tests.test_architecture import _source_stats  # noqa: PLC0415

    return _source_stats()


def _fmt(number: int) -> str:
    return f"{number:,}"


def _sub(pattern: str, replacement: str, page: str, label: str, changes: list[str]) -> str:
    """Replace the first match of ``pattern`` and record the change."""
    match = re.search(pattern, page)
    if match is None:
        raise SystemExit(f"sync_docs: could not find {label} in {PAGE.name}")
    if match.group(0) == replacement:
        return page
    before = match.group(0)
    changes.append(f"{label}: {before.strip()}  ->  {replacement.strip()}")
    return page[: match.start()] + replacement + page[match.end() :]


def _sub_optional(pattern: str, replacement: str, page: str, label: str, changes: list[str]) -> str:
    """Like :func:`_sub`, but a module the page does not document is not an error."""
    if not re.search(pattern, page):
        return page
    return _sub(pattern, replacement, page, label, changes)


def main(argv: list[str]) -> int:
    check = "--check" in argv
    stats = _stats()
    page = PAGE.read_text(encoding="utf-8")
    changes: list[str] = []

    test_files = len(list((ROOT / "rister" / "rister" / "tests").glob("test_*.py")))
    source_loc = sum(bucket["loc"] for bucket in stats["per_module"].values())
    test_loc = stats["loc"] - source_loc
    govern_share = round(100 * stats["per_module"]["govern"]["loc"] / source_loc)
    core_loc = stats["per_module"]["core"]["loc"]
    govern_loc = _fmt(stats["per_module"]["govern"]["loc"])

    # header chips ---------------------------------------------------------
    page = _sub(r'<div class="chip"><b>([\d,]+)</b> LOC</div>',
                f'<div class="chip"><b>{_fmt(stats["loc"])}</b> LOC</div>', page, "chip LOC", changes)
    page = _sub(r'<div class="chip"><b>([\d,]+)</b> exports</div>',
                f'<div class="chip"><b>{stats["exports"]}</b> exports</div>', page, "chip exports", changes)
    page = _sub(r'<div class="chip"><b>([\d,]+)</b> tests</div>',
                f'<div class="chip"><b>{stats["tests"]}</b> tests</div>', page, "chip tests", changes)

    # hero stat tiles ------------------------------------------------------
    for label, value in [
        ("modules", stats["modules"]),
        ("files", stats["files"]),
        ("significant LOC", _fmt(stats["loc"])),
        ("public classes", stats["classes"]),
        ("exception types", stats["exceptions"]),
    ]:
        page = _sub(
            r'<div class="stat"><div class="n">([\d,]+)</div><div class="k">'
            + re.escape(label)
            + r"</div></div>",
            f'<div class="stat"><div class="n">{value}</div><div class="k">{label}</div></div>',
            page,
            f"stat {label}",
            changes,
        )

    # per-module cards -----------------------------------------------------
    for module, bucket in sorted(stats["per_module"].items()):
        pattern = r'\{id:"' + re.escape(module) + r'", loc:(\d+), files:"\d+ files?"'
        plural = "file" if bucket["files"] == 1 else "files"
        replacement = f'{{id:"{module}", loc:{bucket["loc"]}, files:"{bucket["files"]} {plural}"'
        page = _sub_optional(pattern, replacement, page, f"module {module}", changes)

    # prose that quotes a measurement -------------------------------------
    page = _sub(r"(\d[\d,]*) lines — \d+% of the framework",
                f"{govern_loc} lines — {govern_share}% of the framework", page, "prose govern", changes)
    page = _sub(r"core is \d[\d,]* lines and imports nothing",
                f"core is {core_loc} lines and imports nothing", page, "prose core", changes)
    page = _sub(r"([\d,]+) lines of tests",
                f"{_fmt(test_loc)} lines of tests", page, "prose test LOC", changes)
    page = _sub(r"\d+ tests across \d+ files",
                f'{stats["tests"]} tests across {test_files} files', page, "prose test count", changes)

    # version + footer -----------------------------------------------------
    version = re.search(r'__version__\s*=\s*"([^"]+)"',
                        (ROOT / "rister" / "rister" / "core" / "meta.py").read_text(encoding="utf-8"))
    if version is None:
        raise SystemExit("sync_docs: could not read __version__ from core/meta.py")
    page = _sub(r"system design · v[\d.]+", f"system design · v{version.group(1)}",
                page, "hero version", changes)
    page = _sub(r"<div>RISTER [\d.]+ · generated",
                f"<div>RISTER {version.group(1)} · generated", page, "footer version", changes)

    page = _sub(r"· (\d+) files · ([\d,]+) significant LOC</div>",
                f'· {stats["files"]} files · {_fmt(stats["loc"])} significant LOC</div>',
                page, "footer", changes)

    # prose in the other hand-maintained files -----------------------------
    prose_files = [
        ROOT / "README.md",
        ROOT / "rister" / "README.md",
        ROOT / "Makefile",
    ]
    for path in prose_files:
        if not path.exists():  # pragma: no cover - always present in this repo
            continue
        text = path.read_text(encoding="utf-8")
        patched, hits = re.subn(r"(?<![\d,])(\d[\d,]*) (tests\b)",
                                lambda m: f'{stats["tests"]} {m.group(2)}', text)
        if patched != text:
            for before, after in zip(re.findall(r"(?<![\d,])(\d[\d,]*) tests\b", text),
                                     re.findall(r"(?<![\d,])(\d[\d,]*) tests\b", patched)):
                if before != after:
                    changes.append(f"{path.name}: {before} tests  ->  {after} tests")
            path.write_text(patched, encoding="utf-8")

    design = ROOT / "docs" / "07-rister.md"
    if design.exists():
        text = design.read_text(encoding="utf-8")
        patched = re.sub(r"`rister\.mcp`, \d+ lines",
                         f'`rister.mcp`, {stats["per_module"]["mcp"]["loc"]} lines', text)
        if patched != text:
            changes.append("07-rister.md: mcp LOC  ->  updated")
            design.write_text(patched, encoding="utf-8")
        text = patched
    if design.exists():
        text = design.read_text(encoding="utf-8")
        patched = re.sub(r"\*\*Tests:\*\* \d+", f'**Tests:** {stats["tests"]}', text)
        if patched != text:
            changes.append("07-rister.md: **Tests:** count updated")
            design.write_text(patched, encoding="utf-8")

    docs = "architecture.html, README.md, rister/README.md, Makefile, 07-rister.md"
    if not changes:
        print(f"sync_docs: docs already match the source ({docs})")
        return 0

    for line in changes:
        print(f"  {line}")

    if check:
        print("sync_docs: FAILED — docs are stale (run without --check to fix)")
        return 1

    PAGE.write_text(page, encoding="utf-8")
    print(f"sync_docs: rewrote {len(changes)} number(s) across the docs")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
