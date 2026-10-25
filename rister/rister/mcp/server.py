"""An in-process MCP server, and a stdio loop to run it as a real process.

This exists for three reasons: it makes the client testable without spawning
anything, it lets you publish a RISTER tool collection to any MCP host, and it
lets a RISTER agent be reached by another agent that only speaks MCP.
"""
from __future__ import annotations

import json
import sys
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence, TextIO

from ..agent.tool import Tool, ToolRegistry
from ..core.meta import __version__
from ..core.errors import RisterError, ToolError
from . import protocol as p

SERVER_NAME = "rister"


@dataclass
class McpServer:
    """Serve a set of RISTER tools over MCP.

    Supports both protocol generations: ``server/discover`` (stateless) and the
    legacy ``initialize`` handshake.
    """

    name: str = SERVER_NAME
    version: str = __version__
    tools: ToolRegistry = field(default_factory=ToolRegistry)
    page_size: int = 50
    instructions: str = ""

    def __init__(
        self,
        tools: Iterable[Any] = (),
        *,
        name: str = SERVER_NAME,
        version: str = __version__,
        page_size: int = 50,
        instructions: str = "",
    ) -> None:
        self.name = name
        self.version = version
        self.tools = ToolRegistry(tools)
        self.page_size = max(1, int(page_size))
        self.instructions = instructions

    # -- catalogue ---------------------------------------------------------
    def add(self, tool: Any) -> Tool:
        entry = self.tools.add(tool)
        self._notify_list_changed = True  # noqa: SLF001 - consumed by hosts that ask
        return entry

    def capabilities(self) -> dict[str, Any]:
        return {"tools": {"listChanged": False}}

    def server_info(self) -> dict[str, Any]:
        return {"name": self.name, "version": self.version}

    def discover(self) -> dict[str, Any]:
        return {
            "versions": list(p.SUPPORTED_VERSIONS),
            "protocolVersion": p.PROTOCOL_VERSION,
            "capabilities": self.capabilities(),
            "serverInfo": self.server_info(),
            "instructions": self.instructions,
        }

    def list_tools(self, cursor: str | None = None) -> dict[str, Any]:
        try:
            offset = int(cursor) if cursor else 0
        except (TypeError, ValueError):
            raise p.McpError(f"invalid cursor {cursor!r}", p.INVALID_PARAMS) from None
        entries = [p.tool_spec(tool) for tool in self.tools]
        window = entries[offset: offset + self.page_size]
        result: dict[str, Any] = {"tools": window}
        if offset + self.page_size < len(entries):
            result["nextCursor"] = str(offset + self.page_size)
        return result

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> dict[str, Any]:
        try:
            value = self.tools.call(name, dict(arguments or {}))
        except ToolError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        result: dict[str, Any] = {"content": _as_content(value), "isError": False}
        # Text blocks cannot carry a type. Structured content can, so a client
        # gets back an int where the tool returned an int.
        if not isinstance(value, str):
            result["structuredContent"] = {"result": value}
        return result

    # -- dispatch ----------------------------------------------------------
    def handle(self, message: Mapping[str, Any]) -> dict[str, Any] | None:
        """Process one JSON-RPC message; returns None for notifications."""
        if not isinstance(message, Mapping):
            return p.error_response(None, p.INVALID_REQUEST, "message must be an object")
        if message.get("jsonrpc") != "2.0":
            return p.error_response(
                message.get("id"), p.INVALID_REQUEST, "jsonrpc must be '2.0'"
            )
        method = message.get("method")
        request_id = message.get("id")
        params = message.get("params") or {}
        if not isinstance(method, str):
            if request_id is None:
                return None  # a response to something we never sent
            return p.error_response(request_id, p.INVALID_REQUEST, "missing method")
        if not isinstance(params, Mapping):
            return p.error_response(request_id, p.INVALID_PARAMS, "params must be an object")

        if request_id is None:
            return None  # notifications need no reply

        try:
            if method == "server/discover":
                return p.response(request_id, self.discover())
            if method == "initialize":
                return p.response(request_id, self._initialize(params))
            if method == "ping":
                return p.response(request_id, {})
            if method == "tools/list":
                return p.response(request_id, self.list_tools(params.get("cursor")))
            if method == "tools/call":
                name = params.get("name")
                if not isinstance(name, str) or not name:
                    raise p.McpError("tools/call needs a name", p.INVALID_PARAMS)
                arguments = params.get("arguments") or {}
                if not isinstance(arguments, Mapping):
                    raise p.McpError("arguments must be an object", p.INVALID_PARAMS)
                return p.response(request_id, self.call_tool(name, arguments))
            if method in ("resources/list", "prompts/list"):
                key = method.split("/")[0]
                return p.response(request_id, {key: []})
        except p.McpError as exc:
            return p.error_response(request_id, exc.code, str(exc), exc.data)
        except RisterError as exc:
            return p.error_response(request_id, p.INTERNAL_ERROR, str(exc))
        return p.error_response(request_id, p.METHOD_NOT_FOUND, f"unknown method {method!r}")

    def _initialize(self, params: Mapping[str, Any]) -> dict[str, Any]:
        offered = str(params.get("protocolVersion") or p.PROTOCOL_VERSION)
        version = offered if offered in p.SUPPORTED_VERSIONS else p.SUPPORTED_VERSIONS[0]
        return {
            "protocolVersion": version,
            "capabilities": self.capabilities(),
            "serverInfo": self.server_info(),
            "instructions": self.instructions,
        }

    # -- transports --------------------------------------------------------
    def serve_stdio(self, stdin: TextIO | None = None, stdout: TextIO | None = None) -> int:
        """Read newline-delimited JSON-RPC until EOF. Returns messages handled."""
        source = stdin or sys.stdin
        sink = stdout or sys.stdout
        count = 0
        for line in source:
            line = line.strip()
            if not line:
                continue
            count += 1  # every non-empty line is a message, even a malformed one
            try:
                message = json.loads(line)
            except json.JSONDecodeError as exc:
                sink.write(json.dumps(p.error_response(None, p.PARSE_ERROR, str(exc))) + "\n")
                sink.flush()
                continue
            reply = self.handle(message)
            if reply is not None:
                sink.write(json.dumps(reply, separators=(",", ":")) + "\n")
                sink.flush()
        return count

    def handle_http(self, body: Any) -> dict[str, Any]:
        """Handle an HTTP POST body (a message or a batch); always returns a body."""
        if isinstance(body, list):
            replies = [self.handle(item) for item in body]
            return {"responses": [r for r in replies if r is not None]}
        reply = self.handle(body)
        return reply if reply is not None else {"accepted": True}

    def describe(self) -> dict[str, Any]:
        return {
            "name": self.name,
            "version": self.version,
            "protocol": p.PROTOCOL_VERSION,
            "tools": [tool.name for tool in self.tools],
        }


def _as_content(value: Any) -> list[dict[str, Any]]:
    """Render any Python value as MCP content blocks."""
    if isinstance(value, Mapping):
        return [{"type": "text", "text": json.dumps(value, sort_keys=True, default=str)}]
    if isinstance(value, (list, tuple)):
        return [{"type": "text", "text": json.dumps(list(value), default=str)}]
    return [{"type": "text", "text": "" if value is None else str(value)}]


def serve(tools: Iterable[Any] = (), *, name: str = SERVER_NAME,
          version: str = __version__) -> int:
    """Run a server on stdio until the peer closes the connection."""
    return McpServer(tools, name=name, version=version).serve_stdio()


def main(argv: Sequence[str] | None = None) -> int:
    """Entry point: ``python -m rister.mcp`` serving the demo tool set."""
    from ..agent.tool import tool

    @tool
    def echo(text: str) -> str:
        """Repeat the given text back to the caller."""
        return text

    @tool
    def add(a: int, b: int) -> int:
        """Add two integers."""
        return a + b

    return serve([echo, add], name="rister-demo")


__all__ = ["McpServer", "serve", "main", "SERVER_NAME"]
