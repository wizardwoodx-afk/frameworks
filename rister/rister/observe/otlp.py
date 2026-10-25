"""Export RISTER traces as OpenTelemetry OTLP/JSON.

Any backend that accepts OTLP over HTTP can receive a RISTER trace without a
collector-side translation layer: the exporter builds the resource/scope/span
envelope the specification defines and posts it with ``urllib``.

    tracer = Tracer("prod", exporters=[OtlpExporter("http://localhost:4318/v1/traces")])

IDs are derived deterministically from the trace, so re-exporting the same run
produces the same trace id — which is what makes retries idempotent on the
collector side.
"""
from __future__ import annotations

import hashlib
import json
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Mapping

from ..core.meta import __version__
from .exporters import Exporter

# OTLP span status codes.
STATUS_UNSET = 0
STATUS_OK = 1
STATUS_ERROR = 2

# OTLP span kinds.
SPAN_INTERNAL = 1

DEFAULT_ENDPOINT = "http://localhost:4318/v1/traces"
SERVICE_NAME = "rister"


def _hex_id(*parts: Any, length: int = 16) -> str:
    """Deterministic hex identifier of ``length`` characters (never all zero).

    OTLP requires 32 hex characters for a trace id (16 bytes) and 16 for a span id
    (8 bytes), so callers pass the width explicitly.
    """
    digest = hashlib.blake2b(digest_size=length // 2)
    for part in parts:
        digest.update(str(part).encode("utf-8"))
        digest.update(b"\x00")
    value = digest.hexdigest()
    return value if value.strip("0") else "1" + value[1:]


def any_value(value: Any) -> dict[str, Any]:
    """Encode a Python value as an OTLP ``AnyValue``."""
    if isinstance(value, bool):
        return {"boolValue": value}
    if isinstance(value, int):
        # proto3 JSON represents 64-bit integers as strings.
        return {"intValue": str(value)}
    if isinstance(value, float):
        return {"doubleValue": value}
    if isinstance(value, (list, tuple)):
        return {"arrayValue": {"values": [any_value(item) for item in value]}}
    if isinstance(value, Mapping):
        return {
            "kvlistValue": {
                "values": [
                    {"key": str(key), "value": any_value(item)}
                    for key, item in value.items()
                ]
            }
        }
    return {"stringValue": "" if value is None else str(value)}


def attributes(pairs: Mapping[str, Any] | None) -> list[dict[str, Any]]:
    """Encode a mapping as OTLP ``KeyValue`` entries, dropping None values."""
    return [
        {"key": str(key), "value": any_value(value)}
        for key, value in (pairs or {}).items()
        if value is not None
    ]


@dataclass
class OtlpExporter(Exporter):
    """POST traces to an OTLP/HTTP endpoint as JSON.

    ``dry_run=True`` keeps the payload in :attr:`last_payload` and sends nothing,
    which is how the format is tested without a collector.
    """

    endpoint: str = DEFAULT_ENDPOINT
    headers: Mapping[str, str] = field(default_factory=dict)
    service_name: str = SERVICE_NAME
    service_version: str = __version__
    timeout: float = 10.0
    dry_run: bool = False
    last_payload: dict[str, Any] | None = field(default=None, init=False)
    _anchors: dict[str, int] = field(default_factory=dict, init=False, repr=False)
    sent: int = field(default=0, init=False)
    failures: int = field(default=0, init=False)
    last_error: str | None = field(default=None, init=False)

    # -- conversion --------------------------------------------------------
    def to_otlp(self, payload: Mapping[str, Any]) -> dict[str, Any]:
        """Build the OTLP ``ExportTraceServiceRequest`` for one tracer payload."""
        trace_name = str(payload.get("tracer", "rister"))
        summary = payload.get("summary") or {}
        trace_id = _hex_id(
            "trace", trace_name, summary.get("spans"), summary.get("total_ms"), length=32
        )

        span_payloads = list(payload.get("spans") or [])
        # Map RISTER span ids to OTLP ids, keeping parent links intact.
        id_map = {
            str(span.get("id")): _hex_id("span", trace_id, span.get("name"), span.get("id"))
            for span in span_payloads
        }
        # Anchor the perf-counter origin to wall clock once per trace, so that
        # re-exporting the same trace (a retry) produces the same payload.
        base_ns = self._anchors.get(trace_id)
        if base_ns is None:
            base_ns = _base_time_ns(span_payloads)
            self._anchors[trace_id] = base_ns

        spans: list[dict[str, Any]] = []
        for span in span_payloads:
            span_id = id_map.get(str(span.get("id")), _hex_id("span", trace_name))
            parent = span.get("parent_id")
            start = float(span.get("start", 0.0) or 0.0)
            duration_ms = float(span.get("duration_ms", 0.0) or 0.0)
            start_ns = base_ns + int(start * 1_000_000_000)
            end_ns = start_ns + int(duration_ms * 1_000_000)
            status_code = STATUS_ERROR if span.get("status") == "error" else STATUS_OK
            entry: dict[str, Any] = {
                "traceId": trace_id,
                "spanId": span_id,
                "name": str(span.get("name", "span")),
                "kind": SPAN_INTERNAL,
                "startTimeUnixNano": str(start_ns),
                "endTimeUnixNano": str(end_ns),
                "attributes": attributes(
                    {**dict(span.get("attributes") or {}),
                     "rister.status": span.get("status"),
                     "rister.duration_ms": round(duration_ms, 3)}
                ),
                "status": {"code": status_code},
            }
            if parent and str(parent) in id_map:
                entry["parentSpanId"] = id_map[str(parent)]
            if span.get("error"):
                entry["status"]["message"] = str(span["error"])[:500]
            events = [
                {
                    "name": str(event.get("name", "event")),
                    "timeUnixNano": str(start_ns),
                    "attributes": attributes(event.get("data") or {}),
                }
                for event in (span.get("events") or [])
            ]
            if events:
                entry["events"] = events
            spans.append(entry)

        return {
            "resourceSpans": [
                {
                    "resource": {
                        "attributes": attributes(
                            {
                                "service.name": self.service_name,
                                "service.version": self.service_version,
                                "telemetry.sdk.name": "rister",
                                "telemetry.sdk.language": "python",
                            }
                        )
                    },
                    "scopeSpans": [
                        {
                            "scope": {"name": "rister.observe", "version": self.service_version},
                            "spans": spans,
                        }
                    ],
                }
            ]
        }

    # -- exporter protocol -------------------------------------------------
    def export(self, payload: Mapping[str, Any]) -> None:
        body = self.to_otlp(payload)
        self.last_payload = body
        if self.dry_run or not body["resourceSpans"][0]["scopeSpans"][0]["spans"]:
            return
        data = json.dumps(body).encode("utf-8")
        request = urllib.request.Request(  # noqa: S310 - endpoint is operator-supplied
            self.endpoint,
            data=data,
            method="POST",
            headers={
                "Content-Type": "application/json",
                "User-Agent": "rister/1.0 otlp",
                **dict(self.headers),
            },
        )
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                response.read()
            self.sent += 1
        except (urllib.error.URLError, OSError, ValueError) as exc:
            # Telemetry must never break the workload.
            self.failures += 1
            self.last_error = str(exc)

    def describe(self) -> dict[str, Any]:
        return {
            "endpoint": self.endpoint,
            "dry_run": self.dry_run,
            "sent": self.sent,
            "failures": self.failures,
            "last_error": self.last_error,
        }


def _base_time_ns(spans: list[Mapping[str, Any]]) -> int:
    """Earliest span start, projected onto wall-clock nanoseconds."""
    starts = [float(span.get("start", 0.0) or 0.0) for span in spans] or [0.0]
    origin = min(starts)
    wall = time.time_ns()
    return wall - int(origin * 1_000_000_000)


def to_otlp(payload: Mapping[str, Any], **kwargs: Any) -> dict[str, Any]:
    """Convenience: convert a tracer payload without constructing an exporter."""
    return OtlpExporter(dry_run=True, **kwargs).to_otlp(payload)


__all__ = [
    "OtlpExporter",
    "to_otlp",
    "any_value",
    "attributes",
    "STATUS_OK",
    "STATUS_ERROR",
    "STATUS_UNSET",
    "SPAN_INTERNAL",
    "DEFAULT_ENDPOINT",
]
