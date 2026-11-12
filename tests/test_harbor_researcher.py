"""Researcher selection uses native Harbor jobs, with a real host recorder socket."""

import json
import socket
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("harbor")
from harbor.job import Job
from harbor.models.task.config import TaskConfig as FilesystemTask
from harbor.models.trial.config import AgentConfig

from autoresearch_infrastructure.coding_agent import CodingAgent, native_agent_cost
from autoresearch_infrastructure.persistence import write_json

IMAGE = "sha256:" + "a" * 64


def test_native_environment_templates_validate_without_mutating_configuration(monkeypatch):
    monkeypatch.setenv("ARI_FIXTURE_TURNS", "3")
    source = AgentConfig(name="claude-code", model_name="fixture-model", override_timeout_sec=60,
                         env={"CLAUDE_CODE_MAX_TURNS": "${ARI_FIXTURE_TURNS}"})
    agent = CodingAgent(agent=source, image=IMAGE)
    assert agent.agent.env == source.env == {"CLAUDE_CODE_MAX_TURNS": "${ARI_FIXTURE_TURNS}"}


class Recorder:
    def __init__(self):
        self.calls = []

    def dispatch_record_candidate(self, session, arguments):
        self.calls.append((session, arguments))
        return {"candidate_id": "candidate-0001", "status": "valid"}


def trajectory(name):
    return {
        "schema_version": "ATIF-v1.8", "session_id": "native-fixture",
        "agent": {"name": name, "version": "fixture", "model_name": "fixture-model"},
        "steps": [{"step_id": 1, "source": "user", "message": "Fixture instruction"},
                  {"step_id": 2, "source": "agent", "message": "Fixture completion"}],
        "final_metrics": {"total_cost_usd": 0.125},
    }


def fake_jobs(monkeypatch, *, failure=None):
    configs, originals = [], []

    async def create(config):
        configs.append(config)

        async def run():
            socket_mount = next(mount for mount in config.environment.mounts
                                if mount["target"] == "/run/autoresearch")
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
                client.connect(str(Path(socket_mount["source"]) / "controller.sock"))
                client.sendall(json.dumps({"name": "record_candidate", "arguments": {"candidate": "workspace"}})
                               .encode() + b"\n")
                returned = json.loads(client.makefile("rb").readline())
            assert returned["status"] == "valid"
            native = config.jobs_dir / config.job_name / "native-trial/agent/trajectory.json"
            write_json(native, trajectory(config.agents[0].name))
            originals.append(native)
            if failure == "raise":
                raise RuntimeError("Fixture native job failed")
            info = SimpleNamespace(exception_type="FixtureTimeout") if failure == "result" else None
            return SimpleNamespace(trial_results=[SimpleNamespace(exception_info=info, trial_name="native-trial")])

        return SimpleNamespace(run=run)

    monkeypatch.setattr(Job, "create", create)
    return configs, originals


@pytest.mark.parametrize("name", ["codex", "claude-code", "terminus-2"])
def test_native_harbor_researcher_dispatches_to_recorder_without_custom_agent(tmp_path, monkeypatch, name):
    configs, originals = fake_jobs(monkeypatch)
    skill = tmp_path / "fixture-skill"
    skill.mkdir()
    (skill / "SKILL.md").write_text(
        "---\nname: fixture-skill\ndescription: Native researcher fixture\n---\nRead the candidate first.\n"
    )
    source = AgentConfig(name=name, model_name="fixture-model", override_timeout_sec=120,
                         skills=[skill])
    agent = CodingAgent(agent=source, image=IMAGE)
    context = tmp_path / "context"
    context.mkdir()
    (context / "policy.md").write_text("Read-only fixture policy")
    recorder = Recorder()
    evidence = tmp_path / "invocations/first"
    metadata = agent.run("Fixture instruction", workspace=tmp_path / "workspace", evidence=evidence,
                         readonly={context: "/context"}, recorder=recorder, jobs_root=tmp_path / "jobs")
    config = configs[0]
    assert config.agents[0].name == name and config.agents[0].import_path is None
    assert config.agents[0].model_name == source.model_name
    assert config.agents[0].override_timeout_sec == 120
    assert config.agents[0].skills == source.skills == [str(skill)]
    assert source.mcp_servers == [] and agent.agent.mcp_servers == []
    server = config.agents[0].mcp_servers[0]
    assert server.name == "autoresearch" and server.transport == "stdio"
    assert server.command == "env"
    assert server.args == ["AUTORESEARCH_SOCKET=/run/autoresearch/controller.sock", "python",
                           "/opt/autoresearch/mcp_proxy.py"]
    assert recorder.calls == [("research", {"candidate": "workspace"})]
    mounts = {mount["target"]: mount for mount in config.environment.mounts}
    assert mounts["/context"]["read_only"] is True
    assert mounts["/run/autoresearch"]["read_only"] is True
    assert mounts["/opt/autoresearch/mcp_proxy.py"]["read_only"] is True
    assert not mounts["/workspace"].get("read_only", False)
    isolation = json.loads(config.environment.extra_docker_compose[0].read_text())["services"]["main"]
    assert isolation["read_only"] and isolation["cap_drop"] == ["ALL"]
    assert isolation["security_opt"] == ["no-new-privileges:true"]
    assert isolation["environment"]["AUTORESEARCH_SOCKET"] == "/run/autoresearch/controller.sock"
    assert config.verifier.disable is True and config.n_attempts == config.n_concurrent_trials == 1
    assert config.retry.max_retries == 0
    assert config.tasks[0].path == evidence / "filesystem-task"
    filesystem_task = FilesystemTask.model_validate_toml(
        (config.tasks[0].path / "task.toml").read_text()
    )
    # Harbor uploads injected skills before agent setup; its default /harbor/skills
    # is outside this launcher's writable mounts.
    assert filesystem_task.environment.skills_dir == "/tmp/harbor-skills"
    assert (evidence / "trajectory.json").read_bytes() == originals[0].read_bytes()
    assert metadata["viewer_job"] == config.job_name and metadata["viewer_trial"] == "native-trial"
    assert native_agent_cost(evidence) == 0.125


