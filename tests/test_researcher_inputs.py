"""Fixture evidence: researcher inputs, presentation, memory views and environments. No model calls."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from autoresearch_infrastructure import launch
from autoresearch_infrastructure.inputs import Environment, Memory, ResearcherInputs, Start, present
from autoresearch_infrastructure.instructions import Instructions
from autoresearch_infrastructure.search import RESEARCHER_PROTOCOL

pytest.importorskip("harbor")

IMAGE = "sha256:" + "a" * 64
PINNED = "python@sha256:" + "b" * 64
AGENT = 'name = "codex"\nmodel_name = "openai/fixture"\n'
TASK = '''
import json
from pathlib import Path

import autoresearch_infrastructure as ari

ERROR = ari.Metric("error", "min")


def evaluate(candidate, context):
    return ari.Evaluation({ERROR: abs(10 - json.loads((candidate / "value.json").read_text()))})


def presenter(inputs, start):
    return "CUSTOM " + inputs.goal + " parent=" + start.parent_id


def task(run):
    return ari.Task(
        seed=Path(__file__).parent / "seed", objective=ERROR, evaluator=evaluate,
        instructions=ari.Instructions(objective="Move value.json toward ten.", rules=["Hold one integer."]),
        context={"note.txt": "Read-only note"},
        EXTRA
    )
'''


@pytest.fixture
def example(tmp_path):
    def make(extra="", settings=f"reviewer = false\n[limits]\ncandidates = 4\nevaluations = 4\n[researcher]\n{AGENT}"):
        directory = tmp_path / "task"
        (directory / "seed").mkdir(parents=True, exist_ok=True)
        (directory / "seed/value.json").write_text("0")
        (directory / "task.py").write_text(TASK.replace("EXTRA", extra))
        (directory / "run.toml").write_text(settings)
        return directory
    return SimpleNamespace(make=make, output=tmp_path / "run", root=tmp_path)


def fixture_agent(monkeypatch, *, fail_on=()):
    """Each start records what it saw, saves a conversation, and submits value = start number."""
    seen = []

    class FixtureAgent:
        def __init__(self, *, agent, image=None, environment=None):
            self.image, self.environment = image, environment

        def run(self, prompt, *, workspace, evidence, readonly, recorder, jobs_root, load_trajectory=None):
            history = next(path for path, target in readonly.items() if target == "/history")
            files = sorted(p.relative_to(history).as_posix() for p in Path(history).rglob("*") if p.is_file())
            seen.append(SimpleNamespace(
                prompt=prompt, files=files, index=json.loads((Path(history) / "index.json").read_text()),
                workspace=sorted(p.relative_to(workspace).as_posix() for p in workspace.rglob("*")),
                load=load_trajectory and json.loads(Path(load_trajectory).read_text()),
                image=self.image, environment=self.environment,
            ))
            evidence.mkdir(parents=True)
            (evidence / "trajectory.json").write_text(json.dumps({"start": len(seen)}))
            (workspace / f"notes-{len(seen)}.txt").write_text("left behind")
            if len(seen) in fail_on:
                raise RuntimeError("fixture failure")
            (workspace / "candidate/value.json").write_text(json.dumps(len(seen)))
            recorder.dispatch_record_candidate("research", {
                "candidate": "workspace", "proposal": {"hypothesis": "Closer", "mechanism": "Next value"},
            })

    monkeypatch.setattr("autoresearch_infrastructure.coding_agent.CodingAgent", FixtureAgent)
    return seen


def run(directory, output, image=IMAGE):
    opened = launch.open_run(directory, directory / "run.toml", output, image)
    with opened.research:
        return launch.run_researchers(opened), opened


def inputs_records(output):
    root = output / "research/sessions/research/agent/inputs"
    return [json.loads(path.read_text()) for path in sorted(root.glob("*/inputs.json"),
                                                          key=lambda p: p.stat().st_mtime_ns)]


def test_default_inputs_keep_the_standard_prompt_and_record_each_start(example, monkeypatch):
    seen = fixture_agent(monkeypatch)
    summary, opened = run(example.make(), example.output)
    assert summary["status"] == "completed" and len(seen) == 3
    plan = json.loads((example.output / "plan.json").read_text())
    assert plan["inputs"] == {"memory": {"history": "all", "preload": None, "conversations": 0,
                                         "conversation_mode": "files"},
                              "environment": {"harbor": None, "workspace": "keep"}, "presenter": "default"}
    standard = plan["instructions"].rstrip() + "\n\n" + RESEARCHER_PROTOCOL.substitute(
        parent_id="candidate-0001", repeats=1, memory_rows="",
        parent_evidence="Read its evidence at `/history/candidates/candidate-0001/` before editing.")
    assert seen[0].prompt == standard
    assert "candidates/candidate-0001/snapshot/value.json" in seen[0].files
    records = inputs_records(example.output)
    assert [r["history"]["view"] for r in records] == ["canonical"] * 3
    assert sorted(records[2]["history"]["candidates"]) == ["candidate-0001", "candidate-0002", "candidate-0003"]
    prompt_file = next((example.output / "research/sessions/research/agent/inputs").glob("*/prompt.md"))
    assert prompt_file.read_text() in {s.prompt for s in seen}
    # The default keeps earlier researchers' files outside the candidate.
    assert "notes-1.txt" in seen[1].workspace


def test_history_window_builds_a_separate_view_and_leaves_evidence_alone(example, monkeypatch):
    seen = fixture_agent(monkeypatch)
    summary, opened = run(example.make("memory=ari.Memory(history=1),"), example.output)
    assert summary["status"] == "completed"
    last = seen[-1]
    assert [c["id"] for c in last.index["candidates"]] == ["candidate-0003"]
    assert not any(f.startswith("candidates/candidate-0001") for f in last.files)
    assert "In this run `/history` holds only 1 earlier candidate(s): `candidate-0003`." in last.prompt
    canonical = json.loads((example.output / "research/sessions/research/history/index.json").read_text())
    assert len(canonical["candidates"]) == 4
    assert not (example.output / "research/sessions/research/agent/view").exists()
    assert inputs_records(example.output)[-1]["history"]["view"] == "filtered"


def test_no_history_with_preloaded_records_that_are_not_evidence(example, monkeypatch):
    records = example.root / "earlier"
    (records / "notes").mkdir(parents=True)
    (records / "notes/idea.md").write_text("Try 9")
    seen = fixture_agent(monkeypatch)
    summary, opened = run(example.make('memory=ari.Memory(history="none", preload=Path(__file__).parent.parent '
                                       '/ "earlier"),'), example.output)
    assert summary["status"] == "completed"
    assert seen[0].index["candidates"] == [] and seen[0].index["selected_candidate"] is None
    assert seen[0].files == ["index.json", "preloaded/notes/idea.md"]
    assert "Its record is not shown." in seen[0].prompt and "`/history/preloaded/`" in seen[0].prompt
    assert list(opened.research.candidates) == ["candidate-0001", "candidate-0002", "candidate-0003",
                                                "candidate-0004"]
    plan = json.loads((example.output / "plan.json").read_text())
    assert plan["inputs"]["memory"]["preload"]
    (example.output / "memory/preloaded/notes/idea.md").write_text("Tampered")
    with pytest.raises(ValueError, match="plan changed"):
        launch.open_run(example.root / "task", example.root / "task/run.toml", example.output, IMAGE)


def test_conversation_window_includes_failed_starts_in_time_order(example, monkeypatch):
    seen = fixture_agent(monkeypatch, fail_on=(2,))
    five = f"reviewer = false\n[limits]\ncandidates = 5\nevaluations = 5\n[researcher]\n{AGENT}"
    summary, opened = run(example.make("memory=ari.Memory(conversations=2),", settings=five), example.output)
    assert summary["status"] == "completed"
    conversations = [[f for f in s.files if f.startswith("conversations/")] for s in seen]
    assert conversations[0] == []
    assert len(conversations[1]) == 1
    assert len(conversations[3]) == 2  # starts 2 (failed) and 3
    assert "`/history/conversations/`" in seen[1].prompt
    assert inputs_records(example.output)[3]["conversations"][0]["invocation"] in conversations[3][0]


def test_native_conversation_loading_continues_the_previous_start(example, monkeypatch):
    seen = fixture_agent(monkeypatch)
    run(example.make('memory=ari.Memory(conversations=1, conversation_mode="native"),'), example.output)
    assert seen[0].load is None
    assert [s.load for s in seen[1:]] == [{"start": 1}, {"start": 2}]
    assert not any(f.startswith("conversations/") for s in seen for f in s.files)
    with pytest.raises(ValueError, match="conversations=1"):
        Memory(conversations=2, conversation_mode="native")


def test_workspace_reset_removes_files_outside_the_candidate(example, monkeypatch):
    seen = fixture_agent(monkeypatch)
    run(example.make('environment=ari.Environment(workspace="reset"),'), example.output)
    assert not any(name.startswith("notes-") for s in seen for name in s.workspace)


def test_a_presenter_changes_the_prompt_but_not_what_the_reviewer_reads(example, monkeypatch):
    seen = fixture_agent(monkeypatch)
    run(example.make("presenter=presenter,"), example.output)
    assert seen[0].prompt == "CUSTOM Move value.json toward ten. parent=candidate-0001"
    plan = json.loads((example.output / "plan.json").read_text())
    assert plan["inputs"]["presenter"] != "default"
    assert "Move value.json toward ten." in plan["instructions"] and "Hold one integer." in plan["instructions"]


def test_resolved_inputs_separate_the_goal_from_what_is_measured():
    inputs = ResearcherInputs.resolve(Instructions("Improve x.", ("Rule A",)), ["Score: `e`. Lower is better."])
    assert inputs.goal == "Improve x." and inputs.measured == ("Score: `e`. Lower is better.",)
    assert "### What is measured" in inputs.specification()
    prompt = present(inputs, Start(1, "candidate-0002", 3))
    assert prompt.startswith(inputs.specification().rstrip()) and "`candidate-0002`" in prompt


def harbor_task(root, dockerfile=f"FROM {PINNED}\n", table='cpus = 2\n', compose=None):
    directory = root / "env-task"
    (directory / "environment").mkdir(parents=True, exist_ok=True)
    (directory / "task.toml").write_text(f"[environment]\n{table}")
    (directory / "instruction.md").write_text("Harbor's own instruction, which ARI ignores")
    if dockerfile is not None:
        (directory / "environment/Dockerfile").write_text(dockerfile)
    if compose is not None:
        (directory / "environment/docker-compose.yaml").write_text(compose)
    return directory


@pytest.mark.parametrize("change, message", [
    ({"dockerfile": "FROM python:3.12\n"}, "pinned by digest"),
    ({"table": 'workdir = "/app"\n'}, "ARI sets"),
    ({"compose": "services:\n  db:\n    image: postgres:16\n"}, "db image must be pinned"),
    ({"dockerfile": None}, "needs"),
    ({"dockerfile": None, "table": 'docker_image = "python:3.12"\n'}, "pinned by digest"),
])
def test_harbor_environments_must_be_pinned_and_leave_ari_settings_alone(tmp_path, change, message):
    from autoresearch_infrastructure import harbor_environment

    with pytest.raises(ValueError, match=message):
        harbor_environment.load(harbor_task(tmp_path, **change))


def test_a_harbor_environment_replaces_the_researcher_image_and_is_frozen(example, monkeypatch):
    from autoresearch_infrastructure import harbor_environment

    environment = harbor_task(example.root, dockerfile=f"FROM {PINNED} AS base\nFROM base\nRUN true\n",
                              compose=f"services:\n  db:\n    image: {PINNED}\n")
    assert harbor_environment.load(environment).config["cpus"] == 2
    seen = fixture_agent(monkeypatch)
    directory = example.make('environment=ari.Environment(harbor="../env-task"),')
    summary, opened = run(directory, example.output, image=None)
    assert summary["status"] == "completed"
    assert seen[0].image is None and seen[0].environment.files == example.output / "environment"
    assert sorted(p.name for p in (example.output / "environment").iterdir()) == ["Dockerfile",
                                                                                  "docker-compose.yaml"]
    plan = json.loads((example.output / "plan.json").read_text())
    assert plan["inputs"]["environment"]["harbor"]
    assert inputs_records(example.output)[0]["environment"] == seen[0].environment.identity
    (environment / "environment/Dockerfile").write_text(f"FROM {PINNED}\nRUN echo changed\n")
    with pytest.raises(ValueError, match="plan changed"):
        launch.open_run(directory, directory / "run.toml", example.output, None)


def test_reviewer_still_needs_the_image_when_the_researcher_has_an_environment(example):
    harbor_task(example.root)
    directory = example.make('environment=ari.Environment(harbor="../env-task"),',
                             settings=f"[limits]\ncandidates = 2\n[researcher]\n{AGENT}")
    with pytest.raises(ValueError, match="--image"):
        launch.open_run(directory, directory / "run.toml", example.output, None)


def test_harbor_environment_files_and_settings_reach_the_filesystem_task(tmp_path, monkeypatch):
    from harbor.job import Job
    from harbor.models.task.config import TaskConfig

    from autoresearch_infrastructure import harbor_environment
    from autoresearch_infrastructure.harbor_filesystem import run_filesystem_agent

    environment = harbor_environment.load(harbor_task(tmp_path, table='cpus = 2\nmemory_mb = 8192\n'))
    seen = {}

    async def create(config):
        task = config.tasks[0].path
        seen["toml"] = TaskConfig.model_validate_toml((task / "task.toml").read_text())
        seen["files"] = sorted(p.name for p in (task / "environment").iterdir())

        async def execute():
            return SimpleNamespace(trial_results=[SimpleNamespace(exception_info=None)])
        return SimpleNamespace(run=execute)

    monkeypatch.setattr(Job, "create", create)
    (tmp_path / "workspace").mkdir()
    run_filesystem_agent(agent={"name": "codex", "model_name": "openai/fixture"}, image=None,
                         environment=environment, instruction="Fixture", readonly={},
                         workspace=tmp_path / "workspace", evidence=tmp_path / "evidence",
                         jobs_root=tmp_path / "jobs")
    config = seen["toml"].environment
    assert (config.cpus, config.memory_mb, config.docker_image) == (2, 8192, None)
    assert config.workdir == "/workspace"
    assert seen["files"] == ["Dockerfile", "isolation.json"]


def test_policies_reject_unknown_values():
    with pytest.raises(ValueError):
        Memory(history=0)
    with pytest.raises(ValueError):
        Memory(conversations=-1)
    with pytest.raises(ValueError):
        Environment(workspace="wipe")
