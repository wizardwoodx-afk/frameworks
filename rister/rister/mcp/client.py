"""MCP client: discover tools on any MCP server and use them as RISTER tools.

The client negotiates whichever generation of the protocol the server speaks —
stateless (``server/discover``) or legacy (``initialize``) — and then presents the
remote tools as ordinary :class:`~rister.agent.tool.Tool` objects, so they flow
through the same guard, audit and tracing path as local ones.

Three transports are provided:

* :class:`StdioTransport` — spawn a server process, newline-delimited JSON-RPC.
* :class:`HttpTransport` — POST JSON to a Streamable HTTP endpoint.
* :class:`LoopbackTransport` — call an in-process server, for tests and embedding.
"""
from __future__ import annotations

import json
import os
import queue
import subprocess
import threading
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Sequence

from ..agent.tool import Tool
from ..core.errors import RisterError, ToolError
from ..core.meta import __version__
from . import protocol as p

DEFAULT_CLIENT_INFO = {"name": "rister", "version": __version__}
STATELESS = "stateless"
LEGACY = "legacy"


class TransportError(RisterError):
    """The transport could not deliver a message."""


# --------------------------------------------------------------------------
# transports
# --------------------------------------------------------------------------
class Transport:
    """Deliver one JSON-RPC message and return the matching response."""

    def exchange(self, message: dict[str, Any]) -> dict[str, Any] | None:  # pragma: no cover
        raise NotImplementedError

    def close(self) -> None:  # pragma: no cover - optional
        pass

    @property
    def protocol_header(self) -> str | None:
        return None


class LoopbackTransport(Transport):
    """Talk to an in-process server object exposing ``handle(message)``."""

    def __init__(self, handler: Callable[[dict[str, Any]], dict[str, Any] | None]) -> None:
        self.handler = handler
        self.closed = False
        self.sent: list[dict[str, Any]] = []

    def exchange(self, message: dict[str, Any]) -> dict[str, Any] | None:
        self.sent.append(message)
        return self.handler(message)

    def close(self) -> None:
        self.closed = True


class StdioTransport(Transport):
    """Spawn a server and speak newline-delimited JSON over its stdio.

    A reader thread drains stdout into a queue so that requests can time out
    instead of blocking forever, and so that unsolicited notifications are
    captured rather than lost.
    """

    def __init__(
        self,
        command: Sequence[str] | str,
        *,
        cwd: str | os.PathLike[str] | None = None,
        env: Mapping[str, str] | None = None,
        timeout: float = 30.0,
    ) -> None:
        self.command = command.split() if isinstance(command, str) else list(command)
        if not self.command:
            raise TransportError("stdio transport needs a command")
        self.timeout = timeout
        merged = dict(os.environ)
        merged.update(dict(env or {}))
        try:
            # rister:ignore SEC003 -- argument list, no shell
            self.process = subprocess.Popen(  # noqa: S603
                self.command,
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                cwd=cwd,
                env=merged,
                text=True,
                encoding="utf-8",
                bufsize=1,
            )
        except OSError as exc:
            raise TransportError(f"could not start {self.command[0]!r}: {exc}") from exc

        self._lines: queue.Queue[dict[str, Any] | None] = queue.Queue()
        self._lock = threading.Lock()
        self.notifications: list[dict[str, Any]] = []
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()

    # -- plumbing ----------------------------------------------------------
    def _read_loop(self) -> None:
        assert self.process.stdout is not None
        for line in self.process.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                self._lines.put(json.loads(line))
            except json.JSONDecodeError:
                # Servers may log to stdout; ignore anything that is not JSON-RPC.
                continue
        self._lines.put(None)

    def _write(self, message: Mapping[str, Any]) -> None:
        if self.process.poll() is not None:
            raise TransportError(self.stderr_tail() or "server exited")
        assert self.process.stdin is not None
        try:
            self.process.stdin.write(json.dumps(message, separators=(",", ":")) + "\n")
            self.process.stdin.flush()
        except (BrokenPipeError, ValueError) as exc:
            raise TransportError(f"server closed the connection: {exc}") from exc

    def exchange(self, message: dict[str, Any]) -> dict[str, Any] | None:
        with self._lock:
            self._write(message)
            if "id" not in message:
                return None
            wanted = message["id"]
            while True:
                try:
                    item = self._lines.get(timeout=self.timeout)
                except queue.Empty:
                    raise TransportError(
                        f"no response from server within {self.timeout:g}s"
                    ) from None
                if item is None:
                    raise TransportError(self.stderr_tail() or "server closed stdout")
                if isinstance(item, dict) and item.get("id") == wanted:
                    return item
                self.notifications.append(item)  # requests from server / notices

    def stderr_tail(self, limit: int = 600) -> str:
        if self.process.stderr is None:
            return ""
        try:
            data = self.process.stderr.read(limit)
        except (OSError, ValueError):
            return ""
        return (data or "").strip()

    def close(self) -> None:
        try:
            if self.process.stdin:
                self.process.stdin.close()
        except OSError:
            pass
        if self.process.poll() is None:
            self.process.terminate()
            try:
                self.process.wait(timeout=5)
            except subprocess.TimeoutExpired:  # pragma: no cover - stubborn server
                self.process.kill()


