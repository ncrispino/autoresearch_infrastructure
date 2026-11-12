"""Measured benchmark failures stay rankable; transport failures do not."""

from pathlib import Path
from types import SimpleNamespace

import pytest

import autoresearch_infrastructure as ari
from autoresearch_infrastructure.harbor_harness import HarborHarnessEvaluator
from autoresearch_infrastructure.harbor_trial import SUCCESS_RATE
from autoresearch_infrastructure.ledger import StartLedger
from autoresearch_infrastructure.source_tree import materialize

pytest.importorskip("harbor")

QUALITY = ari.Metric("quality")
LATENCY = ari.Metric("latency", "min")


class Result(SimpleNamespace):
    def model_dump(self, **kwargs):
        return {"exception_info": None if self.exception_info is None else {"exception_type": "Timeout"},
                "verifier_result": {"rewards": self.verifier_result.rewards},
                "agent_result": {"cost_usd": self.agent_result.cost_usd}}


def valid(candidate, context):
    return ari.Validity()


def setup(tmp_path, monkeypatch, *, rewards=None, metrics=None, usage=None, include_solver_tokens=False,
          network_mode="public"):
    from harbor.trial.trial import Trial

    task = tmp_path / "task"
    materialize(task, {"instruction.md": "Fixture task",
                       "task.toml": f'[environment]\nnetwork_mode="{network_mode}"\nworkdir="/workspace"\n'})
    seed = tmp_path / "seed"
    materialize(seed, {"program.toml": 'id="fixture"\ndescription="Fixture program"\nentrypoint="solve.py:solve"\n',
                       "solve.py": "async def solve(task, runtime): pass\n"})
    ledger = StartLedger(tmp_path / "starts.json", limits={"solver_trial": 3, "solver_session": 6}, identity={})
    configs = []

    async def create(config):
        configs.append(config)
        code = (Path(config.agent.kwargs["program_path"]) / "solve.py").read_text()
        kwargs = config.agent.kwargs
        assert kwargs["ledger_path"] == str(ledger.path)
        assert kwargs["ledger_limits"] == ledger.limits
        assert kwargs["ledger_identity"] == ledger.identity_hash

        async def run():
            return Result(exception_info=SimpleNamespace(exception_type="Timeout") if "#timeout" in code else None,
                          verifier_result=SimpleNamespace(rewards=rewards if rewards is not None else
                                                          {"reward": 0.5 if "#better" in code else 0}),
                          agent_result=SimpleNamespace(**({"cost_usd": 0.125} | (usage or {}))))

        return SimpleNamespace(run=run)

    monkeypatch.setattr(Trial, "create", create)
    evaluator = HarborHarnessEvaluator(tasks={"task": task}, solver_image="sha256:" + "a" * 64, model="fixture",
                               ledger=ledger, metrics=metrics, include_solver_tokens=include_solver_tokens)
    return seed, ledger, evaluator, configs


def test_wrong_answers_and_partial_success_rank_under_default_objective(tmp_path, monkeypatch):
    seed, ledger, evaluator, configs = setup(tmp_path, monkeypatch)
    with ledger.allocation_owner(), ari.Research(
        tmp_path / "run", seed=seed, objective=SUCCESS_RATE, evaluator=evaluator, validity=valid,
        evaluation_config={}, tasks=("task",), limits=ari.Limits(max_evaluations=3),
    ) as research:
        baseline = research.record_candidate()
        assert baseline["evidence"]["metrics"] == {"success_rate": 0}
        assert baseline["evidence"]["feasible"] and baseline["selected"] == baseline["candidate_id"]
        (research.workspace / "solve.py").write_text("async def solve(task, runtime): pass\n#better\n")
        better = research.record_candidate()
        assert better["evidence"]["metrics"] == {"success_rate": 0.5}
        assert better["evidence"]["task_completed"] and better["selected"] == better["candidate_id"]
        (research.workspace / "solve.py").write_text("async def solve(task, runtime): pass\n#timeout\n")
        failed = research.record_candidate()
        assert failed["evidence"]["evaluation"] == "incomplete"
        assert failed["selected"] == better["candidate_id"]
    starts = sorted(ledger.snapshot()["starts"].values(), key=lambda s: s["started_at"])
    assert [s["status"] for s in starts] == ["completed", "completed", "failed"]
    assert all(s["cost_usd"] == 0.125 for s in starts)
    assert len({config.trial_name for config in configs}) == 3


