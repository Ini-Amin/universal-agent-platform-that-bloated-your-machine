"""Stdlib-only MCP stdio client (Master sections 12, 13, 29).

No MCP SDK: this speaks JSON-RPC 2.0 directly over a child process's
stdin/stdout, which keeps the platform's dependency surface at pydantic and
makes the wire behaviour auditable.

Framing decision (documented deliberately): messages are **newline-delimited
JSON** - one JSON object per line - NOT LSP-style `Content-Length:` framing.
The MCP stdio transport used by mcp-go (the bugbounty-mcp server) writes one
JSON object per line, so the client matches it. See
docs/research/bugbounty-mcp-inventory.md section 1.

Request handling:
- request ids are sequential integers; responses are matched by id and any
  unrelated message (a server notification, or a late reply to a timed-out
  request) is skipped;
- a JSON-RPC `error` object becomes an `MCPClientError` carrying code+message;
- a timeout becomes an `MCPClientError` naming the method/tool;
- a dead process becomes an `MCPClientError` carrying the stderr tail.

The client is synchronous and blocking; callers that need concurrency run it in
a worker thread. The MCP adapter wraps it in an async registry tool.
"""

from __future__ import annotations

import collections
import json
import os
import queue
import subprocess
import threading
import time
from typing import Any

import httpx

from uap.mcp.config import MCPServerConfig

PROTOCOL_VERSION = "2024-11-05"
CLIENT_INFO: dict[str, str] = {"name": "uap", "version": "0.1.0"}
STDERR_TAIL_CHARS = 2000
_EOF = object()


class MCPClientError(Exception):
    """Any failure talking to an MCP server over stdio."""


