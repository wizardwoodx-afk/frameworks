"""Core value types shared by every RISTER subsystem.

Deliberately dependency-free: messages, tool calls and usage are plain
dataclasses with explicit serialisation, so the same objects flow through the
graph engine, the agent loop, the HTTP surface and the trace exporter unchanged.
"""
from __future__ import annotations

import json
import time
import uuid
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Iterable, Iterator, Mapping


# --------------------------------------------------------------------------
# identifiers
# --------------------------------------------------------------------------
def new_id(prefix: str = "id") -> str:
    """Return a short, collision-resistant identifier."""
    return f"{prefix}_{uuid.uuid4().hex[:12]}"


# --------------------------------------------------------------------------
# messages
# --------------------------------------------------------------------------
class Role(str, Enum):
    """Who produced a message."""

    SYSTEM = "system"
    USER = "user"
    ASSISTANT = "assistant"
    TOOL = "tool"

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.value


def _coerce_role(value: "Role | str") -> Role:
    if isinstance(value, Role):
        return value
    try:
        return Role(str(value).lower())
    except ValueError as exc:  # pragma: no cover - defensive
        raise ValueError(f"unknown role: {value!r}") from exc


@dataclass
class ToolCall:
    """A request from a model to run a named tool with JSON arguments."""

    name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    id: str = ""

    def __post_init__(self) -> None:
        if not self.id:
            self.id = f"call_{uuid.uuid4().hex[:8]}"
        if isinstance(self.arguments, str):  # tolerate raw JSON
            raw = self.arguments.strip()
            self.arguments = json.loads(raw) if raw else {}

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "arguments": dict(self.arguments)}

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "ToolCall":
        return cls(
            name=data.get("name", ""),
            arguments=data.get("arguments") or {},
            id=data.get("id", ""),
        )

    def as_function_call(self) -> dict[str, Any]:
        """Render one tool call in the standard chat-completions wire shape."""
        return {
            "id": self.id,
            "type": "function",
            "function": {
                "name": self.name,
                "arguments": json.dumps(self.arguments, sort_keys=True),
            },
        }


@dataclass
class Message:
    """A single conversational turn."""

    role: Role | str
    content: str = ""
    name: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    tool_call_id: str | None = None
    meta: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        self.role = _coerce_role(self.role)
        if self.content is None:
            self.content = ""
        if not self.tool_calls and self.meta.get("tool_calls"):
            self.tool_calls = [
                ToolCall.from_dict(tc) for tc in self.meta["tool_calls"]
            ]

    # -- constructors ------------------------------------------------------
    @classmethod
    def system(cls, content: str, **meta: Any) -> "Message":
        return cls(Role.SYSTEM, content, meta=meta)

    @classmethod
    def user(cls, content: str, **meta: Any) -> "Message":
        return cls(Role.USER, content, meta=meta)

    @classmethod
    def assistant(
        cls, content: str = "", tool_calls: Iterable[ToolCall] | None = None, **meta: Any
    ) -> "Message":
        return cls(Role.ASSISTANT, content, tool_calls=list(tool_calls or []), meta=meta)

    @classmethod
    def tool_result(cls, call: ToolCall, content: str, **meta: Any) -> "Message":
        return cls(
            Role.TOOL,
            content,
            name=call.name,
            tool_call_id=call.id,
            meta=meta,
        )
    # -- helpers -----------------------------------------------------------
    @property
    def text(self) -> str:
        return self.content or ""

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"role": self.role.value, "content": self.content}
        if self.name:
            out["name"] = self.name
        if self.tool_calls:
            out["tool_calls"] = [tc.to_dict() for tc in self.tool_calls]
        if self.tool_call_id:
            out["tool_call_id"] = self.tool_call_id
        if self.meta:
            out["meta"] = dict(self.meta)
        return out

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> "Message":
        return cls(
            role=data.get("role", "user"),
            content=data.get("content", "") or "",
            name=data.get("name"),
            tool_calls=[ToolCall.from_dict(tc) for tc in data.get("tool_calls") or []],
            tool_call_id=data.get("tool_call_id"),
            meta=dict(data.get("meta") or {}),
        )

    def as_chat_message(self) -> dict[str, Any]:
        """Render this message for a chat-completions wire request."""
        out: dict[str, Any] = {"role": self.role.value}
        if self.role is Role.TOOL:
            out["content"] = self.content
            if self.tool_call_id:
                out["tool_call_id"] = self.tool_call_id
            if self.name:
                out["name"] = self.name
            return out
        if self.tool_calls:
            out["content"] = self.content or None
            out["tool_calls"] = [tc.as_function_call() for tc in self.tool_calls]
            return out
        out["content"] = self.content
        return out


