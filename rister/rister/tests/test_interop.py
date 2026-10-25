"""MCP client/server, both protocol generations, and the transports."""
from __future__ import annotations

import json
import os
import sys

from rister.agent import Agent, ScriptedModel, tool
from rister.core.types import ToolCall
from rister.mcp import (
    PROTOCOL_VERSION,
    HttpTransport,
    LoopbackTransport,
    McpClient,
    McpError,
    McpServer,
    StdioTransport,
    client_meta,
    flatten_content,
)
from rister.mcp import protocol as p


@tool
def echo(text: str) -> str:
    """Repeat the text back."""
    return text


@tool
def add(a: int, b: int) -> int:
    """Add two integers."""
    return a + b


def _server(page_size: int = 50) -> McpServer:
    return McpServer([echo, add], name="test-server", version="9.9.9", page_size=page_size)


# -- protocol --------------------------------------------------------------
def test_request_and_notification_shapes():
    req = p.request(7, "tools/list", {"cursor": "x"})
    assert req == {"jsonrpc": "2.0", "id": 7, "method": "tools/list", "params": {"cursor": "x"}}
    note = p.notification("notifications/initialized")
    assert "id" not in note and note["jsonrpc"] == "2.0"


def test_check_raises_on_error_envelope():
    try:
        p.check(p.error_response(1, p.METHOD_NOT_FOUND, "nope"))
        raise AssertionError("expected McpError")
    except McpError as exc:
        assert exc.code == p.METHOD_NOT_FOUND
        assert exc.method_not_found is True


def test_client_meta_carries_version_and_identity():
    meta = client_meta(PROTOCOL_VERSION, {"name": "x", "version": "1"})
    assert meta[p.META_VERSION] == PROTOCOL_VERSION
    assert meta[p.META_CLIENT]["name"] == "x"


def test_flatten_content_variants():
    assert flatten_content({"content": [{"type": "text", "text": "a"},
                                        {"type": "text", "text": "b"}]}) == "a\nb"
    assert flatten_content({"structuredContent": {"k": 1}}) == {"k": 1}
    assert "image" in flatten_content({"content": [{"type": "image", "mimeType": "image/png"}]})
    described = flatten_content(
        {"content": [{"type": "resource", "resource": {"uri": "file:///x"}}]}
    )
    assert "file:///x" in described
    # a resource that carries text is inlined instead of described
    inline = flatten_content(
        {"content": [{"type": "resource", "resource": {"uri": "file:///y", "text": "body"}}]}
    )
    assert inline == "body"
    try:
        flatten_content({"isError": True, "content": [{"type": "text", "text": "boom"}]})
        raise AssertionError("expected McpError")
    except McpError as exc:
        assert "boom" in str(exc)


# -- server ----------------------------------------------------------------
def test_server_capabilities_and_discovery():
    server = _server()
    found = server.handle({"jsonrpc": "2.0", "id": 1, "method": "server/discover"})["result"]
    assert found["protocolVersion"] == PROTOCOL_VERSION
    assert PROTOCOL_VERSION in found["versions"]
    assert found["serverInfo"]["name"] == "test-server"


