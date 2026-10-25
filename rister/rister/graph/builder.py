"""Graph construction.

The builder is intentionally tiny: nodes, static edges, conditional edges and a
compiler. Validation happens at compile time so mistakes surface before any
model call is paid for.
"""
from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any, Callable, Mapping, Sequence

from ..core.errors import GraphError
from ..state import State
from .command import END, START, Command


@dataclass
class Node:
    """A unit of work in the graph."""

    name: str
    fn: Callable[..., Any]
    retry: int = 0
    metadata: dict[str, Any] = field(default_factory=dict)
    arity: int = 2
    subgraph: Any = None

    def __post_init__(self) -> None:
        if callable(self.fn):
            self.arity = _arity(self.fn)


def _arity(fn: Callable[..., Any]) -> int:
    """How many positional arguments does this node accept (1 or 2)?"""
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):  # builtins / exotic callables
        return 2
    positional = [
        p
        for p in sig.parameters.values()
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD)
        and p.default is p.empty
    ]
    return 1 if len(positional) <= 1 else 2


@dataclass
class Conditional:
    """A router plus an optional mapping from router output to node names."""

    router: Callable[[State], Any]
    mapping: dict[str, Sequence[str] | str] | None = None


class Graph:
    """Declare a stateful computation as nodes and edges."""

    def __init__(self, state_cls: type[State] = State, name: str = "graph") -> None:
        if not (isinstance(state_cls, type) and issubclass(state_cls, State)):
            raise GraphError("state_cls must be a State subclass")
        self.state_cls = state_cls
        self.name = name
        self.nodes: dict[str, Node] = {}
        self.edges: dict[str, list[str]] = {}
        self.conditionals: dict[str, Conditional] = {}
        self._entry: str | None = None

    # -- nodes -------------------------------------------------------------
    def add_node(
        self,
        name: str,
        fn: Any,
        *,
        retry: int = 0,
        metadata: Mapping[str, Any] | None = None,
    ) -> str:
        if name in (START, END):
            raise GraphError(f"{name!r} is reserved")
        if name in self.nodes:
            raise GraphError(f"node {name!r} already exists")
        if retry < 0:
            raise GraphError("retry must be >= 0")

        subgraph = None
        if hasattr(fn, "invoke") and hasattr(fn, "state_cls"):  # CompiledGraph
            subgraph = fn
            fn = _subgraph_adapter(subgraph)

        if not callable(fn):
            raise GraphError(f"node {name!r} is not callable")
        self.nodes[name] = Node(
            name=name, fn=fn, retry=retry, metadata=dict(metadata or {}), subgraph=subgraph
        )
        return name

    def add_subgraph(self, name: str, compiled: Any, *, retry: int = 0) -> str:
        """Mount a compiled graph as a node (its value diff becomes the update)."""
        if not (hasattr(compiled, "invoke") and hasattr(compiled, "state_cls")):
            raise GraphError("add_subgraph expects a compiled graph")
        return self.add_node(name, compiled, retry=retry)

    # -- edges -------------------------------------------------------------
    def add_edge(self, src: str, dst: str) -> None:
        self._check_source(src)
        if dst != END:
            self._check_target(dst)
        if src in self.conditionals:
            raise GraphError(f"node {src!r} already has conditional edges")
        if src == START:
            self._entry = dst
            return
        targets = self.edges.setdefault(src, [])
        if dst not in targets:
            targets.append(dst)

    def set_entry(self, node: str) -> None:
        self.add_edge(START, node)

    def add_conditional_edges(
        self,
        src: str,
        router: Callable[[State], Any],
        mapping: Mapping[str, Sequence[str] | str] | None = None,
    ) -> None:
        self._check_source(src)
        if src in self.edges:
            raise GraphError(f"node {src!r} already has static edges")
        if src in self.conditionals:
            raise GraphError(f"node {src!r} already has conditional edges")
        if not callable(router):
            raise GraphError("router must be callable")
        if mapping:
            for value in mapping.values():
                for target in ([value] if isinstance(value, str) else value):
                    if target != END:
                        self._check_target(target)
        self.conditionals[src] = Conditional(router, dict(mapping) if mapping else None)

    # -- introspection -----------------------------------------------------
    def entry(self) -> str:
        if not self._entry:
            raise GraphError("graph has no entry point (add_edge(START, '<node>'))")
        if self._entry not in self.nodes:
            raise GraphError(f"entry {self._entry!r} is not a node")
        return self._entry

    def successors(self, node: str) -> list[str]:
        return list(self.edges.get(node, []))

    def validate(self, strict: bool = False) -> list[str]:
        """Raise on structural errors; return advisory warnings.

        Unreachable nodes are a *warning*, not an error: nodes can be reached
        dynamically through ``Command(goto=...)`` or a router that returns a
        node name directly, neither of which is visible in the static graph.
        Pass ``strict=True`` (``compile(strict=True)``) to escalate warnings.
        """
        entry = self.entry()
        incoming = self._incoming()
        for name in self.nodes:
            for target in self.edges.get(name, []):
                if target != END and target not in self.nodes:
                    raise GraphError(f"edge {name!r} -> {target!r} points at a missing node")

        warnings: list[str] = []
        for name in self.nodes:
            if name != entry and name not in incoming:
                warnings.append(
                    f"node {name!r} has no incoming edge (reachable only via "
                    f"Command routing or not at all)"
                )
            has_static = bool(self.edges.get(name))
            has_cond = name in self.conditionals
            if not has_static and not has_cond:
                warnings.append(
                    f"node {name!r} has no outgoing edge; the run will stop after it"
                )
        if strict and warnings:
            raise GraphError("; ".join(warnings))
        return warnings

    def _incoming(self) -> set[str]:
        found = {self._entry} if self._entry else set()
        for targets in self.edges.values():
            found.update(t for t in targets if t != END)
        for cond in self.conditionals.values():
            for value in (cond.mapping or {}).values():
                found.update([value] if isinstance(value, str) else value)
        return found

    def _check_source(self, src: str) -> None:
        if src != START and src not in self.nodes:
            raise GraphError(f"unknown source node {src!r}")

    def _check_target(self, dst: str) -> None:
        if dst not in self.nodes:
            raise GraphError(f"unknown target node {dst!r}")

    # -- compile -----------------------------------------------------------
    def compile(
        self,
        *,
        store: Any = None,
        max_steps: int = 100,
        tracer: Any = None,
        strict: bool = False,
    ) -> Any:
        from .engine import CompiledGraph

        self.warnings = self.validate(strict=strict)
        return CompiledGraph(self, store=store, max_steps=max_steps, tracer=tracer)

    # -- diagram -----------------------------------------------------------
    def to_dot(self) -> str:
        """Render the topology as Graphviz DOT (handy for docs and debugging)."""
        lines = [f'digraph {self.name!r} {{', "  rankdir=LR;", f'  {START} [shape=point];']
        for name in self.nodes:
            shape = "box" if name in self.conditionals else "ellipse"
            lines.append(f"  {name!r} [shape={shape}];")
        lines.append(f'  {END} [shape=doublecircle];')
        lines.append(f"  {START} -> {self._entry!r};" if self._entry else "")
        for src, targets in self.edges.items():
            for dst in targets:
                lines.append(f"  {src!r} -> {dst!r};")
        for src, cond in self.conditionals.items():
            for key in (cond.mapping or {}):
                for dst in _as_list((cond.mapping or {})[key]):
                    lines.append(f"  {src!r} -> {dst!r} [label={str(key)!r}, style=dashed];")
        lines.append("}")
        return "\n".join(line for line in lines if line)


def _as_list(value: Sequence[str] | str) -> list[str]:
    return [value] if isinstance(value, str) else list(value)


def _subgraph_adapter(compiled: Any) -> Callable[..., Any]:
    """Wrap a compiled graph so it can be mounted as a node.

    The child receives the parent's values; only the keys it *changed* are
    returned, so append-style reducers on the parent do not double-count data
    that merely passed through.
    """

    def run_subgraph(state: State, ctx: Any = None) -> dict[str, Any]:
        before = state.to_dict()
        result = compiled.invoke(before, config={"thread_id": f"{getattr(ctx, 'thread_id', 'sub')}:{ctx.node if ctx else 'sub'}"})
        after = dict(result.values)
        return {k: v for k, v in after.items() if before.get(k) != v}

    run_subgraph.__name__ = f"subgraph_{getattr(compiled, 'name', 'graph')}"
    return run_subgraph


__all__ = ["Node", "Conditional", "Graph", "START", "END", "Command"]
