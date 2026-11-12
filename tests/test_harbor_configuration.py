"""Supported native transports are frozen and forwarded, never silently substituted."""

from pathlib import Path

import pytest

from autoresearch_infrastructure import EvaluationContext, pilot
from autoresearch_infrastructure.harbor_filesystem import native_agent_config
from autoresearch_infrastructure.harbor_harness import HarborHarnessEvaluator
from autoresearch_infrastructure.ledger import StartLedger
from autoresearch_infrastructure.providers import provider_for
from autoresearch_infrastructure.source_tree import materialize


@pytest.mark.parametrize("harness,provider,endpoint,variable", [
    ("codex", "openai", "https://api.openai.com/v1", "OPENAI_API_KEY"),
    ("codex", "openrouter", "https://openrouter.ai/api/v1", "OPENROUTER_API_KEY"),
    ("claude-code", "anthropic", "https://api.anthropic.com", "ANTHROPIC_API_KEY"),
    ("claude-code", "openrouter", "https://openrouter.ai/api", "OPENROUTER_API_KEY"),
])
def test_evaluator_forwards_native_harness_and_exact_provider_transport(
    tmp_path, monkeypatch, harness, provider, endpoint, variable,
):
    pytest.importorskip("harbor")
    from harbor.trial.trial import Trial

    captured = []

    async def stop_before_launch(config):
        captured.append(config.agent)
        raise RuntimeError("Fixture stopped before any environment/model launch")

    monkeypatch.setattr(Trial, "create", stop_before_launch)
    task = tmp_path / "task"
    materialize(task, {"instruction.md": "Fixture public task", "task.toml": ""})
    candidate = tmp_path / "candidate"
    materialize(candidate, {
        "program.toml": 'id="fixture"\ndescription="fixture"\nentrypoint="solve.py:solve"\n',
        "solve.py": "async def solve(task,runtime): pass\n",
    })
    evaluator = HarborHarnessEvaluator(
        tasks={"task": task}, solver_image="sha256:" + "a" * 64, model="fixture-model",
        ledger=StartLedger(tmp_path / "ledger.json", limits={"solver_trial": 1, "solver_session": 1}, identity={}),
        harness=harness, provider=provider_for(harness, provider),
    )
    assert evaluator.identity["harness"] == harness
    assert evaluator.identity["provider"]["base_url"] == endpoint
    context = EvaluationContext("task", "screen", 1, "fixture", "fixture", tmp_path / "artifacts")
    with pytest.raises(RuntimeError, match="Fixture stopped"):
        evaluator(candidate, context)
    kwargs = captured[0].kwargs
    assert kwargs["harness"] == harness
    assert kwargs["provider_name"] == provider
    assert kwargs["provider_base_url"] == endpoint
    assert kwargs["provider_key_variable"] == variable
    assert evaluator.ledger.snapshot()["counts"]["solver_trial"] == 1


