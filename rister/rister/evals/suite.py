"""Evaluation suites: cases, a target, and scored results."""
from __future__ import annotations

import json
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..core.errors import EvalError
from .scorers import Scorer, ScorerResult


@dataclass
class Case:
    """One evaluation input with expectations."""

    input: str
    expected: Any = None
    name: str = ""
    metadata: dict[str, Any] = field(default_factory=dict)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], index: int = 0) -> "Case":
        return cls(
            input=str(data.get("input", "")),
            expected=data.get("expected"),
            name=str(data.get("name") or f"case{index}"),
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "input": self.input,
            "expected": self.expected,
            "metadata": self.metadata,
        }


@dataclass
class CaseResult:
    """Outcome for one case."""

    case: Case
    output: Any
    scores: list[ScorerResult] = field(default_factory=list)
    duration_ms: float = 0.0
    error: str | None = None

    @property
    def passed(self) -> bool:
        if self.error:
            return False
        return bool(self.scores) and all(s.passed for s in self.scores)

    @property
    def score(self) -> float:
        if not self.scores:
            return 0.0
        return sum(s.score for s in self.scores) / len(self.scores)

    def to_dict(self) -> dict[str, Any]:
        return {
            "case": self.case.name or self.case.input[:40],
            "input": self.case.input,
            "output": self.output,
            "expected": self.case.expected,
            "passed": self.passed,
            "score": round(self.score, 4),
            "duration_ms": self.duration_ms,
            "error": self.error,
            "scores": [s.to_dict() for s in self.scores],
        }


@dataclass
class Report:
    """Aggregate outcome of a suite."""

    suite: str
    results: list[CaseResult] = field(default_factory=list)
    started: float = field(default_factory=time.time)
    finished: float = 0.0

    # -- aggregates --------------------------------------------------------
    @property
    def total(self) -> int:
        return len(self.results)

    @property
    def passed(self) -> int:
        return sum(1 for r in self.results if r.passed)

    @property
    def failed(self) -> int:
        return self.total - self.passed

    @property
    def pass_rate(self) -> float:
        return round(self.passed / self.total, 4) if self.total else 0.0

    @property
    def mean_score(self) -> float:
        return round(sum(r.score for r in self.results) / self.total, 4) if self.total else 0.0

    @property
    def duration_ms(self) -> float:
        end = self.finished or time.time()
        return round((end - self.started) * 1000, 2)

    def failures(self) -> list[CaseResult]:
        return [r for r in self.results if not r.passed]

    def to_dict(self) -> dict[str, Any]:
        return {
            "suite": self.suite,
            "total": self.total,
            "passed": self.passed,
            "failed": self.failed,
            "pass_rate": self.pass_rate,
            "mean_score": self.mean_score,
            "duration_ms": self.duration_ms,
            "results": [r.to_dict() for r in self.results],
        }

    def to_json(self, indent: int = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, sort_keys=True)

    def to_markdown(self) -> str:
        lines = [
            f"# Eval report: {self.suite}",
            "",
            f"- Cases: **{self.total}**",
            f"- Passed: **{self.passed}** ({self.pass_rate:.0%})",
            f"- Mean score: **{self.mean_score:.3f}**",
            f"- Duration: {self.duration_ms} ms",
            "",
            "| Case | Passed | Score | Detail |",
            "|---|---|---|---|",
        ]
        for result in self.results:
            detail = result.error or "; ".join(
                s.detail for s in result.scores if not s.passed
            ) or "ok"
            lines.append(
                f"| {result.case.name or result.case.input[:30]} | "
                f"{'yes' if result.passed else 'no'} | {result.score:.2f} | {detail[:80]} |"
            )
        return "\n".join(lines)

    def summary(self) -> str:
        return (
            f"{self.passed}/{self.total} passed ({self.pass_rate:.0%}), "
            f"mean score {self.mean_score:.3f}, {self.duration_ms}ms"
        )


class Suite:
    """A named set of cases scored by one or more scorers."""

    def __init__(
        self,
        name: str,
        cases: Iterable[Case | Mapping[str, Any]],
        scorers: Sequence[Scorer],
        target: Callable[[str], Any] | Any | None = None,
    ) -> None:
        self.name = name
        self.cases = [
            Case.from_dict(case, index) if isinstance(case, Mapping) else case
            for index, case in enumerate(cases)
        ]
        self.scorers = list(scorers)
        self.target = target
        if not self.scorers:
            raise EvalError("a suite needs at least one scorer")

    # -- data --------------------------------------------------------------
    @classmethod
    def load(
        cls,
        path: str | Path,
        scorers: Sequence[Scorer],
        target: Callable[[str], Any] | Any | None = None,
    ) -> "Suite":
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, Mapping):
            name = data.get("name") or Path(path).stem
            cases = data.get("cases") or []
        else:
            name, cases = Path(path).stem, data
        return cls(name, cases, scorers, target)

    # -- execution ---------------------------------------------------------
    def _call_target(self, text: str) -> Any:
        if self.target is None:
            raise EvalError("suite has no target; pass a callable or an agent")
        if callable(self.target) and not hasattr(self.target, "run"):
            return self.target(text)
        return self.target.run(text)

    def run(self, target: Callable[[str], Any] | Any | None = None) -> Report:
        chosen = target or self.target
        if chosen is None:
            raise EvalError("suite has no target; pass a callable or an agent")
        previous, self.target = self.target, chosen
        report = Report(suite=self.name)
        try:
            for case in self.cases:
                started = time.perf_counter()
                output: Any = None
                error: str | None = None
                scores: list[ScorerResult] = []
                try:
                    output = self._call_target(case.input)
                    if hasattr(output, "text"):
                        output = output.text
                    if hasattr(output, "data") and getattr(output, "data") is not None:
                        output = output.data
                except Exception as exc:  # noqa: BLE001 - recorded per case
                    error = f"{type(exc).__name__}: {exc}"
                if error is None:
                    scores = [scorer(output, case) for scorer in self.scorers]
                report.results.append(
                    CaseResult(
                        case=case,
                        output=output,
                        scores=scores,
                        duration_ms=round((time.perf_counter() - started) * 1000, 2),
                        error=error,
                    )
                )
        finally:
            self.target = previous
        report.finished = time.time()
        return report


def evaluate(
    cases: Iterable[Case | Mapping[str, Any]],
    scorers: Sequence[Scorer],
    target: Callable[[str], Any] | Any,
    name: str = "eval",
) -> Report:
    """One-shot helper: ``rister.evaluate(cases, [Exact()], agent)``."""
    return Suite(name, cases, scorers, target).run()


__all__ = ["Case", "CaseResult", "Suite", "Report", "evaluate"]
