"""Both native MCP clients and terminal clients use the same candidate controller."""

import json
import os
import subprocess
import sys

import pytest

from autoresearch_infrastructure import mcp_proxy
from autoresearch_infrastructure.tool_server import CandidateToolServer


def run_proxy(tmp_path, runner, arguments, *, stdio=False):
    argument_file = tmp_path / "proposal.json"
    argument_file.write_text(json.dumps(arguments))
    schema_file = tmp_path / "tool.json"
    schema_file.write_text(json.dumps({
        "name": "record_candidate", "inputSchema": {"properties": {"custom": {"type": "string"}}},
    }))
    target = tmp_path / "controller.sock"
    environment = {**os.environ, "AUTORESEARCH_SOCKET": str(target),
                   "AUTORESEARCH_INFRASTRUCTURE_TOOL_SCHEMA": str(schema_file)}
    command = [sys.executable, mcp_proxy.__file__]
    requests = None
    if stdio:
        requests = "\n".join(json.dumps(request) for request in (
            {"jsonrpc": "2.0", "id": 1, "method": "initialize"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 2, "method": "tools/list"},
            {"jsonrpc": "2.0", "id": 3, "method": "tools/call", "params": {
                "name": "record_candidate", "arguments": arguments,
            }},
        )) + "\n"
    else:
        command += ["--call", str(argument_file)]
    with CandidateToolServer(runner, "research", target):
        return subprocess.run(command, input=requests, capture_output=True, text=True,
                              env=environment, timeout=10)


@pytest.mark.parametrize("stdio", [False, True])
def test_argument_file_and_stdio_forward_identical_custom_arguments(tmp_path, stdio):
    arguments = {"candidate": "workspace", "custom": "Baseline's approach\nUse π carefully"}

    class Recorder:
        calls = []

        def dispatch_record_candidate(self, session, received):
            self.calls.append((session, received))
            return {"candidate_id": "candidate-0002", "status": "invalid", "selected": "candidate-0001",
                    "remaining_evaluations": 3, "evidence": {
                        "validity": {"verdict": "invalid", "reasons": ["Changed permissions"],
                                     "checks": {"scope": False}},
                        "evaluation": "not_started", "metrics": None,
                        "cells": {"large-details": "x" * 20000},
                    }}

    runner = Recorder()
    completed = run_proxy(tmp_path, runner, arguments, stdio=stdio)
    assert completed.returncode == 0, completed.stderr
    assert runner.calls == [("research", arguments)]
    replies = [json.loads(line) for line in completed.stdout.splitlines()]
    if stdio:
        assert len(replies) == 3  # Initialized notifications still have no response.
        assert replies[1]["result"]["tools"][0]["inputSchema"]["properties"] == {
            "custom": {"type": "string"},
        }
        assert replies[-1]["result"]["isError"] is False
        value = json.loads(replies[-1]["result"]["content"][0]["text"])
    else:
        value = replies[0]
    assert value["candidate_id"] == "candidate-0002" and value["status"] == "invalid"
    assert value["validity"]["checks"] == {"scope": False}
    assert value["evaluation"] == {"evaluation": "not_started", "metrics": None}
    assert value["history_relative_path"] == "candidates/candidate-0002/"
    assert len(completed.stdout) < 2000


@pytest.mark.parametrize("stdio", [False, True])
def test_controller_rejection_is_an_error_not_a_recorded_invalid_candidate(tmp_path, stdio):
    class RejectingRecorder:
        def dispatch_record_candidate(self, *_):
            raise ValueError("Proposal required")

    completed = run_proxy(tmp_path, RejectingRecorder(), {"candidate": "workspace"}, stdio=stdio)
    if stdio:
        result = json.loads(completed.stdout.splitlines()[-1])["result"]
        assert result["isError"] is True
        value = json.loads(result["content"][0]["text"])
    else:
        assert completed.returncode == 1
        value = json.loads(completed.stdout)
    assert value == {"error": "Proposal required"}


@pytest.mark.parametrize("stdio", [False, True])
def test_recorded_candidate_error_is_feedback_not_a_submission_rejection(tmp_path, stdio):
    class Recorder:
        def dispatch_record_candidate(self, *_):
            return {"candidate_id": "candidate-0002", "status": "valid", "error": "Evaluation interrupted",
                    "evidence": {"validity": {"verdict": "valid"}, "evaluation": "incomplete"}}

    completed = run_proxy(tmp_path, Recorder(), {"candidate": "workspace"}, stdio=stdio)
    assert completed.returncode == 0
    if stdio:
        result = json.loads(completed.stdout.splitlines()[-1])["result"]
        assert result["isError"] is False
        value = json.loads(result["content"][0]["text"])
    else:
        value = json.loads(completed.stdout)
    assert value["candidate_id"] == "candidate-0002"
    assert value["error"] == "Evaluation interrupted"


@pytest.mark.parametrize("content", ["[]", "not json"])
def test_call_rejects_bad_arguments_without_contacting_controller(tmp_path, monkeypatch, capsys, content):
    arguments = tmp_path / "arguments.json"
    arguments.write_text(content)
    monkeypatch.setattr(mcp_proxy, "_call_controller", lambda _: pytest.fail("Must not submit"))
    assert mcp_proxy.main(["--call", str(arguments)]) == 1
    assert "error" in json.loads(capsys.readouterr().out)


def test_compact_feedback_never_infers_a_score():
    result = mcp_proxy._feedback({"candidate_id": "candidate-0002", "evidence": {
        "validity": {"verdict": "valid"}, "evaluation": "incomplete", "metrics": None,
        "cells": {"one": {"result": {"metrics": {"score": 1}}}},
    }})
    assert result["evaluation"] == {"evaluation": "incomplete", "metrics": None}


def test_compact_feedback_preserves_measured_aggregate_and_selection():
    evaluation = {"evaluation": "completed", "metrics": {"accuracy": 0.75, "tokens": 300},
                  "score": 0.75, "total_repeats": 2, "task_completed": True, "feasible": True,
                  "constraint_failures": []}
    result = mcp_proxy._feedback({
        "candidate_id": "candidate-0002", "status": "valid", "selected": "candidate-0002",
        "remaining_evaluations": 4, "evidence": {**evaluation,
            "validity": {"verdict": "valid", "reasons": []}, "cells": {"details": "omitted"}},
    })
    assert result["evaluation"] == evaluation
    assert result["selected"] == "candidate-0002" and result["remaining_evaluations"] == 4
