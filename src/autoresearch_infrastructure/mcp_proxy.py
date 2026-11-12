"""The researcher's client for ``record_candidate``, copied into its container.

Run with no arguments it is an MCP stdio server that forwards the tool to the
controller's socket. Run with ``--call FILE`` it sends the arguments in FILE once
and prints the reply. It uses the standard library only.
"""

from __future__ import annotations

import argparse
import json
import os
import socket
import sys
from pathlib import Path


def _tool():
    schema = os.environ.get("AUTORESEARCH_INFRASTRUCTURE_TOOL_SCHEMA")
    if schema:
        return json.loads(Path(schema).read_text())
    return {
        "name": "record_candidate",
        "description": "Record candidate or add missing repeats",
        "inputSchema": {
            "type": "object",
            "properties": {
                "candidate": {"type": "string"},
                "total_repeats": {"type": "integer", "minimum": 1},
                "parent_id": {"type": ["string", "null"]},
                "proposal": {"type": "object"},
            },
            "required": ["candidate"],
            "additionalProperties": False,
        },
    }


def _call_controller(arguments: dict) -> dict:
    target = os.environ.get("AUTORESEARCH_SOCKET")
    if not target:
        raise RuntimeError("AUTORESEARCH_SOCKET is not configured")
    request = json.dumps({"name": "record_candidate", "arguments": arguments}).encode() + b"\n"
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as connection:
        connection.connect(target)
        connection.sendall(request)
        stream = connection.makefile("rb")
        line = stream.readline(8_000_001)
    if not line or len(line) > 8_000_000:
        raise RuntimeError("Candidate controller returned no bounded response")
    value = json.loads(line)
    if not isinstance(value, dict):
        raise RuntimeError("Candidate controller returned an invalid response")
    return value


def _feedback(value: dict) -> dict:
    """Keep native terminal feedback readable; full evidence remains in history."""
    evidence = value.get("evidence")
    if not value.get("candidate_id") or not isinstance(evidence, dict):
        return value
    result = {key: value[key] for key in (
        "candidate_id", "status", "source_hash", "contract_hash", "selected", "remaining_evaluations",
    ) if key in value}
    if "error" in value:
        result["error"] = value["error"]
    validity = evidence.get("validity", {})
    result["validity"] = {key: validity[key] for key in (
        "verdict", "reasons", "checks", "controller_error",
    ) if key in validity}
    result["evaluation"] = {key: evidence[key] for key in (
        "evaluation", "metrics", "score", "total_repeats", "task_completed", "feasible",
        "constraint_failures", "error",
    ) if key in evidence}
    result["history_relative_path"] = f"candidates/{value['candidate_id']}/"
    return result


def _response(request: dict) -> dict | None:
    method = request.get("method")
    request_id = request.get("id")
    if method == "notifications/initialized":
        return None
    if method == "initialize":
        result = {
            "protocolVersion": "2025-06-18",
            "capabilities": {"tools": {}},
            "serverInfo": {"name": "autoresearch_infrastructure", "version": "1"},
        }
    elif method == "tools/list":
        result = {"tools": [_tool()]}
    elif method == "tools/call":
        params = request.get("params", {})
        if params.get("name") != "record_candidate" or not isinstance(params.get("arguments"), dict):
            raise ValueError("Unknown tool or invalid arguments")
        value = _feedback(_call_controller(params["arguments"]))
        rejected = "error" in value and not value.get("candidate_id")
        result = {"content": [{"type": "text", "text": json.dumps(value)}], "isError": rejected}
    elif method == "ping":
        result = {}
    else:
        raise ValueError("Unsupported MCP method")
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--call", type=Path, metavar="JSON_FILE",
                        help="Send a record_candidate arguments object, then print the result and exit")
    args = parser.parse_args(argv)
    if args.call is not None:
        try:
            arguments = json.loads(args.call.read_text(encoding="utf-8"))
            if not isinstance(arguments, dict):
                raise ValueError("The arguments file must contain a JSON object")
            value = _feedback(_call_controller(arguments))
        except Exception as exc:
            value = {"error": str(exc)}
        print(json.dumps(value, ensure_ascii=False))
        return int("error" in value and not value.get("candidate_id"))
    for line in sys.stdin:
        request = None
        try:
            request = json.loads(line)
            if not isinstance(request, dict) or request.get("jsonrpc") != "2.0":
                raise ValueError("Invalid JSON-RPC request")
            reply = _response(request)
        except Exception as exc:
            reply = {
                "jsonrpc": "2.0",
                "id": request.get("id") if isinstance(request, dict) else None,
                "error": {"code": -32603, "message": str(exc)},
            }
        if reply is not None:
            sys.stdout.write(json.dumps(reply, separators=(",", ":")) + "\n")
            sys.stdout.flush()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
