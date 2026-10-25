"""The agent loop.

An agent is a small, explicit state machine:

    system + memory + task  ->  model  ->  tool calls?  ->  execute  ->  model ...
                                        \\-> final answer

Everything that happens is an event, so ``stream()`` and ``run()`` share one
implementation (``run`` simply drives ``stream`` and returns the final result).
Governance, memory and tracing are optional collaborators, not subclasses:
pass a ``guard`` and every tool call is authorised before it runs.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Iterator, Mapping, Sequence

from ..core.errors import (
    ApprovalRequired,
    BudgetExceeded,
    ModelError,
    PolicyDenied,
    RisterError,
    ToolError,
)
from ..core.types import Completion, Config, Message, ToolCall, Usage, as_config, jsonable
from .backend import Model, ScriptedModel
from .memory import Memory
from .prompt import describe_tools, render_instructions
from .tool import Tool, ToolRegistry, parse_tool_arguments

STATUS_OK = "ok"
STATUS_MAX_TURNS = "max_turns"
STATUS_BUDGET = "budget_exceeded"
STATUS_INCOMPLETE = "incomplete"


# --------------------------------------------------------------------------
# results
# --------------------------------------------------------------------------
@dataclass
class AgentResult:
    """Outcome of an agent run."""

    text: str = ""
    data: Any = None
    messages: list[Message] = field(default_factory=list)
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    usage: Usage = field(default_factory=Usage)
    turns: int = 0
    status: str = STATUS_OK
    thread_id: str = ""
    denied: list[dict[str, Any]] = field(default_factory=list)
    agent: str = ""

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    def to_dict(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "thread_id": self.thread_id,
            "text": self.text,
            "data": jsonable(self.data),
            "usage": self.usage.to_dict(),
            "turns": self.turns,
            "status": self.status,
            "tool_calls": jsonable(self.tool_calls),
            "denied": jsonable(self.denied),
            "messages": [m.to_dict() for m in self.messages],
        }


# --------------------------------------------------------------------------
# structured output
# --------------------------------------------------------------------------
_TYPE_NAMES = {
    "str": "string",
    "int": "integer",
    "float": "number",
    "bool": "boolean",
    "list": "array",
    "dict": "object",
}


def _json_type_name(annotation: Any) -> str:
    """Map a type or its (postponed) string form to a JSON schema type."""
    if isinstance(annotation, str):
        return _TYPE_NAMES.get(annotation.strip(), "string")
    return _TYPE_NAMES.get(getattr(annotation, "__name__", ""), "string")


def json_schema_of(spec: Any) -> dict[str, Any]:
    """Normalise a schema spec (JSON schema, dataclass or TypedDict) to JSON schema.

    Handles PEP 563 ("from __future__ import annotations"), where annotations are
    strings rather than types — otherwise typed fields would silently degrade to
    ``string``.
    """
    if spec is None:
        return {}
    if isinstance(spec, Mapping):
        return dict(spec)
    annotations = getattr(spec, "__annotations__", None)
    if annotations is None:
        raise RisterError(f"cannot derive a schema from {spec!r}")

    resolved: dict[str, Any] = dict(annotations)
    try:  # resolve real types when the module scope allows it
        import typing

        resolved.update(typing.get_type_hints(spec))
    except Exception:  # noqa: BLE001 - fall back to the raw annotations
        pass

    properties = {
        name: {"type": _json_type_name(annotation)}
        for name, annotation in resolved.items()
    }
    return {"type": "object", "properties": properties, "required": sorted(resolved)}


_JSON_BLOCK = re.compile(r"```(?:json)?\s*(\{.*?\}|\[.*?\])\s*```", re.S)


def extract_json(text: str) -> Any:
    """Pull the first JSON value out of a model reply (tolerates code fences)."""
    if not text:
        raise RisterError("empty model output, no JSON to parse")
    fenced = _JSON_BLOCK.search(text)
    candidates = [fenced.group(1)] if fenced else []
    stripped = text.strip()
    candidates.append(stripped)
    start = min(
        (i for i in (stripped.find("{"), stripped.find("[")) if i >= 0), default=-1
    )
    if start >= 0:
        candidates.append(stripped[start:])
    for candidate in candidates:
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    raise RisterError(f"could not parse JSON from model output: {text[:200]!r}")


def coerce_structured(spec: Any, payload: Any) -> Any:
    """Validate a parsed payload against a spec and coerce primitive types."""
    schema = json_schema_of(spec)
    if not isinstance(payload, Mapping):
        return payload
    properties = schema.get("properties", {})
    coercers = {
        "integer": lambda v: int(v) if not isinstance(v, bool) else v,
        "number": float,
        "string": lambda v: v if isinstance(v, str) else str(v),
        "boolean": bool,
        "array": lambda v: list(v) if isinstance(v, (list, tuple, set)) else [v],
        "object": lambda v: dict(v) if isinstance(v, Mapping) else v,
    }
    out: dict[str, Any] = {}
    for key, value in payload.items():
        declared = (properties.get(key) or {}).get("type")
        coerce = coercers.get(declared or "")
        try:
            out[key] = coerce(value) if coerce else value
        except (TypeError, ValueError):
            out[key] = value
    missing = [k for k in schema.get("required", []) if k not in out]
    if missing:
        raise RisterError(f"structured output missing required keys: {missing}")
    return out


# --------------------------------------------------------------------------
# agent
# --------------------------------------------------------------------------
class Agent:
    """A model, instructions, tools and memory wired into a loop."""

    def __init__(
        self,
        name: str = "agent",
        *,
        model: Model | None = None,
        instructions: Any = "",
        tools: Iterable[Any] = (),
        memory: Memory | None = None,
        guard: Any = None,
        tracer: Any = None,
        max_turns: int = 8,
        output_schema: Any = None,
        text_tools: bool = False,
        tool_choice: str | None = None,
        on_event: Callable[[dict[str, Any]], None] | None = None,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        if not name:
            raise RisterError("an agent needs a name")
        self.name = name
        self.model = model if model is not None else ScriptedModel([""], name="unset")
        self.instructions = instructions
        self.tools = ToolRegistry(tools)
        self.memory = memory
        self.guard = guard
        self.tracer = tracer
        self.max_turns = max(1, int(max_turns))
        self.output_schema = output_schema
        self.text_tools = text_tools
        self.tool_choice = tool_choice
        self.on_event = on_event
        self.metadata = dict(metadata or {})

    # -- description -------------------------------------------------------
    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Agent {self.name!r} tools={self.tools.names()}>"

    def _system_message(self, cfg: Config) -> str:
        text = render_instructions(
            self.instructions, config=cfg, agent=self, subject=cfg.subject
        )
        if self.text_tools and len(self.tools):
            text = (text + "\n\n" if text else "") + describe_tools(self.tools.specs())
        if self.output_schema:
            schema = json_schema_of(self.output_schema)
            text = (
                (text + "\n\n" if text else "")
                + "Respond with a single JSON object matching this schema and nothing "
                + f"else:\n{json.dumps(schema, indent=2, sort_keys=True)}"
            )
        return text

    def _emit_listener(self, event: dict[str, Any]) -> None:
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:  # noqa: BLE001 - listeners must not break runs
                pass

    def _span(self, name: str, **attributes: Any) -> Any:
        import contextlib

        if self.tracer is None or not hasattr(self.tracer, "span"):
            return contextlib.nullcontext(None)
        return self.tracer.span(name, **attributes)

    # -- public api --------------------------------------------------------
    def run(self, task: str, config: Any = None) -> AgentResult:
        """Run to completion and return the result."""
        result: AgentResult | None = None
        for event in self._events(task, config):
            if event["event"] == "__result__":
                result = event["result"]
        if result is None:  # pragma: no cover
            raise RisterError("agent produced no result")
        return result

    def stream(self, task: str, config: Any = None) -> Iterator[dict[str, Any]]:
        """Yield every event, excluding the internal result envelope."""
        for event in self._events(task, config):
            if event["event"] == "__result__":
                continue
            yield event

    def _events(self, task: str, config: Any = None) -> Iterator[dict[str, Any]]:
        cfg = as_config(config)
        thread = cfg.thread_id

        with self._span(f"agent:{self.name}", thread_id=thread) as span:
            system = self._system_message(cfg)
            messages: list[Message] = []
            if system.strip():
                messages.append(Message.system(system))
            if self.memory is not None:
                messages.extend(self.memory.history(thread))
            user = Message.user(task)
            messages.append(user)
            if self.memory is not None:
                self.memory.append(thread, user)

            usage = Usage()
            budget_stop = False
            executed: list[dict[str, Any]] = []
            denied: list[dict[str, Any]] = []
            turns = 0
            status = STATUS_MAX_TURNS
            text = ""
            data: Any = None

            started = {
                "event": "run_start",
                "agent": self.name,
                "thread_id": thread,
                "task": task,
            }
            self._emit_listener(started)
            yield started

            tool_specs = self.tools.function_specs() if len(self.tools) else None

            while turns < self.max_turns:
                turns += 1
                yield {"event": "turn_start", "turn": turns}

                with self._span(f"model:{self.name}", turn=turns) as model_span:
                    try:
                        completion = self.model.complete(
                            messages,
                            tool_specs if not self.text_tools else None,
                            tool_choice=self.tool_choice,
                        )
                    except BudgetExceeded as exc:
                        # Consuming more is not an option, so end the run here.
                        status = STATUS_BUDGET
                        text = text or f"[budget exhausted: {exc}]"
                        yield {"event": "budget_exceeded", "stage": "model", "detail": str(exc)}
                        break
                    if model_span is not None and hasattr(model_span, "set"):
                        model_span.set("tokens", completion.usage.total_tokens)

                usage = usage + completion.usage
                assistant = completion.message
                messages.append(assistant)
                if self.memory is not None:
                    self.memory.append(thread, assistant)

                calls = list(completion.tool_calls or assistant.tool_calls)
                if not calls and self.text_tools:
                    calls = self._parse_text_tools(assistant.text)

                yield {
                    "event": "model_end",
                    "turn": turns,
                    "text": assistant.text,
                    "tool_calls": [tc.to_dict() for tc in calls],
                    "usage": completion.usage.to_dict(),
                }

                if calls:
                    for call in calls:
                        yield {
                            "event": "tool_start",
                            "turn": turns,
                            "tool": call.name,
                            "arguments": jsonable(call.arguments),
                        }
                        with self._span(
                            f"tool:{call.name}", tool=call.name, turn=turns
                        ) as tool_span:
                            try:
                                # rister:ignore SEC017 -- the registry authorises before the body runs
                                payload, ok, note = self._execute_tool(call, cfg)
                            except BudgetExceeded as exc:
                                status = STATUS_BUDGET
                                text = text or f"[budget exhausted: {exc}]"
                                yield {"event": "budget_exceeded", "stage": "tool",
                                       "tool": call.name, "detail": str(exc)}
                                budget_stop = True
                                break
                            if tool_span is not None:
                                tool_span.set("ok", ok)
                                if not ok:
                                    tool_span.set("refused", note or "")
                        record = {
                            "tool": call.name,
                            "arguments": jsonable(call.arguments),
                            "ok": ok,
                            "result": payload,
                        }
                        if ok:
                            executed.append(record)
                        else:
                            denied.append(record)
                            note and record.update({"reason": note})
                        yield {
                            "event": "tool_end",
                            "turn": turns,
                            "tool": call.name,
                            "ok": ok,
                            "result": payload,
                        }
                        feedback = Message.tool_result(call, _stringify(payload))
                        messages.append(feedback)
                        if self.memory is not None:
                            self.memory.append(thread, feedback)
                    if budget_stop:
                        break
                    continue

                text = assistant.text
                if self.output_schema and text.strip():
                    try:
                        data = coerce_structured(self.output_schema, extract_json(text))
                        status = STATUS_OK
                    except RisterError as exc:
                        data = None
                        status = STATUS_INCOMPLETE
                        text = f"{text}\n\n[structured output error: {exc}]"
                else:
                    status = STATUS_OK
                break

            if turns >= self.max_turns and status == STATUS_MAX_TURNS:
                yield {"event": "max_turns", "turns": turns}

            result = AgentResult(
                text=text,
                data=data,
                messages=[m for m in messages if m.role.value != "system"],
                tool_calls=executed,  # rister:ignore SEC017 -- results of authorised calls
                usage=usage,
                turns=turns,
                status=status,
                thread_id=thread,
                denied=denied,
                agent=self.name,
            )

            if span is not None and hasattr(span, "set"):
                span.set("turns", turns)
                span.set("status", status)

            ended = {
                "event": "run_end",
                "agent": self.name,
                "thread_id": thread,
                "status": status,
                "text": text,
                "usage": usage.to_dict(),
                "turns": turns,
            }
            self._emit_listener(ended)
            yield ended
            yield {"event": "__result__", "result": result}

    # -- tool handling -----------------------------------------------------
    def _execute_tool(  # rister:ignore SEC017 -- delegates to the guarded registry
        self, call: ToolCall, cfg: Config
    ) -> tuple[Any, bool, str | None]:
        """Run one tool call. Never raises: a refusal is fed back to the model."""
        try:
            entry: Tool | None = self.tools.get(call.name)
        except ToolError as exc:
            return f"ERROR: {exc}", False, str(exc)

        if entry is not None and entry.requires_approval and self.guard is None:
            reason = (
                f"tool {call.name!r} requires approval but the agent has no guard; "
                "attach a guard or approve it explicitly"
            )
            return f"APPROVAL_REQUIRED: {reason}", False, reason

        try:
            arguments = parse_tool_arguments(call.arguments)
            value = self.tools.call(
                call.name, arguments, config=cfg, guard=self.guard
            )
            return value, True, None
        except (PolicyDenied, ApprovalRequired) as exc:
            return f"DENIED: {exc}", False, str(exc)
        except ToolError as exc:
            return f"ERROR: {exc}", False, str(exc)

    def _parse_text_tools(self, text: str) -> list[ToolCall]:
        """Recover tool calls from JSON emitted as plain text."""
        if not text or "{" not in text:
            return []
        try:
            payload = extract_json(text)
        except RisterError:
            return []
        if not isinstance(payload, Mapping) or "tool" not in payload:
            return []
        try:
            return [
                ToolCall(
                    name=str(payload["tool"]),
                    arguments=payload.get("arguments") or payload.get("args") or {},
                )
            ]
        except Exception:  # noqa: BLE001 - malformed suggestion is ignored
            return []

    # -- integrations ------------------------------------------------------
    def as_node(
        self,
        *,
        input_key: str = "task",
        output_key: str = "output",
        extra_keys: Sequence[str] = (),
    ) -> Callable[[Any, Any], dict[str, Any]]:
        """Return a graph node that runs this agent.

            graph.add_node("writer", writer.as_node(input_key="topic"))

        The node reads ``state[input_key]`` and writes ``state[output_key]``.
        """

        def node(state: Any, ctx: Any = None) -> dict[str, Any]:
            task = state.get(input_key)
            if task is None:
                raise RisterError(
                    f"agent {self.name!r} node expected state field {input_key!r}"
                )
            cfg = getattr(ctx, "config", None) or Config()
            result = self.run(str(task), cfg)
            update: dict[str, Any] = {output_key: result.text, "usage_total": result.usage.total_tokens}
            for key in extra_keys:
                update[key] = getattr(result, key, None)
            if ctx is not None and hasattr(ctx, "emit"):
                ctx.emit("agent", agent=self.name, turns=result.turns, status=result.status)
            return update

        node.__name__ = f"agent_{self.name}"
        return node

    def as_tool(
        self,
        *,
        name: str | None = None,
        description: str | None = None,
    ) -> Tool:
        """Expose this agent as a tool another agent can call."""
        tool_name = name or f"ask_{re.sub(r'[^a-zA-Z0-9_]+', '_', self.name).strip('_')}"

        def delegate(task: str) -> str:
            return self.run(task).text

        return Tool(
            name=tool_name,
            fn=delegate,
            description=description or f"Delegate a task to the {self.name} agent.",
            parameters={
                "type": "object",
                "properties": {"task": {"type": "string"}},
                "required": ["task"],
            },
        )

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "model": getattr(self.model, "name", type(self.model).__name__),
            "tools": self.tools.specs(),
            "max_turns": self.max_turns,
            "structured": bool(self.output_schema),
            "memory": type(self.memory).__name__ if self.memory else None,
        }


def _stringify(value: Any) -> str:
    if isinstance(value, str):
        return value
    try:
        return json.dumps(jsonable(value), sort_keys=True, default=str)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        return str(value)


__all__ = [
    "Agent",
    "AgentResult",
    "STATUS_OK",
    "STATUS_MAX_TURNS",
    "STATUS_INCOMPLETE",
    "extract_json",
    "coerce_structured",
    "json_schema_of",
]
