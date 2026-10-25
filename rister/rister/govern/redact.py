"""Sensitive-data detection and redaction for both ends of a model call.

Prompt injection gets the headlines; leaking a customer's card number into a
trace is the incident that reaches the regulator. This module provides the
input and output processors described in OWASP LLM02 (sensitive information
disclosure):

    redactor = Redactor()
    agent = Agent("a", model=RedactingModel(model, redactor),
                  tools=[redact_tool(search, redactor)])

Detection is deterministic — regular expressions plus a Luhn checksum for card
numbers — so it is cheap, auditable and testable. It is a safety net, not a
classification model: it will not catch a name in prose, and it says so.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping

from ..core.errors import RisterError
from ..core.types import Completion, Message

#: What a redacted value is replaced with. ``{kind}`` and ``{last4}`` expand.
DEFAULT_MASK = "[REDACTED:{kind}]"


class RedactionBlocked(RisterError):
    """Raised by ``Redactor.assert_clean`` when sensitive data is present."""

    def __init__(self, findings: Iterable["Match"]) -> None:
        self.findings = list(findings)
        kinds = sorted({item.kind for item in self.findings})
        super().__init__(f"sensitive data refused: {', '.join(kinds)}")


@dataclass(frozen=True)
class Match:
    """One detected span of sensitive text."""

    kind: str
    value: str
    start: int
    end: int
    severity: str = "high"

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind,
            "severity": self.severity,
            "start": self.start,
            "end": self.end,
            "length": len(self.value),
        }

    def __str__(self) -> str:
        return f"{self.kind} at {self.start}:{self.end}"


@dataclass(frozen=True)
class Detector:
    """A named pattern that recognises one kind of sensitive data."""

    kind: str
    pattern: str
    severity: str = "high"
    description: str = ""
    group: int = 0
    checksum: str | None = None  # "luhn" to require a valid card number

    def scan(self, text: str) -> list[Match]:
        out: list[Match] = []
        for found in re.finditer(self.pattern, text):
            value = found.group(self.group)
            if not value:
                continue
            if self.checksum == "luhn" and not luhn_valid(value):
                continue
            start, end = found.span(self.group)
            out.append(Match(self.kind, value, start, end, self.severity))
        return out


def luhn_valid(number: str) -> bool:
    """Validate a card-like digit string with the Luhn checksum."""
    digits = [int(ch) for ch in number if ch.isdigit()]
    if not 12 <= len(digits) <= 19:
        return False
    checksum = 0
    for index, digit in enumerate(reversed(digits)):
        if index % 2 == 1:
            digit *= 2
            if digit > 9:
                digit -= 9
        checksum += digit
    return checksum % 10 == 0


#: The default detector set. Ordered most-specific first so that, for example,
#: a bearer token is not also reported as a generic long secret.
DETECTORS: tuple[Detector, ...] = (
    Detector(
        "private_key",
        r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----",
        "critical",
        "A private key embedded in text.",
    ),
    Detector(
        "api_key",
        r"\b(?:sk|rk|pk)-[A-Za-z0-9_\-]{16,}\b"
        r"|\bgh[pousr]_[A-Za-z0-9]{20,}\b"
        r"|\bAKIA[0-9A-Z]{16}\b"
        r"|\bxox[baprs]-[A-Za-z0-9\-]{10,}\b",
        "critical",
        "A provider-style API key.",
    ),
    Detector(
        "jwt",
        r"\beyJ[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\.[A-Za-z0-9_\-]{8,}\b",
        "critical",
        "A JSON Web Token.",
    ),
    Detector(
        "bearer_token",
        r"(?i)\bbearer\s+([A-Za-z0-9._\-]{16,})",
        "critical",
        "A bearer token in an authorization header.",
        group=1,
    ),
    Detector(
        "credit_card",
        r"\b(?:\d[ -]?){12,18}\d\b",
        "critical",
        "A payment card number (Luhn-checked).",
        checksum="luhn",
    ),
    Detector(
        "ssn",
        r"\b\d{3}-\d{2}-\d{4}\b",
        "high",
        "A US social security number.",
    ),
    Detector(
        "iban",
        r"\b[A-Z]{2}\d{2}[A-Z0-9]{10,30}\b",
        "high",
        "An international bank account number.",
    ),
    Detector(
        "email",
        r"\b[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}\b",
        "medium",
        "An email address.",
    ),
    Detector(
        "phone",
        r"(?<![\w.])(?:\+\d{1,3}[ -]?)?(?:\(\d{2,4}\)[ -]?)?\d{3}[ -]?\d{3,4}[ -]?\d{3,4}(?![\w.])",
        "medium",
        "A telephone number.",
    ),
    Detector(
        "ipv4",
        r"\b(?:(?:25[0-5]|2[0-4]\d|1?\d?\d)\.){3}(?:25[0-5]|2[0-4]\d|1?\d?\d)\b",
        "low",
        "An IPv4 address.",
    ),
)


class Redactor:
    """Find and mask sensitive data in strings, messages and tool arguments."""

    def __init__(
        self,
        detectors: Iterable[Detector] | None = None,
        *,
        mask: str = DEFAULT_MASK,
        only: Iterable[str] | None = None,
        skip: Iterable[str] | None = None,
        keep_last: int = 0,
        allowlist: Iterable[str] = (),
    ) -> None:
        chosen = list(DETECTORS if detectors is None else detectors)
        if only is not None:
            wanted = set(only)
            chosen = [d for d in chosen if d.kind in wanted]
        if skip is not None:
            unwanted = set(skip)
            chosen = [d for d in chosen if d.kind not in unwanted]
        if not chosen:
            raise RisterError("a redactor needs at least one detector")
        self.detectors = tuple(chosen)
        self.mask = mask
        self.keep_last = max(0, int(keep_last))
        self.allowlist = {value.lower() for value in allowlist}

    # -- detection ---------------------------------------------------------
    def find(self, text: str) -> list[Match]:
        """Every sensitive span, de-overlapped and ordered by position."""
        if not isinstance(text, str) or not text:
            return []
        found: list[Match] = []
        for detector in self.detectors:
            for match in detector.scan(text):
                if match.value.lower() in self.allowlist:
                    continue
                found.append(match)
        found.sort(key=lambda m: (m.start, -(m.end - m.start)))
        kept: list[Match] = []
        for match in found:
            if kept and match.start < kept[-1].end:
                continue  # overlap: the earlier, longer match wins
            kept.append(match)
        return kept

    def kinds(self, text: str) -> list[str]:
        """Sorted unique kinds present in ``text``."""
        return sorted({match.kind for match in self.find(text)})

    def clean(self, text: str) -> bool:
        return not self.find(text)

    # -- redaction ---------------------------------------------------------
    def redact(self, text: str) -> tuple[str, list[Match]]:
        """Return ``(safe_text, findings)``."""
        findings = self.find(text)
        if not findings:
            return text, []
        out: list[str] = []
        cursor = 0
        for match in findings:
            out.append(text[cursor:match.start])
            out.append(self._mask_for(match))
            cursor = match.end
        out.append(text[cursor:])
        return "".join(out), findings

    def __call__(self, text: str) -> str:
        return self.redact(text)[0]

    def _mask_for(self, match: Match) -> str:
        tail = ""
        if self.keep_last:
            digits = re.sub(r"\D", "", match.value)
            if len(digits) > self.keep_last:
                tail = digits[-self.keep_last:]
        masked = self.mask.format(kind=match.kind, last4=tail, tail=tail)
        if tail and "{last4}" not in self.mask and "{tail}" not in self.mask:
            # The default template has no slot for the tail, so append it: asking
            # to keep four digits and silently losing them would be a trap.
            masked = f"{masked}···{tail}"
        return masked

    def redact_data(self, value: Any) -> tuple[Any, list[Match]]:
        """Recursively redact strings inside dicts, lists and tuples."""
        if isinstance(value, str):
            return self.redact(value)
        if isinstance(value, Mapping):
            out: dict[Any, Any] = {}
            findings: list[Match] = []
            for key, item in value.items():
                clean, found = self.redact_data(item)
                out[key] = clean
                findings.extend(found)
            return out, findings
        if isinstance(value, (list, tuple)):
            items = []
            findings = []
            for item in value:
                clean, found = self.redact_data(item)
                items.append(clean)
                findings.extend(found)
            return type(value)(items), findings
        return value, []

    def redact_message(self, message: Message) -> Message:
        """Return a copy of ``message`` with sensitive content masked.

        The original is never mutated: a caller may still hold the raw text for
        a local audit record that is itself protected.
        """
        from ..core.types import ToolCall

        text, _ = self.redact(message.content or "")
        calls = [
            ToolCall(
                call.name,
                self.redact_data(dict(call.arguments or {}))[0],
                id=call.id,
            )
            for call in message.tool_calls
        ]
        return Message(
            role=message.role,
            content=text,
            name=message.name,
            tool_calls=calls,
            tool_call_id=message.tool_call_id,
            meta=dict(message.meta),
        )

    def assert_clean(self, text: str) -> str:
        """Return ``text`` unchanged, or raise :class:`RedactionBlocked`."""
        findings = self.find(text)
        if findings:
            raise RedactionBlocked(findings)
        return text

    def audit(self, texts: Iterable[str]) -> dict[str, Any]:
        """Summarise what a corpus contains, without storing the values."""
        counts: dict[str, int] = {}
        for text in texts:
            for kind in self.kinds(text):
                counts[kind] = counts.get(kind, 0) + 1
        return {"detectors": len(self.detectors), "kinds": dict(sorted(counts.items()))}

    def describe(self) -> dict[str, Any]:
        return {
            "detectors": [
                {"kind": d.kind, "severity": d.severity, "description": d.description}
                for d in self.detectors
            ],
            "mask": self.mask,
            "keep_last": self.keep_last,
        }


class RedactingModel:
    """Redact outbound messages and inbound completions around any model."""

    def __init__(self, inner: Any, redactor: Redactor | None = None,
                 *, redact_output: bool = True, name: str | None = None) -> None:
        if not hasattr(inner, "complete"):
            raise RisterError("RedactingModel needs a model with complete()")
        self.inner = inner
        self.redactor = redactor or Redactor()
        self.redact_output = redact_output
        self.name = name or getattr(inner, "name", "redacting")
        self.redactions = 0

    def complete(self, messages: Any, tools: Any = None, **options: Any) -> Completion:
        safe = [self.redactor.redact_message(m) for m in messages]
        completion = self.inner.complete(safe, tools, **options)
        if self.redact_output and getattr(completion, "message", None) is not None:
            clean, findings = self.redactor.redact(completion.message.content or "")
            self.redactions += len(findings)
            completion.message.content = clean
        return completion

    async def acomplete(self, messages: Any, tools: Any = None, **options: Any) -> Completion:
        safe = [self.redactor.redact_message(m) for m in messages]
        completion = await self.inner.acomplete(safe, tools, **options)
        if self.redact_output and getattr(completion, "message", None) is not None:
            clean, findings = self.redactor.redact(completion.message.content or "")
            self.redactions += len(findings)
            completion.message.content = clean
        return completion

    def __repr__(self) -> str:
        return f"<RedactingModel {self.name} redactions={self.redactions}>"


def redact_tool(tool: Any, redactor: Redactor | None = None) -> Any:
    """Wrap a tool so its arguments and result are redacted before they travel.

    The wrapper keeps the name, schema and flags, so governance still sees the
    same tool — only the values change.

    Tool objects are rebuilt with :func:`dataclasses.replace`, so this module
    never imports the agent runtime: governance stays below it in the dependency
    graph, exactly like the guard, which accepts any object that has a ``fn``.
    """
    redactor = redactor or Redactor()
    is_tool = hasattr(tool, "fn") and hasattr(tool, "parameters") and hasattr(tool, "name")
    inner_fn: Callable[..., Any] = tool.fn if is_tool else tool

    def wrapped(**arguments: Any) -> Any:
        clean_args, _ = redactor.redact_data(arguments)
        value = inner_fn(**clean_args)  # type: ignore[arg-type]
        clean_value, _ = redactor.redact_data(value)
        return clean_value

    if is_tool:
        from dataclasses import replace

        return replace(tool, fn=wrapped)
    wrapped.__name__ = getattr(tool, "__name__", "tool")
    return wrapped


__all__ = [
    "Detector",
    "Match",
    "Redactor",
    "RedactingModel",
    "RedactionBlocked",
    "redact_tool",
    "luhn_valid",
    "DETECTORS",
    "DEFAULT_MASK",
]
