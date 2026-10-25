"""Declarative policy: decisions, rules and sets.

Policies are plain data (JSON, or YAML when PyYAML is available) so they can be
reviewed, versioned and linted independently of the code they constrain.

    {
      "name": "production",
      "default": "deny",
      "rules": [
        {"name": "read", "action": ["read_*", "search"], "effect": "allow"},
        {"name": "no-destructive", "action": ["delete_*", "drop_*"], "effect": "deny",
         "reason": "destructive tools are not allowed in production"},
        {"name": "email-approval", "action": "send_email", "effect": "approve"}
      ]
    }

Effects are ``allow``, ``deny`` and ``approve`` (explicit human sign-off).
Matching is glob-based on the action name, with optional argument predicates.
"""
from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..core.errors import PolicyError

ALLOW = "allow"
DENY = "deny"
APPROVE = "approve"
EFFECTS = (ALLOW, DENY, APPROVE)


# --------------------------------------------------------------------------
# decision
# --------------------------------------------------------------------------
@dataclass
class Decision:
    """The outcome of evaluating a policy."""

    effect: str = ALLOW
    reason: str = ""
    rule: str | None = None
    policy: str | None = None
    action: str = ""
    subject: str | None = None
    metadata: dict[str, Any] = field(default_factory=dict)

    @property
    def allowed(self) -> bool:
        return self.effect == ALLOW

    @property
    def denied(self) -> bool:
        return self.effect == DENY

    @property
    def needs_approval(self) -> bool:
        return self.effect == APPROVE

    def to_dict(self) -> dict[str, Any]:
        return {
            "effect": self.effect,
            "allowed": self.allowed,
            "reason": self.reason,
            "rule": self.rule,
            "policy": self.policy,
            "action": self.action,
            "subject": self.subject,
            "metadata": self.metadata,
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Decision":
        return cls(
            effect=data.get("effect", ALLOW),
            reason=data.get("reason", ""),
            rule=data.get("rule"),
            policy=data.get("policy"),
            action=data.get("action", ""),
            subject=data.get("subject"),
            metadata=dict(data.get("metadata") or {}),
        )

    def __bool__(self) -> bool:
        return self.allowed


def allow(action: str = "", reason: str = "", **kw: Any) -> Decision:
    return Decision(effect=ALLOW, action=action, reason=reason, **kw)


def deny(action: str = "", reason: str = "", **kw: Any) -> Decision:
    return Decision(effect=DENY, action=action, reason=reason, **kw)


def approve(action: str = "", reason: str = "", **kw: Any) -> Decision:
    return Decision(effect=APPROVE, action=action, reason=reason, **kw)


# --------------------------------------------------------------------------
# conditions
# --------------------------------------------------------------------------
def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        return list(value)
    return [value]


def match_condition(condition: Any, value: Any) -> bool:
    """Evaluate one ``when`` condition against an argument value."""
    if callable(condition):
        return bool(condition(value))
    if isinstance(condition, Mapping):
        for op, expected in condition.items():
            if op in ("in", "any_of"):
                if value not in _as_list(expected):
                    return False
            elif op == "not_in":
                if value in _as_list(expected):
                    return False
            elif op in ("matches", "regex"):
                if not isinstance(value, str) or not re.search(str(expected), value):
                    return False
            elif op == "contains":
                if value is None or expected not in value:
                    return False
            elif op == "not":
                if value == expected:
                    return False
            elif op == "gt":
                if not (value is not None and value > expected):
                    return False
            elif op == "lt":
                if not (value is not None and value < expected):
                    return False
            elif op == "exists":
                if (value is not None) is not bool(expected):
                    return False
            else:
                raise PolicyError(f"unknown condition operator {op!r}")
        return True
    if isinstance(condition, (list, tuple)):
        return value in condition
    return value == condition


def _subject_view(subject: Any) -> dict[str, Any]:
    if subject is None:
        return {}
    if isinstance(subject, Mapping):
        return dict(subject)
    data: dict[str, Any] = {}
    for attr in ("id", "kind", "roles", "attributes", "name"):
        if hasattr(subject, attr):
            value = getattr(subject, attr)
            data[attr] = dict(value) if attr == "attributes" and isinstance(value, Mapping) else value
    return data


def matches_when(
    when: Mapping[str, Any] | None,
    arguments: Mapping[str, Any],
    subject: Any,
) -> bool:
    """All ``when`` clauses must hold. Supports ``arg``, ``subject`` and ``any_of``."""
    if not when:
        return True
    view = _subject_view(subject)
    for key, condition in when.items():
        if key == "any_of":
            if not any(matches_when(rule, arguments, subject) for rule in _as_list(condition)):
                return False
        elif key == "all_of":
            if not all(matches_when(rule, arguments, subject) for rule in _as_list(condition)):
                return False
        elif key == "not":
            if matches_when(condition, arguments, subject):
                return False
        elif key == "subject":
            if isinstance(condition, Mapping):
                if not all(
                    match_condition(cond, view.get(name)) for name, cond in condition.items()
                ):
                    return False
            elif not match_condition(condition, view.get("id")):
                return False
        elif key.startswith("subject."):
            if not match_condition(condition, view.get(key.split(".", 1)[1])):
                return False
        elif key.startswith("args."):
            if not match_condition(condition, arguments.get(key.split(".", 1)[1])):
                return False
        else:
            if not match_condition(condition, arguments.get(key)):
                return False
    return True


# --------------------------------------------------------------------------
# rule & policy
# --------------------------------------------------------------------------
@dataclass
class Rule:
    """One named policy clause."""

    name: str
    action: list[str] = field(default_factory=list)
    effect: str = ALLOW
    when: dict[str, Any] | None = None
    reason: str = ""
    priority: int = 0
    enabled: bool = True
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.action = [str(a) for a in _as_list(self.action)]
        if self.effect not in EFFECTS:
            raise PolicyError(
                f"rule {self.name!r} has effect {self.effect!r}; expected one of {EFFECTS}"
            )
        if not self.action:
            raise PolicyError(f"rule {self.name!r} matches no action")

    def matches_action(self, action: str) -> bool:
        if not self.enabled:
            return False
        return any(fnmatch.fnmatchcase(action, pattern) for pattern in self.action)

    def matches(
        self, action: str, arguments: Mapping[str, Any], subject: Any
    ) -> bool:
        return self.matches_action(action) and matches_when(self.when, arguments, subject)

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], index: int = 0) -> "Rule":
        return cls(
            name=str(data.get("name") or f"rule{index}"),
            action=data.get("action", []),
            effect=str(data.get("effect", ALLOW)),
            when=dict(data["when"]) if isinstance(data.get("when"), Mapping) else data.get("when"),
            reason=str(data.get("reason", "")),
            priority=int(data.get("priority", 0)),
            enabled=bool(data.get("enabled", True)),
            metadata=dict(data.get("metadata") or {}),
        )

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {
            "name": self.name,
            "action": self.action,
            "effect": self.effect,
        }
        if self.when:
            out["when"] = self.when
        if self.reason:
            out["reason"] = self.reason
        if self.priority:
            out["priority"] = self.priority
        if not self.enabled:
            out["enabled"] = False
        return out


