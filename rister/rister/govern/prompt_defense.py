"""Static analysis of system prompts against 12 injection vectors.

This evaluates the **defensive language present in a system prompt**: it is a
deterministic linter, not a runtime classifier of user input. Same input, same
report; no model calls, no network. Useful in CI to stop a prompt from losing
its guardrails during an edit.
"""
from __future__ import annotations

import hashlib
import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Sequence

MAX_PROMPT_LENGTH = 100_000


# --------------------------------------------------------------------------
# vectors
# --------------------------------------------------------------------------
@dataclass(frozen=True)
class Vector:
    """One defensive property a prompt should exhibit."""

    id: str
    name: str
    owasp: str
    severity: str
    weight: int
    patterns: tuple[str, ...]

    def compiled(self) -> tuple[re.Pattern[str], ...]:
        return tuple(re.compile(p, re.I) for p in self.patterns)


VECTORS: tuple[Vector, ...] = (
    Vector(
        "instruction-override", "Instruction override resistance", "ASI01/LLM01",
        "high", 12,
        (r"ignore (any|all) (previous|prior|earlier|other) instructions",
         r"do not (follow|obey) instructions (that|which) (appear|come)",
         r"instructions (in|from) (user|tool|retrieved) (content|data|output)"),
    ),
    Vector(
        "data-leakage", "Secret and prompt confidentiality", "LLM02/LLM06",
        "critical", 14,
        (r"never (reveal|disclose|repeat) (the )?(system prompt|instructions)",
         r"do not (reveal|share|disclose).*(secret|credential|api key|token|password)",
         r"(api keys?|credentials|passwords?).*(never|must not|do not)"),
    ),
    Vector(
        "role-escape", "Role and persona anchoring", "ASI01",
        "high", 10,
        (r"you are (a|an|the) ", r"stay in (your )?role",
         r"do not (change|switch|adopt) (your )?(role|persona)", r"remain "),
    ),
    Vector(
        "indirect-injection", "Indirect / retrieved content handling", "LLM01/ASI02",
        "critical", 13,
        (r"treat .*(as|like) (untrusted|data|content)", r"untrusted (data|content|input)",
         r"tool (output|results?) .*(may|can|could) (contain|include)",
         r"ignore instructions (inside|within|embedded)"),
    ),
    Vector(
        "output-manipulation", "Output shaping rules", "LLM05",
        "medium", 8,
        (r"respond (with|in) (json|markdown|plain text)",
         r"do not (output|include|emit) (code|html|scripts)",
         r"(exactly|only) (one|a single) (object|json|answer)"),
    ),
    Vector(
        "social-engineering", "Social engineering resistance", "LLM09/ASI03",
        "medium", 8,
        (r"(verify|confirm) (the )?(identity|authorisation|permission)",
         r"(urgency|emergency|immediately).*(verify|confirm|check)",
         r"do not (trust|act on) (claims|assertions) of authority"),
    ),
    Vector(
        "output-weaponization", "Harmful output refusal", "LLM05/LLM08",
        "high", 10,
        (r"refuse .*(harm|weapon|exploit|malware|illegal)",
         r"do not (provide|give|help with) .*(harm|weapon|exploit)",
         r"(never|must not) (assist|help) .*(attack|abuse)"),
    ),
    Vector(
        "abuse-prevention", "Use-policy limits", "LLM04/ASI09",
        "medium", 7,
        (r"only (use|operate) .*(approved|allowed|authorised)",
         r"(prohibited|not permitted|disallowed)",
         r"if .*(outside|beyond) .*scope"),
    ),
    Vector(
        "input-validation", "Input validation guidance", "LLM03/ASI04",
        "high", 9,
        (r"validate .*(input|argument|parameter)",
         r"reject .*(invalid|malformed|unexpected)",
         r"(schema|whitelist|allow-?list)"),
    ),
    Vector(
        "multilang-bypass", "Cross-language coverage", "LLM01",
        "medium", 6,
        (r"(regardless|irrespective) of (the )?(language|script)",
         r"(any|all) (language|locale|script)"),
    ),
    Vector(
        "unicode-attack", "Unicode / homoglyph handling", "LLM01",
        "low", 5,
        (r"(unicode|homoglyph|confusable)", r"normalise|normalize"),
    ),
    Vector(
        "context-overflow", "Context length discipline", "LLM10/ASI06",
        "low", 5,
        (r"(max|maximum) (length|tokens?)",
         r"(truncate|summarise|summarize) .*(context|history)",
         r"(limit|bound) .*(size|length)"),
    ),
)

_BY_ID = {v.id: v for v in VECTORS}
_GRADES = ((90, "A"), (80, "B"), (70, "C"), (50, "D"), (30, "E"), (0, "F"))
_GRADE_ORDER = {g: i for i, g in enumerate("ABCDEF")}


def grade_for(score: int) -> str:
    for threshold, grade in _GRADES:
        if score >= threshold:
            return grade
    return "F"


