"""The guard: one place where every action is authorised and recorded.

The guard is deliberately boring and deterministic. Given an action name, its
arguments and the acting identity it:

1. evaluates the policy,
2. escalates to approval when the tool or policy demands it,
3. writes an audit entry either way,
4. raises :class:`PolicyDenied` / :class:`ApprovalRequired` on refusal.

Because it is a plain object with a ``check`` method, :class:`rister.agent.tool.ToolRegistry`
calls it on every tool invocation and agents need no knowledge of it.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Mapping

from ..core.errors import ApprovalRequired, PolicyDenied, PolicyError
from ..core.types import Config
from .approval import APPROVED, QueueApprover
from .audit import AuditLog
from .identity import SYSTEM, Identity
from .policy import APPROVE, ALLOW, DENY, Decision, Policy, PolicySet

PolicyLike = Policy | PolicySet | None


def _as_subject(value: Any) -> Any:
    if value is None or isinstance(value, Identity):
        return value
    if isinstance(value, str):
        return Identity(id=value)
    if isinstance(value, Mapping):
        return Identity.from_dict(value) if "id" in value else value
    return value


@dataclass
class Guard:
    """Authorise actions against a policy and log every decision."""

    policy: PolicyLike = None
    audit: AuditLog | None = None
    approver: Any = None
    sandbox: Any = None
    default_subject: Any = None
    escalate: tuple[str, ...] = ()
    audit_allow: bool = True

    def __post_init__(self) -> None:
        if self.policy is None:
            # No policy means "allow", which is explicit rather than accidental.
            self.policy = Policy(name="permissive", rules=[], default=ALLOW)

    # -- subject resolution ------------------------------------------------
    def subject_from(self, config: Config | Mapping[str, Any] | None) -> Any:
        if config is None:
            return self.default_subject
        if isinstance(config, Mapping):
            candidate = config.get("subject")
            return _as_subject(candidate) if candidate is not None else self.default_subject
        if getattr(config, "subject", None) is not None:
            return _as_subject(config.subject)
        identity = getattr(config, "metadata", {}).get("identity")
        return _as_subject(identity) if identity is not None else self.default_subject

    # -- evaluation --------------------------------------------------------
    def explain(
        self,
        action: str,
        arguments: Mapping[str, Any] | None = None,
        subject: Any = None,
    ) -> Decision:
        """Evaluate without approving, auditing or raising."""
        return self.policy.evaluate(action, arguments, subject)

    def check(
        self,
        action: str,
        *,
        arguments: Mapping[str, Any] | None = None,
        config: Any = None,
        tool: Any = None,
    ) -> Decision:
        """Authorise an action, raising on refusal. Returns the allow decision."""
        args = dict(arguments or {})
        subject = self.subject_from(config)
        decision = self.policy.evaluate(action, args, subject)

        # Tools flagged as needing sign-off escalate an allow into an approval.
        if tool is not None and getattr(tool, "requires_approval", False):
            if decision.effect == ALLOW:
                decision = Decision(
                    effect=APPROVE,
                    reason=decision.reason or "tool requires approval",
                    rule=decision.rule,
                    policy=decision.policy,
                    action=action,
                    subject=decision.subject,
                )

        # A subject listed in `escalate` can never self-approve.
        if decision.effect in (ALLOW, APPROVE) and subject is not None:
            if getattr(subject, "id", subject) in self.escalate:
                decision = Decision(
                    effect=APPROVE,
                    reason="subject is on the escalation list",
                    action=action,
                    subject=getattr(subject, "id", None),
                )

        if decision.effect == DENY:
            self._audit(action, decision, args, tool)
            raise PolicyDenied(
                action, decision.reason, decision.rule, decision.subject
            )

        if decision.effect == APPROVE:
            approved = self._request_approval(action, decision, args, subject)
            if not approved:
                self._audit(
                    action,
                    Decision(
                        effect=DENY,
                        reason=f"approval refused: {decision.reason}",
                        rule=decision.rule,
                        policy=decision.policy,
                        action=action,
                        subject=decision.subject,
                    ),
                    args,
                    tool,
                )
                raise ApprovalRequired(action, decision.reason, decision.rule)
            decision = Decision(
                effect=ALLOW,
                reason=f"approved: {decision.reason}" if decision.reason else "approved",
                rule=decision.rule,
                policy=decision.policy,
                action=action,
                subject=decision.subject,
                metadata={**decision.metadata, "approved": True},
            )

        self._audit(action, decision, args, tool)
        return decision

    # -- helpers -----------------------------------------------------------
    def _request_approval(
        self, action: str, decision: Decision, args: Mapping[str, Any], subject: Any
    ) -> bool:
        if self.approver is None:
            return False
        if not hasattr(self.approver, "request"):
            raise PolicyError(
                "approver must expose request(action, reason, subject, arguments) -> Ticket"
            )
        ticket = self.approver.request(
            action, decision.reason, subject, args
        )
        if ticket.open:
            if isinstance(self.approver, QueueApprover):
                ticket = self.approver.wait(ticket.id)
            if ticket.open:  # still undecided: treat as refused
                return False
        approved = ticket.status == APPROVED
        decision.metadata["ticket"] = ticket.id
        decision.metadata["approved"] = approved
        return approved

    def _audit(
        self,
        action: str,
        decision: Decision,
        arguments: Mapping[str, Any],
        tool: Any,
    ) -> None:
        if self.audit is None:
            return
        if decision.effect == ALLOW and not self.audit_allow:
            return
        self.audit.record(
            action,
            decision.effect,
            actor=decision.subject or (SYSTEM.id if self.default_subject is None else None),
            rule=decision.rule,
            reason=decision.reason,
            arguments=arguments,
            metadata={
                "policy": decision.policy,
                "tool": getattr(tool, "name", None),
                **{k: v for k, v in decision.metadata.items() if k != "ticket"},
            },
        )

    # -- ergonomics --------------------------------------------------------
    def wrap(self, fn: Callable[..., Any], action: str | None = None) -> Callable[..., Any]:
        """Decorate a bare function so calling it is authorised first."""
        name = action or getattr(fn, "__name__", "action")

        def guarded(*args: Any, **kwargs: Any) -> Any:
            self.check(name, arguments=kwargs, config=None)
            return fn(*args, **kwargs)

        guarded.__name__ = getattr(fn, "__name__", "guarded")
        return guarded

    def describe(self) -> dict[str, Any]:
        policy = self.policy
        return {
            "policy": policy.to_dict() if isinstance(policy, Policy) else policy.to_dict(),
            "audit_entries": len(self.audit) if self.audit is not None else 0,
            "approver": type(self.approver).__name__ if self.approver else None,
            "sandbox": self.sandbox.describe() if self.sandbox is not None else None,
            "escalate": list(self.escalate),
        }


__all__ = ["Guard", "PolicyLike"]
