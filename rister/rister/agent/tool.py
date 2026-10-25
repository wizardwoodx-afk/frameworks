"""Typed tools.

A tool is a named, described callable with a JSON-schema argument contract.
Schema inference is deliberately dependency-free: annotations are mapped to
JSON types, defaults become optional properties, and the docstring becomes the
description. Pass an explicit ``parameters`` schema when you need more control.

Execution always funnels through :meth:`ToolRegistry.call`, which is the single
choke point where a governance guard (policy, sandbox, audit) can inspect the
call before the body runs.
"""
from __future__ import annotations

import asyncio
import inspect
import json
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..core.errors import PolicyDenied, ToolError
from ..core.types import jsonable

#: annotation -> JSON schema type
_JSON_TYPES: dict[Any, str] = {
    str: "string",
    int: "integer",
    float: "number",
    bool: "boolean",
    list: "array",
    dict: "object",
}


def _json_type(annotation: Any) -> dict[str, Any]:
    if annotation in _JSON_TYPES:
        return {"type": _JSON_TYPES[annotation]}
    origin = getattr(annotation, "__origin__", None)
    if origin in (list, tuple, set):
        args = getattr(annotation, "__args__", ())
        item = _json_type(args[0]) if args else {"type": "string"}
        return {"type": "array", "items": item}
    if origin is dict:
        return {"type": "object"}
    if origin is not None:  # Optional[...] / Union[...]
        args = [a for a in getattr(annotation, "__args__", ()) if a is not type(None)]
        if len(args) == 1:
            return _json_type(args[0])
    return {"type": "string"}


