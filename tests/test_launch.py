"""Fixture evidence: tasks, run settings and the researcher loop with a fake agent. No model calls."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

import autoresearch_infrastructure as ari
from autoresearch_infrastructure import launch
from autoresearch_infrastructure.persistence import write_json

pytest.importorskip("harbor")

IMAGE = "sha256:" + "a" * 64
TASK = '''
import json
from pathlib import Path

import autoresearch_infrastructure as ari

ERROR = ari.Metric("error", "min")


def validity(candidate, context):
    value = json.loads((candidate / "value.json").read_text())
    return ari.Validity() if isinstance(value, int) else ari.Validity("invalid", ("Use an integer",))


def evaluate(candidate, context):
    return ari.Evaluation({ERROR: abs(10 - json.loads((candidate / "value.json").read_text()))})


def task(run):
    return ari.Task(
        seed=Path(__file__).parent / "seed", objective=ERROR, evaluator=evaluate, validity=validity,
        instructions=ari.Instructions(
            objective="Move value.json toward ten.", rules=["value.json holds one integer."],
        ),
        context={"target.json": {"target": 10}, "note.txt": "Read-only note"},
    )
'''
AGENT = 'name = "codex"\nmodel_name = "openai/fixture"\n'


@pytest.fixture
def example(tmp_path):
    directory = tmp_path / "task"
    (directory / "seed").mkdir(parents=True)
    (directory / "seed/value.json").write_text("0")
    (directory / "task.py").write_text(TASK)

    def settings(text):
        (directory / "run.toml").write_text(text)
        return directory / "run.toml"

    return SimpleNamespace(directory=directory, settings=settings, output=tmp_path / "run")


def fake_researcher(monkeypatch, values, *, fail_on=()):
    """Each start writes the next value and submits it, as a Harbor researcher would."""
    prompts = []

    class FixtureAgent:
        def __init__(self, *, agent, image):
            assert agent["name"] == "codex" and image == IMAGE

        def run(self, prompt, *, workspace, evidence, readonly, recorder, jobs_root):
            prompts.append(prompt)
            assert sorted(readonly.values()) == ["/context", "/history"]
            context = next(path for path, target in readonly.items() if target == "/context")
            assert json.loads((context / "target.json").read_text()) == {"target": 10}
            if len(prompts) in fail_on:
                raise RuntimeError("fixture researcher failure")
            (workspace / "candidate/value.json").write_text(json.dumps(values[len(prompts) - 1]))
            recorder.dispatch_record_candidate("research", {
                "candidate": "workspace", "proposal": {"hypothesis": "Closer", "mechanism": "New value"},
            })

    monkeypatch.setattr("autoresearch_infrastructure.coding_agent.CodingAgent", FixtureAgent)
    return prompts


def test_check_freezes_the_plan_and_starts_nothing(example):
    settings = example.settings(
        f"reviewer = false\n[limits]\ncandidates = 3\nevaluations = 3\n[researcher]\n{AGENT}"
    )
    run = launch.open_run(example.directory, settings, example.output, IMAGE)
    with run.research:
        summary = launch.check_run(run)
    assert summary["status"] == "checked" and summary["candidates"] == []
    assert summary["starts"] == {"researcher": 0}
    assert summary["review"] == "the task's validity function"
    plan = json.loads((example.output / "plan.json").read_text())
    assert plan["settings"]["researcher"]["override_timeout_sec"] == 600
    assert "Move value.json toward ten." in plan["instructions"]
    assert "- Starting files: `value.json`." in plan["instructions"]
    assert "- Score: `error`. Lower is better." in plan["instructions"]
    assert (example.output / "context/note.txt").read_text() == "Read-only note"
    ledger = (example.output / "starts.json").read_bytes()
    launch.open_run(example.directory, settings, example.output, IMAGE).research.close()
    assert (example.output / "starts.json").read_bytes() == ledger


@pytest.mark.parametrize("change", ["settings", "prompt", "image"])
def test_a_changed_task_settings_or_image_needs_a_fresh_output(example, change):
    settings = example.settings(f"reviewer = false\n[limits]\ncandidates = 2\n[researcher]\n{AGENT}")
    launch.open_run(example.directory, settings, example.output, IMAGE).research.close()
    plan = (example.output / "plan.json").read_bytes()
    image = IMAGE
    if change == "settings":
        example.settings(f"reviewer = false\n[limits]\ncandidates = 3\n[researcher]\n{AGENT}")
    elif change == "prompt":
        task = example.directory / "task.py"
        task.write_text(task.read_text().replace("toward ten", "toward eleven"))
    else:
        image = "sha256:" + "b" * 64
    with pytest.raises(ValueError, match="plan changed"):
        launch.open_run(example.directory, settings, example.output, image)
    assert (example.output / "plan.json").read_bytes() == plan
    assert json.loads((example.output / "starts.json").read_text())["counts"] == {"researcher": 0}
    assert sorted(path.name for path in example.output.iterdir() if path.name.startswith(".context")) == []


@pytest.mark.parametrize("text, reason", [
    ("[researcher", "Expected"),
    ("[agents]\n", "accept only"),
    ("reviewer = false\n[limits]\nmax_rounds = 3\n", "\\[limits\\] accepts only"),
    ("reviewer = false\n[limits]\ncandidates = 0\n", "positive integers"),
    ("", "No reviewer is configured"),
    ('[researcher]\nname = "nop"\nmodel_name = "fixture"\n', "MCP servers and ATIF"),
    (f'[reviewer]\n{AGENT}[reviewer.kwargs]\nunknown_option = true\n', "Unknown"),
])
def test_invalid_settings_fail_before_anything_is_created(example, text, reason):
    with pytest.raises(ValueError, match=reason):
        launch.open_run(example.directory, example.settings(text), example.output, IMAGE)
    assert not example.output.exists()


def test_agents_need_an_image_and_review_must_be_turned_off_explicitly(example):
    settings = example.settings(f"[researcher]\n{AGENT}")
    with pytest.raises(ValueError, match="--image"):
        launch.open_run(example.directory, settings, example.output, None)
    with pytest.raises(ValueError, match="reviewer = false"):
        launch.open_run(example.directory, None, example.output, None)
    task = example.directory / "task.py"
    task.write_text(task.read_text().replace("validity=validity,", ""))
    run = launch.open_run(example.directory, example.settings("reviewer = false\n"), example.output, None)
    with run.research:
        assert launch.check_run(run)["review"] == "off"
        assert launch.record_workspace(run)["candidates"][0]["reasons"] == [
            "Review is turned off and the task has no validity function"]


def test_the_reviewer_defaults_to_the_researchers_agent(example):
    run = launch.open_run(example.directory, example.settings(f"[researcher]\n{AGENT}"), example.output, IMAGE)
    with run.research as research:
        summary = launch.check_run(run)
        assert summary["review"] == "the task's validity function, then a reviewer"
        # One review for every candidate, one researcher for every candidate after the baseline.
        assert summary["starts"] == {"researcher": 0, "reviewer": 0}
        assert run.ledger.limits == {"researcher": 9, "reviewer": 10}
        assert research.validity.agent.name == "codex"
        assert research.validity.criteria == {"rule_1": "value.json holds one integer."}


def test_record_evaluates_manual_edits_without_agents(example):
    run = launch.open_run(example.directory, example.settings("reviewer = false\n"), example.output, None)
    with run.research as research:
        assert launch.record_workspace(run)["status"] == "recorded"
        (research.workspace / "value.json").write_text("9")
        summary = launch.record_workspace(run)
        assert [row["metrics"] for row in summary["candidates"]] == [{"error": 10.0}, {"error": 1.0}]
        assert summary["selected"] == "candidate-0002" and "starts" not in summary
        (research.workspace / "value.json").write_text('"nine"')
        rejected = launch.record_workspace(run)
        assert rejected["status"] == "not_measured"
        assert rejected["candidates"][-1]["reasons"] == ["Use an integer"]
        assert rejected["budget"]["evaluations"]["used"] == 2
        with pytest.raises(ValueError, match=r"needs a \[researcher\]"):
            launch.run_researchers(run)
    assert json.loads((example.output / "summary.json").read_text()) == rejected
    assert Path(rejected["viewer"]).read_text().startswith("<!")


def test_researchers_propose_from_the_selected_candidate_until_starts_run_out(example, monkeypatch):
    prompts = fake_researcher(monkeypatch, [4, 2, 9])
    settings = example.settings(
        f"reviewer = false\n[limits]\ncandidates = 4\nevaluations = 5\n[researcher]\n{AGENT}"
    )
    run = launch.open_run(example.directory, settings, example.output, IMAGE)
    with run.research as research:
        summary = launch.run_researchers(run)
        assert summary["status"] == "completed" and summary["stopped"] == "researcher starts used"
        assert [row["metrics"]["error"] for row in summary["candidates"]] == [10, 6, 8, 1]
        # The second proposal was worse, so the third again started from the first.
        assert [item["parent_id"] for item in summary["proposals"]] == [
            "candidate-0001", "candidate-0002", "candidate-0002"]
        assert summary["selected"] == "candidate-0004" and summary["improved"] is True
        assert summary["starts"] == {"researcher": 3} and summary["total_cost_usd"] is None
        assert "Move value.json toward ten." in prompts[0] and "`candidate-0002`" in prompts[2]
        assert "mcp_proxy.py --call /tmp/proposal.json" in prompts[0]
        assert json.loads((research.workspace / "value.json").read_text()) == 9
        # Running again spends nothing more.
        again = launch.run_researchers(run)
        assert again["starts"] == {"researcher": 3} and len(prompts) == 3 and again["proposals"] == []


def test_a_failed_researcher_is_charged_and_the_next_one_continues(example, monkeypatch):
    prompts = fake_researcher(monkeypatch, [None, 7], fail_on={1})
    settings = example.settings(f"reviewer = false\n[limits]\ncandidates = 3\n[researcher]\n{AGENT}")
    run = launch.open_run(example.directory, settings, example.output, IMAGE)
    with run.research:
        summary = launch.run_researchers(run)
    assert [item["status"] for item in summary["proposals"]] == ["failed", "completed"]
    assert summary["proposals"][0]["error"] == "RuntimeError: fixture researcher failure"
    assert summary["proposals"][0]["submitted"] is False
    assert summary["starts"] == {"researcher": 2} and len(prompts) == 2
    assert summary["selected"] == "candidate-0002"
    failure = Path(summary["proposals"][0]["evidence"]) / "failure.json"
    assert json.loads(failure.read_text())["type"] == "RuntimeError"


def test_the_loop_stops_before_a_candidate_it_cannot_evaluate(example, monkeypatch):
    prompts = fake_researcher(monkeypatch, [5, 6, 7])
    settings = example.settings(
        f"reviewer = false\n[limits]\ncandidates = 4\nevaluations = 2\n[researcher]\n{AGENT}"
    )
    run = launch.open_run(example.directory, settings, example.output, IMAGE)
    with run.research:
        summary = launch.run_researchers(run)
    assert summary["stopped"] == "too few evaluations left for another candidate"
    assert len(prompts) == 1 and summary["starts"] == {"researcher": 1}


def test_an_invalid_baseline_starts_no_researcher(example, monkeypatch):
    prompts = fake_researcher(monkeypatch, [5])
    (example.directory / "seed/value.json").write_text('"zero"')
    settings = example.settings(f"reviewer = false\n[limits]\ncandidates = 2\n[researcher]\n{AGENT}")
    run = launch.open_run(example.directory, settings, example.output, IMAGE)
    with run.research:
        summary = launch.run_researchers(run)
    assert summary["status"] == "failed" and "no researcher was started" in summary["error"]
    assert prompts == [] and summary["starts"] == {"researcher": 0}


def test_the_reviewer_sees_only_candidates_that_pass_the_validity_function(example, monkeypatch):
    reviews, contexts, instructions = [], [], []

    def review(**kwargs):
        assignment = json.loads(next(
            path for path, target in kwargs["readonly"].items() if target == "/review/assignment.json"
        ).read_text())
        reviews.append(assignment["required_criteria"])
        contexts.append(assignment["context"])
        instructions.append(kwargs["instruction"])
        write_json(kwargs["workspace"] / "validity/verdict.json", {
            "candidate_hash": assignment["candidate_hash"], "contract_hash": assignment["contract_hash"],
            "verdict": "valid", "reasons": ["Fixture review"],
            "checks": [{"criterion": "rule_1", "status": "pass", "evidence_paths": ["/review/candidate/value.json"]}],
        })
        return SimpleNamespace(agent_result=SimpleNamespace(cost_usd=0.25), exception_info=None)

    monkeypatch.setattr("autoresearch_infrastructure.review.run_filesystem_agent", review)
    settings = example.settings(f"[limits]\ncandidates = 2\n[reviewer]\n{AGENT}")
    run = launch.open_run(example.directory, settings, example.output, IMAGE)
    with run.research as research:
        assert isinstance(research.validity, ari.LLMValidity)
        first = launch.record_workspace(run)
        assert first["review"] == "the task's validity function, then a reviewer" and first["starts"] == {"reviewer": 1}
        assert first["observed_cost_usd"] == first["total_cost_usd"] == 0.25
        (research.workspace / "value.json").write_text('"nine"')
        second = launch.record_workspace(run)
    # The reviewer is given exactly the text the researcher reads, and nothing else.
    plan = json.loads((example.output / "plan.json").read_text())
    assert contexts == [{"task_specification": plan["instructions"]}]
    assert "- Score: `error`. Lower is better." in contexts[0]["task_specification"]
    assert "mcp_proxy" not in json.dumps(contexts[0]) and "target.json" not in json.dumps(contexts[0])
    assert "It adds no criteria" in instructions[0] and "Submit one candidate" not in instructions[0]
    assert second["candidates"][-1]["reasons"] == ["Use an integer"]
    assert second["starts"] == {"reviewer": 1} and reviews[0] == {"rule_1": "value.json holds one integer."}


def test_task_files_must_define_task(tmp_path):
    (tmp_path / "task.py").write_text("TASK = None\n")
    with pytest.raises(ValueError, match="must define task"):
        launch.open_run(tmp_path, None, tmp_path / "run", None)
    with pytest.raises(ValueError, match="No task file"):
        launch.open_run(tmp_path / "missing", None, tmp_path / "run", None)


def test_review_needs_rules_and_misplaced_sections_are_refused(example):
    task = example.directory / "task.py"
    original = task.read_text()
    task.write_text(original.replace('rules=["value.json holds one integer."],', ""))
    with pytest.raises(ValueError, match="lists? no rules"):
        launch.open_run(example.directory, example.settings(f"[researcher]\n{AGENT}"), example.output, IMAGE)
    with pytest.raises(ValueError, match="Unexpected heading"):
        ari.Instructions.parse("## Objective\nImprove it.\n\n## Advice\nTry harder.\n")
    with pytest.raises(ValueError, match="bullet list"):
        ari.Instructions.parse("## Objective\nImprove it.\n\n## Constraints\n### Rules\nBe good.\n")
    parsed = ari.Instructions.parse(
        "# Title\n\n## Objective\nImprove it.\n### Interface\nOne file.\n\n"
        "## Constraints\n### Rules\n- First rule\n  continued.\n- Second.\n### Environment\n- No network.\n"
    )
    assert parsed.title == "Title" and parsed.rules == ("First rule continued.", "Second.")
    assert parsed.environment == ("No network.",) and "### Interface" in parsed.objective


@pytest.mark.parametrize("edit", [
    ('objective="Move value.json toward ten."', 'objective="Move value.json toward ten, as close as you can."'),
    ('rules=["value.json holds one integer."],', 'rules=["value.json holds one integer."], environment=["The target is fixed."],'),
])
def test_an_objective_or_environment_edit_reaches_the_reviewer_and_changes_its_identity(example, edit):
    settings = example.settings(f"[limits]\ncandidates = 2\n[reviewer]\n{AGENT}")
    first = launch.open_run(example.directory, settings, example.output, IMAGE)
    before = first.research.validity
    first.research.close()
    task = example.directory / "task.py"
    task.write_text(task.read_text().replace(*edit))
    second = launch.open_run(example.directory, settings, example.output.with_name("edited"), IMAGE)
    after = second.research.validity
    second.research.close()
    assert before.criteria == after.criteria  # the Rules list did not change
    assert before.context != after.context and edit[1].split('"')[1][:20] in json.dumps(after.context)
    assert before.identity != after.identity
    assert first.research.contract_hash != second.research.contract_hash