# --------------------------------------------------------------------------
# usage & completions
# --------------------------------------------------------------------------
@dataclass
class Usage:
    """Token accounting."""

    prompt_tokens: int = 0
    completion_tokens: int = 0

    @property
    def total_tokens(self) -> int:
        return self.prompt_tokens + self.completion_tokens

    def __add__(self, other: "Usage") -> "Usage":
        return Usage(
            self.prompt_tokens + other.prompt_tokens,
            self.completion_tokens + other.completion_tokens,
        )

    def to_dict(self) -> dict[str, int]:
        return {
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
        }


@dataclass
class Completion:
    """A model response."""

    message: Message
    tool_calls: list[ToolCall] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    finish_reason: str = "stop"
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def text(self) -> str:
        return self.message.text

    def to_dict(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "tool_calls": [tc.to_dict() for tc in self.tool_calls],
            "usage": self.usage.to_dict(),
            "finish_reason": self.finish_reason,
        }


# --------------------------------------------------------------------------
# run configuration
# --------------------------------------------------------------------------
@dataclass
class Config:
    """Per-run settings threaded through graphs, agents and tools."""

    thread_id: str = field(default_factory=lambda: new_id("thread"))
    subject: Any = None
    tracer: Any = None
    metadata: dict[str, Any] = field(default_factory=dict)
    dry_run: bool = False

    def get(self, key: str, default: Any = None) -> Any:
        if hasattr(self, key):
            return getattr(self, key)
        return self.metadata.get(key, default)

    def child(self, **overrides: Any) -> "Config":
        data = {
            "thread_id": self.thread_id,
            "subject": self.subject,
            "tracer": self.tracer,
            "metadata": dict(self.metadata),
            "dry_run": self.dry_run,
        }
        data.update(overrides)
        return Config(**data)


def as_config(value: "Config | Mapping[str, Any] | None") -> Config:
    """Accept a Config, a plain dict, or None."""
    if value is None:
        return Config()
    if isinstance(value, Config):
        return value
    if isinstance(value, Mapping):
        known = {f for f in ("thread_id", "subject", "tracer", "metadata", "dry_run")}
        kwargs = {k: v for k, v in value.items() if k in known}
        extra = {k: v for k, v in value.items() if k not in known}
        cfg = Config(**kwargs)
        if extra:
            cfg.metadata.update(extra)
        return cfg
    raise TypeError(f"cannot interpret {value!r} as a run configuration")


def utc_now() -> float:
    """Wall-clock seconds; isolated so tests can monkeypatch one place."""
    return time.time()


def jsonable(value: Any) -> Any:
    """Best-effort conversion to something ``json.dumps`` accepts."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, (list, tuple, set)):
        return [jsonable(v) for v in value]
    if isinstance(value, Mapping):
        return {str(k): jsonable(v) for k, v in value.items()}
    if hasattr(value, "to_dict"):
        return jsonable(value.to_dict())
    return repr(value)


__all__ = [
    "new_id",
    "Role",
    "ToolCall",
    "Message",
    "Usage",
    "Completion",
    "Config",
    "as_config",
    "utc_now",
    "jsonable",
]
