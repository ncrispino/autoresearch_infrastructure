"""Controller quota and lifetime tests; no model calls."""

import json
import socket
import threading

import pytest

from autoresearch_infrastructure import ValidityContext
from autoresearch_infrastructure.ledger import StartLedger, StartLimitError
from autoresearch_infrastructure.persistence import digest
from autoresearch_infrastructure.pilot import ProgramValidity
from autoresearch_infrastructure.search import SingleSubmissionRecorder
from autoresearch_infrastructure.source_tree import materialize, snapshot
from autoresearch_infrastructure.tool_server import CandidateToolServer


def test_proposer_gets_one_workspace_submission_with_the_selected_parent():
    calls = []

    class Runner:
        def dispatch_record_candidate(self, session, arguments):
            calls.append((session, arguments))
            return {"candidate_id": "candidate-0002"}

    recorder = SingleSubmissionRecorder(Runner(), "candidate-0001", 1)
    with pytest.raises(ValueError, match="new workspace"):
        recorder.dispatch_record_candidate("research", {"candidate": "candidate-0001"})
    result = recorder.dispatch_record_candidate(
        "research",
        {"candidate": "workspace", "proposal": {"hypothesis": "coverage", "mechanism": "range checks"}},
    )
    assert result["candidate_id"] == "candidate-0002"
    assert calls[0][1]["parent_id"] == "candidate-0001"
    assert calls[0][1]["total_repeats"] == 1
    with pytest.raises(ValueError, match="already submitted"):
        recorder.dispatch_record_candidate("research", {"candidate": "workspace"})
    assert len(calls) == 1


def test_screen_review_cannot_spend_the_confirmation_reserve(tmp_path, monkeypatch):
    pytest.importorskip("harbor")
    ledger = StartLedger(tmp_path / "starts.json", limits={"reviewer": 2}, identity={})
    ledger.start("reviewer", "earlier screen review")
    monkeypatch.setattr("autoresearch_infrastructure.review.run_filesystem_agent",
                        lambda **kwargs: pytest.fail("No new reviewer may launch"))
    candidate = tmp_path / "candidate"
    materialize(candidate, {
        "program.toml": 'id="fixture"\ndescription="Fixture program"\nentrypoint="solve.py:solve"\n',
        "solve.py": "from autoresearch_infrastructure.programs import SolverOutput\nasync def solve(task, runtime):\n    return SolverOutput('fixture')\n",
    })
    reviewer = ProgramValidity(agent={"name": "codex", "model_name": "fixture-model"}, image="sha256:" + "a" * 64,
                               ledger=ledger, reserved_reviews=1).for_output(
                                   tmp_path, baseline=candidate, baseline_hash="b" * 64)
    with pytest.raises(StartLimitError, match="start budget"):
        reviewer(candidate, ValidityContext(digest(snapshot(candidate)), "fixture", tmp_path / "attempt"))
    assert ledger.snapshot()["counts"]["reviewer"] == 1


def test_tool_server_waits_for_owned_evaluation_after_client_disconnect(tmp_path):
    started, release, persisted, closing, closed = (threading.Event() for _ in range(5))

    class Runner:
        def dispatch_record_candidate(self, session, arguments):
            started.set()
            assert release.wait(5)
            persisted.set()
            return {"saved": True}

    path = tmp_path / "controller.sock"
    server = CandidateToolServer(Runner(), "research", path)
    server.__enter__()
    client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    client.connect(str(path))
    client.sendall(json.dumps({"name": "record_candidate", "arguments": {}}).encode() + b"\n")
    assert started.wait(2)
    client.close()

    def close():
        closing.set()
        server.__exit__(None, None, None)
        closed.set()

    thread = threading.Thread(target=close)
    thread.start()
    assert closing.wait(1)
    try:
        assert not closed.wait(0.6), "Controller released its evaluation while it was still running"
    finally:
        release.set()
        thread.join(5)
    assert persisted.is_set() and closed.is_set()
    assert not path.exists()


def test_opaque_allocation_identity_preserves_charges_without_exposing_plan(tmp_path):
    identity = {"screen": ["public-case"], "confirmation": ["sealed-case"]}
    path = tmp_path / "starts.json"
    original = StartLedger(path, limits={"solver_session": 2}, identity=identity)
    original.start("solver_session", "first")
    opaque = StartLedger(path, limits=original.limits, identity=original.identity_hash)
    assert opaque.snapshot()["counts"]["solver_session"] == 1
    opaque.start("solver_session", "second")
    with pytest.raises(StartLimitError):
        original.start("solver_session", "third")
    with pytest.raises(ValueError, match="SHA256"):
        StartLedger(tmp_path / "invalid.json", limits=original.limits, identity="malformed")
