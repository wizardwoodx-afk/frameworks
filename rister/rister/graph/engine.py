"""Superstep execution engine.

Semantics (deliberate and documented):

* A *superstep* runs the current frontier. Every node in a frontier sees the
  **same** input state, exactly like a bulk-synchronous round.
* Nodes return partial updates; the updates are merged, in frontier order,
  through the state reducers. Merging is deterministic.
* After merging, routing is decided: an explicit ``Command`` wins, then
  conditional edges (evaluated against the merged state), then static edges.
* A snapshot is written after every superstep and before returning on an
  interrupt, so any thread can be resumed or replayed from disk.
* ``max_steps`` bounds runaway loops; a node may be given a ``retry`` budget.
"""
from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any, Callable, Iterator, Mapping

from ..core.errors import GraphError, Interrupt, NodeError, NotFound
from ..core.types import Config, as_config, jsonable
from ..state import State
from .command import END, Command
from .snapshot import (
    STATUS_INTERRUPTED,
    STATUS_OK,
    STATUS_RUNNING,
    MemorySnapshotStore,
    Snapshot,
    SnapshotStore,
)

_UNSET = object()


# --------------------------------------------------------------------------
# per-node execution context
# --------------------------------------------------------------------------
@dataclass
class NodeContext:
    """Runtime handle passed to nodes that accept two arguments."""

    node: str
    step: int
    thread_id: str
    config: Config
    resume_value: Any = _UNSET
    events: list[dict[str, Any]] = field(default_factory=list)
    _listener: Callable[[dict[str, Any]], None] | None = None
    _resume_pending: bool = False
    _interrupt_count: int = 0

    # -- state -------------------------------------------------------------
    @property
    def resumed(self) -> bool:
        """True when this execution is continuing after an interrupt."""
        return self.resume_value is not _UNSET

    @property
    def subject(self) -> Any:
        return self.config.subject

    # -- observability -----------------------------------------------------
    def emit(self, name: str, **data: Any) -> dict[str, Any]:
        """Publish a custom stream event."""
        event = {
            "event": "custom",
            "name": name,
            "node": self.node,
            "step": self.step,
            "data": jsonable(data),
        }
        self.events.append(event)
        if self._listener is not None:
            self._listener(event)
        return event

    def log(self, message: str, **data: Any) -> dict[str, Any]:
        return self.emit("log", message=message, **data)

    # -- human in the loop -------------------------------------------------
    def interrupt(self, payload: Any = None) -> Any:
        """Pause the run and hand ``payload`` to the caller.

        On the first call in a fresh execution this raises :class:`Interrupt`.
        When the run is later resumed, the same call returns the value supplied
        to ``resume(...)``, so node code reads as a straight line.
        """
        if self._resume_pending:
            self._resume_pending = False
            self._interrupt_count += 1
            return self.resume_value
        self._interrupt_count += 1
        raise Interrupt(payload)


# --------------------------------------------------------------------------
# result
# --------------------------------------------------------------------------
@dataclass
class RunResult:
    """Outcome of a graph run."""

    thread_id: str
    status: str
    values: dict[str, Any]
    steps: int = 0
    interrupt: Any = None
    node: str | None = None

    @property
    def ok(self) -> bool:
        return self.status == STATUS_OK

    @property
    def interrupted(self) -> bool:
        return self.status == STATUS_INTERRUPTED

    def to_dict(self) -> dict[str, Any]:
        return {
            "thread_id": self.thread_id,
            "status": self.status,
            "values": jsonable(self.values),
            "steps": self.steps,
            "interrupt": jsonable(self.interrupt),
            "node": self.node,
        }


