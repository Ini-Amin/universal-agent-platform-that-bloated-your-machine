#!/usr/bin/env python3
"""A tiny MCP stdio server used only by tests/test_mcp.py.

Speaks newline-delimited JSON-RPC 2.0 on stdin/stdout (one JSON object per
line), matching the mcp-go stdio transport that `uap.mcp.client` targets. It
exposes three tools:

- ``echo``      requires ``text`` (string) -> returns it as text content;
- ``fail_tool`` -> answers with a JSON-RPC error object;
- ``slow_tool`` -> sleeps ~2s, then replies (used to exercise timeouts).

Unknown methods get JSON-RPC error -32601. The script is started as a
subprocess via ``sys.executable``; it is not imported by the test process.
"""

from __future__ import annotations

import json
import sys
import time

PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "echo",
        "description": "Echo text back to the caller.",
        "inputSchema": {
            "type": "object",
            "properties": {"text": {"type": "string", "description": "Text to echo."}},
            "required": ["text"],
        },
    },
    {
        "name": "fail_tool",
        "description": "Always fails with a JSON-RPC error.",
        "inputSchema": {"type": "object", "properties": {}},
    },
    {
        "name": "slow_tool",
        "description": "Sleeps two seconds before replying.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def _send(message: dict) -> None:
    sys.stdout.write(json.dumps(message) + "\n")
    sys.stdout.flush()


def _result(request_id, result) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "result": result})


def _error(request_id, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": request_id, "error": {"code": code, "message": message}})


def _handle(request: dict) -> None:
    method = request.get("method")
    request_id = request.get("id")
    params = request.get("params") or {}

    # Notifications carry no id and must never be answered.
    if request_id is None:
        return

    if method == "initialize":
        _result(
            request_id,
            {
                "protocolVersion": params.get("protocolVersion", PROTOCOL_VERSION),
                "capabilities": {"tools": {}},
                "serverInfo": {"name": "mock-mcp", "version": "0.0.1"},
            },
        )
    elif method == "tools/list":
        _result(request_id, {"tools": TOOLS})
    elif method == "tools/call":
        _call(request_id, params)
    else:
        _error(request_id, -32601, f"method not found: {method}")


def _call(request_id, params: dict) -> None:
    name = params.get("name")
    arguments = params.get("arguments") or {}

    if name == "echo":
        text = arguments.get("text")
        if not isinstance(text, str):
            _error(request_id, -32602, "invalid params: 'text' must be a string")
            return
        _result(request_id, {"content": [{"type": "text", "text": text}]})
    elif name == "fail_tool":
        _error(request_id, -32000, "fail_tool exploded on purpose")
    elif name == "slow_tool":
        time.sleep(2.0)
        _result(request_id, {"content": [{"type": "text", "text": "slept 2s"}]})
    else:
        _error(request_id, -32602, f"unknown tool: {name}")


def main() -> None:
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(request, dict):
            _handle(request)


if __name__ == "__main__":
    main()
