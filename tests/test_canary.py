"""Baseline calibration and timing fixtures; no provider requests."""

import json

import pytest

from autoresearch_infrastructure.api import Evaluation, ValidityContext
from autoresearch_infrastructure.canary import BASELINE, BaselineValidity
from autoresearch_infrastructure.harbor_trial import SOLVER_TOKENS, SUCCESS_RATE, trial_timings
from autoresearch_infrastructure.ledger import StartLedger
from autoresearch_infrastructure.persistence import digest, write_json
from autoresearch_infrastructure.source_tree import materialize


def test_trial_timing_preserves_unknown_and_distinguishes_phase_durations():
    result = {
        "started_at": "2026-01-01T00:00:00+00:00",
        "finished_at": "2026-01-01T00:01:00+00:00",
        "environment_setup": {"started_at": "2026-01-01T00:00:00Z", "finished_at": "2026-01-01T00:00:10Z"},
        "agent_execution": {"started_at": "2026-01-01T00:00:10Z", "finished_at": "2026-01-01T00:00:30Z"},
        "verifier": {"started_at": "2026-01-01T00:00:30Z"},
    }
    timing = trial_timings(result)
    assert timing == {
        "trial_seconds": 60.0,
        "environment_setup_seconds": 10.0,
        "agent_setup_seconds": None,
        "agent_execution_seconds": 20.0,
        "verifier_seconds": None,
    }
    result["finished_at"] = "2025-12-31T23:59:00Z"
    with pytest.raises(ValueError, match="before"):
        trial_timings(result)


def test_only_the_exact_baseline_is_admitted(tmp_path):
    materialize(tmp_path / "seed", BASELINE)
    context = ValidityContext(digest(BASELINE), "fixture", tmp_path / "validity")
    assert BaselineValidity()(tmp_path / "seed", context).verdict == "valid"
    (tmp_path / "seed/solve.py").write_text("raise RuntimeError('replacement')")
    assert BaselineValidity()(tmp_path / "seed", context).verdict == "invalid"


def test_baseline_canary_retains_timings_and_resumes_without_new_paid_starts(tmp_path, monkeypatch):
    from autoresearch_infrastructure import canary

    ledger = StartLedger(
        tmp_path / "starts.json", limits={"solver_trial": 3, "solver_session": 3}, identity={}
    )
    called = []

    class FixtureEvaluator:
        max_harness_sessions = 1

        def __init__(self, **kwargs):
            self.ledger = ledger

        def __call__(self, candidate, context):
            key = ledger.start("solver_trial", context.task)
            called.append(context.task)
            ledger.finish(key, status="completed", cost_usd=0.0)
            return Evaluation(
                {SUCCESS_RATE: 1.0, SOLVER_TOKENS: 10},
                True,
                metadata={"timing": {"end_to_end_seconds": 12.5}, "cost_usd": 0.0},
            )

    monkeypatch.setattr(canary, "HarborHarnessEvaluator", FixtureEvaluator)
    output = tmp_path / "run"
    output.mkdir()
    tasks = {name: tmp_path / name for name in ("a", "b", "c")}
    identity = {
        "task_hashes": dict.fromkeys(tasks, "fixture"),
        "build_overlays": dict.fromkeys(tasks, "fixture-overlay"),
        "solver_image_id": "fixture",
    }
    controls = {}
    for agent in ("oracle", "nop"):
        for name in tasks:
            if agent == "nop" and name != "a":
                continue
            result = {
                "exception_info": None,
                "verifier_result": {"rewards": {"reward": 1.0 if agent == "oracle" else 0.0}},
            }
            row = {
                "agent": agent,
                "task": name,
                "task_hash": "fixture",
                "build_overlay_hash": "fixture-overlay",
                "result_hash": digest(result),
                "passed": True,
            }
            folder = output / "controls" / f"{agent}-{name}"
            write_json(folder / "harbor-result.json", result)
            write_json(folder / "receipt.json", row)
            controls[f"{agent}-{name}"] = row
    write_json(output / "references.json", {**identity, "controls": controls, "all_passed": True})
    plan = {
        "benchmark": {"negative_control_task_ids": ["a"]},
        "solver_model": "fixture",
        "solver_provider": "openrouter",
        "solver_timeout_seconds": 180,
        "max_solver_trial_starts": 3,
    }
    first = canary.baseline(plan, tasks, {}, identity, {}, ledger, output)
    assert first["status"] == "completed" and len(first["tasks"]) == 3
    assert all(row["timing"]["end_to_end_seconds"] == 12.5 for row in first["tasks"])
    second = canary.baseline(plan, tasks, {}, identity, {}, ledger, output)
    assert second["contract_hash"] == first["contract_hash"]
    assert called == ["a", "b", "c"] and ledger.snapshot()["counts"]["solver_trial"] == 3
    original = (output / "controls/oracle-a/harbor-result.json").read_text()
    write_json(
        output / "controls/oracle-a/harbor-result.json",
        {"exception_info": None, "verifier_result": {"rewards": {"reward": 0.0}}},
    )
    with pytest.raises(ValueError, match="changed or mismatched"):
        canary.baseline(plan, tasks, {}, identity, {}, ledger, output)
    (output / "controls/oracle-a/harbor-result.json").write_text(original)
    refs = json.loads((output / "references.json").read_text())
    refs["build_overlays"] = {"a": "changed"}
    write_json(output / "references.json", refs)
    with pytest.raises(ValueError, match="exact task/build"):
        canary.baseline(plan, tasks, {}, identity, {}, ledger, output)
    assert len(called) == 3
