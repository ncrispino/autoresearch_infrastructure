"""Fixture evidence: researcher inputs, presentation, memory views and environments. No model calls."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from autoresearch_infrastructure import launch
from autoresearch_infrastructure.inputs import Environment, Memory, ResearcherInputs, Start, present
from autoresearch_infrastructure.instructions import Instructions
from autoresearch_infrastructure.search import _ONE_SUBMISSION as ONE_SUBMISSION
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

        def run(self, prompt, *, workspace, evidence, readonly, recorder, jobs_root):
            history = next(path for path, target in readonly.items() if target == "/history")
            files = sorted(p.relative_to(history).as_posix() for p in Path(history).rglob("*") if p.is_file())
            seen.append(SimpleNamespace(
                prompt=prompt, files=files, index=json.loads((Path(history) / "index.json").read_text()),
                workspace=sorted(p.relative_to(workspace).as_posix() for p in workspace.rglob("*")),
                image=self.image, environment=self.environment, mounts=sorted(readonly.values()),
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
                                         "conversation_mode": "files", "log": False, "summarize": None},
                              "environment": {"harbor": None, "workspace": "keep", "candidate_at": None},
                              "presenter": "default",
                              "transforms": [], "original_instructions": None,
                              "rule_names": ["rule_1"], "environment_names": [], "instruction_split": None,
                              "researcher_timeout": None}
    assert plan["reviewer_image"] == IMAGE and plan["review_environment"] is None
    standard = plan["instructions"].rstrip() + "\n\n" + RESEARCHER_PROTOCOL.substitute(
        parent_id="candidate-0001", repeats=1, memory_rows="",
        parent_evidence="Read its evidence at `/history/candidates/candidate-0001/` before editing.",
        after_recording=ONE_SUBMISSION,
        context_row="| `/context/` | Read-only task material. |\n", readonly="`/context` and `/history` are",
        candidate_row="| `/workspace/candidate/` | Your editable solution, copied from `candidate-0001`. Only this "
                      "directory is submitted. |\n")
    assert "Stop\n  there, even if it is invalid" in standard and "$" not in standard
    assert seen[0].mounts == ["/context", "/history"]  # this task passes a context file
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


def session_agent(monkeypatch, rounds, *, crash_after=None):
    """One session keeps submitting, as an agent loop would, until the reply's `next` is null."""
    sessions = []

    class SessionAgent:
        def __init__(self, *, agent, image=None, environment=None):
            pass

        def run(self, prompt, *, workspace, evidence, readonly, recorder, jobs_root):
            history = Path(next(path for path, target in readonly.items() if target == "/history"))
            session = {"prompt": prompt, "replies": [], "starting_values": [], "history": []}
            sessions.append(session)
            evidence.mkdir(parents=True)
            (evidence / "trajectory.json").write_text(json.dumps({"session": len(sessions)}))
            for value in rounds[len(sessions) - 1]:
                session["starting_values"].append(json.loads((workspace / "candidate/value.json").read_text()))
                session["history"].append([c["id"] for c in json.loads(
                    (history / "index.json").read_text())["candidates"]])
                (workspace / "candidate/value.json").write_text(json.dumps(value))
                reply = recorder.dispatch_record_candidate("research", {
                    "candidate": "workspace", "proposal": {"hypothesis": f"Try {value}", "mechanism": "Next"},
                })
                session["replies"].append(reply)
                if crash_after == len(session["replies"]):
                    raise RuntimeError("fixture crash mid-session")
                if reply["next"] is None:
                    break

    monkeypatch.setattr("autoresearch_infrastructure.coding_agent.CodingAgent", SessionAgent)
    return sessions


def limits(candidates, evaluations=None):
    return (f"reviewer = false\n[limits]\ncandidates = {candidates}\nevaluations = {evaluations or candidates}\n"
            f"[researcher]\n{AGENT}")