@dataclass
class HttpTransport(Transport):
    """POST JSON-RPC to a Streamable HTTP endpoint (stateless by design)."""

    url: str
    headers: Mapping[str, str] = field(default_factory=dict)
    timeout: float = 30.0
    protocol_version: str = p.PROTOCOL_VERSION
    session_id: str | None = None
    notifications: list[dict[str, Any]] = field(default_factory=list)

    @property
    def protocol_header(self) -> str:
        return self.protocol_version

    def exchange(self, message: dict[str, Any]) -> dict[str, Any] | None:
        if not self.url:
            raise TransportError("http transport needs a url")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": self.protocol_version,
            **dict(self.headers),
        }
        if message.get("method"):
            headers.setdefault("Mcp-Method", str(message["method"]))
        params = message.get("params") or {}
        if isinstance(params, Mapping) and params.get("name"):
            headers.setdefault("Mcp-Name", str(params["name"]))
        if self.session_id:
            headers["Mcp-Session-Id"] = self.session_id

        body = json.dumps(message).encode("utf-8")
        # rister:ignore SEC018 -- explicit scheme check below
        request = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                session = response.headers.get("Mcp-Session-Id")
                if session:
                    self.session_id = session
                content_type = (response.headers.get("Content-Type") or "").lower()
                payload = response.read().decode("utf-8", "replace")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:400]
            raise TransportError(f"HTTP {exc.code} from {self.url}: {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise TransportError(f"could not reach {self.url}: {exc}") from exc

        if not payload.strip():
            return None
        if "text/event-stream" in content_type:
            for event in _iter_sse(payload):
                if event.get("id") == message.get("id"):
                    return event
                self.notifications.append(event)
            return None
        try:
            data = json.loads(payload)
        except json.JSONDecodeError as exc:
            raise TransportError(f"server returned invalid JSON: {exc}") from exc
        if isinstance(data, list):  # batch response
            for item in data:
                if isinstance(item, dict) and item.get("id") == message.get("id"):
                    return item
            return None
        return data


def _iter_sse(payload: str) -> Iterable[dict[str, Any]]:
    """Yield the JSON objects carried by a server-sent-events body."""
    for block in payload.split("\n\n"):
        data_lines = [
            line[5:].strip()
            for line in block.splitlines()
            if line.startswith("data:")
        ]
        if not data_lines:
            continue
        try:
            yield json.loads("\n".join(data_lines))
        except json.JSONDecodeError:
            continue


# --------------------------------------------------------------------------
# client
# --------------------------------------------------------------------------
class McpClient:
    """A connected MCP session that hands out RISTER tools."""

    def __init__(
        self,
        transport: Transport,
        *,
        client_info: Mapping[str, Any] | None = None,
        capabilities: Mapping[str, Any] | None = None,
        timeout: float = 30.0,
        auto_connect: bool = True,
    ) -> None:
        self.transport = transport
        self.client_info = dict(client_info or DEFAULT_CLIENT_INFO)
        self.capabilities = dict(capabilities or {})
        self.timeout = timeout
        self.profile: str | None = None
        self.protocol_version: str | None = None
        self.server_info: dict[str, Any] = {}
        self.server_capabilities: dict[str, Any] = {}
        self._next_id = 0
        self._catalogue: list[dict[str, Any]] | None = None
        if auto_connect:
            self.connect()

    # -- construction helpers ---------------------------------------------
    @classmethod
    def stdio(cls, command: Sequence[str] | str, **kwargs: Any) -> "McpClient":
        timeout = kwargs.pop("timeout", 30.0)
        return cls(StdioTransport(command, timeout=timeout, **kwargs), timeout=timeout)

    @classmethod
    def http(cls, url: str, **kwargs: Any) -> "McpClient":
        timeout = kwargs.pop("timeout", 30.0)
        headers = kwargs.pop("headers", None)
        return cls(
            HttpTransport(url=url, headers=headers or {}, timeout=timeout),
            timeout=timeout,
            **kwargs,
        )

    @classmethod
    def loopback(cls, handler: Callable[[dict[str, Any]], dict[str, Any] | None], **kwargs: Any) -> "McpClient":
        """Attach to an in-process server (used by tests and embedded servers)."""
        return cls(LoopbackTransport(handler), **kwargs)

    # -- session -----------------------------------------------------------
    def _meta(self) -> dict[str, Any]:
        return p.client_meta(self.protocol_version or p.PROTOCOL_VERSION,
                             self.client_info, self.capabilities)

    def _send(self, method: str, params: Mapping[str, Any] | None = None) -> Any:
        message_params: dict[str, Any] = dict(params or {})
        if self.profile == STATELESS or self.profile is None:
            message_params["_meta"] = self._meta()
        self._next_id += 1
        envelope = p.request(self._next_id, method, message_params)
        reply = self.transport.exchange(envelope)
        if reply is None:
            raise TransportError(f"no reply to {method!r}")
        return p.check(reply)

    def _notify(self, method: str, params: Mapping[str, Any] | None = None) -> None:
        self.transport.exchange(p.notification(method, params))

    def connect(self) -> "McpClient":
        """Negotiate with the server, preferring the stateless profile."""
        try:
            found = self._send("server/discover")
        except p.McpError as exc:
            if not exc.method_not_found and exc.code != p.INVALID_REQUEST:
                raise
            self._handshake()
        else:
            self.profile = STATELESS
            self._absorb_discovery(found)
        return self

    def _handshake(self) -> None:
        """Legacy path: ``initialize`` then ``notifications/initialized``."""
        result = self._send(
            "initialize",
            {
                "protocolVersion": p.PROTOCOL_VERSION,
                "capabilities": self.capabilities,
                "clientInfo": self.client_info,
            },
        )
        if not isinstance(result, Mapping):
            raise p.McpError("initialize returned no object", p.INVALID_REQUEST)
        version = str(result.get("protocolVersion") or p.PROTOCOL_VERSION)
        if version not in p.SUPPORTED_VERSIONS:
            version = "2024-11-05"
        self.profile = LEGACY
        self.protocol_version = version
        self.server_info = dict(result.get("serverInfo") or {})
        self.server_capabilities = dict(result.get("capabilities") or {})
        self._notify("notifications/initialized")

    def _absorb_discovery(self, found: Any) -> None:
        if not isinstance(found, Mapping):
            raise p.McpError("server/discover returned no object", p.INVALID_REQUEST)
        offered = found.get("versions") or found.get("supportedVersions") or []
        chosen = None
        for version in p.SUPPORTED_VERSIONS:
            if version in offered:
                chosen = version
                break
        self.protocol_version = chosen or str(
            found.get("protocolVersion") or p.PROTOCOL_VERSION
        )
        self.server_info = dict(found.get("serverInfo") or {})
        self.server_capabilities = dict(found.get("capabilities") or {})
        header = self.transport.protocol_header
        if header is not None and hasattr(self.transport, "protocol_version"):
            setattr(self.transport, "protocol_version", self.protocol_version)

    # -- tools -------------------------------------------------------------
    def list_tools(self, *, refresh: bool = False) -> list[dict[str, Any]]:
        """Enumerate the server's tools, following pagination cursors."""
        if self._catalogue is not None and not refresh:
            return list(self._catalogue)
        tools: list[dict[str, Any]] = []
        cursor: str | None = None
        seen: set[str] = set()
        for _ in range(100):  # hard stop: a server must not paginate forever
            params = {"cursor": cursor} if cursor else None
            result = self._send("tools/list", params)
            if not isinstance(result, Mapping):
                raise p.McpError("tools/list returned no object", p.INVALID_REQUEST)
            for entry in result.get("tools") or []:
                if isinstance(entry, Mapping):
                    tools.append(p.parse_tool_listing(entry))
            cursor = result.get("nextCursor")
            if not cursor or cursor in seen:
                break
            seen.add(str(cursor))
        self._catalogue = tools
        return list(tools)

    def call_tool(self, name: str, arguments: Mapping[str, Any] | None = None) -> Any:
        """Invoke a remote tool and flatten its content blocks."""
        if not name:
            raise ToolError("MCP tool name must not be empty")
        result = self._send(
            "tools/call", {"name": name, "arguments": dict(arguments or {})}
        )
        if not isinstance(result, Mapping):
            return result
        return p.flatten_content(result)

    def tools(
        self,
        *,
        requires_approval: bool = False,
        dangerous: bool = False,
        tags: Sequence[str] = (),
        prefix: str = "",
        only: Iterable[str] | None = None,
    ) -> list[Tool]:
        """Wrap the remote catalogue as RISTER tools.

        The wrappers are ordinary tools, so a :class:`~rister.govern.guard.Guard`
        sees, authorises and audits them exactly like local functions.
        """
        wanted = set(only) if only is not None else None
        wrapped: list[Tool] = []
        for entry in self.list_tools():
            name = entry["name"]
            if not name or (wanted is not None and name not in wanted):
                continue
            wrapped.append(
                Tool(
                    name=f"{prefix}{name}",
                    fn=_remote_callable(self, name),
                    description=entry.get("description") or f"MCP tool {name}",
                    parameters=entry["inputSchema"],
                    requires_approval=requires_approval,
                    dangerous=dangerous,
                    tags=tuple(tags),
                )
            )
        return wrapped

    # -- lifecycle ---------------------------------------------------------
    def ping(self) -> bool:
        try:
            self._send("ping")
            return True
        except (p.McpError, TransportError):
            return False

    def describe(self) -> dict[str, Any]:
        return {
            "profile": self.profile,
            "protocol_version": self.protocol_version,
            "server": self.server_info,
            "capabilities": sorted(self.server_capabilities),
            "tools": [entry["name"] for entry in (self._catalogue or [])],
        }

    def close(self) -> None:
        self.transport.close()

    def __enter__(self) -> "McpClient":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        self.close()

    def __repr__(self) -> str:
        server = self.server_info.get("name", "?")
        return f"<McpClient {server} profile={self.profile} tools={len(self._catalogue or [])}>"


def _remote_callable(client: McpClient, remote_name: str) -> Callable[..., Any]:
    """Adapt a remote tool to a keyword-argument callable."""

    def call(**arguments: Any) -> Any:
        return client.call_tool(remote_name, arguments)

    call.__name__ = remote_name
    return call


def connect(command: Sequence[str] | str | None = None, url: str | None = None,
            **kwargs: Any) -> McpClient:
    """Convenience: connect over stdio (``command``) or HTTP (``url``)."""
    if command and url:
        raise RisterError("pass either command or url, not both")
    if url:
        return McpClient.http(url, **kwargs)
    if command:
        return McpClient.stdio(command, **kwargs)
    raise RisterError("connect() needs a command or a url")


__all__ = [
    "Transport",
    "TransportError",
    "LoopbackTransport",
    "StdioTransport",
    "HttpTransport",
    "McpClient",
    "connect",
    "STATELESS",
    "LEGACY",
]
