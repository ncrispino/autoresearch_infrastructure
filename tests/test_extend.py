"""Fixture evidence for extending a finished run into a new output (issue #2). No model calls."""

import hashlib
import json
import os
from pathlib import Path

import pytest

from autoresearch_infrastructure import cli, launch
from autoresearch_infrastructure.viewer import build_index

pytest.importorskip("harbor")

IMAGE = "sha256:" + "a" * 64
AGENT = 'name = "codex"\nmodel_name = "openai/fixture"\n'
TASK = '''
import json
from pathlib import Path

import autoresearch_infrastructure as ari

ERROR = ari.Metric("error", "min")
CALLS = Path(__file__).parent / "evaluations.log"


def evaluate(candidate, context):
    value = json.loads((candidate / "value.json").read_text())
    with CALLS.open("a") as log:
        log.write(f"{value}\\n")
    return ari.Evaluation({ERROR: abs(10 - value)})


def task(run):
    return ari.Task(
        seed=Path(__file__).parent / "seed", objective=ERROR, evaluator=evaluate,
        instructions=ari.Instructions(objective="Move value.json toward ten.", rules=["Hold one integer."]),
    )
'''


def settings(directory, candidates, evaluations=None, name="run.toml"):
    path = directory / name
    path.write_text(f"reviewer = false\n[limits]\ncandidates = {candidates}\n"
                    f"evaluations = {evaluations or candidates}\n[researcher]\n{AGENT}")
    return path


def fixture_agent(monkeypatch, values, *, fail_on=()):
    seen = []

    class FixtureAgent:
        def __init__(self, *, agent, image=None, environment=None):
            pass

        def run(self, prompt, *, workspace, evidence, readonly, recorder, jobs_root, load_trajectory=None):
            history = next(path for path, target in readonly.items() if target == "/history")
            seen.append({"prompt": prompt, "start": json.loads((workspace / "candidate/value.json").read_text()),
                         "index": json.loads((Path(history) / "index.json").read_text())})
            if len(seen) in fail_on:
                raise RuntimeError("fixture failure")
            (workspace / "candidate/value.json").write_text(json.dumps(values[len(seen) - 1]))
            recorder.dispatch_record_candidate("research", {
                "candidate": "workspace", "proposal": {"hypothesis": f"Try {values[len(seen) - 1]}",
                                                       "mechanism": "Fixture"},
            })

    monkeypatch.setattr("autoresearch_infrastructure.coding_agent.CodingAgent", FixtureAgent)
    return seen


def tree_hash(root):
    hasher = hashlib.sha256()
    for path in sorted(Path(root).rglob("*")):
        if path.is_file() and not path.name.endswith((".lock", ".owner")):
            hasher.update(path.relative_to(root).as_posix().encode() + path.read_bytes())
    return hasher.hexdigest()


def writable(path):
    for parent in [path.parent, *path.parents]:
        if parent.exists():
            os.chmod(parent, 0o755)
    os.chmod(path, 0o644)


@pytest.fixture
def finished(tmp_path, monkeypatch):
    """A finished source run: the baseline, two proposals and one failed researcher start."""
    directory = tmp_path / "task"
    (directory / "seed").mkdir(parents=True)
    (directory / "seed/value.json").write_text("0")
    (directory / "task.py").write_text(TASK)
    fixture_agent(monkeypatch, [4, 0, 7], fail_on=(2,))
    opened = launch.open_run(directory, settings(directory, 4, 3), tmp_path / "source", IMAGE)
    with opened.research:
        summary = launch.run_researchers(opened)
    assert [row["candidate_id"] for row in summary["candidates"]] == [
        "candidate-0001", "candidate-0002", "candidate-0003"]
    assert summary["starts"] == {"researcher": 3}
    return directory, tmp_path / "source", tmp_path


def evaluations(directory):
    return (directory / "evaluations.log").read_text().split()


