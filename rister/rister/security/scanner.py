"""Repository scanner.

Walks a tree, applies the enabled rules line by line, and produces a report that
can be rendered as JSON, SARIF, markdown or CSV. Findings are fingerprinted so
they stay stable across runs, which is what makes them usable for baselining and
for de-duplicating in a code-scanning dashboard.
"""
from __future__ import annotations

import csv
import hashlib
import io
import json
import os
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

from ..core.types import new_id
from .rules import RULES, Rule, get_rules, severity_at_least

DEFAULT_SKIP_DIRS = frozenset(
    {".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build",
     ".mypy_cache", ".pytest_cache", ".ruff_cache", ".tox", "site-packages",
     ".next", ".turbo", "coverage", "vendor"}
)

SCAN_EXTENSIONS = (
    ".py", ".js", ".mjs", ".cjs", ".jsx", ".ts", ".tsx", ".json", ".yml", ".yaml",
    ".toml", ".ini", ".env", ".sh", ".bash", ".go", ".rs", ".cs", ".java", ".rb",
    ".php", ".md",
)


_SUPPRESS = re.compile(r"rister:ignore(-file)?(?:\s+([A-Za-z0-9,\s]+))?")


def parse_suppressions(text: str) -> tuple[set[str], dict[int, set[str] | None]]:
    """Read inline suppression directives.

    ``# rister:ignore SEC001,SEC002`` suppresses those rules on the directive's
    line **and the following line**; a bare ``rister:ignore`` suppresses every
    rule there. ``rister:ignore-file`` in the first ten lines disables the whole
    file. Returns ``(file_rules, {line: rules_or_None})`` where ``None`` means
    "all rules".
    """
    file_rules: set[str] = set()
    file_all = False
    lines: dict[int, set[str] | None] = {}
    for index, line in enumerate(text.splitlines()[:10_000], start=1):
        match = _SUPPRESS.search(line)
        if not match:
            continue
        is_file, listed = match.group(1), match.group(2)
        rules = {r.strip().upper() for r in (listed or "").split(",") if r.strip()}
        if is_file and index <= 10:
            if rules:
                file_rules |= rules
            else:
                file_all = True
            continue
        for target in (index, index + 1):
            if rules:
                lines[target] = (lines.get(target) or set()) | rules
            else:
                lines[target] = None
    if file_all:
        return {"*"}, {}
    return file_rules, lines


@dataclass
class Finding:
    """One rule hit at one location."""

    rule: str
    severity: str
    title: str
    message: str
    path: str
    line: int
    column: int = 1
    snippet: str = ""
    cwe: str = ""
    fix: str = ""
    confidence: str = "medium"
    id: str = field(default_factory=lambda: new_id("find"))
    fingerprint: str = ""

    def __post_init__(self) -> None:
        if not self.fingerprint:
            basis = f"{self.rule}|{self.path}|{self.line}|{self.snippet.strip()}"
            self.fingerprint = hashlib.sha256(basis.encode("utf-8")).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "fingerprint": self.fingerprint,
            "rule": self.rule,
            "severity": self.severity,
            "title": self.title,
            "message": self.message,
            "path": self.path,
            "line": self.line,
            "column": self.column,
            "snippet": self.snippet,
            "cwe": self.cwe,
            "fix": self.fix,
            "confidence": self.confidence,
        }

    def __str__(self) -> str:
        return f"{self.path}:{self.line} [{self.severity}] {self.rule} {self.title}"


@dataclass
class Suppression:
    """A finding that an inline directive deliberately silenced.

    Suppressions are recorded rather than dropped: "we chose not to fix this" is a
    governance decision and must stay visible in the report.
    """

    path: str
    line: int
    rule: str
    severity: str
    title: str
    directive: str = "line"

    def to_dict(self) -> dict[str, Any]:
        return {
            "path": self.path,
            "line": self.line,
            "rule": self.rule,
            "severity": self.severity,
            "title": self.title,
            "directive": self.directive,
        }

    def __str__(self) -> str:
        where = "file" if self.directive == "file" else "inline"
        return f"{self.path}:{self.line} {self.rule} ({self.severity}) via {where} directive"