@pytest.mark.parametrize("provider,harness", [("unknown", "codex"), ("openai", "claude-code")])
def test_pilot_rejects_unsupported_transport_before_task_or_docker_access(tmp_path, provider, harness):
    shipped = Path(__file__).parents[1] / "examples/task-bank/spreadsheet-canary/long.toml"
    plan = tmp_path / "plan.toml"
    plan.write_text(shipped.read_text().replace('solver_provider = "openrouter"', f'solver_provider = "{provider}"')
                    .replace('[harness]', f'[harness]\nname = "{harness}"'))
    with pytest.raises(ValueError, match="Unsupported provider"):
        pilot.prepare(plan, tmp_path / "missing-tasks", tmp_path / "output", "no-docker")
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("old,new", [
    ('name = "terminus-2"', 'name = "unregistered-reviewer"'),
    ('model_name = "openrouter/openai/gpt-6-luna"', 'model_name = ""'),
    ('override_timeout_sec = 240', 'override_timeout_sec = 0'),
    ('override_timeout_sec = 240', 'override_timeout_sec = 240\noverride_setup_timeout_sec = inf'),
    ('override_timeout_sec = 240', 'override_timeout_sec = 240\nmax_timeout_sec = 7201'),
    ('override_timeout_sec = 240', 'override_timeout_sec = 240\n[[reviewer.mcp_servers]]\n'
     'name="autoresearch"\ntransport="stdio"\ncommand="python"'),
])
def test_pilot_rejects_invalid_native_reviewer_before_task_or_docker_access(tmp_path, old, new):
    pytest.importorskip("harbor")
    shipped = Path(__file__).parents[1] / "examples/task-bank/spreadsheet-canary/long.toml"
    plan = tmp_path / "plan.toml"
    before, remainder = shipped.read_text().split("[reviewer]", 1)
    reviewer, researcher = remainder.split("[researcher]", 1)
    plan.write_text(before + "[reviewer]" + reviewer.replace(old, new) + "[researcher]" + researcher)
    with pytest.raises(ValueError):
        pilot.prepare(plan, tmp_path / "missing-tasks", tmp_path / "output", "no-docker")
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("image", ["solver:latest", "solver:1.0", "sha256:short", "", None])
def test_evaluator_rejects_mutable_images_before_task_access_or_starts(tmp_path, image):
    ledger = StartLedger(tmp_path / "starts.json", limits={"solver_trial": 1, "solver_session": 1}, identity={})
    with pytest.raises(ValueError, match="immutable image"):
        HarborHarnessEvaluator(tasks={"task": tmp_path / "missing-task"}, solver_image=image,
                        model="fixture", ledger=ledger)
    assert ledger.snapshot()["counts"] == {"solver_trial": 0, "solver_session": 0}


@pytest.mark.parametrize("image", ["sha256:" + "a" * 64, "registry.example/solver@sha256:" + "b" * 64])
def test_evaluator_accepts_immutable_image_references_without_launch(tmp_path, image):
    task = tmp_path / "task"
    materialize(task, {"instruction.md": "Fixture public task"})
    ledger = StartLedger(tmp_path / "starts.json", limits={"solver_trial": 1, "solver_session": 1}, identity={})
    evaluator = HarborHarnessEvaluator(tasks={"task": task}, solver_image=image, model="fixture", ledger=ledger)
    assert evaluator.identity["image"] == image
    assert ledger.snapshot()["counts"] == {"solver_trial": 0, "solver_session": 0}


@pytest.mark.parametrize("old,new", [
    ('name = "terminus-2"', 'name = "unregistered-researcher"'),
    ('model_name = "openrouter/openai/gpt-6-luna"', 'model_name = ""'),
    ('override_timeout_sec = 7200', 'override_timeout_sec = 0'),
    ('override_timeout_sec = 7200', 'override_timeout_sec = inf'),
    ('override_timeout_sec = 7200', 'override_timeout_sec = 7201'),
    ('record_terminal_session = false', 'not_a_native_option = true'),
    ('override_timeout_sec = 7200', 'override_timeout_sec = 7200\noverride_setup_timeout_sec = inf'),
    ('override_timeout_sec = 7200', 'override_timeout_sec = 7200\nmax_timeout_sec = 7201'),
])
def test_pilot_rejects_invalid_native_researcher_before_task_or_docker_access(tmp_path, old, new):
    pytest.importorskip("harbor")
    shipped = Path(__file__).parents[1] / "examples/task-bank/spreadsheet-canary/long.toml"
    before, researcher = shipped.read_text().split("[researcher]", 1)
    plan = tmp_path / "plan.toml"
    plan.write_text(before + "[researcher]" + researcher.replace(old, new))
    with pytest.raises(ValueError):
        pilot.prepare(plan, tmp_path / "missing-tasks", tmp_path / "output", "no-docker")
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("name", ["codex", "claude-code", "terminus-2"])
def test_pilot_accepts_native_researcher_choices_without_own_harness_registry(tmp_path, name):
    pytest.importorskip("harbor")
    shipped = Path(__file__).parents[1] / "examples/task-bank/spreadsheet-canary/long.toml"
    before = shipped.read_text().split("[researcher]", 1)[0]
    plan = tmp_path / "plan.toml"
    plan.write_text(before + f'[researcher]\nname="{name}"\nmodel_name="fixture"\n'
                    'override_timeout_sec=60\n')
    with pytest.raises(ValueError, match="Evidence tree must be a regular directory"):
        pilot.prepare(plan, tmp_path / "missing-tasks", tmp_path / "output", "no-docker")
    assert not (tmp_path / "output").exists()


