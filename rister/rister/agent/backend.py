"""Model backends.

RISTER does not bundle a vendor SDK. A backend only has to implement
``complete(messages, tools, **options) -> Completion``. Four ship in the box:

* :class:`ScriptedModel` — deterministic replies for tests and demos.
* :class:`CallableModel` — wrap any function.
* :class:`HTTPModel`    — any endpoint implementing the standard
  ``/chat/completions`` wire shape (hosted or local), using only the standard
  library (``urllib``), so RISTER stays dependency-free.
* :class:`RouterModel`  — choose between backends per request (cheap/strong).
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..core.errors import ConfigError, ModelError
from ..core.types import Completion, Message, Role, ToolCall, Usage


class Model:
    """Base class for a chat model backend."""

    name: str = "model"

    def complete(
        self,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, Any]] | None = None,
        **options: Any,
    ) -> Completion:  # pragma: no cover - interface
        raise NotImplementedError

    async def acomplete(
        self,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, Any]] | None = None,
        **options: Any,
    ) -> Completion:
        """Async wrapper; backends that are natively async override this."""
        import asyncio

        return await asyncio.to_thread(self.complete, messages, tools, **options)

    def bind(self, **options: Any) -> "BoundModel":
        return BoundModel(self, options)

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<{type(self).__name__} {self.name!r}>"


class BoundModel(Model):
    """A model with default options baked in."""

    def __init__(self, inner: Model, options: Mapping[str, Any]) -> None:
        self.inner = inner
        self.options = dict(options)
        self.name = inner.name

    def complete(self, messages, tools=None, **options):
        merged = {**self.options, **options}
        return self.inner.complete(messages, tools, **merged)

    async def acomplete(self, messages, tools=None, **options):
        merged = {**self.options, **options}
        return await self.inner.acomplete(messages, tools, **merged)


# --------------------------------------------------------------------------
# deterministic backends
# --------------------------------------------------------------------------
class ScriptedModel(Model):
    """Replay a fixed list of replies.

    Each entry may be a ``str``, a :class:`Message`, a :class:`Completion`, a
    list of :class:`ToolCall` (meaning "call these now"), a mapping with
    ``text``/``tool_calls``, or a callable receiving the message list. The final
    entry repeats if the agent asks for more turns.
    """

    name = "scripted"

    def __init__(self, replies: Iterable[Any], name: str = "scripted") -> None:
        self.replies = list(replies)
        self.name = name
        self.calls: list[list[Message]] = []

    def complete(self, messages, tools=None, **options):
        self.calls.append(list(messages))
        if not self.replies:
            raise ModelError("ScriptedModel ran out of scripted replies")
        index = min(len(self.calls) - 1, len(self.replies) - 1)
        return _coerce_completion(self.replies[index])


class CallableModel(Model):
    """Delegate to a Python function."""

    name = "callable"

    def __init__(self, fn: Callable[..., Any], name: str = "callable") -> None:
        self.fn = fn
        self.name = name

    def complete(self, messages, tools=None, **options):
        try:
            return _coerce_completion(self.fn(list(messages), tools or [], **options))
        except ModelError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ModelError(f"{self.name} failed: {type(exc).__name__}: {exc}") from exc


class RouterModel(Model):
    """Pick a backend per request."""

    name = "router"

    def __init__(self, routes: Mapping[str, Model], choose: Callable[..., str] | None = None):
        self.routes = dict(routes)
        self.choose = choose or (lambda messages, tools, options: next(iter(self.routes)))

    def complete(self, messages, tools=None, **options):
        key = options.pop("route", None) or self.choose(messages, tools, options)
        if key not in self.routes:
            raise ModelError(f"no route {key!r}; available: {sorted(self.routes)}")
        return self.routes[key].complete(messages, tools, **options)


# --------------------------------------------------------------------------
# standard-library HTTP backend
# --------------------------------------------------------------------------
@dataclass
class HTTPModel(Model):
    """Call any endpoint that implements the standard chat-completions shape.

    Provider-neutral by design: there is no baked-in vendor URL or key name, so
    you cannot accidentally call a third party. Pass ``base_url`` explicitly,
    and either ``api_key`` or the name of the environment variable holding it.
    """

    model: str = "default"
    base_url: str = ""
    api_key: str | None = None
    api_key_env: str = "RISTER_API_KEY"
    temperature: float | None = None
    max_tokens: int | None = None
    timeout: float = 120.0
    extra_headers: dict[str, str] = field(default_factory=dict)
    name: str = "http"

    def __post_init__(self) -> None:
        self.name = self.model
        self.base_url = (self.base_url or "").rstrip("/")
        if not self.base_url:
            raise ConfigError(
                "HTTPModel needs an explicit base_url (for example "
                "'http://localhost:11434/v1'); RISTER ships no vendor default"
            )

    # -- internals ---------------------------------------------------------
    def _key(self) -> str | None:
        return self.api_key or os.environ.get(self.api_key_env)

    def payload(
        self,
        messages: Sequence[Message],
        tools: Sequence[Mapping[str, Any]] | None = None,
        **options: Any,
    ) -> dict[str, Any]:
        body: dict[str, Any] = {
            "model": options.pop("model", self.model),
            "messages": [m.as_chat_message() for m in messages],
        }
        temperature = options.pop("temperature", self.temperature)
        if temperature is not None:
            body["temperature"] = temperature
        max_tokens = options.pop("max_tokens", self.max_tokens)
        if max_tokens is not None:
            body["max_tokens"] = max_tokens
        if tools:
            body["tools"] = list(tools)
            if "tool_choice" in options:
                body["tool_choice"] = options.pop("tool_choice")
        body.update(options)
        return body

    def complete(self, messages, tools=None, **options):
        body = self.payload(messages, tools, **options)
        url = f"{self.base_url}/chat/completions"
        headers = {"Content-Type": "application/json", **self.extra_headers}
        key = self._key()
        if key:
            headers.setdefault("Authorization", f"Bearer {key}")
        request = urllib.request.Request(
            url, data=json.dumps(body).encode("utf-8"), headers=headers, method="POST"
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:500]
            raise ModelError(f"{self.name}: HTTP {exc.code} from {url}: {detail}") from exc
        except urllib.error.URLError as exc:
            raise ModelError(f"{self.name}: cannot reach {url}: {exc.reason}") from exc
        return self._parse(raw)

    def _parse(self, raw: Mapping[str, Any]) -> Completion:
        """Parse a chat-completions response body."""
        try:
            choice = raw["choices"][0]
        except (KeyError, IndexError) as exc:
            raise ModelError(f"{self.name}: response has no choices: {raw!r:.200}") from exc
        msg = choice.get("message") or {}
        calls = [
            ToolCall(
                name=(tc.get("function") or {}).get("name", ""),
                arguments=(tc.get("function") or {}).get("arguments") or {},
                id=tc.get("id", ""),
            )
            for tc in (msg.get("tool_calls") or [])
        ]
        usage = raw.get("usage") or {}
        return Completion(
            message=Message.assistant(msg.get("content") or "", tool_calls=calls),
            tool_calls=calls,
            usage=Usage(
                int(usage.get("prompt_tokens", 0)), int(usage.get("completion_tokens", 0))
            ),
            finish_reason=choice.get("finish_reason", "stop") or "stop",
            raw=dict(raw),
        )


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------
def _coerce_completion(value: Any) -> Completion:
    """Accept the convenient shapes a scripted/callable model may return."""
    if isinstance(value, Completion):
        return value
    if isinstance(value, Message):
        return Completion(message=value, tool_calls=list(value.tool_calls))
    if isinstance(value, str):
        return Completion(message=Message.assistant(value))
    if isinstance(value, Mapping):
        if "tool_calls" in value or "text" in value:
            calls = [
                ToolCall.from_dict(tc) if isinstance(tc, Mapping) else tc
                for tc in (value.get("tool_calls") or [])
            ]
            text = value.get("text") or value.get("content") or ""
            usage = value.get("usage") or {}
            return Completion(
                message=Message.assistant(text, tool_calls=calls),
                tool_calls=calls,
                usage=Usage(
                    int(usage.get("prompt_tokens", 0)),
                    int(usage.get("completion_tokens", 0)),
                )
                if isinstance(usage, Mapping)
                else Usage(),
                finish_reason=value.get("finish_reason", "stop"),
            )
        raise ModelError(f"cannot interpret completion mapping keys {sorted(value)}")
    if (
        isinstance(value, (list, tuple))
        and value
        and all(isinstance(item, ToolCall) for item in value)
    ):
        calls = list(value)
        return Completion(message=Message.assistant("", tool_calls=calls), tool_calls=calls)
    raise ModelError(f"cannot interpret {type(value).__name__} as a completion")


__all__ = [
    "Model",
    "BoundModel",
    "ScriptedModel",
    "CallableModel",
    "RouterModel",
    "HTTPModel",
]