# --------------------------------------------------------------------------
# compiled graph
# --------------------------------------------------------------------------
class CompiledGraph:
    """An executable graph: invoke, stream, resume, replay."""

    def __init__(
        self,
        graph: Any,
        *,
        store: SnapshotStore | None = None,
        max_steps: int = 100,
        tracer: Any = None,
    ) -> None:
        self.graph = graph
        self.name = graph.name
        self.state_cls: type[State] = graph.state_cls
        self.store: SnapshotStore = store if store is not None else MemorySnapshotStore()
        self.max_steps = max_steps
        self.tracer = tracer

    # -- public api --------------------------------------------------------
    def invoke(
        self,
        initial: "State | Mapping[str, Any] | None" = None,
        config: "Config | Mapping[str, Any] | None" = None,
    ) -> RunResult:
        result: RunResult | None = None
        for event in self._execute(initial, as_config(config), _UNSET):
            if event["event"] == "__result__":
                result = event["result"]
        if result is None:  # pragma: no cover - engine always reports
            raise GraphError("run produced no result")
        return result

    def stream(
        self,
        initial: "State | Mapping[str, Any] | None" = None,
        config: "Config | Mapping[str, Any] | None" = None,
    ) -> Iterator[dict[str, Any]]:
        for event in self._execute(initial, as_config(config), _UNSET):
            if event["event"] == "__result__":
                continue
            yield event

    def resume(
        self,
        value: Any = None,
        config: "Config | Mapping[str, Any] | None" = None,
    ) -> RunResult:
        cfg = as_config(config)
        result: RunResult | None = None
        for event in self._execute(None, cfg, value):
            if event["event"] == "__result__":
                result = event["result"]
        if result is None:  # pragma: no cover
            raise GraphError("resume produced no result")
        return result

    # -- introspection -----------------------------------------------------
    def get_state(self, thread_id: str) -> Snapshot | None:
        return self.store.load(thread_id)

    def history(self, thread_id: str) -> list[Snapshot]:
        return self.store.history(thread_id)

    def replay(self, thread_id: str, step: int) -> Snapshot:
        """Return the snapshot recorded at a given step (time travel)."""
        for snap in self.store.history(thread_id):
            if snap.step == step:
                return snap
        raise NotFound(f"thread {thread_id!r} has no snapshot at step {step}")

    def reset(self, thread_id: str) -> None:
        self.store.clear(thread_id)

    def to_dot(self) -> str:
        return self.graph.to_dot()

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<CompiledGraph {self.name!r} nodes={len(self.graph.nodes)}>"

    # -- engine ------------------------------------------------------------
    def _span(self, name: str, **attributes: Any) -> Any:
        tracer = self.tracer
        if tracer is None or not hasattr(tracer, "span"):
            return contextlib.nullcontext(None)
        return tracer.span(name, **attributes)

    def _execute(
        self,
        initial: "State | Mapping[str, Any] | None",
        cfg: Config,
        resume: Any,
    ) -> Iterator[dict[str, Any]]:
        thread = cfg.thread_id
        tracer = self.tracer or cfg.tracer

        with self._span(f"run:{self.name}", thread_id=thread) as run_span:
            # ---- reconstruct or seed state -----------------------------
            if initial is None:
                snap = self.store.load(thread)
                if snap is None:
                    raise NotFound(f"no snapshot recorded for thread {thread!r}")
                state = self.state_cls.from_dict(snap.values)
                frontier = list(snap.next)
                step = snap.step
                resume_value = resume
                if not frontier:
                    result = RunResult(
                        thread, snap.status, state.to_dict(), step, snap.interrupt
                    )
                    yield {"event": "run_start", "thread_id": thread, "values": state.to_dict()}
                    yield {
                        "event": "run_end",
                        "thread_id": thread,
                        "status": result.status,
                        "values": result.values,
                        "steps": step,
                    }
                    yield {"event": "__result__", "result": result}
                    return
            else:
                state = (
                    initial
                    if isinstance(initial, State)
                    else self.state_cls(**(dict(initial) if initial else {}))
                )
                if not isinstance(state, self.state_cls):
                    # A different State subclass was supplied: re-project it.
                    state = self.state_cls(**state.to_dict())
                frontier = [self.graph.entry()]
                step = 0
                resume_value = _UNSET

            yield {
                "event": "run_start",
                "thread_id": thread,
                "graph": self.name,
                "values": state.to_dict(),
            }

            # ---- superstep loop ----------------------------------------
            while frontier:
                step += 1
                if step > self.max_steps:
                    raise GraphError(
                        f"graph {self.name!r} exceeded max_steps={self.max_steps} "
                        f"(possible cycle); last frontier={frontier}"
                    )

                base = state
                deltas: list[dict[str, Any]] = []
                explicit: dict[str, list[str]] = {}
                interrupted_node: str | None = None
                interrupt_payload: Any = None

                for name in frontier:
                    node = self.graph.nodes.get(name)
                    if node is None:
                        raise GraphError(f"frontier references unknown node {name!r}")

                    yield {"event": "node_start", "node": name, "step": step}

                    ctx = NodeContext(
                        node=name,
                        step=step,
                        thread_id=thread,
                        config=cfg,
                        resume_value=resume_value,
                        _resume_pending=resume_value is not _UNSET,
                    )
                    try:
                        with self._span(f"node:{name}", step=step, node=name) as node_span:
                            output = self._call_node(node, base, ctx)
                            if node_span is not None and hasattr(node_span, "set"):
                                node_span.set("step", step)
                    except Interrupt as exc:
                        interrupted_node = name
                        interrupt_payload = exc.payload
                        break

                    if isinstance(output, Command):
                        if output.update:
                            deltas.append(dict(output.update))
                        explicit[name] = output.targets()
                        summary: dict[str, Any] = dict(output.update)
                    elif output is None:
                        summary = {}
                    elif isinstance(output, Mapping):
                        deltas.append(dict(output))
                        summary = dict(output)
                    else:
                        raise GraphError(
                            f"node {name!r} returned unsupported type "
                            f"{type(output).__name__}; expected mapping, Command or None"
                        )

                    yield {
                        "event": "node_end",
                        "node": name,
                        "step": step,
                        "update": jsonable(summary),
                    }

                # merge this superstep's updates in frontier order
                for delta in deltas:
                    state = state.update(delta)

                if interrupted_node is not None:
                    snap = Snapshot(
                        thread_id=thread,
                        step=step,
                        values=state.to_dict(),
                        next=[interrupted_node],
                        status=STATUS_INTERRUPTED,
                        interrupt=interrupt_payload,
                    )
                    self.store.save(snap)
                    yield {
                        "event": "interrupt",
                        "node": interrupted_node,
                        "step": step,
                        "payload": jsonable(interrupt_payload),
                    }
                    yield {
                        "event": "run_end",
                        "thread_id": thread,
                        "status": STATUS_INTERRUPTED,
                        "values": state.to_dict(),
                        "steps": step,
                    }
                    yield {
                        "event": "__result__",
                        "result": RunResult(
                            thread,
                            STATUS_INTERRUPTED,
                            state.to_dict(),
                            step,
                            interrupt_payload,
                            interrupted_node,
                        ),
                    }
                    return

                # ---- routing -------------------------------------------
                next_frontier: list[str] = []
                for name in frontier:
                    candidates = self._route(name, state, explicit)
                    for candidate in candidates:
                        if candidate == END:
                            continue
                        if candidate not in self.graph.nodes:
                            raise GraphError(
                                f"routing from {name!r} produced unknown node {candidate!r}"
                            )
                        if candidate not in next_frontier:
                            next_frontier.append(candidate)

                status = STATUS_OK if not next_frontier else STATUS_RUNNING
                self.store.save(
                    Snapshot(
                        thread_id=thread,
                        step=step,
                        values=state.to_dict(),
                        next=next_frontier,
                        status=status,
                    )
                )

                if not next_frontier:
                    if run_span is not None and hasattr(run_span, "set"):
                        run_span.set("steps", step)
                    yield {
                        "event": "run_end",
                        "thread_id": thread,
                        "status": STATUS_OK,
                        "values": state.to_dict(),
                        "steps": step,
                    }
                    yield {
                        "event": "__result__",
                        "result": RunResult(thread, STATUS_OK, state.to_dict(), step),
                    }
                    return

                frontier = next_frontier
                # A resume value is only meaningful for the node that paused.
                resume_value = _UNSET

            # A frontier-less graph (single node, no edges) finishes here.
            self.store.save(
                Snapshot(
                    thread_id=thread,
                    step=step,
                    values=state.to_dict(),
                    next=[],
                    status=STATUS_OK,
                )
            )
            yield {
                "event": "run_end",
                "thread_id": thread,
                "status": STATUS_OK,
                "values": state.to_dict(),
                "steps": step,
            }
            yield {
                "event": "__result__",
                "result": RunResult(thread, STATUS_OK, state.to_dict(), step),
            }

    # -- helpers -----------------------------------------------------------
    def _call_node(self, node: Any, state: State, ctx: NodeContext) -> Any:
        attempts = node.retry + 1
        last: BaseException | None = None
        for attempt in range(1, attempts + 1):
            try:
                return node.fn(state, ctx) if node.arity >= 2 else node.fn(state)
            except Interrupt:
                raise
            except Exception as exc:  # noqa: BLE001 - retried, then wrapped
                last = exc
                if attempt < attempts:
                    ctx.emit("retry", attempt=attempt, error=f"{type(exc).__name__}: {exc}")
                    continue
        raise NodeError(node.name, last, attempts) from last

    def _route(
        self, name: str, state: State, explicit: Mapping[str, list[str]]
    ) -> list[str]:
        if name in explicit:
            return list(explicit[name])

        conditional = self.graph.conditionals.get(name)
        if conditional is not None:
            decision = conditional.router(state)
            if isinstance(decision, Command):
                if decision.update:
                    raise GraphError(
                        "routers may return a Command for routing only; "
                        "return a node name (or list) and put updates in the node"
                    )
                return decision.targets()
            if isinstance(decision, (list, tuple, set)):
                return [str(d) for d in decision]
            key = decision
            mapping = conditional.mapping
            if mapping is not None:
                if key not in mapping:
                    raise GraphError(
                        f"router for {name!r} returned {key!r} which is not in "
                        f"the mapping {sorted(mapping)}"
                    )
                target = mapping[key]
                return [target] if isinstance(target, str) else list(target)
            return [str(key)]

        return self.graph.successors(name)


__all__ = ["NodeContext", "RunResult", "CompiledGraph"]
