"""Scorers: deterministic checks that turn an output into a score in [0, 1].

Every scorer receives ``(output, case)`` and returns a :class:`ScorerResult`.
Deterministic scorers need no model and are safe in CI. ``ModelJudge`` wraps a
model for subjective criteria; it is clearly marked because it is not
reproducible.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from ..agent.agent import extract_json, json_schema_of
from ..core.types import Message


@dataclass
class ScorerResult:
    """One scorer's verdict."""

    score: float
    passed: bool
    detail: str = ""
    scorer: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "scorer": self.scorer,
            "score": round(self.score, 4),
            "passed": self.passed,
            "detail": self.detail,
        }


class Scorer:
    """Base class; ``threshold`` decides pass/fail from the score."""

    name = "scorer"

    def __init__(self, threshold: float = 1.0) -> None:
        self.threshold = threshold

    def score(self, output: Any, case: Any) -> tuple[float, str]:  # pragma: no cover
        raise NotImplementedError

    def __call__(self, output: Any, case: Any) -> ScorerResult:
        try:
            value, detail = self.score(output, case)
        except Exception as exc:  # noqa: BLE001 - a broken scorer is a failed case
            return ScorerResult(0.0, False, f"{type(exc).__name__}: {exc}", self.name)
        value = max(0.0, min(1.0, float(value)))
        return ScorerResult(value, value >= self.threshold, detail, self.name)


def _expected(case: Any, key: str = "expected") -> Any:
    if isinstance(case, Mapping):
        return case.get(key)
    return getattr(case, key, None)


class Exact(Scorer):
    """Normalised string equality."""

    name = "exact"

    def __init__(self, case_sensitive: bool = False, strip: bool = True, threshold: float = 1.0):
        super().__init__(threshold)
        self.case_sensitive = case_sensitive
        self.strip = strip

    def score(self, output: Any, case: Any) -> tuple[float, str]:
        expected = str(_expected(case) or "")
        actual = str(output or "")
        if self.strip:
            expected, actual = expected.strip(), actual.strip()
        if not self.case_sensitive:
            expected, actual = expected.lower(), actual.lower()
        return (1.0 if expected == actual else 0.0), f"expected {expected[:60]!r}"


class Contains(Scorer):
    """Every required substring must appear."""

    name = "contains"

    def score(self, output: Any, case: Any) -> tuple[float, str]:
        required = _expected(case) or []
        if isinstance(required, str):
            required = [required]
        text = str(output or "").lower()
        hits = [needle for needle in required if str(needle).lower() in text]
        missing = [needle for needle in required if needle not in hits]
        score = len(hits) / len(required) if required else 1.0
        return score, f"missing {missing}" if missing else "all substrings present"


class Regex(Scorer):
    """Output must match a pattern."""

    name = "regex"

    def score(self, output: Any, case: Any) -> tuple[float, str]:
        pattern = _expected(case) or ""
        match = re.search(str(pattern), str(output or ""), re.S)
        return (1.0 if match else 0.0), f"pattern {pattern!r}"


class JsonKeys(Scorer):
    """Output must be JSON containing the expected keys, with matching types."""

    name = "json"

    def score(self, output: Any, case: Any) -> tuple[float, str]:
        expected = _expected(case) or {}
        payload = output if isinstance(output, Mapping) else extract_json(str(output or ""))
        if not isinstance(payload, Mapping):
            return 0.0, "output is not a JSON object"
        # Accept three shapes: a full JSON schema, a flat {key: type-name} map,
        # or a dataclass/TypedDict whose annotations describe the shape.
        if isinstance(expected, Mapping) and "properties" in expected:
            properties = dict(expected.get("properties") or {})
        elif isinstance(expected, Mapping):
            properties = {
                key: ({"type": value} if isinstance(value, str) else dict(value))
                for key, value in expected.items()
            }
        else:
            properties = json_schema_of(expected).get("properties", {})
        checks = 0
        passed = 0
        problems: list[str] = []
        for key, spec in properties.items():
            checks += 1
            if key not in payload:
                problems.append(f"missing {key}")
                continue
            declared = spec.get("type") if isinstance(spec, Mapping) else None
            value = payload[key]
            ok = {
                "string": isinstance(value, str),
                "integer": isinstance(value, int) and not isinstance(value, bool),
                "number": isinstance(value, (int, float)),
                "boolean": isinstance(value, bool),
                "array": isinstance(value, list),
                "object": isinstance(value, Mapping),
            }.get(declared or "", True)
            if ok:
                passed += 1
            else:
                problems.append(f"{key} is not {declared}")
        score = passed / checks if checks else (1.0 if payload else 0.0)
        return score, "; ".join(problems) or "shape ok"


class NumericClose(Scorer):
    """A number embedded in the output must be within tolerance."""

    name = "numeric"

    def __init__(self, tolerance: float = 0.01, threshold: float = 1.0):
        super().__init__(threshold)
        self.tolerance = tolerance

    def score(self, output: Any, case: Any) -> tuple[float, str]:
        expected = float(_expected(case))
        numbers = [float(n) for n in re.findall(r"-?\d+(?:\.\d+)?", str(output or ""))]
        if not numbers:
            return 0.0, "no number found in output"
        best = min(numbers, key=lambda n: abs(n - expected))
        error = abs(best - expected)
        allowed = max(abs(expected) * self.tolerance, self.tolerance)
        return (1.0 if error <= allowed else 0.0), f"closest {best} (error {error:g})"


class Length(Scorer):
    """Output length within bounds."""

    name = "length"

    def __init__(self, minimum: int = 0, maximum: int = 10_000, threshold: float = 1.0):
        super().__init__(threshold)
        self.minimum = minimum
        self.maximum = maximum

    def score(self, output: Any, case: Any) -> tuple[float, str]:
        size = len(str(output or ""))
        ok = self.minimum <= size <= self.maximum
        return (1.0 if ok else 0.0), f"{size} chars (bounds {self.minimum}..{self.maximum})"


class ModelJudge(Scorer):
    """Ask a model to grade the output against criteria (not reproducible)."""

    name = "model-judge"

    def __init__(self, model: Any, criteria: str, threshold: float = 0.7) -> None:
        super().__init__(threshold)
        self.model = model
        self.criteria = criteria

    def score(self, output: Any, case: Any) -> tuple[float, str]:
        prompt = [
            Message.system(
                "You grade a candidate answer against criteria. Reply with JSON only: "
                '{"score": <0..1>, "reason": "<short>"}.'
            ),
            Message.user(
                f"Criteria: {self.criteria}\n\n"
                f"Input: {_expected(case, 'input')}\n\n"
                f"Candidate answer: {output}"
            ),
        ]
        completion = self.model.complete(prompt)
        payload = extract_json(completion.text)
        if not isinstance(payload, Mapping):
            return 0.0, "judge did not return an object"
        return float(payload.get("score", 0.0)), str(payload.get("reason", ""))


__all__ = [
    "Scorer",
    "ScorerResult",
    "Exact",
    "Contains",
    "Regex",
    "JsonKeys",
    "NumericClose",
    "Length",
    "ModelJudge",
]