def test_one_session_makes_k_proposals_then_a_fresh_session_begins(example, monkeypatch):
    # Values: 4 is selected, 2 is worse so the workspace returns to 4, then 6 is selected.
    sessions = session_agent(monkeypatch, [[4, 2, 6, 99], [8, 9, 99]])
    summary, opened = run(example.make('memory=ari.Memory(conversations=3, conversation_mode="continue"),',
                                       settings=limits(6)), example.output)
    assert summary["status"] == "completed" and len(sessions) == 2
    first, second = sessions
    assert "you may record up to 3 candidates" in first["prompt"] and "Read `next`" in first["prompt"]
    assert first["starting_values"] == [0, 4, 4]  # reset to the selected candidate, not the last submission
    assert [r["next"] and r["next"]["parent_id"] for r in first["replies"]] == [
        "candidate-0002", "candidate-0002", None]
    assert first["replies"][0]["next"]["remaining_in_this_session"] == 2
    assert first["replies"][0]["next"]["note"].startswith("This session continues.")
    assert first["replies"][2]["stopped"] == "this session's candidates are used"
    # /history is live: the session sees its own earlier candidates after each submission.
    assert first["history"][2] == ["candidate-0001", "candidate-0002", "candidate-0003"]
    # The second session is fresh, starts from the best candidate, and stops when the budget does.
    assert second["starting_values"] == [6, 8] and second["replies"][1]["next"] is None
    assert second["replies"][1]["stopped"] == "candidate limit reached"
    assert summary["starts"] == {"researcher": 2} and len(summary["candidates"]) == 6
    assert summary["proposals"][0]["candidates"] == ["candidate-0002", "candidate-0003", "candidate-0004"]
    records = inputs_records(example.output)
    assert [s["next_parent_id"] for s in records[0]["submissions"]] == ["candidate-0002", "candidate-0002", None]
    with pytest.raises(ValueError, match="2 or more"):
        Memory(conversations=1, conversation_mode="continue")


def test_all_lets_one_session_use_the_whole_budget_and_a_filtered_history_is_refreshed(example, monkeypatch):
    sessions = session_agent(monkeypatch, [[4, 6, 8, 99]])
    summary, opened = run(example.make(
        'memory=ari.Memory(history=1, log=True, conversations="all", conversation_mode="continue"),',
        settings=limits(4)), example.output)
    assert len(sessions) == 1 and summary["starts"] == {"researcher": 1}
    assert "as many candidates as the run's budget allows" in sessions[0]["prompt"]
    assert sessions[0]["history"] == [["candidate-0001"], ["candidate-0002"], ["candidate-0003"]]
    assert sessions[0]["replies"][-1]["stopped"] == "candidate limit reached"


def test_a_session_that_crashes_keeps_its_candidates_and_the_next_start_is_fresh(example, monkeypatch):
    session_agent(monkeypatch, [[4, 5], [6, 7, 99]], crash_after=2)
    summary, opened = run(example.make('memory=ari.Memory(conversations=3, conversation_mode="continue"),',
                                       settings=limits(5)), example.output)
    assert summary["status"] == "completed" and summary["proposals"][0]["status"] == "failed"
    assert summary["proposals"][0]["candidates"] == ["candidate-0002", "candidate-0003"]
    assert len(summary["candidates"]) == 5 and summary["starts"] == {"researcher": 2}


def test_a_session_cannot_submit_after_it_is_told_to_stop(example, monkeypatch):
    from autoresearch_infrastructure.search import SubmissionRecorder

    class Research:
        def dispatch_record_candidate(self, session, arguments):
            return {"candidate_id": "candidate-0002"}

    def stop():
        raise StopIteration("candidate limit reached")

    recorder = SubmissionRecorder(Research(), "candidate-0001", 1, limit=None, advance=stop)
    arguments = {"candidate": "workspace", "proposal": {"hypothesis": "h", "mechanism": "m"}}
    assert recorder.dispatch_record_candidate("research", arguments)["next"] is None
    with pytest.raises(ValueError, match="no more candidates"):
        recorder.dispatch_record_candidate("research", arguments)


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


def fake_docker(monkeypatch):
    """Docker as the preparation step sees it: builds return a fixed image ID."""
    from autoresearch_infrastructure import harbor_environment

    calls, images = [], set()

    def docker(args):
        calls.append(args)
        if args[0] == "build":
            Path(args[args.index("--iidfile") + 1]).write_text(BUILT)
            images.add(BUILT)
            return ""
        if args[:2] == ["image", "inspect"]:
            if args[-1] not in images:
                raise RuntimeError("no such image")
            return args[-1]
        raise AssertionError(args)

    monkeypatch.setattr(harbor_environment, "_docker", docker)
    return calls, images