def test_pilot_resolves_native_researcher_environment_templates_before_validation(tmp_path, monkeypatch):
    pytest.importorskip("harbor")
    monkeypatch.setenv("ARI_FIXTURE_TURNS", "3")
    shipped = Path(__file__).parents[1] / "examples/task-bank/spreadsheet-canary/long.toml"
    before = shipped.read_text().split("[researcher]", 1)[0]
    plan = tmp_path / "plan.toml"
    plan.write_text(before + '[researcher]\nname="claude-code"\nmodel_name="fixture"\n'
                    'override_timeout_sec=60\n[researcher.env]\n'
                    'CLAUDE_CODE_MAX_TURNS="${ARI_FIXTURE_TURNS}"\n')
    with pytest.raises(ValueError, match="Evidence tree must be a regular directory"):
        pilot.prepare(plan, tmp_path / "missing-tasks", tmp_path / "output", "no-docker")
    assert not (tmp_path / "output").exists()


def test_pilot_rejects_researcher_without_candidate_mcp_or_atif_capability(tmp_path):
    pytest.importorskip("harbor")
    shipped = Path(__file__).parents[1] / "examples/task-bank/spreadsheet-canary/long.toml"
    before = shipped.read_text().split("[researcher]", 1)[0]
    plan = tmp_path / "plan.toml"
    plan.write_text(before + '[researcher]\nname="oracle"\nmodel_name="fixture"\n'
                    'override_timeout_sec=60\n')
    with pytest.raises(ValueError, match="MCP servers and ATIF"):
        pilot.prepare(plan, tmp_path / "missing-tasks", tmp_path / "output", "no-docker")
    assert not (tmp_path / "output").exists()


def test_pilot_rejects_reserved_researcher_mcp_name_before_task_or_image_access(tmp_path):
    pytest.importorskip("harbor")
    shipped = Path(__file__).parents[1] / "examples/task-bank/spreadsheet-canary/long.toml"
    before, researcher = shipped.read_text().split("[researcher]", 1)
    researcher = researcher.replace('[researcher.kwargs]', '[[researcher.mcp_servers]]\n'
                                    'name="autoresearch"\ntransport="stdio"\n'
                                    'command="python"\nargs=["untrusted.py"]\n\n[researcher.kwargs]')
    plan = tmp_path / "plan.toml"
    plan.write_text(before + "[researcher]" + researcher)
    with pytest.raises(ValueError, match="reserved"):
        pilot.prepare(plan, tmp_path / "missing-tasks", tmp_path / "output", "no-docker")
    assert not (tmp_path / "output").exists()


@pytest.mark.parametrize("field", ["override_timeout_sec", "override_setup_timeout_sec", "max_timeout_sec"])
@pytest.mark.parametrize("timeout", [0, -1, float("inf"), float("nan"), 7201])
def test_native_agent_validation_bounds_every_timeout_before_launch(field, timeout):
    pytest.importorskip("harbor")
    with pytest.raises(ValueError, match=field):
        native_agent_config({"name": "codex", "model_name": "fixture", field: timeout}, default_timeout=240)


def test_native_agent_validation_copies_options_and_resolves_environment_only_for_validation(monkeypatch):
    pytest.importorskip("harbor")
    from harbor.models.trial.config import AgentConfig

    monkeypatch.setenv("ARI_FIXTURE_TURNS", "3")
    source = AgentConfig(name="claude-code", model_name="fixture",
                         env={"CLAUDE_CODE_MAX_TURNS": "${ARI_FIXTURE_TURNS}"})
    agent = native_agent_config(source, default_timeout=240)
    assert agent.override_timeout_sec == 240 and source.override_timeout_sec is None
    assert agent.env == source.env == {"CLAUDE_CODE_MAX_TURNS": "${ARI_FIXTURE_TURNS}"}
    agent.env["CLAUDE_CODE_MAX_TURNS"] = "4"
    assert source.env["CLAUDE_CODE_MAX_TURNS"] == "${ARI_FIXTURE_TURNS}"
    with pytest.raises(ValueError, match="override_timeout_sec"):
        native_agent_config(source, default_timeout=None)
