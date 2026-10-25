"""Wire format for the Model Context Protocol (JSON-RPC 2.0).

Two profiles exist in the wild and RISTER speaks both:

* **stateless** (2026-07-28 and later) — no handshake. Every request carries its
  protocol version, client info and capabilities in ``params._meta``, and
  capabilities are fetched on demand with ``server/discover``.
* **legacy** (2025-11-25 and earlier) — an ``initialize`` request negotiates the
  version once, followed by a ``notifications/initialized`` notification.

:class:`~rister.mcp.client.McpClient` detects which one a server speaks, so the
same adapter works against both generations.
"""
from __future__ import annotations

import json
from typing import Any, Mapping

from ..core.errors import RisterError
from ..core.meta import __version__

#: Newest protocol revision RISTER implements.
PROTOCOL_VERSION = "2026-07-28"

#: Revisions this client can negotiate, newest first.
SUPPORTED_VERSIONS: tuple[str, ...] = (
    "2026-07-28",
    "2025-11-25",
    "2025-06-18",
    "2024-11-05",
)

#: Revisions that still use the ``initialize`` handshake.
LEGACY_VERSIONS: tuple[str, ...] = ("2025-11-25", "2025-06-18", "2024-11-05")

# Reserved ``_meta`` keys from the specification.
META_VERSION = "io.modelcontextprotocol/protocolVersion"
META_CLIENT = "io.modelcontextprotocol/clientInfo"
META_CLIENT_CAPS = "io.modelcontextprotocol/clientCapabilities"
META_SERVER = "io.modelcontextprotocol/serverInfo"

# JSON-RPC 2.0 error codes.
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603

# MCP-specific codes.
RESOURCE_NOT_FOUND = -32002

ERROR_CODES: dict[int, str] = {
    PARSE_ERROR: "parse error",
    INVALID_REQUEST: "invalid request",
    METHOD_NOT_FOUND: "method not found",
    INVALID_PARAMS: "invalid params",
    INTERNAL_ERROR: "internal error",
    RESOURCE_NOT_FOUND: "resource not found",
}


class McpError(RisterError):
    """A JSON-RPC error returned by a server, or a protocol violation."""

    def __init__(self, message: str, code: int = INTERNAL_ERROR,
                 data: Any = None) -> None:
        super().__init__(message)
        self.code = code
        self.data = data

    @property
    def method_not_found(self) -> bool:
        return self.code == METHOD_NOT_FOUND

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {"code": self.code, "message": str(self)}
        if self.data is not None:
            out["data"] = self.data
        return out