def infer_schema(fn: Callable[..., Any], overrides: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build a JSON schema from a function signature and annotations."""
    try:
        signature = inspect.signature(fn)
        hints = inspect.get_annotations(fn, eval_str=True)
    except (TypeError, ValueError):  # pragma: no cover - builtins
        return {"type": "object", "properties": {}, "required": []}

    overrides = dict(overrides or {})
    properties: dict[str, Any] = {}
    required: list[str] = []
    for name, param in signature.parameters.items():
        if name == "self" or param.kind in (param.VAR_POSITIONAL, param.VAR_KEYWORD):
            continue
        schema = _json_type(hints.get(name, str))
        if name in overrides:
            extra = overrides[name]
            schema = {**schema, **(extra if isinstance(extra, Mapping) else {"description": str(extra)})}
        if param.default is param.empty:
            required.append(name)
        else:
            schema["default"] = param.default
        properties[name] = schema
    return {"type": "object", "properties": properties, "required": required}


def _doc_summary(fn: Callable[..., Any]) -> str:
    doc = (inspect.getdoc(fn) or "").strip()
    if not doc:
        return ""
    return doc.split("\n\n")[0].replace("\n", " ").strip()


@dataclass
class Tool:
    """A callable with a name, description and argument schema."""

    name: str
    fn: Callable[..., Any]
    description: str = ""
    parameters: dict[str, Any] = field(
        default_factory=lambda: {"type": "object", "properties": {}, "required": []}
    )
    requires_approval: bool = False
    dangerous: bool = False
    tags: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.name:
            raise ToolError("a tool needs a non-empty name")
        if not callable(self.fn):
            raise ToolError(f"tool {self.name!r} is not callable")

    # -- execution ---------------------------------------------------------
    def __call__(self, *args: Any, **kwargs: Any) -> Any:
        return self.fn(*args, **kwargs)

    def invoke(self, arguments: Mapping[str, Any] | None = None) -> Any:
        args = dict(arguments or {})
        try:
            result = self.fn(**args)
        except TypeError as exc:
            raise ToolError(f"tool {self.name!r} rejected arguments {args}: {exc}") from exc
        except ToolError:
            raise
        except Exception as exc:  # noqa: BLE001
            raise ToolError(f"tool {self.name!r} raised {type(exc).__name__}: {exc}") from exc
        if inspect.iscoroutine(result):  # sync caller, async tool body
            return _run_sync(result)
        return result

    def validate(self, arguments: Mapping[str, Any]) -> list[str]:
        """Lightweight argument validation; returns a list of problems."""
        problems: list[str] = []
        props = self.parameters.get("properties", {})
        for key in arguments:
            if props and key not in props:
                problems.append(f"unknown argument {key!r}")
        for key in self.parameters.get("required", []):
            if key not in arguments:
                problems.append(f"missing required argument {key!r}")
        return problems

    # -- schema ------------------------------------------------------------
    def spec(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "description": self.description,
            "parameters": self.parameters,
            "requires_approval": self.requires_approval,
            "dangerous": self.dangerous,
            "tags": list(self.tags),
        }

    def as_function_spec(self) -> dict[str, Any]:
        """Render as a chat-completions function declaration."""
        return {
            "type": "function",
            "function": {
                "name": self.name,
                "description": self.description,
                "parameters": self.parameters,
            },
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Tool {self.name!r}>"


def _run_sync(value: Any) -> Any:
    async def _await() -> Any:
        return await value

    return asyncio.run(_await())


def tool(
    fn: Callable[..., Any] | None = None,
    *,
    name: str | None = None,
    description: str | None = None,
    parameters: Mapping[str, Any] | None = None,
    schema_overrides: Mapping[str, Any] | None = None,
    requires_approval: bool = False,
    dangerous: bool = False,
    tags: Sequence[str] = (),
) -> Any:
    """Decorator that turns a function into a :class:`Tool`.

        @tool(requires_approval=True)
        def send_email(to: str, body: str) -> str:
            \"\"\"Send an email.\"\"\"
    """

    def wrap(target: Callable[..., Any]) -> Tool:
        is_async = inspect.iscoroutinefunction(target)
        if is_async:  # adapt coroutines to the synchronous tool contract

            def body(*args: Any, **kwargs: Any) -> Any:  # type: ignore[misc]
                return _run_sync(target(*args, **kwargs))

        else:
            body: Callable[..., Any] = target  # type: ignore[no-redef]

        return Tool(
            name=name or target.__name__,
            fn=body,
            description=description or _doc_summary(target),
            parameters=dict(parameters)
            if parameters is not None
            else infer_schema(target, schema_overrides),
            requires_approval=requires_approval,
            dangerous=dangerous,
            tags=tuple(tags),
        )

    return wrap(fn) if fn is not None else wrap


class ToolRegistry:
    """A named collection of tools with a single execution choke point."""

    def __init__(self, tools: Iterable[Any] = ()) -> None:
        self._tools: dict[str, Tool] = {}
        for item in tools:
            self.add(item)

    # -- collection --------------------------------------------------------
    def add(self, item: Any) -> Tool:
        entry = item if isinstance(item, Tool) else _coerce_tool(item)
        if entry.name in self._tools:
            raise ToolError(f"tool {entry.name!r} is already registered")
        self._tools[entry.name] = entry
        return entry

    def remove(self, name: str) -> None:
        self._tools.pop(name, None)

    def get(self, name: str) -> Tool:
        try:
            return self._tools[name]
        except KeyError:
            raise ToolError(
                f"unknown tool {name!r}; available: {sorted(self._tools)}"
            ) from None

    def __len__(self) -> int:
        return len(self._tools)

    def __contains__(self, name: object) -> bool:
        return name in self._tools

    def __iter__(self):
        return iter(self._tools.values())

    def names(self) -> list[str]:
        return sorted(self._tools)

    def specs(self) -> list[dict[str, Any]]:
        return [t.spec() for t in self._tools.values()]

    def function_specs(self) -> list[dict[str, Any]]:
        """All tools rendered for a chat-completions request."""
        return [t.as_function_spec() for t in self._tools.values()]

    # -- execution ---------------------------------------------------------
    def call(
        self,
        name: str,
        arguments: Mapping[str, Any] | None = None,
        *,
        config: Any = None,
        guard: Any = None,
    ) -> Any:
        """Validate, authorise and run a tool.

        ``guard`` is any object exposing ``check(action, arguments=..., config=...)``.
        It may raise :class:`PolicyDenied` / :class:`ApprovalRequired`, or return
        a decision object; a denial from either path stops execution here.
        """
        entry = self.get(name)
        args = dict(arguments or {})
        problems = entry.validate(args)
        if problems:
            raise ToolError(f"tool {name!r} arguments invalid: {'; '.join(problems)}")

        if guard is not None:
            check = getattr(guard, "check", None)
            if callable(check):
                decision = check(name, arguments=args, config=config, tool=entry)
                if getattr(decision, "denied", False):
                    raise PolicyDenied(
                        name,
                        getattr(decision, "reason", "") or "denied by guard",
                        getattr(decision, "rule", None),
                    )
        return entry.invoke(args)


def _coerce_tool(item: Any) -> Tool:
    if isinstance(item, Tool):
        return item
    if callable(item):
        return tool(item)
    if isinstance(item, Mapping) and "name" in item and callable(item.get("fn")):
        return Tool(**item)
    raise ToolError(f"cannot register {type(item).__name__} as a tool")


def parse_tool_arguments(raw: Any) -> dict[str, Any]:
    """Normalise tool arguments that may arrive as JSON text."""
    if raw is None:
        return {}
    if isinstance(raw, Mapping):
        return dict(raw)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ToolError(f"tool arguments are not valid JSON: {text[:120]!r}") from exc
        if not isinstance(parsed, Mapping):
            raise ToolError(f"tool arguments must be an object, got {type(parsed).__name__}")
        return dict(parsed)
    raise ToolError(f"unsupported tool argument type {type(raw).__name__}")


__all__ = [
    "Tool",
    "tool",
    "ToolRegistry",
    "infer_schema",
    "parse_tool_arguments",
    "jsonable",
]
