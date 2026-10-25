"""Routing primitives returned by nodes.

A node normally returns a partial state update. Returning a :class:`Command`
additionally controls where execution goes next, which lets a node decide its
own successor instead of relying on static edges.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

#: Virtual entry node.
START = "__start__"
#: Terminal sentinel; routing to it finishes the run.
END = "__end__"

_UNSET = object()


@dataclass
class Command:
    """Combine a state update with explicit routing.

    ``goto`` may be a node name, a list of node names (fan-out), or ``END``.
    ``resume`` is used by callers when continuing an interrupted run.
    """

    goto: str | list[str] | None = None
    update: dict[str, Any] = field(default_factory=dict)
    resume: Any = _UNSET

    def targets(self) -> list[str]:
        if self.goto is None:
            return []
        if isinstance(self.goto, str):
            return [self.goto]
        return [str(g) for g in self.goto]


def goto(*nodes: str, update: dict[str, Any] | None = None) -> Command:
    """Convenience constructor: ``return goto("next", update={"x": 1})``."""
    names = list(nodes)
    target: str | list[str] = names[0] if len(names) == 1 else names
    return Command(goto=target, update=update or {})


def fan_out(*nodes: str) -> list[str]:
    """Return several node names so a frontier runs them as parallel branches."""
    return list(nodes)


__all__ = ["START", "END", "Command", "goto", "fan_out"]