@dataclass
class Policy:
    """An ordered rule set with a default effect."""

    name: str = "policy"
    rules: list[Rule] = field(default_factory=list)
    default: str = DENY
    combine: str = "first-match"  # or "deny-overrides"

    def __post_init__(self) -> None:
        if self.default not in EFFECTS:
            raise PolicyError(f"default effect {self.default!r} is invalid")
        if self.combine not in ("first-match", "deny-overrides"):
            raise PolicyError(f"combine {self.combine!r} must be first-match or deny-overrides")
        # Accept plain mappings so policies can be written inline.
        self.rules = [
            Rule.from_dict(rule, index) if isinstance(rule, Mapping) else rule
            for index, rule in enumerate(self.rules)
        ]
        self.rules = sorted(self.rules, key=lambda r: -r.priority)

    # -- evaluation --------------------------------------------------------
    def evaluate(
        self,
        action: str,
        arguments: Mapping[str, Any] | None = None,
        subject: Any = None,
    ) -> Decision:
        args = dict(arguments or {})
        matched: list[Rule] = [r for r in self.rules if r.matches(action, args, subject)]

        if not matched:
            return Decision(
                effect=self.default,
                reason=f"no rule matched {action!r}; policy default is {self.default}",
                policy=self.name,
                action=action,
                subject=getattr(subject, "id", None) if subject is not None else None,
            )

        if self.combine == "deny-overrides":
            for effect in (DENY, APPROVE):
                for rule in matched:
                    if rule.effect == effect:
                        return self._decision(rule, action, subject)
            return self._decision(matched[0], action, subject)

        return self._decision(matched[0], action, subject)

    def _decision(self, rule: Rule, action: str, subject: Any) -> Decision:
        return Decision(
            effect=rule.effect,
            reason=rule.reason or f"rule {rule.name!r}",
            rule=rule.name,
            policy=self.name,
            action=action,
            subject=getattr(subject, "id", None) if subject is not None else None,
        )

    def add(self, rule: "Rule | Mapping[str, Any]") -> "Policy":
        if isinstance(rule, Mapping):
            rule = Rule.from_dict(rule, len(self.rules))
        self.rules.append(rule)
        self.rules.sort(key=lambda r: -r.priority)
        return self

    # -- serialisation -----------------------------------------------------
    def to_dict(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "default": self.default,
            "combine": self.combine,
            "rules": [r.to_dict() for r in self.rules],
        }

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Policy":
        rules = [
            Rule.from_dict(item, index)
            for index, item in enumerate(data.get("rules") or [])
        ]
        return cls(
            name=data.get("name", "policy"),
            rules=rules,
            default=data.get("default", DENY),
            combine=data.get("combine", "first-match"),
        )

    @classmethod
    def load(cls, path: str | Path) -> "Policy":
        return cls.from_dict(load_document(path))

    def save(self, path: str | Path) -> Path:
        target = Path(path)
        target.write_text(json.dumps(self.to_dict(), indent=2) + "\n", encoding="utf-8")
        return target


