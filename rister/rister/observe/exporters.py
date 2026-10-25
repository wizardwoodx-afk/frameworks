"""Trace exporters."""
from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any, Callable, Mapping


class Exporter:
    """Consumes one trace payload (``{"tracer", "spans", "summary"}``)."""

    def export(self, payload: Mapping[str, Any]) -> None:  # pragma: no cover - interface
        raise NotImplementedError


class JSONLExporter(Exporter):
    """Append one JSON object per trace to a file."""

    def __init__(self, path: str | Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)

    def export(self, payload: Mapping[str, Any]) -> None:
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(payload, default=str, sort_keys=True) + "\n")


class ConsoleExporter(Exporter):
    """Print a compact tree of finished traces."""

    def __init__(self, stream: Any = None, show_attributes: bool = False) -> None:
        self.stream = stream or sys.stdout
        self.show_attributes = show_attributes

    def export(self, payload: Mapping[str, Any]) -> None:
        spans = payload.get("spans") or []
        summary = payload.get("summary") or {}
        print(
            f"trace: {summary.get('spans', 0)} spans, "
            f"{summary.get('total_ms', 0)}ms, {summary.get('errors', 0)} errors",
            file=self.stream,
        )
        children: dict[str | None, list[Mapping[str, Any]]] = {}
        for span in spans:
            children.setdefault(span.get("parent_id"), []).append(span)

        def render(parent: str | None, depth: int) -> None:
            for span in children.get(parent, []):
                status = "" if span.get("status") == "ok" else f" [{span.get('status')}]"
                print(
                    f"{'  ' * depth}- {span['name']} {span['duration_ms']}ms{status}",
                    file=self.stream,
                )
                if self.show_attributes and span.get("attributes"):
                    print(f"{'  ' * (depth + 1)}{span['attributes']}", file=self.stream)
                render(span["id"], depth + 1)

        render(None, 0)


class CallableExporter(Exporter):
    """Hand the payload to any function."""

    def __init__(self, fn: Callable[[Mapping[str, Any]], Any]) -> None:
        self.fn = fn

    def export(self, payload: Mapping[str, Any]) -> None:
        self.fn(payload)


__all__ = ["Exporter", "JSONLExporter", "ConsoleExporter", "CallableExporter"]
