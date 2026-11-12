"""Fixture evidence for HarborTaskEvaluator: no Docker and no model calls."""

import asyncio
import json
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

import autoresearch_infrastructure as ari
from autoresearch_infrastructure.harbor_task import HarborTaskEvaluator
from autoresearch_infrastructure.harbor_trial import SUCCESS_RATE
from autoresearch_infrastructure.ledger import StartLedger, StartLimitError
from autoresearch_infrastructure.persistence import digest
from autoresearch_infrastructure.source_tree import materialize, snapshot

pytest.importorskip("harbor")


def baseline(root):
    materialize(root, {
        "task.toml": '[environment]\nworkdir="/workspace"\nnetwork_mode="no-network"\ndocker_image="sha256:' + 'a' * 64 + '"\n',
        "instruction.md": "Sum the input numbers and save /workspace/answer.txt.",
        "environment/Dockerfile": "FROM python:3.12-slim\nWORKDIR /workspace\nCOPY numbers.txt .\n",
        "environment/numbers.txt": "2 3\n",
        "tests/test.sh": 'test "$(cat /workspace/answer.txt)" = 5\n',
        "solution/solve.sh": "printf 5 > /workspace/answer.txt\n",
    })
    return root


def evaluator(tmp_path, *, editable=None, agent=None, limit=4, metrics=None):
    task = baseline(tmp_path / "baseline")
    if editable is not None:
        (task / "fixture.bin").write_bytes(b"\xff\x00")
    ledger = StartLedger(tmp_path / "ledger.json", limits={"solver_trial": limit}, identity={})
    result = HarborTaskEvaluator(
        tasks={"sum": task}, agent=agent or {"name": "nop"}, ledger=ledger,
        editable=editable, metrics=metrics,
    )
    result.seed(tmp_path / "seed")
    return result


class TrialResult(SimpleNamespace):
    def model_dump(self, **kwargs):
        return {"exception_info": None, "verifier_result": {"rewards": self.verifier_result.rewards}}


def fake_trial(monkeypatch, *, rewards=None, error=False):
    from harbor.trial.trial import Trial

    captured = []

    async def create(config):
        captured.append(config)
        if error:
            raise RuntimeError("fixture setup failed")

        async def run():
            return TrialResult(
                exception_info=None, verifier_result=SimpleNamespace(rewards=rewards or {"reward": 0}),
                agent_result=SimpleNamespace(cost_usd=None),
            )

        return SimpleNamespace(run=run)

    monkeypatch.setattr(Trial, "create", create)
    return captured


def context(tmp_path, candidate):
    return ari.EvaluationContext("sum", "test", 1, digest(snapshot(candidate)), "fixture", tmp_path / "artifacts")


@pytest.mark.parametrize("agent", ["nop", "oracle", "terminus-2"])
def test_selected_inputs_keep_binary_assets_and_run_the_frozen_native_agent(tmp_path, monkeypatch, agent):
    original = {"name": agent, "model_name": "fixture-model"}
    ev = evaluator(tmp_path, agent=original, editable={"sum": ["instruction.md"]})
    original["name"] = "codex"
    candidate = tmp_path / "seed"
    (candidate / "sum/instruction.md").write_text("Calculate the total; write answer.txt.")
    captured = fake_trial(monkeypatch)
    value = ev(candidate, context(tmp_path, candidate))
    assert captured[0].agent.name == agent
    assert captured[0].agent.model_name == "fixture-model"
    assert (captured[0].task.path / "fixture.bin").read_bytes() == b"\xff\x00"
    assert captured[0].task.path != candidate
    assert (captured[0].task.path / "tests/test.sh").stat().st_mode & 0o111
    assert value.metrics == {SUCCESS_RATE: 0}
    assert value.completed is True  # A measured failure is a result, not missing evidence.
    assert value.trajectory is None  # No invented agent trace in a no-model fixture.
    overlay = json.loads(captured[0].environment.extra_docker_compose[0].read_text())
    assert overlay["services"]["main"]["read_only"] is True
    assert "build" not in overlay["services"]["main"]  # Immutable prebuilt image stays prebuilt.
    assert ev.ledger.snapshot()["counts"]["solver_trial"] == 1


def test_native_trial_accepts_static_offline_docker_without_dynamic_egress_support(tmp_path, monkeypatch):
    from harbor.environments.docker.docker import DockerEnvironment
    from harbor.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig, TrialConfig
    from harbor.trial.trial import Trial

    monkeypatch.setattr(DockerEnvironment, "_egress_control_kernel_support", staticmethod(lambda: False))
    task = baseline(tmp_path / "task")
    config = TrialConfig(
        task=TaskConfig(path=task), trial_name="offline", trials_dir=tmp_path / "jobs",
        agent=AgentConfig(name="nop"),
        environment=EnvironmentConfig(import_path="autoresearch_infrastructure.harbor_offline:OfflineDockerEnvironment"),
    )
    trial = asyncio.run(Trial.create(config))  # Construct only: no environment/model launch.
    environment = trial.agent_environment
    assert environment.capabilities.disable_internet
    assert not environment.capabilities.dynamic_network_policy
    assert environment.network_policy.network_mode == "no-network"
    assert environment._docker_compose_paths[-1].name == "docker-compose-no-network.yaml"


def test_environment_edits_force_an_isolated_rebuild_and_keep_the_baseline_unchanged(tmp_path, monkeypatch):
    ev = evaluator(tmp_path)
    candidate = tmp_path / "seed"
    (candidate / "sum/environment/numbers.txt").write_text("1 4\n")
    (candidate / "sum/environment/marker.py").write_text("raise RuntimeError('must not import on host')")
    captured = fake_trial(monkeypatch, rewards={"reward": 1})
    ev(candidate, context(tmp_path, candidate))
    assert captured[0].environment.force_build is True
    overlay = json.loads(captured[0].environment.extra_docker_compose[0].read_text())
    assert overlay["services"]["main"]["build"]["network"] == "none"
    assert (ev.tasks["sum"] / "environment/numbers.txt").read_text() == "2 3\n"
    assert (captured[0].task.path / "environment/numbers.txt").read_text() == "1 4\n"


