"""A dependency-free HTTP surface for agents and graphs.

Standard library only (``http.server``), so it runs anywhere Python does. It is
intentionally small: health, discovery, run endpoints and a trace summary. Put a
real ASGI/WSGI server and TLS in front of it for production; this is for local
development, demos and service-to-service calls inside a trusted network.

    app = App(agents={"writer": writer}, graphs={"pipeline": compiled})
    app.serve(port=8000)            # blocking
    server = app.make_server()      # for embedding / tests
"""
from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Mapping
from urllib.parse import parse_qs, urlparse

from .core.meta import __version__
from .core.errors import RisterError
from .core.types import Config, as_config, jsonable, new_id

MAX_BODY_BYTES = 1_000_000


@dataclass
class App:
    """Routes agents and graphs over HTTP."""

    agents: dict[str, Any] = field(default_factory=dict)
    graphs: dict[str, Any] = field(default_factory=dict)
    tracer: Any = None
    auth_token: str | None = None
    name: str = "rister"
    version: str = __version__
    approvers: dict[str, Any] = field(default_factory=dict)
    mcp: dict[str, Any] = field(default_factory=dict)
    counters: dict[str, int] = field(default_factory=lambda: {"requests": 0, "errors": 0})

    # -- introspection -----------------------------------------------------
    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "agents": {
                name: agent.describe() if hasattr(agent, "describe") else {"name": name}
                for name, agent in self.agents.items()
            },
            "graphs": {
                name: {"nodes": sorted(graph.graph.nodes)}
                if hasattr(graph, "graph")
                else {"name": name}
                for name, graph in self.graphs.items()
            },
            "approvers": sorted(self.approvers),
            "mcp": sorted(self.mcp),
            "routes": [
                "GET  /health",
                "GET  /describe",
                "GET  /metrics",
                "POST /agents/<name>          {input, thread_id?}",
                "POST /graphs/<name>          {input, thread_id?}",
                "GET  /agents/<name>/stream   ?input=...   (server-sent events)",
                "GET  /tickets                ?status=pending",
                "POST /tickets/<id>           {decision: approve|deny, by?, note?}",
                "POST /mcp/<name>             JSON-RPC 2.0",
                "GET  /traces",
            ],
        }

    # -- request handling --------------------------------------------------
    def handle(self, method: str, path: str, body: Any = None,
               headers: Mapping[str, str] | None = None) -> tuple[int, dict[str, Any]]:
        """Route one request. Pure function: easy to unit test without sockets."""
        headers = {k.lower(): v for k, v in (headers or {}).items()}
        parsed = urlparse(path)
        route = parsed.path.rstrip("/") or "/"

        if self.auth_token:
            supplied = headers.get("authorization", "")
            expected = f"Bearer {self.auth_token}"
            if supplied != expected:
                return 401, {"error": "unauthorized"}

        if route == "/health":
            return 200, {"ok": True, "name": self.name, "version": self.version}

        if route == "/describe":
            return 200, self.describe()

        if route == "/metrics" and method == "GET":
            return 200, self.metrics()

        if route == "/tickets" and method == "GET":
            pending = (parse_qs(parsed.query).get("status") or ["pending"])[0]
            return 200, {"tickets": self.tickets(pending)}

        if route.startswith("/tickets/") and method == "POST":
            return self._decide_ticket(route.split("/", 2)[2], body)

        if route.startswith("/mcp/") and method == "POST":
            name = route.split("/", 2)[2]
            server = self.mcp.get(name)
            if server is None:
                return 404, {"error": f"no mcp endpoint {name!r}", "mcp": sorted(self.mcp)}
            if not isinstance(body, (Mapping, list)):
                return 400, {"error": "body must be a JSON-RPC message or batch"}
            return 200, server.handle_http(body)

        if route == "/traces" and method == "GET":
            if self.tracer is None:
                return 200, {"spans": 0, "summary": {}}
            return 200, self.tracer.summary()

        if route.startswith("/agents/") and method == "POST":
            name = route.split("/", 2)[2]
            agent = self.agents.get(name)
            if agent is None:
                return 404, {"error": f"no agent {name!r}", "agents": sorted(self.agents)}
            payload = body if isinstance(body, Mapping) else {}
            task = payload.get("input") or payload.get("task")
            if not isinstance(task, str) or not task.strip():
                return 400, {"error": "body must include a non-empty 'input' string"}
            cfg = self._config(payload)
            try:
                result = agent.run(task, cfg)
            except RisterError as exc:
                return 422, {"error": type(exc).__name__, "detail": str(exc)}
            return 200, result.to_dict()

        if route.startswith("/graphs/") and method == "POST":
            name = route.split("/", 2)[2]
            graph = self.graphs.get(name)
            if graph is None:
                return 404, {"error": f"no graph {name!r}", "graphs": sorted(self.graphs)}
            payload = body if isinstance(body, Mapping) else {}
            initial = payload.get("input") or payload.get("state") or {}
            if not isinstance(initial, Mapping):
                return 400, {"error": "'input' must be an object of state fields"}
            cfg = self._config(payload)
            try:
                result = graph.invoke(dict(initial), cfg)
            except RisterError as exc:
                return 422, {"error": type(exc).__name__, "detail": str(exc)}
            return 200, result.to_dict()

        known = {"/health", "/describe", "/traces"}
        if route in known:
            return 405, {"error": f"method {method} not allowed for {route}"}
        return 404, {"error": f"no route {route}"}

    def stream_events(self, name: str, task: str, payload: Mapping[str, Any]):
        """Yield server-sent-event frames for one agent run."""
        agent = self.agents.get(name)
        if agent is None:
            yield _sse({"error": f"no agent {name!r}"})
            return
        cfg = self._config(payload)
        try:
            for event in agent.stream(task, cfg):
                yield _sse(event)
        except RisterError as exc:
            yield _sse({"error": type(exc).__name__, "detail": str(exc)})
        except Exception as exc:  # noqa: BLE001 - never kill the socket silently
            yield _sse({"error": "internal", "detail": str(exc)})
        yield "event: done\ndata: {}\n\n"

    # -- approvals ---------------------------------------------------------
    def tickets(self, status: str = "pending") -> list[dict[str, Any]]:
        """Approval tickets across every configured approver."""
        out: list[dict[str, Any]] = []
        for name, approver in self.approvers.items():
            lister = getattr(approver, "all_tickets", None) or getattr(approver, "pending", None)
            if not callable(lister):
                continue
            try:
                entries = lister() if lister.__name__ == "all_tickets" else lister()
            except TypeError:  # pragma: no cover - defensive
                continue
            for ticket in entries or []:
                data = ticket.to_dict() if hasattr(ticket, "to_dict") else dict(ticket)
                data.setdefault("approver", name)
                if status in ("all", data.get("status")):
                    out.append(data)
        return out

    def _decide_ticket(self, ticket_id: str, body: Any) -> tuple[int, dict[str, Any]]:
        payload = body if isinstance(body, Mapping) else {}
        decision = str(payload.get("decision") or payload.get("status") or "").lower()
        if decision not in ("approve", "approved", "deny", "denied", "reject", "rejected"):
            return 400, {"error": "decision must be 'approve' or 'deny'"}
        wanted = "approve" if decision.startswith("approv") else "deny"
        for name, approver in self.approvers.items():
            method = getattr(approver, wanted, None)
            if not callable(method):
                continue
            try:
                ticket = method(
                    ticket_id,
                    by=str(payload.get("by") or "api"),
                    note=str(payload.get("note") or ""),
                )
            except (KeyError, LookupError):
                continue
            except TypeError:  # signature without by/note
                ticket = method(ticket_id)
            data = ticket.to_dict() if hasattr(ticket, "to_dict") else {"id": ticket_id}
            data["approver"] = name
            return 200, {"ticket": data}
        return 404, {"error": f"no open ticket {ticket_id!r}"}

    def metrics(self) -> dict[str, Any]:
        """A tiny, dependency-free metrics surface."""
        tickets = self.tickets("pending")
        data: dict[str, Any] = {
            "requests": self.counters.get("requests", 0),
            "errors": self.counters.get("errors", 0),
            "agents": len(self.agents),
            "graphs": len(self.graphs),
            "open_approvals": len(tickets),
        }
        if self.tracer is not None and hasattr(self.tracer, "summary"):
            summary = self.tracer.summary()
            data["spans"] = summary.get("spans", 0)
            data["span_errors"] = summary.get("errors", 0)
        return data

    def _config(self, payload: Mapping[str, Any]) -> Config:
        thread = payload.get("thread_id") or new_id("thread")
        cfg = as_config({"thread_id": thread, "tracer": self.tracer})
        if payload.get("subject") is not None:
            cfg.subject = payload["subject"]
        return cfg

    # -- server ------------------------------------------------------------
    def make_handler(self) -> type[BaseHTTPRequestHandler]:
        app = self

        class Handler(BaseHTTPRequestHandler):
            server_version = f"{app.name}/{app.version}"

            def _send(self, status: int, payload: Mapping[str, Any]) -> None:
                data = json.dumps(jsonable(payload), sort_keys=True).encode("utf-8")
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

            def _stream(self, path: str) -> bool:
                """Serve /agents/<name>/stream as server-sent events."""
                app.counters["requests"] = app.counters.get("requests", 0) + 1
                parsed = urlparse(path)
                route = parsed.path.rstrip("/")
                parts = route.split("/")
                if len(parts) != 4 or parts[1] != "agents" or parts[3] != "stream":
                    return False
                name = parts[2]
                query = parse_qs(parsed.query)
                task = (query.get("input") or query.get("task") or [""])[0]
                payload = {"thread_id": (query.get("thread_id") or [""])[0] or None}
                if not task.strip():
                    self._send(400, {"error": "stream needs ?input=<task>"})
                    return True
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Connection", "close")
                self.end_headers()
                try:
                    for frame in app.stream_events(name, task, payload):
                        self.wfile.write(frame.encode("utf-8"))
                        self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    return True
                return True

            def do_GET(self) -> None:  # noqa: N802 - http.server API
                if self._stream(self.path):
                    return
                app.counters["requests"] = app.counters.get("requests", 0) + 1
                status, payload = app.handle("GET", self.path, None, dict(self.headers))
                if status >= 400:
                    app.counters["errors"] = app.counters.get("errors", 0) + 1
                self._send(status, payload)

            def do_POST(self) -> None:  # noqa: N802
                length = int(self.headers.get("Content-Length") or 0)
                if length > MAX_BODY_BYTES:
                    self._send(413, {"error": "request body too large"})
                    return
                raw = self.rfile.read(length) if length else b""
                body: Any = {}
                if raw:
                    try:
                        body = json.loads(raw.decode("utf-8"))
                    except (UnicodeDecodeError, json.JSONDecodeError):
                        self._send(400, {"error": "body must be JSON"})
                        return
                app.counters["requests"] = app.counters.get("requests", 0) + 1
                status, payload = app.handle("POST", self.path, body, dict(self.headers))
                if status >= 400:
                    app.counters["errors"] = app.counters.get("errors", 0) + 1
                self._send(status, payload)

            def log_message(self, fmt: str, *args: Any) -> None:  # quieter default
                if getattr(app, "verbose", False):
                    super().log_message(fmt, *args)

        return Handler

    def make_server(self, host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
        return ThreadingHTTPServer((host, port), self.make_handler())

    def serve(self, host: str = "127.0.0.1", port: int = 8000, background: bool = False) -> Any:
        """Start serving. Returns the server; join it yourself if background."""
        server = self.make_server(host, port)
        if background:
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            return server
        try:
            server.serve_forever()
        except KeyboardInterrupt:  # pragma: no cover - interactive
            pass
        finally:
            server.server_close()
        return server

    verbose: bool = False


def _sse(event: Mapping[str, Any]) -> str:
    """Encode one server-sent-event frame."""
    name = str((event or {}).get("event") or "message")
    return f"event: {name}\ndata: {json.dumps(jsonable(event), sort_keys=True)}\n\n"


def create_app(
    agents: Mapping[str, Any] | None = None,
    graphs: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> App:
    return App(agents=dict(agents or {}), graphs=dict(graphs or {}), **kwargs)


__all__ = ["App", "create_app", "MAX_BODY_BYTES"]