class StdioMCPClient:
    """Launch one MCP server as a subprocess and speak JSON-RPC over stdio."""

    def __init__(self, config: MCPServerConfig, timeout_s: float = 20.0) -> None:
        self.config = config
        self.timeout_s = float(timeout_s)
        self.server_info: dict[str, Any] | None = None

        self._proc: subprocess.Popen[str] | None = None
        self._next_id = 0
        self._stdout_q: queue.Queue[Any] = queue.Queue()
        self._stderr_lines: collections.deque[str] = collections.deque(maxlen=400)
        self._stderr_lock = threading.Lock()
        self._threads: list[threading.Thread] = []

    # ------------------------------------------------------------------ #
    # Introspection (used by the adapter and tests)
    # ------------------------------------------------------------------ #

    @property
    def started(self) -> bool:
        """True once a subprocess has been spawned by `start()`."""
        return self._proc is not None

    @property
    def process(self) -> subprocess.Popen[str] | None:
        """The underlying process, if any (read-only; for tests/observability)."""
        return self._proc

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def start(self) -> dict[str, Any]:
        """Spawn the server, run the initialize handshake, return server info."""
        if self._proc is not None:
            return self.server_info or {}

        env = {**os.environ, **self.config.env}
        try:
            self._proc = subprocess.Popen(
                [self.config.command, *self.config.args],
                stdin=subprocess.PIPE,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
                env=env,
            )
        except OSError as exc:
            raise MCPClientError(
                f"failed to start MCP server {self.config.name!r}: {exc}"
            ) from exc

        self._start_readers()
        try:
            result = self._request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": dict(CLIENT_INFO),
                },
                label="initialize",
            )
        except Exception:
            self.close()
            raise

        # Accept whatever protocolVersion the server negotiated.
        self.server_info = result if isinstance(result, dict) else {}
        self._notify("notifications/initialized", {})
        return self.server_info

    def close(self) -> None:
        """Terminate the subprocess and readers. Never raises."""
        proc = self._proc
        if proc is not None:
            try:
                if proc.stdin is not None and not proc.stdin.closed:
                    try:
                        proc.stdin.close()
                    except Exception:
                        pass
                if proc.poll() is None:
                    proc.terminate()
                    try:
                        proc.wait(timeout=2.0)
                    except subprocess.TimeoutExpired:
                        proc.kill()
                        try:
                            proc.wait(timeout=2.0)
                        except Exception:
                            pass
            except Exception:
                pass
            finally:
                for stream in (proc.stdout, proc.stderr):
                    try:
                        if stream is not None and not stream.closed:
                            stream.close()
                    except Exception:
                        pass

        for thread in self._threads:
            try:
                thread.join(timeout=1.0)
            except Exception:
                pass
        self._threads = []

    def __enter__(self) -> "StdioMCPClient":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self.close()
        return False

    # ------------------------------------------------------------------ #
    # MCP operations
    # ------------------------------------------------------------------ #

    def list_tools(self) -> list[dict[str, Any]]:
        """`tools/list` -> the raw list of {name, description, inputSchema}."""
        result = self._request("tools/list", {}, label="tools/list")
        if not isinstance(result, dict):
            return []
        tools = result.get("tools", [])
        return [t for t in tools if isinstance(t, dict)] if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """`tools/call` -> the raw JSON-RPC result payload (not interpreted)."""
        result = self._request(
            "tools/call",
            {"name": name, "arguments": dict(arguments)},
            label=f"tools/call:{name}",
        )
        return result if isinstance(result, dict) else {}

    # ------------------------------------------------------------------ #
    # Wire plumbing
    # ------------------------------------------------------------------ #

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _send(self, message: dict[str, Any]) -> None:
        proc = self._proc
        if proc is None or proc.stdin is None or proc.poll() is not None:
            raise MCPClientError(self._dead_message("cannot send request"))
        try:
            proc.stdin.write(json.dumps(message) + "\n")
            proc.stdin.flush()
        except (BrokenPipeError, OSError, ValueError) as exc:
            raise MCPClientError(self._dead_message(f"write failed: {exc}")) from exc

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        self._send({"jsonrpc": "2.0", "method": method, "params": params})

    def _request(
        self, method: str, params: dict[str, Any], *, label: str | None = None
    ) -> Any:
        if self._proc is None:
            raise MCPClientError(
                f"MCP client for {self.config.name!r} is not started"
            )
        label = label or method
        request_id = self._new_id()
        self._send(
            {
                "jsonrpc": "2.0",
                "id": request_id,
                "method": method,
                "params": params,
            }
        )

        deadline = time.monotonic() + self.timeout_s
        while True:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise MCPClientError(self._timeout_message(label))
            message = self._read_message(remaining, label)

            # Skip notifications and any reply not addressed to this request.
            if message.get("id") != request_id:
                continue

            error = message.get("error")
            if error is not None:
                code = error.get("code") if isinstance(error, dict) else None
                text = (
                    error.get("message") if isinstance(error, dict) else str(error)
                )
                raise MCPClientError(
                    f"MCP server error {code} from {label}: {text}"
                )
            return message.get("result")

    def _read_message(self, timeout: float, label: str) -> dict[str, Any]:
        try:
            item = self._stdout_q.get(timeout=max(timeout, 0.0))
        except queue.Empty:
            raise MCPClientError(self._timeout_message(label)) from None

        if item is _EOF:
            raise MCPClientError(
                self._dead_message(f"process exited before answering {label}")
            )
        try:
            message = json.loads(item)
        except json.JSONDecodeError as exc:
            raise MCPClientError(
                f"MCP server {self.config.name!r} sent invalid JSON: {exc}"
            ) from exc
        if not isinstance(message, dict):
            raise MCPClientError(
                f"MCP server {self.config.name!r} sent a non-object JSON-RPC message"
            )
        return message

    def _timeout_message(self, label: str) -> str:
        return (
            f"MCP request {label!r} timed out after {self.timeout_s:g}s "
            f"(server {self.config.name!r})"
        )

    def _dead_message(self, prefix: str) -> str:
        tail = self._stderr_tail()
        detail = f"; stderr: {tail}" if tail else ""
        return f"MCP server {self.config.name!r} is not running ({prefix}){detail}"

    def _stderr_tail(self) -> str:
        with self._stderr_lock:
            text = "".join(self._stderr_lines)
        return text[-STDERR_TAIL_CHARS:].strip()

    # ------------------------------------------------------------------ #
    # Reader threads: pipe I/O with timeouts without blocking on readline.
    # ------------------------------------------------------------------ #

    def _start_readers(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None and proc.stderr is not None
        stdout_thread = threading.Thread(
            target=self._pump_stdout, args=(proc,), daemon=True
        )
        stderr_thread = threading.Thread(
            target=self._pump_stderr, args=(proc,), daemon=True
        )
        self._threads = [stdout_thread, stderr_thread]
        stdout_thread.start()
        stderr_thread.start()

    def _pump_stdout(self, proc: subprocess.Popen[str]) -> None:
        try:
            for line in proc.stdout:  # type: ignore[union-attr]
                self._stdout_q.put(line)
        except Exception:
            pass
        finally:
            self._stdout_q.put(_EOF)

    def _pump_stderr(self, proc: subprocess.Popen[str]) -> None:
        try:
            for line in proc.stderr:  # type: ignore[union-attr]
                with self._stderr_lock:
                    self._stderr_lines.append(line)
        except Exception:
            pass


class HttpMCPClient:
    """Speak MCP JSON-RPC to a REMOTE server over Streamable HTTP.

    The transport is the second official MCP transport (stdio is the first;
    WebSocket is not part of MCP). A request is a POST of one JSON-RPC message
    with ``Accept: application/json, text/event-stream``; the reply arrives
    either as a JSON body or as an SSE stream carrying the message. Servers may
    hand back an ``Mcp-Session-Id`` header that must be echoed afterwards.

    Public surface mirrors :class:`StdioMCPClient` exactly so
    :func:`uap.mcp.adapter.register_mcp_server` is transport-agnostic.
    """

    def __init__(self, config: MCPServerConfig, timeout_s: float = 20.0) -> None:
        if config.transport != "http":
            raise MCPClientError(
                f"HttpMCPClient requires transport='http', got {config.transport!r}"
            )
        if not config.url:
            raise MCPClientError("http transport requires a url")
        self.config = config
        self.timeout_s = float(timeout_s)
        self.server_info: dict[str, Any] | None = None
        self._session_id: str | None = None
        self._next_id = 0
        self._client: httpx.Client | None = None

    # ------------------------------------------------------------------ #
    # Introspection (mirrors StdioMCPClient)
    # ------------------------------------------------------------------ #

    @property
    def started(self) -> bool:
        """True once the initialize handshake has succeeded."""
        return self.server_info is not None

    @property
    def process(self) -> None:
        """No subprocess exists for a remote server (mirrors the stdio API)."""
        return None

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def start(self) -> dict[str, Any]:
        """Run the initialize handshake; return the server info."""
        if self.server_info is not None:
            return self.server_info
        if self._client is None:
            # Only build a real transport when one was not injected (tests
            # inject a MockTransport-backed client; clobbering it would make
            # the call hit the network).
            self._client = httpx.Client(timeout=self.timeout_s)
        try:
            result = self._request(
                "initialize",
                {
                    "protocolVersion": PROTOCOL_VERSION,
                    "capabilities": {},
                    "clientInfo": dict(CLIENT_INFO),
                },
                label="initialize",
            )
            self.server_info = result if isinstance(result, dict) else {}
            # Best-effort: the spec's initialized notification. Servers that do
            # not expect it simply ignore the extra POST.
            try:
                self._notify("notifications/initialized", {})
            except MCPClientError:
                pass
            return self.server_info
        except Exception:
            self.close()
            raise

    def close(self) -> None:
        """Release the HTTP client (no subprocess to reap)."""
        client, self._client = self._client, None
        if client is not None:
            try:
                client.close()
            except Exception:
                pass
        self.server_info = None
        self._session_id = None

    def __enter__(self) -> "HttpMCPClient":
        self.start()
        return self

    def __exit__(self, *exc_info: object) -> bool:
        self.close()
        return False

    # ------------------------------------------------------------------ #
    # MCP operations (mirror StdioMCPClient)
    # ------------------------------------------------------------------ #

    def list_tools(self) -> list[dict[str, Any]]:
        """`tools/list` -> the raw list of {name, description, inputSchema}."""
        result = self._request("tools/list", {}, label="tools/list")
        if not isinstance(result, dict):
            return []
        tools = result.get("tools", [])
        return [t for t in tools if isinstance(t, dict)] if isinstance(tools, list) else []

    def call_tool(self, name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        """`tools/call` -> the raw JSON-RPC result payload (not interpreted)."""
        result = self._request(
            "tools/call",
            {"name": name, "arguments": dict(arguments)},
            label=f"tools/call:{name}",
        )
        return result if isinstance(result, dict) else {}

    # ------------------------------------------------------------------ #
    # Wire plumbing
    # ------------------------------------------------------------------ #

    def _new_id(self) -> int:
        self._next_id += 1
        return self._next_id

    def _headers(self) -> dict[str, str]:
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
        if self._session_id:
            headers["Mcp-Session-Id"] = self._session_id
        # Optional auth from config env. The value is never logged.
        auth = self.config.env.get("AUTHORIZATION") or self.config.env.get("Authorization")
        if not auth:
            key = self.config.env.get("SMITHERY_API_KEY") or self.config.env.get("MCP_AUTH_TOKEN")
            if key:
                auth = f"Bearer {key}"
        if auth:
            headers["Authorization"] = auth
        return headers

    def _notify(self, method: str, params: dict[str, Any]) -> None:
        """Send a JSON-RPC notification (no id, no reply expected)."""
        if self._client is None:
            raise MCPClientError("client is not started; call start() first")
        payload = {"jsonrpc": "2.0", "method": method, "params": dict(params)}
        try:
            self._client.post(str(self.config.url), json=payload, headers=self._headers())
        except httpx.HTTPError as exc:
            raise MCPClientError(f"notification {method} failed: {exc}") from exc

    def _request(
        self, method: str, params: dict[str, Any], *, label: str
    ) -> Any:
        if self._client is None:
            raise MCPClientError("client is not started; call start() first")
        request_id = self._new_id()
        payload = {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": method,
            "params": dict(params),
        }
        try:
            response = self._client.post(
                str(self.config.url), json=payload, headers=self._headers()
            )
        except httpx.TimeoutException as exc:
            raise MCPClientError(f"{label} timed out: {exc}") from exc
        except httpx.HTTPError as exc:
            raise MCPClientError(f"{label} failed: {exc}") from exc

        # Remember a session id the server handed back, to echo from now on.
        session = response.headers.get("Mcp-Session-Id")
        if session:
            self._session_id = session

        if response.status_code >= 400:
            raise MCPClientError(
                f"{label} failed: HTTP {response.status_code}: {response.text[:200]}"
            )

        message = self._decode_message(response, label=label)
        if not isinstance(message, dict):
            raise MCPClientError(f"{label}: malformed reply (not a JSON object)")

        error = message.get("error")
        if error:
            raise MCPClientError(f"{label} error: {error}")
        return message.get("result")

    @staticmethod
    def _decode_message(response: httpx.Response, *, label: str) -> Any:
        """Extract the JSON-RPC message from a JSON body OR an SSE stream."""
        content_type = response.headers.get("content-type", "")
        if "text/event-stream" not in content_type.lower():
            try:
                return response.json()
            except ValueError as exc:
                raise MCPClientError(f"{label}: malformed JSON reply: {exc}") from exc

        # SSE: the message rides in `data:` lines; take the last complete one.
        message: Any = None
        for line in response.text.splitlines():
            if not line.startswith("data:"):
                continue
            chunk = line[len("data:") :].strip()
            if not chunk:
                continue
            try:
                parsed = json.loads(chunk)
            except ValueError:
                continue
            # Skip notifications / unrelated ids: we want the reply object.
            if isinstance(parsed, dict) and ("result" in parsed or "error" in parsed):
                message = parsed
        if message is None:
            raise MCPClientError(f"{label}: SSE reply carried no result message")
        return message


__all__ = ["MCPClientError", "StdioMCPClient", "HttpMCPClient", "PROTOCOL_VERSION", "CLIENT_INFO"]