@pytest.mark.parametrize("path,contents", [
    ("task.toml", '[environment]\nworkdir="/etc"\n'),
    ("environment/docker-compose.yaml", "services: {main: {privileged: true}}"),
    (".env", "DOCKER_HOST=forbidden"),
    ("solution/solve.sh", "printf 999 > answer.txt"),
])
def test_full_package_cannot_change_runtime_or_the_fixed_oracle(tmp_path, path, contents):
    ev = evaluator(tmp_path, agent={"name": "oracle"})
    candidate = tmp_path / "seed"
    (candidate / "sum" / path).write_text(contents)
    with pytest.raises(ValueError, match="protected runtime"):
        ev(candidate, context(tmp_path, candidate))
    assert ev.ledger.snapshot()["counts"]["solver_trial"] == 0


def test_selected_scope_rejects_undeclared_files_and_changed_baselines_before_start(tmp_path):
    ev = evaluator(tmp_path, editable={"sum": ["instruction.md"]})
    candidate = tmp_path / "seed"
    (candidate / "sum/extra.txt").write_text("undeclared")
    with pytest.raises(ValueError, match="exactly"):
        ev.prepare_task(candidate, "sum", tmp_path / "task1")
    (candidate / "sum/extra.txt").unlink()
    (ev.tasks["sum"] / "tests/test.sh").write_text("always pass")
    with pytest.raises(ValueError, match="Baseline"):
        ev.prepare_task(candidate, "sum", tmp_path / "task2")
    assert ev.ledger.snapshot()["counts"]["solver_trial"] == 0


def test_complete_panel_is_checked_before_any_harbor_start_and_failed_starts_stay_charged(tmp_path, monkeypatch):
    ev = evaluator(tmp_path, limit=1)
    captured = fake_trial(monkeypatch, error=True)
    with ari.Research(
        tmp_path / "run", seed=tmp_path / "seed", objective=SUCCESS_RATE,
        evaluator=ev, validity=lambda *args: ari.Validity(), evaluation_config={},
        tasks=("sum",), limits=ari.Limits(max_evaluations=2),
    ) as research:
        with pytest.raises(StartLimitError, match="start budget"):
            research.record_candidate(total_repeats=2)
    assert not captured
    assert ev.ledger.snapshot()["counts"]["solver_trial"] == 0
    with pytest.raises(RuntimeError, match="fixture setup"):
        ev(tmp_path / "seed", context(tmp_path, tmp_path / "seed"))
    starts = list(ev.ledger.snapshot()["starts"].values())
    assert len(starts) == 1 and starts[0]["status"] == "failed"
    assert (tmp_path / "artifacts/timing.json").is_file()


def test_independent_validity_rejects_a_proposed_always_pass_grader_before_evaluation(tmp_path, monkeypatch):
    ev = evaluator(tmp_path)
    candidate = tmp_path / "seed"
    (candidate / "sum/tests/test.sh").write_text("exit 0\n")
    captured = fake_trial(monkeypatch)

    def validity(path, context):
        # Trusted rule for this fixture only; not a general semantic-validity algorithm.
        if "exit 0" in (path / "sum/tests/test.sh").read_text():
            return ari.Validity("invalid", ("The proposed grader accepts an empty answer",))
        return ari.Validity()

    with ari.Research(
        tmp_path / "run", seed=candidate, objective=SUCCESS_RATE, evaluator=ev,
        validity=validity, evaluation_config={}, tasks=("sum",),
    ) as research:
        result = research.record_candidate()
    assert result["status"] == "invalid"
    assert not captured and ev.ledger.snapshot()["counts"]["solver_trial"] == 0


def test_custom_reward_mapping_preserves_all_raw_components(tmp_path, monkeypatch):
    quality, latency = ari.Metric("quality"), ari.Metric("seconds", "min")
    ev = evaluator(tmp_path, metrics={quality: "accuracy", latency: "elapsed"})
    fake_trial(monkeypatch, rewards={"accuracy": .5, "elapsed": 12, "other": 99})
    value = ev(tmp_path / "seed", context(tmp_path, tmp_path / "seed"))
    assert value.metrics == {quality: .5, latency: 12}
    assert value.metadata["benchmark_rewards"]["other"] == 99


def test_concurrent_research_cells_use_distinct_jobs_and_do_not_race_native_event_loops(tmp_path, monkeypatch):
    from harbor.trial.trial import Trial

    ev = evaluator(tmp_path, limit=2)
    active, peak, names = 0, 0, []

    async def create(config):
        names.append(config.trial_name)

        async def run():
            nonlocal active, peak
            active += 1
            peak = max(peak, active)
            await asyncio.sleep(.02)
            active -= 1
            return TrialResult(exception_info=None, verifier_result=SimpleNamespace(rewards={"reward": 1}),
                               agent_result=None)

        return SimpleNamespace(run=run)

    monkeypatch.setattr(Trial, "create", create)
    candidate = tmp_path / "seed"

    def evaluate(index):
        ctx = ari.EvaluationContext("sum", str(index), 1, digest(snapshot(candidate)), "fixture",
                                    tmp_path / f"artifacts-{index}")
        return ev(candidate, ctx)

    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(evaluate, (1, 2)))
    assert all(v.metrics == {SUCCESS_RATE: 1} for v in results)
    assert peak == 1 and len(set(names)) == 2