# --------------------------------------------------------------------------
# message builders
# --------------------------------------------------------------------------
def request(request_id: int | str, method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build a JSON-RPC request object."""
    message: dict[str, Any] = {"jsonrpc": "2.0", "id": request_id, "method": method}
    if params is not None:
        message["params"] = dict(params)
    return message


def notification(method: str, params: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build a JSON-RPC notification (no id, so no response is expected)."""
    message: dict[str, Any] = {"jsonrpc": "2.0", "method": method}
    if params is not None:
        message["params"] = dict(params)
    return message


def response(request_id: Any, result: Any) -> dict[str, Any]:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def error_response(request_id: Any, code: int, message: str,
                   data: Any = None) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def client_meta(
    protocol_version: str = PROTOCOL_VERSION,
    client_info: Mapping[str, Any] | None = None,
    capabilities: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """The ``_meta`` block a stateless request must carry."""
    return {
        META_VERSION: protocol_version,
        META_CLIENT: dict(client_info or {"name": "rister", "version": __version__}),
        META_CLIENT_CAPS: dict(capabilities or {}),
    }


# --------------------------------------------------------------------------
# result helpers
# --------------------------------------------------------------------------
def check(message: Mapping[str, Any]) -> Any:
    """Validate a JSON-RPC envelope and return its result, raising on error."""
    if not isinstance(message, Mapping):
        raise McpError("response is not an object", INVALID_REQUEST)
    if message.get("jsonrpc") != "2.0":
        raise McpError("missing or wrong jsonrpc version", INVALID_REQUEST)
    if "error" in message and message["error"] is not None:
        error = message["error"]
        code = int(error.get("code", INTERNAL_ERROR))
        detail = error.get("message") or ERROR_CODES.get(code, "server error")
        raise McpError(str(detail), code, error.get("data"))
    if "result" not in message:
        raise McpError("response has neither result nor error", INVALID_REQUEST)
    return message["result"]


def flatten_content(result: Mapping[str, Any]) -> Any:
    """Turn a ``tools/call`` result into something a Python caller can use.

    Text blocks are concatenated; structured content is returned as a dict;
    images and binary resources are described rather than decoded; an
    ``isError`` result is raised instead of returned.
    """
    if not isinstance(result, Mapping):
        return result

    if result.get("isError"):
        text = _text_of(result) or "tool reported an error"
        raise McpError(text, INTERNAL_ERROR, {"isError": True})

    if result.get("structuredContent") is not None:
        structured = result["structuredContent"]
        # A RISTER server wraps the value in {"result": ...} to preserve its type.
        if isinstance(structured, Mapping) and set(structured) == {"result"}:
            return structured["result"]
        return structured

    blocks = result.get("content") or []
    if isinstance(blocks, str):
        return blocks
    parts: list[str] = []
    for block in blocks:
        if not isinstance(block, Mapping):
            parts.append(str(block))
            continue
        kind = block.get("type")
        if kind == "text":
            parts.append(block.get("text", ""))
        elif kind == "image":
            parts.append(f"[image {block.get('mimeType', 'image/*')}]")
        elif kind == "audio":
            parts.append(f"[audio {block.get('mimeType', 'audio/*')}]")
        elif kind == "resource":
            resource = block.get("resource") or {}
            if "text" in resource:
                parts.append(resource["text"])
            else:
                parts.append(f"[resource {resource.get('uri', 'unknown')}]")
        else:
            parts.append(json.dumps(block, sort_keys=True, default=str))
    return "\n".join(part for part in parts if part)


def _text_of(result: Mapping[str, Any]) -> str:
    blocks = result.get("content") or []
    if isinstance(blocks, str):
        return blocks
    return "\n".join(
        block.get("text", "")
        for block in blocks
        if isinstance(block, Mapping) and block.get("type") == "text"
    )


def tool_spec(tool: Any) -> dict[str, Any]:
    """Render a RISTER :class:`~rister.agent.tool.Tool` as an MCP tool listing."""
    return {
        "name": tool.name,
        "description": tool.description,
        "inputSchema": tool.parameters
        or {"type": "object", "properties": {}, "required": []},
    }


def parse_tool_listing(entry: Mapping[str, Any]) -> dict[str, Any]:
    """Normalise one entry of a ``tools/list`` result."""
    schema = entry.get("inputSchema") or entry.get("input_schema") or {}
    if not isinstance(schema, Mapping):
        schema = {}
    return {
        "name": str(entry.get("name", "")),
        "description": str(entry.get("description") or ""),
        "inputSchema": {
            "type": "object",
            "properties": dict(schema.get("properties") or {}),
            "required": list(schema.get("required") or []),
        },
    }


__all__ = [
    "PROTOCOL_VERSION",
    "SUPPORTED_VERSIONS",
    "LEGACY_VERSIONS",
    "META_VERSION",
    "META_CLIENT",
    "META_CLIENT_CAPS",
    "META_SERVER",
    "PARSE_ERROR",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "INVALID_PARAMS",
    "INTERNAL_ERROR",
    "RESOURCE_NOT_FOUND",
    "ERROR_CODES",
    "McpError",
    "request",
    "notification",
    "response",
    "error_response",
    "client_meta",
    "check",
    "flatten_content",
    "tool_spec",
    "parse_tool_listing",
]