BUILT = "sha256:" + "c" * 64


def test_a_harbor_environment_replaces_the_researcher_image_and_is_frozen(example, monkeypatch):
    from autoresearch_infrastructure import harbor_environment

    calls, _ = fake_docker(monkeypatch)
    environment = harbor_task(example.root, dockerfile=f"FROM {PINNED} AS base\nFROM base\nRUN true\n",
                              compose=f"services:\n  db:\n    image: {PINNED}\n")
    assert harbor_environment.load(environment).config["cpus"] == 2
    seen = fixture_agent(monkeypatch)
    directory = example.make('environment=ari.Environment(harbor="../env-task"),')
    checked = launch.open_run(directory, directory / "run.toml", example.output, None)
    with checked.research:
        assert launch.check_run(checked)["environments"] == {"researcher": "not prepared"}
    assert calls == []  # opening and checking build nothing
    summary, opened = run(directory, example.output, image=None)
    assert summary["status"] == "completed"
    assert [c[0] for c in calls].count("build") == 1  # prepared once, reused by every start
    prepared = json.loads((example.output / "prepared/environment.json").read_text())
    assert prepared["main"] == BUILT and prepared["built"] is True
    assert prepared["sidecars"] == {"db": PINNED}
    assert summary["environments"]["researcher"]["main"] == BUILT
    assert seen[0].image is None and seen[0].environment.files == example.output / "environment"
    assert sorted(p.name for p in (example.output / "environment").iterdir()) == ["Dockerfile",
                                                                                  "docker-compose.yaml"]
    plan = json.loads((example.output / "plan.json").read_text())
    assert plan["inputs"]["environment"]["harbor"]
    assert inputs_records(example.output)[0]["environment"]["main"] == BUILT
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

    fake_docker(monkeypatch)
    environment = harbor_environment.load(harbor_task(tmp_path, table='cpus = 2\nmemory_mb = 8192\n'))
    environment = harbor_environment.HarborEnvironment(environment.config, environment.files,
                                                       tmp_path / "prepared.json")
    seen = {}

    async def create(config):
        task = config.tasks[0].path
        seen["toml"] = TaskConfig.model_validate_toml((task / "task.toml").read_text())
        seen["files"] = sorted(p.name for p in (task / "environment").iterdir())
        seen["environment"] = config.environment

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
    # The prepared image runs; the Dockerfile is not rebuilt by Harbor.
    assert (config.cpus, config.memory_mb, config.docker_image) == (2, 8192, BUILT)
    assert config.workdir == "/workspace"
    assert seen["files"] == ["isolation.json"]
    assert seen["environment"].type == "docker" and seen["environment"].delete is True


def test_policies_reject_unknown_values():
    with pytest.raises(ValueError):
        Memory(history=0)
    with pytest.raises(ValueError):
        Memory(conversations=-1)
    with pytest.raises(ValueError):
        Environment(workspace="wipe")


def test_preparation_is_reused_and_a_missing_prepared_image_is_refused(tmp_path, monkeypatch):
    from autoresearch_infrastructure import harbor_environment

    calls, images = fake_docker(monkeypatch)
    loaded = harbor_environment.load(harbor_task(tmp_path))
    environment = harbor_environment.HarborEnvironment(loaded.config, loaded.files, tmp_path / "prepared.json")
    first = harbor_environment.prepare(environment)
    assert harbor_environment.prepare(environment) == first
    assert [c[0] for c in calls].count("build") == 1
    images.clear()
    with pytest.raises(ValueError, match="no longer available"):
        harbor_environment.prepare(environment)
    prebuilt = harbor_environment.load(harbor_task(tmp_path / "pre", dockerfile=None,
                                                  table=f'docker_image = "{PINNED}"\n'))
    assert harbor_environment.prepare(prebuilt) == {"definition": prebuilt.identity, "main": PINNED,
                                                    "built": False, "sidecars": {}}