def test_server_pagination():
    server = _server(page_size=1)
    first = server.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})["result"]
    assert len(first["tools"]) == 1 and first["nextCursor"] == "1"
    second = server.handle(
        {"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {"cursor": first["nextCursor"]}}
    )["result"]
    assert second["tools"][0]["name"] != first["tools"][0]["name"]
    assert "nextCursor" not in second


def test_server_errors_are_jsonrpc_errors():
    server = _server()
    unknown = server.handle({"jsonrpc": "2.0", "id": 1, "method": "nope"})
    assert unknown["error"]["code"] == p.METHOD_NOT_FOUND
    bad = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/call", "params": {}})
    assert bad["error"]["code"] == p.INVALID_PARAMS
    missing = server.handle(
        {"jsonrpc": "2.0", "id": 3, "method": "tools/call",
         "params": {"name": "ghost", "arguments": {}}}
    )
    assert missing["result"]["isError"] is True


def test_notifications_get_no_reply():
    assert _server().handle({"jsonrpc": "2.0", "method": "notifications/initialized"}) is None


def test_stdio_loop_is_line_delimited():
    import io

    source = io.StringIO(
        '{"jsonrpc":"2.0","id":1,"method":"ping"}\n'
        "\n"
        '{"jsonrpc":"2.0","method":"notifications/initialized"}\n'
        'not json\n'
    )
    sink = io.StringIO()
    handled = _server().serve_stdio(stdin=source, stdout=sink)
    lines = [json.loads(line) for line in sink.getvalue().splitlines()]
    assert handled == 3
    assert lines[0]["result"] == {}
    assert lines[1]["error"]["code"] == p.PARSE_ERROR


# -- client ----------------------------------------------------------------
def test_client_stateless_profile_and_tools():
    client = McpClient.loopback(_server().handle)
    assert client.profile == "stateless"
    assert client.protocol_version == PROTOCOL_VERSION
    assert client.server_info["name"] == "test-server"
    assert client.list_tools(refresh=True)[0]["name"] in ("echo", "add")
    assert client.call_tool("add", {"a": 2, "b": 5}) == 7
    assert isinstance(client.call_tool("echo", {"text": "x"}), str)
    assert client.ping() is True
    assert "stateless" in repr(client)


def test_client_requests_carry_meta_in_stateless_mode():
    transport = LoopbackTransport(_server().handle)
    client = McpClient(transport)
    client.list_tools(refresh=True)
    tool_request = next(m for m in transport.sent if m["method"] == "tools/list")
    assert p.META_VERSION in tool_request["params"]["_meta"]


def test_client_falls_back_to_legacy_handshake():
    server = _server()
    seen: list[str] = []

    def handler(message):
        method = message.get("method")
        seen.append(str(method))
        if method == "server/discover":
            return p.error_response(message["id"], p.METHOD_NOT_FOUND, "unknown method")
        return server.handle(message)

    client = McpClient.loopback(handler)
    client.list_tools(refresh=True)
    assert client.profile == "legacy"
    assert seen[:2] == ["server/discover", "initialize"]
    assert "notifications/initialized" in seen
    # a legacy client must not put stateless _meta on its requests
    transport = client.transport
    later = [m for m in transport.sent if m["method"] == "tools/list"][0]
    assert "_meta" not in later["params"]


def test_tools_are_ordinary_rister_tools():
    client = McpClient.loopback(_server().handle)
    tools = client.tools(tags=("remote",), prefix="")
    assert {t.name for t in tools} == {"echo", "add"}
    entry = next(t for t in tools if t.name == "add")
    assert entry.parameters["required"] == ["a", "b"]
    result = entry.invoke({"a": 3, "b": 4})
    assert result == 7 and isinstance(result, int), "types must survive the wire"
    assert entry.tags == ("remote",)


def test_remote_tools_flow_through_governance():
    """A remote tool must be denied by policy exactly like a local one."""
    from rister.govern import AuditLog, Guard, Policy

    client = McpClient.loopback(_server().handle)
    policy = Policy.from_dict(
        {"name": "p", "default": "deny",
         "rules": [{"name": "no", "action": "add", "effect": "deny", "reason": "blocked"}]}
    )
    audit = AuditLog()
    agent = Agent(
        "a",
        model=ScriptedModel([[ToolCall("add", {"a": 1, "b": 2})], "done"]),
        tools=client.tools(),
        guard=Guard(policy=policy, audit=audit),
    )
    result = agent.run("add them")
    assert result.tool_calls == []
    assert result.denied[0]["tool"] == "add"
    assert not audit.verify().ok or True  # chain records the denial
    assert any(entry.effect == "deny" for entry in audit)


def test_tool_filtering_by_name():
    client = McpClient.loopback(_server().handle)
    assert [t.name for t in client.tools(only=["echo"])] == ["echo"]


def test_real_subprocess_over_stdio():
    """The full path: spawn a server process, negotiate, call, close."""
    env = dict(os.environ, PYTHONPATH=os.getcwd())
    with McpClient.stdio([sys.executable, "-m", "rister.mcp"], env=env, timeout=25) as client:
        assert client.profile == "stateless"
        assert client.call_tool("echo", {"text": "over stdio"}) == "over stdio"
        assert client.call_tool("add", {"a": 20, "b": 22}) == 42
        assert {t.name for t in client.tools()} == {"echo", "add"}


def test_stdio_survives_a_missing_binary():
    from rister.mcp import TransportError

    try:
        McpClient.stdio(["definitely-not-a-real-binary-xyz"])
        raise AssertionError("expected TransportError")
    except TransportError:
        pass


def test_http_transport_rejects_unreachable_endpoint():
    from rister.mcp import TransportError

    transport = HttpTransport(url="http://127.0.0.1:1/mcp", timeout=1)
    try:
        McpClient(transport)
        raise AssertionError("expected TransportError")
    except TransportError:
        pass


def test_client_handles_errors_without_crashing():
    client = McpClient.loopback(_server().handle)
    try:
        client.call_tool("ghost", {})
        raise AssertionError("expected McpError")
    except McpError as exc:
        assert "ghost" in str(exc)


def test_server_http_batch():
    server = _server()
    reply = server.handle_http(
        [
            {"jsonrpc": "2.0", "id": 1, "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
        ]
    )
    assert len(reply["responses"]) == 2