class PolicySet:
    """Several policies evaluated in order; the first explicit match wins."""

    def __init__(self, policies: Iterable[Policy] = (), *, default: str = DENY) -> None:
        self.policies = list(policies)
        self.default = default

    def add(self, policy: Policy) -> "PolicySet":
        self.policies.append(policy)
        return self

    def evaluate(
        self,
        action: str,
        arguments: Mapping[str, Any] | None = None,
        subject: Any = None,
    ) -> Decision:
        for policy in self.policies:
            decision = policy.evaluate(action, arguments, subject)
            # A default-derived decision is not an explicit match: keep looking.
            if decision.rule is not None:
                return decision
        return Decision(
            effect=self.default,
            reason=f"no policy matched {action!r}",
            action=action,
            subject=getattr(subject, "id", None) if subject is not None else None,
        )

    def to_dict(self) -> dict[str, Any]:
        return {"default": self.default, "policies": [p.to_dict() for p in self.policies]}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "PolicySet":
        return cls(
            [Policy.from_dict(p) for p in data.get("policies") or []],
            default=data.get("default", DENY),
        )


# --------------------------------------------------------------------------
# loading & linting
# --------------------------------------------------------------------------
def load_document(path: str | Path) -> dict[str, Any]:
    """Load a JSON or YAML policy document (YAML requires PyYAML)."""
    target = Path(path)
    if not target.exists():
        raise PolicyError(f"policy file not found: {target}")
    text = target.read_text(encoding="utf-8")
    suffix = target.suffix.lower()
    if suffix in (".yaml", ".yml"):
        try:
            import yaml  # type: ignore[import-not-found]
        except ModuleNotFoundError as exc:  # pragma: no cover - optional dep
            raise PolicyError(
                "YAML policies need PyYAML; install it or use JSON "
                f"(could not load {target.name})"
            ) from exc
        data = yaml.safe_load(text)
    else:
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PolicyError(f"{target.name} is not valid JSON: {exc}") from exc
    if not isinstance(data, Mapping):
        raise PolicyError(f"{target.name} must contain an object at the top level")
    return dict(data)


def lint_policy_data(data: Mapping[str, Any]) -> list[str]:
    """Static checks for the mistakes that bite people most often."""
    problems: list[str] = []
    if "rules" not in data:
        problems.append("document has no 'rules' list")
    if data.get("default") not in EFFECTS and data.get("default") is not None:
        problems.append(f"default must be one of {EFFECTS}, got {data.get('default')!r}")

    seen: set[str] = set()
    for index, rule in enumerate(data.get("rules") or []):
        label = rule.get("name") or f"rule[{index}]"
        if not rule.get("action"):
            problems.append(f"{label}: no action patterns")
        if rule.get("effect") not in EFFECTS:
            problems.append(f"{label}: effect must be one of {EFFECTS}")
        if rule.get("name") in seen:
            problems.append(f"{label}: duplicate rule name")
        seen.add(rule.get("name"))
        for pattern in _as_list(rule.get("action")):
            if pattern == "*":
                problems.append(
                    f"{label}: wildcard '*' matches every action; scope it or rely on default"
                )
        if rule.get("effect") == "allow" and pattern_is_broad(rule.get("action")):
            problems.append(f"{label}: broad allow pattern may over-permit")
    if data.get("default") == "allow":
        problems.append("default allow is risky: prefer deny with explicit allows")
    return problems


def pattern_is_broad(action: Any) -> bool:
    patterns = _as_list(action)
    return any(str(p).count("*") >= 2 and str(p).replace("*", "") == "" for p in patterns)


__all__ = [
    "ALLOW",
    "DENY",
    "APPROVE",
    "EFFECTS",
    "Decision",
    "allow",
    "deny",
    "approve",
    "Rule",
    "Policy",
    "PolicySet",
    "load_document",
    "lint_policy_data",
    "matches_when",
    "match_condition",
]