@pytest.mark.parametrize("compose, message", [
    ("services:\n  main:\n    privileged: true\n", "main service"),
    ("services:\n  main:\n    volumes: ['/:/host']\n", "main service"),
    (f"services:\n  db:\n    image: {PINNED}\n    privileged: true\n", "may not set"),
    (f"services:\n  db:\n    image: {PINNED}\n    volumes: ['/var/run/docker.sock:/s']\n", "bind host paths"),
    (f"services:\n  db:\n    image: {PINNED}\n    network_mode: host\n", "host network"),
    ("services:\n  db:\n    build: .\n", "may not set"),
])
def test_compose_cannot_loosen_isolation_or_hide_an_image(tmp_path, compose, message):
    from autoresearch_infrastructure import harbor_environment

    with pytest.raises(ValueError, match=message):
        harbor_environment.load(harbor_task(tmp_path, compose=compose))


def test_providers_other_than_local_docker_are_refused(tmp_path):
    from autoresearch_infrastructure import harbor_environment

    with pytest.raises(ValueError, match="local Docker"):
        harbor_environment.load(harbor_task(tmp_path, table='[environment.tpu]\ntype = "v5e"\ntopology = "2x2"\n'))


def reviewed(example, extra="", reviewer_image=None, image=IMAGE):
    directory = example.make(extra, settings=f"[limits]\ncandidates = 3\n[researcher]\n{AGENT}")
    return launch.open_run(directory, directory / "run.toml", example.output, image, reviewer_image=reviewer_image)


def test_the_reviewer_can_have_its_own_image_or_harbor_environment(example, monkeypatch):
    other = "sha256:" + "d" * 64
    opened = reviewed(example, reviewer_image=other)
    with opened.research:
        assert opened.research.validity.image == other and opened.image == IMAGE
    plan = json.loads((example.output / "plan.json").read_text())
    assert (plan["image"], plan["reviewer_image"]) == (IMAGE, other)

    harbor_task(example.root)
    output = example.root / "second"
    directory = example.make('review_environment="../env-task",',
                             settings=f"[limits]\ncandidates = 3\n[researcher]\n{AGENT}")
    opened = launch.open_run(directory, directory / "run.toml", output, IMAGE)
    with opened.research:
        validity = opened.research.validity
        assert validity.image is None and validity.environment.files == output / "review-environment"
        assert validity.identity["environment"] == validity.environment.identity
    assert json.loads((output / "plan.json").read_text())["review_environment"]


def test_retained_files_are_exported_sealed_and_restored(example, monkeypatch):
    seen = fixture_agent(monkeypatch)
    run(example.make(), example.output)
    assert seen[2].workspace.count("notes-1.txt") == 1 and "notes-2.txt" in seen[2].workspace
    starts = json.loads((example.output / "research/sessions/research/agent/researcher-starts.json").read_text())
    assert all(start["retained"]["sha256"] for start in starts["starts"])
    assert inputs_records(example.output)[1]["restored"]["path"] == starts["starts"][0]["retained"]["path"]


def test_a_changed_export_is_refused_before_the_next_start(tmp_path):
    from autoresearch_infrastructure import search
    from autoresearch_infrastructure.persistence import write_json

    research = SimpleNamespace(root=tmp_path)
    (tmp_path / "workspace/notes").mkdir(parents=True)
    (tmp_path / "workspace/notes/a.txt").write_text("kept")
    (tmp_path / "workspace/link").symlink_to("/etc/passwd")
    exported = search._export(research, "first")
    assert exported["dropped"] == ["link"] and (tmp_path / exported["path"] / "notes/a.txt").is_file()
    write_json(tmp_path / "agent/researcher-starts.json", {"starts": [{"retained": exported}]})
    (tmp_path / exported["path"] / "notes/a.txt").write_text("edited between starts")
    with pytest.raises(ValueError, match="changed after they were exported"):
        search._restore(research)


