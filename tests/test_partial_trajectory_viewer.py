"""Partial native evidence needs explicit controller approval and matching hashes."""

import hashlib
import json

import pytest

from autoresearch_infrastructure import Metric, Research, Validity
from autoresearch_infrastructure.persistence import digest, write_json
from autoresearch_infrastructure.viewer import build_index, write_html_preview


def test_partial_native_inventory_is_explicit_bound_and_never_manufactures_score(tmp_path):
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "program.txt").write_text("fixture")

    def evaluate(candidate, context):
        for namespace, cost in [("native-runner", 0.07), ("task-output-spoof", 999)]:
            session = context.artifacts / namespace / "sessions/session-1"
            session.mkdir(parents=True)
            document = {
                "session_id": namespace,
                "agent": {"name": "fixture"},
                "steps": [],
                "final_metrics": {"total_cost_usd": cost},
            }
            native = session / "trajectory.json"
            write_json(native, document)
            write_json(
                session / "result.json",
                {
                    "completed": False,
                    "trajectory": {"sha256": hashlib.sha256(native.read_bytes()).hexdigest()},
                },
            )
        raise TimeoutError("Fixture native session deadline")

    with Research(
        tmp_path / "run",
        seed=seed,
        objective=Metric("quality"),
        validity=lambda *args: Validity(),
        evaluator=evaluate,
        evaluation_config={"fixture": "partial-native-evidence"},
    ) as research:
        candidate = research.record_candidate()
        assert candidate["evidence"]["evaluation"] == "incomplete"
    output = tmp_path / "run"
    index = build_index(output)
    saved_candidate = index["sessions"][0]["candidates"][0]
    evidence = saved_candidate["evidence"][0]
    cell = evidence["cells"][0]
    assert cell["status"] == "missing" and cell["result"] is None
    attempt = cell["trial_starts"][0]
    assert attempt["solver_sessions"] == []  # Never discover authority by scanning task output.
    native = next(
        output.glob(
            "sessions/*/history/candidates/*/evaluations/*/cells/*/*/*/trial-start-*/artifacts/native-runner/sessions/session-1/trajectory.json"
        )
    )
    sha = hashlib.sha256(native.read_bytes()).hexdigest()
    manifest = tmp_path / "controller-approved-partials.json"
    record = {
        "result_path": attempt["result_path"],
        "candidate_hash": saved_candidate["source_hash"],
        "contract_hash": evidence["contract"],
        "solver_sessions": [
            {
                "session_id": "session-1",
                "parent_id": None,
                "role": "solver",
                "status": "failed",
                "trajectory": {
                    "format": "ATIF",
                    "sha256": sha,
                    "path": "/" + native.relative_to(output / "sessions/research").as_posix(),
                    "usage": {"cost_usd": 999},
                },
            }
        ],
    }
    inventory = {
        "schema_version": 1,
        "run_state_sha256": digest(json.loads((output / "state.json").read_text())),
        "records": [record],
    }
    write_json(manifest, inventory)
    approved = build_index(output, partial_evidence=manifest)
    evidence = approved["sessions"][0]["candidates"][0]["evidence"][0]
    assert evidence["cells"][0]["status"] == "missing" and evidence["cells"][0]["result"] is None
    session = evidence["cells"][0]["trial_starts"][0]["solver_sessions"][0]
    assert session["status"] == "failed"
    assert session["trajectory"]["saved_key"].startswith("/api/research/trajectories/")
    assert approved["run"]["costs"]["solver"]["cost_usd"] == 0.07
    summary = write_html_preview(output, tmp_path / "partial.html", partial_evidence=manifest)
    assert summary["trajectories"] == 1
    assert approved["run"]["partial_evidence_sha256"] == hashlib.sha256(manifest.read_bytes()).hexdigest()

    wrong = json.loads(manifest.read_text())
    wrong["records"][0]["candidate_hash"] = "a" * 64
    write_json(tmp_path / "wrong-candidate.json", wrong)
    rejected = build_index(output, partial_evidence=tmp_path / "wrong-candidate.json")
    assert rejected["run"]["costs"]["solver"]["trajectory_count"] == 0
    assert any("mismatched candidate/contract" in warning for warning in rejected["warnings"])
    wrong["run_state_sha256"] = "b" * 64
    write_json(tmp_path / "wrong-state.json", wrong)
    with pytest.raises(ValueError, match="saved run state"):
        build_index(output, partial_evidence=tmp_path / "wrong-state.json")

    native.write_text(json.dumps({"agent": {}, "steps": [], "final_metrics": {"total_cost_usd": 999}}))
    tampered = build_index(output, partial_evidence=manifest)
    assert not tampered["sessions"][0]["candidates"][0]["evidence"][0]["cells"][0]["trial_starts"][0][
        "solver_sessions"
    ]
    assert tampered["run"]["costs"]["solver"]["cost_usd"] == 0
    assert any("hash mismatch" in warning for warning in tampered["warnings"])
