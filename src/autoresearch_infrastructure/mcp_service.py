"""Serve a solver session's task tools over HTTP MCP. Every call goes through the program's hooks."""

from __future__ import annotations

import asyncio
import json
import secrets
import threading
from contextlib import contextmanager
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .programs.parsing import _parse_json

MAX_MESSAGE = 1_000_000
MCP_PROXY_PATH = Path(__file__).with_name("task_mcp.py")


def mcp_tools(tools: list[dict]) -> list[dict]:
    """Convert tool definitions to MCP's schema. Only the public definition is copied."""
    result = []
    for tool in tools:
        row = {
            "name": tool["name"],
            "description": tool.get("description", ""),
            "inputSchema": tool.get("inputSchema", tool.get("parameters", {"type": "object"})),
        }
        if "annotations" in tool:
            row["annotations"] = tool["annotations"]
        metadata = dict(tool.get("_meta", {}))
        metadata.update({key[2:]: value for key, value in tool.items() if key.startswith("x-")})
        if metadata:
            row["_meta"] = metadata
        result.append(row)
    if len({row["name"] for row in result}) != len(result):
        raise ValueError("Duplicate public tool names")
    return result


@contextmanager
def session_mcp_service(tools, handler, loop, *, timeout_seconds=120, native_handler=None):
    """Serve stateless Streamable HTTP JSON-RPC with a fresh per-session capability.

    ``handler`` is the bridge's async hook-mediated call dispatcher. The server has
    no environment object or raw execution endpoint. Keep its thread off the event
    loop while a model session is running. Optional native hook messages use a private
    adapter method on this transport; no extra MCP tool is registered.
    """
    public = mcp_tools(tools)
    names = {tool["name"] for tool in public}
    token = secrets.token_urlsafe(32)
    closed = threading.Event()

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, value=None):
            raw = json.dumps(value, allow_nan=False).encode() if value is not None else b""
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(raw)))
            self.end_headers()
            self.wfile.write(raw)

        def do_GET(self):
            # No SSE stream or legacy raw /call endpoint exists on this server.
            self.reply(405, {"error": "Use POST /mcp"})

        def do_POST(self):
            if not secrets.compare_digest(self.headers.get("Authorization", ""), f"Bearer {token}"):
                return self.reply(403, {"error": "Unauthorized"})
            if self.path != "/mcp" or closed.is_set():
                return self.reply(404, {"error": "Not found"})
            request_id = None
            try:
                size = int(self.headers.get("Content-Length", "0"))
                if not 0 < size <= MAX_MESSAGE:
                    raise ValueError("Invalid MCP message size")
                self.connection.settimeout(min(30, timeout_seconds))
                document = _parse_json(self.rfile.read(size).decode("utf-8"), max_bytes=MAX_MESSAGE)
                if not isinstance(document, dict) or document.get("jsonrpc") != "2.0":
                    raise ValueError("Expected one JSON-RPC request")
                candidate_id = document.get("id")
                method = document.get("method")
                if method == "notifications/initialized" and "id" not in document:
                    return self.reply(202)
                if candidate_id is None or not isinstance(candidate_id, (str, int, float)) or isinstance(candidate_id, bool):
                    raise ValueError("MCP request needs a scalar ID")
                request_id = candidate_id
                if method == "initialize":
                    result = {
                        "protocolVersion": "2025-03-26", "capabilities": {"tools": {}},
                        "serverInfo": {"name": "ari-task-tools", "version": "1"},
                    }
                elif method == "ping":
                    result = {}
                elif method == "tools/list":
                    result = {"tools": public}
                elif method == "tools/call":
                    params = document.get("params", {})
                    if (
                        not isinstance(params, dict) or not isinstance(params.get("name"), str)
                        or params["name"] not in names
                    ):
                        raise ValueError("Unknown session tool")
                    arguments = params.get("arguments", {})
                    if not isinstance(arguments, dict):
                        raise ValueError("Tool arguments must be an object")
                    future = asyncio.run_coroutine_threadsafe(
                        handler({"name": params["name"], "args": arguments}), loop,
                    )
                    try:
                        observation = future.result(timeout=timeout_seconds)
                    except BaseException:
                        future.cancel()
                        raise
                    if hasattr(observation, "model_dump"):
                        observation = observation.model_dump(mode="json")
                    if not isinstance(observation, dict):
                        raise ValueError("Invalid tool observation")
                    result = {
                        "content": [{"type": "text", "text": json.dumps(observation, allow_nan=False)}],
                        "isError": bool(observation.get("error") or observation.get("denied")),
                    }
                elif method == "ari/native_hook" and native_handler is not None:
                    # Private adapter transport, never advertised as a model tool.
                    future = asyncio.run_coroutine_threadsafe(
                        native_handler(document.get("params")), loop,
                    )
                    try:
                        result = future.result(timeout=timeout_seconds)
                    except BaseException:
                        future.cancel()
                        raise
                else:
                    return self.reply(200, {
                        "jsonrpc": "2.0", "id": request_id,
                        "error": {"code": -32601, "message": "Method not found"},
                    })
                self.reply(200, {"jsonrpc": "2.0", "id": request_id, "result": result})
            except Exception as exc:
                self.reply(200, {
                    "jsonrpc": "2.0", "id": request_id,
                    "error": {"code": -32603, "message": str(exc)[:1000]},
                })

    server = ThreadingHTTPServer(("0.0.0.0", 0), Handler)
    server.daemon_threads = True
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://tool-host:{server.server_port}/mcp", token
    finally:
        closed.set()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