# --------------------------------------------------------------------------
# report
# --------------------------------------------------------------------------
@dataclass
class Finding:
    """Result for one vector."""

    vector_id: str
    name: str
    owasp: str
    severity: str
    defended: bool
    matched: int
    weight: int
    evidence: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "vector": self.vector_id,
            "name": self.name,
            "owasp": self.owasp,
            "severity": self.severity,
            "defended": self.defended,
            "matched": self.matched,
            "weight": self.weight,
            "evidence": self.evidence,
        }


@dataclass
class Report:
    """The audit result for one system prompt."""

    score: int
    grade: str
    total: int
    findings: list[Finding] = field(default_factory=list)
    prompt_hash: str = ""
    evaluated_at: float = field(default_factory=time.time)
    min_grade: str = "C"

    # -- views -------------------------------------------------------------
    @property
    def missing(self) -> list[str]:
        return [f.vector_id for f in self.findings if not f.defended]

    @property
    def defended(self) -> list[str]:
        return [f.vector_id for f in self.findings if f.defended]

    @property
    def coverage(self) -> float:
        weights = sum(f.weight for f in self.findings) or 1
        earned = sum(f.weight for f in self.findings if f.defended)
        return round(earned / weights, 4)

    def is_blocking(self, min_grade: str | None = None) -> bool:
        threshold = min_grade or self.min_grade
        return _GRADE_ORDER.get(self.grade, 5) > _GRADE_ORDER.get(threshold, 2)

    def worst(self) -> list[Finding]:
        return sorted(
            (f for f in self.findings if not f.defended),
            key=lambda f: -f.weight,
        )

    # -- serialisation -----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "score": self.score,
            "grade": self.grade,
            "coverage": self.coverage,
            "total": self.total,
            "missing": self.missing,
            "prompt_hash": self.prompt_hash,
            "evaluated_at": self.evaluated_at,
            "findings": [f.to_dict() for f in self.findings],
        }

    def to_json(self) -> str:
        import json

        return json.dumps(self.to_dict(), indent=2, sort_keys=True)

    def summary(self) -> str:
        lines = [
            f"grade {self.grade} ({self.score}/100), {self.total - len(self.missing)}/{self.total} vectors defended",
        ]
        for finding in self.worst():
            lines.append(f"  missing {finding.vector_id} [{finding.severity}] {finding.name}")
        return "\n".join(lines)


# --------------------------------------------------------------------------
# evaluator
# --------------------------------------------------------------------------
class PromptDefense:
    """Score system prompts against the twelve vectors."""

    def __init__(
        self,
        vectors: Sequence[str] | None = None,
        min_grade: str = "C",
    ) -> None:
        self.min_grade = min_grade
        selected = VECTORS if vectors is None else tuple(_BY_ID[v] for v in vectors)
        self._rules = [(v, v.compiled()) for v in selected]

    def evaluate(self, prompt: str) -> Report:
        if prompt is None:
            prompt = ""
        if len(prompt) > MAX_PROMPT_LENGTH:
            raise ValueError(
                f"prompt length {len(prompt)} exceeds {MAX_PROMPT_LENGTH} (ReDoS guard)"
            )
        findings: list[Finding] = []
        for vector, patterns in self._rules:
            matches = [p for p in patterns if p.search(prompt)]
            evidence = ""
            if matches:
                found = matches[0].search(prompt)
                if found:
                    start = max(0, found.start() - 30)
                    evidence = prompt[start : found.end() + 30].replace("\n", " ").strip()
            findings.append(
                Finding(
                    vector_id=vector.id,
                    name=vector.name,
                    owasp=vector.owasp,
                    severity=vector.severity,
                    defended=bool(matches),
                    matched=len(matches),
                    weight=vector.weight,
                    evidence=evidence,
                )
            )
        total_weight = sum(f.weight for f in findings) or 1
        earned = sum(f.weight for f in findings if f.defended)
        score = int(round(100 * earned / total_weight))
        return Report(
            score=score,
            grade=grade_for(score),
            total=len(findings),
            findings=findings,
            prompt_hash=hashlib.sha256(prompt.encode("utf-8")).hexdigest(),
            min_grade=self.min_grade,
        )

    def evaluate_file(self, path: str | Path) -> Report:
        return self.evaluate(Path(path).read_text(encoding="utf-8"))

    def evaluate_batch(self, prompts: Iterable[str]) -> list[Report]:
        return [self.evaluate(p) for p in prompts]

    def to_audit_entry(self, report: Report, agent_id: str) -> dict[str, Any]:
        """Shape a report for an audit log record."""
        return {
            "action": "prompt_defense",
            "effect": "deny" if report.is_blocking() else "allow",
            "actor": agent_id,
            "reason": f"prompt grade {report.grade} (score {report.score})",
            "metadata": {
                "grade": report.grade,
                "score": report.score,
                "missing": report.missing,
                "prompt_hash": report.prompt_hash,
            },
        }


def evaluate(prompt: str, vectors: Sequence[str] | None = None) -> Report:
    """Convenience wrapper: ``report = rister.govern.evaluate(prompt)``."""
    return PromptDefense(vectors=vectors).evaluate(prompt)


__all__ = [
    "Vector",
    "VECTORS",
    "Finding",
    "Report",
    "PromptDefense",
    "evaluate",
    "grade_for",
    "MAX_PROMPT_LENGTH",
]