@dataclass
class ScanReport:
    """Result of scanning a tree."""

    root: str = ""
    findings: list[Finding] = field(default_factory=list)
    files_scanned: int = 0
    files_skipped: int = 0
    bytes_scanned: int = 0
    started: float = field(default_factory=time.time)
    finished: float = 0.0
    rules_used: list[str] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    suppressions: list[Suppression] = field(default_factory=list)

    # -- views -------------------------------------------------------------
    @property
    def suppressed(self) -> int:
        """How many findings were silenced by inline directives."""
        return len(self.suppressions)

    @property
    def duration_ms(self) -> float:
        end = self.finished or time.time()
        return round((end - self.started) * 1000, 2)

    def counts(self) -> dict[str, int]:
        out = {name: 0 for name in ("critical", "high", "medium", "low", "info")}
        for finding in self.findings:
            out[finding.severity] = out.get(finding.severity, 0) + 1
        return out

    def has_at_or_above(self, severity: str) -> bool:
        return any(severity_at_least(f.severity, severity) for f in self.findings)

    def by_severity(self) -> list[Finding]:
        order = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 4}
        return sorted(self.findings, key=lambda f: (order.get(f.severity, 9), f.path, f.line))

    def for_path(self, path: str) -> list[Finding]:
        return [f for f in self.findings if f.path == path]

    def summary(self) -> str:
        counts = self.counts()
        parts = ", ".join(f"{k} {v}" for k, v in counts.items() if v)
        return (
            f"{len(self.findings)} finding(s) in {self.files_scanned} file(s) "
            f"({parts or 'none'}) — {self.duration_ms}ms"
        )

    # -- serialisation -----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "tool": "rister",
            "root": self.root,
            "files_scanned": self.files_scanned,
            "files_skipped": self.files_skipped,
            "bytes_scanned": self.bytes_scanned,
            "duration_ms": self.duration_ms,
            "counts": self.counts(),
            "findings": [f.to_dict() for f in self.by_severity()],
            "rules_used": self.rules_used,
            "suppressed": self.suppressed,
            "suppressions": [item.to_dict() for item in self.suppressions],
            "coverage": self.coverage,
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    def to_sarif(self, tool_uri: str = "https://example.invalid/rister") -> dict[str, Any]:
        """SARIF 2.1.0, ready for GitHub code scanning."""
        rules = {f.rule for f in self.findings}
        rule_meta = [r for r in RULES if r.id in rules]
        return {
            "$schema": "https://json.schemastore.org/sarif-2.1.0.json",
            "version": "2.1.0",
            "runs": [
                {
                    "tool": {
                        "driver": {
                            "name": "rister",
                            "informationUri": tool_uri,
                            "rules": [
                                {
                                    "id": rule.id,
                                    "name": rule.title,
                                    "shortDescription": {"text": rule.title},
                                    "fullDescription": {"text": rule.message},
                                    "help": {"text": rule.fix or rule.message},
                                    "properties": {
                                        "severity": rule.severity,
                                        "cwe": rule.cwe,
                                        "tags": list(rule.tags),
                                    },
                                }
                                for rule in rule_meta
                            ],
                        }
                    },
                    "results": [
                        {
                            "ruleId": finding.rule,
                            "level": {
                                "critical": "error",
                                "high": "error",
                                "medium": "warning",
                                "low": "note",
                                "info": "note",
                            }.get(finding.severity, "warning"),
                            "message": {"text": f"{finding.title}: {finding.message}"},
                            "partialFingerprints": {
                                "rister/v1": finding.fingerprint
                            },
                            "locations": [
                                {
                                    "physicalLocation": {
                                        "artifactLocation": {"uri": finding.path},
                                        "region": {
                                            "startLine": finding.line,
                                            "startColumn": finding.column,
                                            "snippet": {"text": finding.snippet},
                                        },
                                    }
                                }
                            ],
                            "properties": {
                                "severity": finding.severity,
                                "cwe": finding.cwe,
                                "confidence": finding.confidence,
                            },
                        }
                        for finding in self.by_severity()
                    ],
                }
            ],
        }

    def to_sarif_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_sarif(), indent=indent, sort_keys=True)

    def to_markdown(self) -> str:
        counts = self.counts()
        lines = [
            "# Security scan report",
            "",
            f"- Root: `{self.root}`",
            f"- Files scanned: **{self.files_scanned}** (skipped {self.files_skipped})",
            f"- Duration: {self.duration_ms} ms",
            f"- Findings: " + ", ".join(f"{k} {v}" for k, v in counts.items() if v) or "none",
            "",
        ]
        if not self.findings:
            lines.append("No findings. This is not proof the code is secure — review coverage.")
            return "\n".join(lines)

        lines += ["| Severity | Rule | Location | Issue |", "|---|---|---|---|"]
        for finding in self.by_severity():
            lines.append(
                f"| {finding.severity} | {finding.rule} | `{finding.path}:{finding.line}` "
                f"| {finding.title} |"
            )
        lines += ["", "## Details", ""]
        for finding in self.by_severity():
            lines += [
                f"### {finding.rule} — {finding.title} ({finding.severity})",
                "",
                f"- Location: `{finding.path}:{finding.line}` ({finding.cwe or 'no CWE'})",
                f"- Issue: {finding.message}",
            ]
            if finding.fix:
                lines.append(f"- Fix: {finding.fix}")
            if finding.snippet:
                lines += ["", "```", finding.snippet, "```"]
            lines.append("")
        return "\n".join(lines)

    def to_csv(self) -> str:
        buffer = io.StringIO()
        writer = csv.DictWriter(
            buffer,
            fieldnames=["severity", "rule", "title", "path", "line", "cwe", "fingerprint"],
        )
        writer.writeheader()
        for finding in self.by_severity():
            writer.writerow(
                {
                    "severity": finding.severity,
                    "rule": finding.rule,
                    "title": finding.title,
                    "path": finding.path,
                    "line": finding.line,
                    "cwe": finding.cwe,
                    "fingerprint": finding.fingerprint,
                }
            )
        return buffer.getvalue()

    def save(self, path: str | os.PathLike[str], fmt: str | None = None) -> Path:
        target = Path(path)
        chosen = (fmt or target.suffix.lstrip(".")).lower()
        if chosen in ("sarif", "sarif.json"):
            target.write_text(self.to_sarif_json(), encoding="utf-8")
        elif chosen == "json":
            target.write_text(self.to_json(), encoding="utf-8")
        elif chosen in ("md", "markdown"):
            target.write_text(self.to_markdown(), encoding="utf-8")
        elif chosen == "csv":
            target.write_text(self.to_csv(), encoding="utf-8")
        else:
            raise ValueError(f"unknown report format {chosen!r}; use json/sarif/md/csv")
        return target