def test_an_extension_reuses_evidence_and_counts_prior_usage(finished, monkeypatch):
    directory, source, root = finished
    before, evaluated = tree_hash(source), len(evaluations(directory))
    seen = fixture_agent(monkeypatch, [9, 10])
    opened = launch.open_run(directory, settings(directory, 6, 5, "more.toml"), root / "child", IMAGE,
                             extends=source)
    with opened.research:
        checked = launch.check_run(opened)
        assert seen == [] and len(evaluations(directory)) == evaluated  # opening is free
        assert checked["extends"]["imported_candidates"] == ["candidate-0001", "candidate-0002", "candidate-0003"]
        assert checked["budget"]["candidates"]["used"] == 3 and checked["starts"] == {"researcher": 3}
        assert checked["start_limits"] == {"researcher": 5}
        research = opened.research
        assert research.selected == "candidate-0003"
        assert research.candidates["candidate-0003"]["parent_id"] == "candidate-0002"
        assert research.candidates["candidate-0003"]["proposal"]["hypothesis"] == "Try 7"
        summary = launch.run_researchers(opened)
    assert tree_hash(source) == before
    # Only the two new candidates are evaluated; the imported baseline is not measured again.
    assert evaluations(directory)[evaluated:] == ["9", "10"]
    assert [row["candidate_id"] for row in summary["candidates"]][-2:] == ["candidate-0004", "candidate-0005"]
    assert summary["selected"] == "candidate-0005" and summary["stopped"] == "researcher starts used"
    assert seen[0]["start"] == 7  # from the imported selected candidate
    assert [c["id"] for c in seen[0]["index"]["candidates"]] == [
        "candidate-0001", "candidate-0002", "candidate-0003"]
    ledger = json.loads((root / "child/starts.json").read_text())
    assert ledger["counts"] == {"researcher": 5}
    assert sorted(start["status"] for start in ledger["starts"].values()) == [
        "completed", "completed", "completed", "completed", "failed"]
    index = build_index(root / "child/research")
    assert index["run"]["extends"]["imported_candidates"] == 3
    imported = {c["id"]: c["imported"] for c in index["sessions"][0]["candidates"]}
    assert imported["candidate-0001"] and not imported["candidate-0004"]


def test_reopening_an_extension_is_idempotent_and_needs_an_unchanged_source(finished, monkeypatch):
    directory, source, root = finished
    more = settings(directory, 6, 5, "more.toml")
    launch.open_run(directory, more, root / "child", IMAGE, extends=source).research.close()
    plan = (root / "child/plan.json").read_bytes()
    launch.open_run(directory, more, root / "child", IMAGE).research.close()  # finds its source
    assert (root / "child/plan.json").read_bytes() == plan
    starts = source / "starts.json"
    starts.write_text(starts.read_text().replace('"failed"', '"completed"'))
    with pytest.raises(ValueError, match="plan changed"):
        launch.open_run(directory, more, root / "child", IMAGE)


@pytest.mark.parametrize("tamper", ["snapshot", "result", "contract"])
def test_tampered_sources_are_rejected_and_leave_no_child(finished, tamper):
    directory, source, root = finished
    candidate = source / "research/sessions/research/history/candidates/candidate-0002"
    if tamper == "snapshot":
        path = candidate / "snapshot/value.json"
    elif tamper == "result":
        path = next(candidate.glob("evaluations/*/cells/**/trial-start-1/result.json"))
    else:
        path = source / "research/contract.json"
    writable(path)
    path.write_text(path.read_text().replace("4", "5") if tamper != "contract" else
                    path.read_text().replace('"python"', '"python "'))
    with pytest.raises(ValueError):
        launch.open_run(directory, settings(directory, 6, 5, "more.toml"), root / "child", IMAGE, extends=source)
    assert not (root / "child").exists()


def test_an_evaluation_change_is_refused_with_the_field_that_changed(finished):
    directory, source, root = finished
    task_file = directory / "task.py"
    task_file.write_text(task_file.read_text().replace("abs(10 - value)", "abs(11 - value)"))
    with pytest.raises(ValueError, match="evaluation contract changed in task_file"):
        launch.open_run(directory, settings(directory, 6, 5, "more.toml"), root / "child", IMAGE, extends=source)
    assert not (root / "child").exists()


def test_limits_may_not_shrink_below_the_source(finished):
    directory, source, root = finished
    with pytest.raises(ValueError, match="cannot lower max_evaluations"):
        launch.open_run(directory, settings(directory, 6, 2, "fewer.toml"), root / "child", IMAGE,
                        extends=source)


def test_the_command_reports_lineage_and_starts_nothing(finished, capsys):
    directory, source, root = finished
    code = cli.main(["extend", str(directory), "--from", str(source), "--output", str(root / "child"),
                     "--config", str(settings(directory, 6, 5, "more.toml")), "--image", IMAGE, "--json"])
    summary = json.loads(capsys.readouterr().out)
    assert code == 0 and summary["status"] == "checked"
    assert summary["extends"]["source"] == str(source.resolve())
    budget = summary["budget"]["candidates"]
    assert (budget["used"], budget["limit"], budget["remaining"]) == (3, 6, 3)


def test_a_source_with_an_interrupted_evaluation_is_refused(finished):
    directory, source, root = finished
    state_path = source / "research/state.json"
    state = json.loads(state_path.read_text())
    state["reservations"]["fixture"] = {"status": "active", "remaining_cells": ["x"]}
    state_path.write_text(json.dumps(state))
    with pytest.raises(ValueError, match="interrupted evaluation"):
        launch.open_run(directory, settings(directory, 6, 5, "more.toml"), root / "child", IMAGE, extends=source)
    assert not (root / "child").exists()