def test_a_research_log_and_summary_are_written_from_the_records_shown(example, monkeypatch):
    seen = fixture_agent(monkeypatch)
    (example.root / "summaries.py").write_text(
        "def summarize(index):\n    return f\"{len(index['candidates'])} candidate(s) so far\"\n")
    directory = example.make("memory=ari.Memory(history=2, log=True, summarize=summarize),")
    task = directory / "task.py"
    task.write_text("import sys; sys.path.insert(0, str(__import__('pathlib').Path(__file__).parent.parent))\n"
                    "from summaries import summarize\n" + task.read_text())
    run(directory, example.output)
    assert {"log.md", "summary.md"} <= set(seen[2].files)
    assert "`/history/log.md`" in seen[2].prompt and "`/history/summary.md`" in seen[2].prompt
    records = inputs_records(example.output)
    assert records[2]["summary_sha256"]
    plan = json.loads((example.output / "plan.json").read_text())
    assert plan["inputs"]["memory"]["log"] is True and plan["inputs"]["memory"]["summarize"]


def test_research_log_lists_ideas_verdicts_and_scores():
    from autoresearch_infrastructure.memory import _log

    text = _log({"selected_candidate": "candidate-0002", "candidates": [
        {"id": "candidate-0001", "parent_id": None, "proposal": {"hypothesis": "Unchanged"},
         "validity": {"verdict": "valid", "reasons": []}, "evaluation": {"score": 10, "metrics": {"e": 10}}},
        {"id": "candidate-0002", "parent_id": "candidate-0001", "proposal": {"hypothesis": "Try 4", "mechanism": "m"},
         "validity": {"verdict": "invalid", "reasons": ["Use an integer"]}, "evaluation": {}},
    ]})
    assert "## candidate-0002 (from candidate-0001)" in text and "Try 4" in text
    assert "invalid (Use an integer)" in text and "Selected so far: candidate-0002." in text


def test_transformations_change_what_both_agents_read_and_keep_the_original(example):
    opened = reviewed(example, extra='transforms=[ari.transforms.reword_rule(1, "Hold any number."), '
                                     'ari.transforms.add_rule("Write a comment."), ari.transforms.omit_measured()],')
    with opened.research:
        assert opened.research.validity.criteria == {"rule_1": "Hold any number.", "rule_2": "Write a comment."}
        assert "What is measured" not in opened.inputs.specification()
    plan = json.loads((example.output / "plan.json").read_text())
    assert "Hold one integer." in plan["inputs"]["original_instructions"]
    assert "Hold any number." in plan["instructions"]
    assert len(plan["inputs"]["transforms"]) == 3


def test_transformation_helpers_check_their_arguments():
    from autoresearch_infrastructure import transforms

    inputs = ResearcherInputs.resolve(Instructions("Goal.", ("A", "B")), ["m"])
    assert transforms.omit_rule(1)(inputs).rules == ("B",)
    assert transforms.replace_goal("New goal.")(inputs).goal == "New goal."
    assert transforms.add_environment_note("No network.")(inputs).environment_notes == ("No network.",)
    with pytest.raises(ValueError, match="no rule 3"):
        transforms.omit_rule(3)(inputs)
    assert transforms.omit_rule(2).identity == {"transform": "omit_rule", "rule": 2}


def test_an_unlimited_session_is_told_it_continues_without_a_null_count():
    from autoresearch_infrastructure.search import SubmissionRecorder

    class Research:
        def dispatch_record_candidate(self, session_id, arguments):
            return {"candidate_id": "candidate-0002", "status": "valid"}

    recorder = SubmissionRecorder(Research(), "candidate-0001", 1, limit=None, advance=lambda: "candidate-0002")
    reply = recorder.dispatch_record_candidate("research", {
        "candidate": "workspace", "proposal": {"hypothesis": "h", "mechanism": "m"}})
    assert reply["next"]["parent_id"] == "candidate-0002"
    assert "remaining_in_this_session" not in reply["next"] and None not in reply["next"].values()
    assert reply["next"]["note"].startswith("This session continues.")
    assert "/workspace/candidate/ now holds" in reply["next"]["note"]
    elsewhere = SubmissionRecorder(Research(), "candidate-0001", 1, limit=None, advance=lambda: "candidate-0002",
                                   candidate="/app/output")
    reply = elsewhere.dispatch_record_candidate("research", {
        "candidate": "workspace", "proposal": {"hypothesis": "h", "mechanism": "m"}})
    assert "/app/output/ now holds" in reply["next"]["note"] and "/workspace" not in reply["next"]["note"]