class Scanner:
    """Apply rules to files or directories."""

    def __init__(
        self,
        rules: Sequence[Rule] | None = None,
        *,
        skip_dirs: Iterable[str] = DEFAULT_SKIP_DIRS,
        extensions: Sequence[str] | None = None,
        max_file_bytes: int = 2_000_000,
        min_severity: str = "info",
    ) -> None:
        self.rules = list(rules if rules is not None else get_rules(min_severity=min_severity))
        self.skip_dirs = frozenset(skip_dirs)
        self.extensions = tuple(extensions) if extensions else SCAN_EXTENSIONS
        self.max_file_bytes = max_file_bytes

    # -- scanning ----------------------------------------------------------
    def scan_text(self, text: str, path: str = "<text>") -> list[Finding]:
        """Scan one source string, returning the findings that were not suppressed."""
        findings, _ = self._scan(text, path)
        return findings

    def scan_text_with_suppressions(
        self, text: str, path: str = "<text>"
    ) -> tuple[list[Finding], list[Suppression]]:
        """Scan one source string, returning both live and suppressed findings."""
        return self._scan(text, path)

    def _scan(self, text: str, path: str) -> tuple[list[Finding], list[Suppression]]:
        findings: list[Finding] = []
        suffix = Path(path).suffix.lower()
        lines = text.splitlines()
        for rule in self.rules:
            if not rule.enabled or not rule.applies_to(suffix):
                continue
            for pattern in rule.compiled():
                for match in pattern.finditer(text):
                    line_no = text.count("\n", 0, match.start()) + 1
                    column = match.start() - (text.rfind("\n", 0, match.start()) + 1) + 1
                    snippet = lines[line_no - 1].strip() if line_no - 1 < len(lines) else ""
                    findings.append(
                        Finding(
                            rule=rule.id,
                            severity=rule.severity,
                            title=rule.title,
                            message=rule.message,
                            path=path,
                            line=line_no,
                            column=column,
                            snippet=snippet[:400],
                            cwe=rule.cwe,
                            fix=rule.fix,
                        )
                    )
        # apply inline suppressions, then de-duplicate
        file_rules, line_rules = parse_suppressions(text)
        seen: set[tuple[str, str, int]] = set()
        unique: list[Finding] = []
        silenced: list[Suppression] = []

        def _record(finding: Finding, directive: str) -> None:
            silenced.append(
                Suppression(
                    path=finding.path,
                    line=finding.line,
                    rule=finding.rule,
                    severity=finding.severity,
                    title=finding.title,
                    directive=directive,
                )
            )

        for finding in findings:
            if "*" in file_rules or finding.rule in file_rules:
                _record(finding, "file")
                continue
            if finding.line in line_rules:
                allowed = line_rules[finding.line]
                if allowed is None or finding.rule in allowed:
                    _record(finding, "line")
                    continue
            key = (finding.rule, finding.path, finding.line)
            if key in seen:
                continue
            seen.add(key)
            unique.append(finding)
        return unique, silenced

    def scan_file(self, path: str | os.PathLike[str]) -> list[Finding]:
        """Scan one file, returning findings that were not suppressed."""
        findings, _ = self._scan_file(path)
        return findings

    def _scan_file(
        self, path: str | os.PathLike[str]
    ) -> tuple[list[Finding], list[Suppression]]:
        target = Path(path)
        try:
            if target.stat().st_size > self.max_file_bytes:
                return [], []
            text = target.read_text(encoding="utf-8", errors="replace")
        except OSError:
            return [], []
        return self._scan(text, str(target))

    def scan(self, root: str | os.PathLike[str]) -> ScanReport:
        base = Path(root).expanduser().resolve()
        report = ScanReport(root=str(base), rules_used=[r.id for r in self.rules])
        if base.is_file():
            findings, silenced = self._scan_file(base)
            report.findings.extend(findings)
            report.suppressions.extend(silenced)
            report.files_scanned = 1
            report.finished = time.time()
            return report

        for current, dirnames, filenames in os.walk(base):
            dirnames[:] = sorted(d for d in dirnames if d not in self.skip_dirs)
            for name in sorted(filenames):
                path = Path(current) / name
                if path.suffix.lower() not in self.extensions:
                    report.files_skipped += 1
                    continue
                try:
                    size = path.stat().st_size
                except OSError:
                    report.files_skipped += 1
                    continue
                if size > self.max_file_bytes:
                    report.files_skipped += 1
                    continue
                try:
                    text = path.read_text(encoding="utf-8", errors="replace")
                except OSError:
                    report.files_skipped += 1
                    continue
                report.files_scanned += 1
                report.bytes_scanned += size
                relative = str(path.relative_to(base))
                findings, silenced = self._scan(text, relative)
                report.findings.extend(findings)
                report.suppressions.extend(silenced)

        report.finished = time.time()
        report.coverage = {
            "extensions": list(self.extensions),
            "skipped_dirs": sorted(self.skip_dirs),
            "max_file_bytes": self.max_file_bytes,
            "note": (
                "Heuristic pattern coverage only: absence of findings does not "
                "establish the absence of vulnerabilities."
            ),
        }
        return report


def scan(root: str | os.PathLike[str], **kwargs: Any) -> ScanReport:
    """Convenience wrapper: ``rister.scan(".")``."""
    return Scanner(**kwargs).scan(root)


__all__ = [
    "Finding",
    "Suppression",
    "ScanReport",
    "Scanner",
    "scan",
    "DEFAULT_SKIP_DIRS",
]
