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
    version: str = "1.0.0"

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
            "routes": [
                "GET  /health",
                "GET  /describe",
                "POST /agents/<name>   {input, thread_id?}",
                "POST /graphs/<name>   {input, thread_id?}",
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

            def do_GET(self) -> None:  # noqa: N802 - http.server API
                status, payload = app.handle("GET", self.path, None, dict(self.headers))
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
                status, payload = app.handle("POST", self.path, body, dict(self.headers))
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


def create_app(
    agents: Mapping[str, Any] | None = None,
    graphs: Mapping[str, Any] | None = None,
    **kwargs: Any,
) -> App:
    return App(agents=dict(agents or {}), graphs=dict(graphs or {}), **kwargs)


__all__ = ["App", "create_app", "MAX_BODY_BYTES"]