def test_explicit_reward_mapping_preserves_all_components(tmp_path, monkeypatch):
    rewards = {"accuracy": 0.5, "seconds": 12, "extra": 99}
    seed, ledger, evaluator, _ = setup(tmp_path, monkeypatch, rewards=rewards,
                                     metrics={QUALITY: "accuracy", LATENCY: "seconds"})
    result = evaluator(seed, ari.EvaluationContext("task", "screen", 1, "fixture", "fixture", tmp_path / "artifacts"))
    assert evaluator.ledger is ledger
    assert result.completed and result.metrics == {QUALITY: 0.5, LATENCY: 12}
    assert result.metadata["benchmark_rewards"] == rewards


@pytest.mark.parametrize("rewards,mapping", [
    ({"reward": float("nan")}, None), ({"reward": True}, None), ({}, None),
    ({"a": 1, "b": 2}, None), ({"wrong": 1}, {QUALITY: "accuracy"}),
])
def test_bad_or_missing_rewards_keep_the_start_charged(tmp_path, monkeypatch, rewards, mapping):
    seed, ledger, evaluator, _ = setup(tmp_path, monkeypatch, rewards=rewards, metrics=mapping)
    with pytest.raises(ValueError):
        evaluator(seed, ari.EvaluationContext("task", "screen", 1, "fixture", "fixture", tmp_path / "artifacts"))
    assert ledger.snapshot()["counts"]["solver_trial"] == 1
    start = next(iter(ledger.snapshot()["starts"].values()))
    assert start["status"] == "failed" and start["cost_usd"] == 0.125


def test_partial_session_telemetry_cannot_be_an_efficiency_metric(tmp_path, monkeypatch):
    from autoresearch_infrastructure.programs.bridge import ProgramBridge

    sessions = [
        {"usage": {"n_input_tokens": 10, "n_output_tokens": 2, "cost_usd": 0.1}},
        {"usage": {"n_output_tokens": 3}},
    ]
    usage = ProgramBridge._usage(SimpleNamespace(sessions=sessions, tool_calls=0))
    assert usage["n_input_tokens"] is None
    assert usage["n_output_tokens"] == 5
    assert usage["cost_usd"] is None
    assert usage["observed_n_input_tokens"] == 10
    assert usage["observed_cost_usd"] == 0.1
    seed, ledger, evaluator, _ = setup(tmp_path, monkeypatch, usage=usage, include_solver_tokens=True)
    with pytest.raises(ValueError, match="Native token usage is missing"):
        evaluator(seed, ari.EvaluationContext("task", "screen", 1, "fixture", "fixture", tmp_path / "artifacts"))
    start = next(iter(ledger.snapshot()["starts"].values()))
    assert start["status"] == "failed" and start["cost_usd"] is None


@pytest.mark.parametrize("network_mode", ["public", "no-network"])
def test_task_network_policy_selects_static_offline_backend_without_changing_task(
    tmp_path, monkeypatch, network_mode,
):
    from autoresearch_infrastructure.source_tree import snapshot

    seed, ledger, evaluator, configs = setup(tmp_path, monkeypatch, network_mode=network_mode)
    original = snapshot(tmp_path / "task")
    result = evaluator(seed, ari.EvaluationContext("task", "screen", 1, "fixture", "fixture", tmp_path / "artifacts"))
    assert result.completed
    expected = ("autoresearch_infrastructure.harbor_offline:OfflineDockerEnvironment"
                if network_mode == "no-network" else None)
    assert configs[0].environment.import_path == expected
    assert configs[0].task.path == tmp_path / "task"
    assert snapshot(tmp_path / "task") == original
    assert ledger.snapshot()["counts"]["solver_trial"] == 1
