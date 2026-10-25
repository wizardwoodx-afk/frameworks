"""Prompt composition.

Instructions are just data here: a string, a :class:`Prompt` template, a
callable, or a list of fragments that are joined deterministically. Keeping
this separate from the agent makes prompts testable and versionable.
"""
from __future__ import annotations

import string
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence


class _Keep(dict):
    """``format_map`` helper that leaves unknown placeholders untouched."""

    def __missing__(self, key: str) -> str:
        return "{" + key + "}"


@dataclass
class Prompt:
    """A ``{placeholder}`` template with named defaults."""

    template: str
    defaults: dict[str, Any] = field(default_factory=dict)

    def render(self, **values: Any) -> str:
        merged = {**self.defaults, **values}
        return string.Formatter().vformat(self.template, (), _Keep(merged))

    def __call__(self, **values: Any) -> str:
        return self.render(**values)

    def __str__(self) -> str:
        return self.template

    @classmethod
    def join(cls, *parts: "str | Prompt", separator: str = "\n\n") -> "Prompt":
        """Concatenate fragments, keeping placeholders intact."""
        templates = [p.template if isinstance(p, Prompt) else str(p) for p in parts]
        defaults: dict[str, Any] = {}
        for part in parts:
            if isinstance(part, Prompt):
                defaults.update(part.defaults)
        return cls(separator.join(t for t in templates if t.strip()), defaults)


InstructionLike = str | Prompt | Callable[..., Any] | Sequence[Any] | None


def render_instructions(instructions: InstructionLike, **context: Any) -> str:
    """Normalise any supported instruction shape into a single string."""
    if instructions is None:
        return ""
    if isinstance(instructions, Prompt):
        return instructions.render(**context)
    if isinstance(instructions, str):
        return instructions
    if callable(instructions):
        return str(instructions(**context) or "")
    if isinstance(instructions, (list, tuple)):
        rendered = [render_instructions(item, **context) for item in instructions]
        return "\n\n".join(part for part in rendered if part.strip())
    return str(instructions)


def describe_tools(specs: Iterable[Mapping[str, Any]]) -> str:
    """Render a tool catalogue for models that lack native tool calling.

    The model is asked to reply with a single JSON object describing the call,
    which :meth:`rister.agent.agent.Agent` understands.
    """
    lines = ["Available tools:"]
    for spec in specs:
        params = spec.get("parameters", {}).get("properties", {})
        required = set(spec.get("parameters", {}).get("required", []))
        args = ", ".join(
            f"{name}: {schema.get('type', 'any')}"
            + ("" if name in required else " (optional)")
            for name, schema in params.items()
        )
        lines.append(f"- {spec['name']}({args}): {spec.get('description', '')}".strip())
    lines.append("")
    lines.append(
        "To call a tool, reply with ONLY a JSON object of the form "
        '{"tool": "<name>", "arguments": {...}}. '
        'When you are done, reply with {"final": "<answer>"} or plain text.'
    )
    return "\n".join(lines)


__all__ = ["Prompt", "render_instructions", "describe_tools", "InstructionLike"]
