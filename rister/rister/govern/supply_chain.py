"""Dependency supply-chain checks.

Offline, deterministic heuristics that catch the three failures that matter most
in an agent repository:

* **unpinned** dependencies (``foo`` with no version at all),
* **ranges** in a context that demands reproducibility (``foo>=1``, ``foo~=1.2``),
* **typosquats** — a package whose name is a small edit away from a well-known one.

Freshness (how recently a pinned version was published) needs registry access and
is therefore exposed as a hook rather than performed here.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

#: Common packages used as typosquat anchors. Extend via ``known_packages``.
DEFAULT_KNOWN = frozenset(
    {
        "requests", "urllib3", "numpy", "pandas", "flask", "django", "fastapi",
        "pydantic", "aiohttp", "transformers", "torch", "scikit-learn",
        "httpx", "boto3", "sqlalchemy", "pytest", "setuptools", "cryptography",
        "pyyaml", "click", "rich", "tqdm", "redis", "celery", "jinja2",
        "express", "react", "lodash", "axios", "zod", "typescript",
    }
)

SKIP_DIRS = frozenset(
    {".git", "node_modules", ".venv", "venv", ".tox", ".nox", "dist", "build",
     "__pycache__", ".mypy_cache", ".pytest_cache", ".ruff_cache", "site-packages"}
)

_PINNED = re.compile(r"^==\s*[0-9][^\s,;]*$")
_ANY_RANGE = re.compile(r"(>=|<=|~=|!=|>|<|\^|~|\*)")


# --------------------------------------------------------------------------
# findings
# --------------------------------------------------------------------------
@dataclass
class Finding:
    """One suspicious dependency."""

    package: str
    version: str
    severity: str
    rule: str
    message: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "package": self.package,
            "version": self.version,
            "severity": self.severity,
            "rule": self.rule,
            "message": self.message,
        }

    def __str__(self) -> str:
        return f"[{self.severity}] {self.package} {self.version}: {self.message}"


@dataclass
class SupplyChainConfig:
    """Thresholds for the checks."""

    allow_ranges: bool = False
    known_packages: frozenset[str] = DEFAULT_KNOWN
    typosquat_distance: int = 1
    min_name_length: int = 4
    scan_exclude_dirs: frozenset[str] = SKIP_DIRS
    freshness_check: Callable[[str, str], bool] | None = None


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def edit_distance(a: str, b: str) -> int:
    """Damerau-Levenshtein distance (optimal string alignment).

    Adjacent transpositions count as **one** edit, which matters here: the most
    common typosquat is a swapped pair (``reqeusts`` for ``requests``). Plain
    Levenshtein scores that as 2 and would miss it.
    """
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)

    previous2: list[int] | None = None
    previous = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        current = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            current[j] = min(
                previous[j] + 1,        # deletion
                current[j - 1] + 1,     # insertion
                previous[j - 1] + cost, # substitution
            )
            if (
                previous2 is not None
                and i > 1
                and j > 1
                and a[i - 1] == b[j - 2]
                and a[i - 2] == b[j - 1]
            ):
                current[j] = min(current[j], previous2[j - 2] + 1)  # transposition
        previous2, previous = previous, current
    return previous[-1]


#: Backwards-compatible name for the distance function.
levenshtein = edit_distance


def parse_requirement(line: str) -> tuple[str, str] | None:
    """Split a requirement line into (name, specifier). Comments are skipped."""
    text = line.strip()
    if not text or text.startswith("#") or text.startswith("-"):
        return None
    text = text.split("#", 1)[0].strip()
    if not text:
        return None
    # drop environment markers and extras
    text = text.split(";", 1)[0].strip()
    text = re.sub(r"\[[^\]]*\]", "", text)
    match = re.match(r"^([A-Za-z0-9._-]+)\s*(.*)$", text)
    if not match:
        return None
    return match.group(1), match.group(2).strip()


def normalise(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


# --------------------------------------------------------------------------
# guard
# --------------------------------------------------------------------------
class SupplyChainGuard:
    """Check requirements files and package names for supply-chain risk."""

    def __init__(self, config: SupplyChainConfig | None = None) -> None:
        self.config = config or SupplyChainConfig()

    # -- checks ------------------------------------------------------------
    def check_line(self, line: str) -> list[Finding]:
        parsed = parse_requirement(line)
        if parsed is None:
            return []
        name, spec = parsed
        findings: list[Finding] = []

        if not spec:
            findings.append(
                Finding(name, "*", "high", "unpinned", "dependency has no version pin")
            )
        elif not _PINNED.match(spec):
            severity = "high" if not self.config.allow_ranges else "low"
            findings.append(
                Finding(
                    name,
                    spec,
                    severity,
                    "range",
                    "version is a range, not an exact pin" if not self.config.allow_ranges
                    else "range allowed by configuration",
                )
            )

        typo = self._typosquat_of(name)
        if typo is not None:
            findings.append(
                Finding(
                    name,
                    spec or "*",
                    "critical",
                    "typosquat",
                    f"name is {edit_distance(normalise(name), normalise(typo))} edit(s) "
                    f"from the well-known package {typo!r}",
                )
            )

        if self.config.freshness_check and spec:
            try:
                if not self.config.freshness_check(name, spec):
                    findings.append(
                        Finding(name, spec, "medium", "stale", "pinned version looks stale")
                    )
            except Exception:  # noqa: BLE001 - a hook must never break a scan
                pass

        return findings

    def check_text(self, text: str, source: str = "<text>") -> list[Finding]:
        findings: list[Finding] = []
        for line in text.splitlines():
            for finding in self.check_line(line):
                findings.append(finding)
        return findings

    def check_requirements(self, path: str | Path) -> list[Finding]:
        return self.check_text(Path(path).read_text(encoding="utf-8"), str(path))

    def check_package_lock(self, path: str | Path) -> list[Finding]:
        """Check a simple npm-style lock/deps file that is JSON."""
        import json

        target = Path(path)
        try:
            data = json.loads(target.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return []
        findings: list[Finding] = []
        packages = {}
        if isinstance(data, Mapping):
            packages = data.get("dependencies") or data.get("packages") or {}
        for name, meta in (packages or {}).items():
            name = str(name).lstrip("/").split("node_modules/")[-1]
            version = ""
            if isinstance(meta, Mapping):
                version = str(meta.get("version") or meta.get("specifier") or "")
            if not version or version.startswith(("^", "~", ">", "<", "*")):
                findings.append(
                    Finding(name, version or "*", "high", "range", "dependency is not pinned")
                )
            typo = self._typosquat_of(name)
            if typo is not None:
                findings.append(
                    Finding(name, version or "*", "critical", "typosquat",
                            f"resembles {typo!r}")
                )
        return findings

    def scan_directory(self, root: str | Path) -> list[Finding]:
        """Walk a tree looking for requirements/lock files."""
        findings: list[Finding] = []
        base = Path(root)
        for path in base.rglob("*"):
            if not path.is_file():
                continue
            if any(part in self.config.scan_exclude_dirs for part in path.parts):
                continue
            name = path.name.lower()
            if name.startswith("requirements") and name.endswith(".txt"):
                findings.extend(self.check_requirements(path))
            elif name in ("package-lock.json", "package.json"):
                findings.extend(self.check_package_lock(path))
        return findings

    # -- internals ---------------------------------------------------------
    def _typosquat_of(self, name: str) -> str | None:
        candidate = normalise(name)
        if len(candidate) < self.config.min_name_length:
            return None
        if candidate in {normalise(k) for k in self.config.known_packages}:
            return None
        for known in self.config.known_packages:
            target = normalise(known)
            if candidate == target:
                return None
            if abs(len(candidate) - len(target)) > self.config.typosquat_distance:
                continue
            if edit_distance(candidate, target) <= self.config.typosquat_distance:
                return known
        return None


def check(path: str | Path, config: SupplyChainConfig | None = None) -> list[Finding]:
    """Convenience wrapper for one requirements file."""
    return SupplyChainGuard(config).check_requirements(path)


def scan(root: str | Path, config: SupplyChainConfig | None = None) -> list[Finding]:
    """Convenience wrapper for a directory tree."""
    return SupplyChainGuard(config).scan_directory(root)


__all__ = [
    "DEFAULT_KNOWN",
    "SKIP_DIRS",
    "Finding",
    "SupplyChainConfig",
    "SupplyChainGuard",
    "check",
    "scan",
    "edit_distance",
    "levenshtein",
    "parse_requirement",
]
