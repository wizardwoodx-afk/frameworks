"""A small, dependency-free tracer.

Spans nest through a thread-local stack, so any function can create a child span
without threading a parent object through its signature. Graphs, agents, model
calls and tool calls all emit spans this way, which makes a single trace tell you
exactly where time and tokens went.
"""
from __future__ import annotations

import contextlib
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Mapping

from ..core.errors import Interrupt, Suspend
from ..core.types import jsonable, new_id

_ACTIVE = threading.local()


def _stack() -> list["Span"]:
    stack = getattr(_ACTIVE, "stack", None)
    if stack is None:
        stack = []
        _ACTIVE.stack = stack
    return stack


@dataclass
class Span:
    """One timed unit of work."""

    name: str
    attributes: dict[str, Any] = field(default_factory=dict)
    id: str = field(default_factory=lambda: new_id("span"))
    parent_id: str | None = None
    start: float = field(default_factory=time.perf_counter)
    end: float | None = None
    status: str = "running"
    error: str | None = None
    events: list[dict[str, Any]] = field(default_factory=list)

    # -- mutation ----------------------------------------------------------
    def set(self, key: str, value: Any) -> "Span":
        self.attributes[key] = value
        return self

    def update(self, **attributes: Any) -> "Span":
        self.attributes.update(attributes)
        return self

    def event(self, name: str, **data: Any) -> "Span":
        self.events.append({"name": name, "at": time.perf_counter(), "data": data})
        return self

    def finish(self, status: str = "ok", error: str | None = None) -> "Span":
        if self.end is None:
            self.end = time.perf_counter()
        self.status = status
        if error:
            self.error = error
        return self

    # -- views -------------------------------------------------------------
    @property
    def duration_ms(self) -> float:
        end = self.end if self.end is not None else time.perf_counter()
        return round((end - self.start) * 1000, 3)

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "parent_id": self.parent_id,
            "name": self.name,
            "status": self.status,
            "error": self.error,
            "duration_ms": self.duration_ms,
            "start": round(self.start, 6),
            "attributes": jsonable(self.attributes),
            "events": [
                {"name": e["name"], "data": jsonable(e["data"])} for e in self.events
            ],
        }

    def __repr__(self) -> str:  # pragma: no cover
        return f"<Span {self.name!r} {self.duration_ms}ms {self.status}>"


class Tracer:
    """Collects spans and hands finished traces to exporters."""

    def __init__(
        self,
        name: str = "rister",
        exporters: list[Any] | None = None,
        enabled: bool = True,
    ) -> None:
        self.name = name
        self.exporters = list(exporters or [])
        self.enabled = enabled
        self.spans: list[Span] = []
        self._lock = threading.RLock()

    # -- span lifecycle ----------------------------------------------------
    @contextlib.contextmanager
    def span(self, name: str, **attributes: Any) -> Iterator[Span | None]:
        """Create a nested span; records failures automatically."""
        if not self.enabled:
            yield None
            return

        stack = _stack()
        parent = stack[-1] if stack else None
        span = Span(
            name=name,
            attributes=dict(attributes),
            parent_id=parent.id if parent else None,
        )
        stack.append(span)
        try:
            yield span
        except (Interrupt, Suspend) as exc:
            # A pause is expected control flow, not a failure.
            span.finish(status="interrupted", error=str(exc))
            raise
        except BaseException as exc:  # record, then propagate untouched
            span.finish(status="error", error=f"{type(exc).__name__}: {exc}")
            raise
        finally:
            if stack and stack[-1] is span:
                stack.pop()
            if span.end is None:
                span.finish()
            with self._lock:
                self.spans.append(span)

    # -- reporting ---------------------------------------------------------
    def roots(self) -> list[Span]:
        return [s for s in self.spans if s.parent_id is None]

    def children_of(self, span: Span) -> list[Span]:
        return [s for s in self.spans if s.parent_id == span.id]

    def total_ms(self) -> float:
        return round(sum(s.duration_ms for s in self.roots()), 3)

    def summary(self) -> dict[str, Any]:
        by_name: dict[str, dict[str, Any]] = {}
        for span in self.spans:
            bucket = by_name.setdefault(
                span.name, {"count": 0, "total_ms": 0.0, "errors": 0, "max_ms": 0.0}
            )
            bucket["count"] += 1
            bucket["total_ms"] = round(bucket["total_ms"] + span.duration_ms, 3)
            bucket["max_ms"] = max(bucket["max_ms"], span.duration_ms)
            if span.status == "error":
                bucket["errors"] += 1
        return {
            "tracer": self.name,
            "spans": len(self.spans),
            "errors": sum(1 for s in self.spans if s.status == "error"),
            "total_ms": self.total_ms(),
            "by_name": dict(sorted(by_name.items())),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "tracer": self.name,
            "spans": [s.to_dict() for s in self.spans],
            "summary": self.summary(),
        }

    # -- export ------------------------------------------------------------
    def export(self) -> None:
        payload = self.to_dict()
        for exporter in self.exporters:
            try:
                exporter.export(payload)
            except Exception:  # noqa: BLE001 - an exporter must not break a run
                continue

    def clear(self) -> None:
        with self._lock:
            self.spans.clear()

    def __len__(self) -> int:
        return len(self.spans)


# --------------------------------------------------------------------------
# ambient tracer
# --------------------------------------------------------------------------
_DEFAULT = Tracer(name="rister-default")


def get_tracer() -> Tracer:
    """Return the ambient tracer (a process-wide default)."""
    tracer = getattr(_ACTIVE, "tracer", None)
    return tracer if tracer is not None else _DEFAULT


def set_tracer(tracer: Tracer | None) -> Tracer:
    """Install an ambient tracer for the current thread."""
    previous = get_tracer()
    _ACTIVE.tracer = tracer
    return previous


def current_tracer() -> Tracer:
    return get_tracer()


__all__ = ["Span", "Tracer", "get_tracer", "set_tracer", "current_tracer"]
