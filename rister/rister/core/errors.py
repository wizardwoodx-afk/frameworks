"""RISTER exception hierarchy.

Every error raised by the toolkit derives from :class:`RisterError`, so an
application can guard an entire run with a single ``except`` clause while still
being able to react to specific failures.
"""
from __future__ import annotations

from typing import Any


class RisterError(Exception):
    """Base class for every RISTER error."""


# -- orchestration ---------------------------------------------------------
class StateError(RisterError):
    """The shared state was used incorrectly (bad update, unknown reducer...)."""


class GraphError(RisterError):
    """The graph is malformed or exceeded an execution limit."""


class NodeError(GraphError):
    """A node raised an exception and exhausted its retry budget."""

    def __init__(self, node: str, cause: BaseException, attempts: int = 1) -> None:
        self.node = node
        self.cause = cause
        self.attempts = attempts
        super().__init__(f"node {node!r} failed after {attempts} attempt(s): {cause!r}")


class Interrupt(RisterError):
    """Raised by a node to pause the run and hand control back to the caller."""

    def __init__(self, payload: Any = None) -> None:
        self.payload = payload
        super().__init__(f"interrupted: {payload!r}")


class Suspend(RisterError):
    """Raised by a pipeline step to suspend the flow."""

    def __init__(self, payload: Any = None, step: str | None = None) -> None:
        self.payload = payload
        self.step = step
        super().__init__(f"suspended at {step or '?'}: {payload!r}")


# -- runtime ---------------------------------------------------------------
class ToolError(RisterError):
    """A tool failed or was invoked incorrectly."""


class ModelError(RisterError):
    """A model backend failed or returned an unusable response."""


class ConfigError(RisterError):
    """Invalid configuration or missing required setting."""


class NotFound(RisterError):
    """A named component (agent, tool, thread, snapshot) does not exist."""


# -- governance ------------------------------------------------------------
class PolicyError(RisterError):
    """A policy could not be parsed or evaluated."""


class PolicyDenied(PolicyError):
    """An action was blocked by policy.

    Attributes
    ----------
    action: the action that was attempted (usually a tool name)
    reason: human readable justification taken from the matched rule
    rule:   name of the rule that produced the decision, if any
    subject: identity the decision applied to
    """

    def __init__(
        self,
        action: str,
        reason: str = "",
        rule: str | None = None,
        subject: str | None = None,
    ) -> None:
        self.action = action
        self.reason = reason
        self.rule = rule
        self.subject = subject
        detail = reason or "denied by policy"
        super().__init__(f"action {action!r} denied for {subject or 'unknown'}: {detail}")


class SandboxError(PolicyError):
    """An execution ring, permission or path guard rejected an operation."""


class ApprovalRequired(PolicyError):
    """The policy demands explicit human approval before the action proceeds."""

    def __init__(self, action: str, reason: str = "", rule: str | None = None) -> None:
        self.action = action
        self.reason = reason
        self.rule = rule
        super().__init__(f"action {action!r} requires approval: {reason or 'policy'}")


# -- analysis --------------------------------------------------------------
class ScanError(RisterError):
    """A security scan could not be completed."""


class EvalError(RisterError):
    """An evaluation suite could not be completed."""


class ServeError(RisterError):
    """The HTTP surface failed to start or handle a request."""


__all__ = [
    "RisterError",
    "StateError",
    "GraphError",
    "NodeError",
    "Interrupt",
    "Suspend",
    "ToolError",
    "ModelError",
    "ConfigError",
    "NotFound",
    "PolicyError",
    "PolicyDenied",
    "SandboxError",
    "ApprovalRequired",
    "ScanError",
    "EvalError",
    "ServeError",
]