@pytest.mark.parametrize("failure", ["result", "raise"])
def test_failed_native_researcher_retains_original_partial_trajectory(tmp_path, monkeypatch, failure):
    _, originals = fake_jobs(monkeypatch, failure=failure)
    agent = CodingAgent(agent={"name": "terminus-2", "model_name": "fixture-model"}, image=IMAGE)
    evidence = tmp_path / "agent"
    with pytest.raises(RuntimeError, match="FixtureTimeout|Fixture native job failed"):
        agent.run("Fixture", workspace=tmp_path / "workspace", evidence=evidence, recorder=Recorder())
    assert (evidence / "trajectory.json").read_bytes() == originals[0].read_bytes()
    assert json.loads((evidence / "trajectory-metadata.json").read_text())["sha256"]
    assert native_agent_cost(evidence) == 0.125


def test_repeated_invocations_have_separate_native_tasks_and_do_not_mutate_config(tmp_path, monkeypatch):
    configs, _ = fake_jobs(monkeypatch)
    source = {"name": "terminus-2", "model_name": "fixture-model", "kwargs": {"record_terminal_session": False}}
    agent = CodingAgent(agent=source, image=IMAGE)
    recorder = Recorder()
    for name in ("one", "two"):
        agent.run("Fixture", workspace=tmp_path / "workspace", evidence=tmp_path / "invocations" / name,
                  recorder=recorder, jobs_root=tmp_path / "jobs")
    assert configs[0].tasks[0].path != configs[1].tasks[0].path
    assert len(recorder.calls) == 2 and "mcp_servers" not in source
    assert agent.agent.mcp_servers == []


@pytest.mark.parametrize("config, image, reason", [
    ({"name": "nop", "model_name": "fixture"}, IMAGE, "MCP servers and ATIF"),
    ({"name": "codex"}, IMAGE, "model_name"),
    ({"name": "codex", "model_name": "fixture", "override_timeout_sec": 0}, IMAGE, "timeout"),
    ({"name": "codex", "model_name": "fixture", "override_timeout_sec": float("inf")}, IMAGE, "timeout"),
    ({"name": "codex", "model_name": "fixture", "kwargs": {"unknown_option": True}}, IMAGE, "Unknown"),
    ({"name": "codex", "model_name": "fixture", "mcp_servers": [
        {"name": "autoresearch", "transport": "stdio", "command": "fixture"}]}, IMAGE, "reserved"),
    ({"name": "codex", "model_name": "fixture"}, "solver:latest", "immutable"),
])
def test_invalid_researcher_configuration_is_rejected_without_a_job(config, image, reason):
    with pytest.raises((ValueError, TypeError), match=reason):
        CodingAgent(agent=config, image=image)


def test_researcher_requires_a_recorder_before_creating_workspace(tmp_path):
    agent = CodingAgent(agent={"name": "codex", "model_name": "fixture"}, image=IMAGE)
    with pytest.raises(ValueError, match="requires a candidate recorder"):
        agent.run("Fixture", workspace=tmp_path / "workspace", evidence=tmp_path / "evidence")
    assert not tmp_path.joinpath("workspace").exists()
