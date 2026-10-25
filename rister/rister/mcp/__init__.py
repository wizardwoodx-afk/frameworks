"""Model Context Protocol support — both the stateless and legacy profiles.

    from rister.mcp import McpClient

    with McpClient.stdio(["python", "-m", "rister.mcp"]) as client:
        print(client.describe())
        tools = client.tools()            # ordinary RISTER tools
        agent = Agent("a", model=..., tools=tools, guard=guard)

Remote tools pass through the same guard, audit and tracing path as local ones:
a policy cannot tell the difference, which is the point.
"""
from .client import (
    LEGACY,
    STATELESS,
    HttpTransport,
    LoopbackTransport,
    McpClient,
    StdioTransport,
    Transport,
    TransportError,
    connect,
)
from .protocol import (
    INTERNAL_ERROR,
    INVALID_PARAMS,
    INVALID_REQUEST,
    LEGACY_VERSIONS,
    METHOD_NOT_FOUND,
    PARSE_ERROR,
    PROTOCOL_VERSION,
    SUPPORTED_VERSIONS,
    McpError,
    client_meta,
    flatten_content,
    parse_tool_listing,
    tool_spec,
)
from .server import McpServer, serve

__all__ = [
    "McpClient",
    "McpServer",
    "McpError",
    "Transport",
    "TransportError",
    "StdioTransport",
    "HttpTransport",
    "LoopbackTransport",
    "connect",
    "serve",
    "flatten_content",
    "tool_spec",
    "parse_tool_listing",
    "client_meta",
    "PROTOCOL_VERSION",
    "SUPPORTED_VERSIONS",
    "LEGACY_VERSIONS",
    "STATELESS",
    "LEGACY",
    "PARSE_ERROR",
    "INVALID_REQUEST",
    "METHOD_NOT_FOUND",
    "INVALID_PARAMS",
    "INTERNAL_ERROR",
]
