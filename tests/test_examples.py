"""Fixture evidence: every packaged example opens and its scripted demo runs. No model calls.

The curve-fitting task is a Harbor task, so the two tests that grade it need
Docker; opt in with COEVOLUTION_DOCKER_TESTS=1.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

EXAMPLES = Path(__file__).parents[1] / "examples"
IMAGE = "sha256:" + "a" * 64
TASK_IMAGE = "sha256:" + "b" * 64
docker = pytest.mark.skipif(os.environ.get("COEVOLUTION_DOCKER_TESTS") != "1",
                            reason="opt-in: the curve-fitting grader runs in Docker")


def ari(command, task, output, *extra):
    return subprocess.run(
        [sys.executable, "-m", "autoresearch_infrastructure.cli", command, str(task),
         "--output", str(output), "--json", *map(str, extra)],
        capture_output=True, text=True, timeout=300,
    )


@docker
def test_curve_demo_measures_each_fit_and_rejects_the_out_of_range_proposal(tmp_path):
    result = subprocess.run(
        [sys.executable, str(EXAMPLES / "curve-fitting/demo.py"), "--output", str(tmp_path / "demo"), "--json"],
        capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    rows = summary["candidates"]
    errors = [row["metrics"]["rmse"] for row in rows[:4]]
    assert all(a > b > 0 for a, b in zip(errors, errors[1:]))
    assert rows[-1]["status"] == "invalid" and rows[-1]["evaluation"] == "not_started"
    assert summary["selected"] == "candidate-0004"
    assert summary["budget"]["evaluations"]["used"] == 4
    # The grader's files are published to the researcher's history, not kept private.
    artifacts = list((tmp_path / "demo/research/sessions/research/history").rglob("residuals.json"))
    assert len(artifacts) == 4 and not list((tmp_path / "demo").rglob("private/**/residuals.json"))
    for artifact in artifacts:
        assert len(json.loads(artifact.read_text())["observations"]) == 13
        assert artifact.with_name("fit.svg").read_text().startswith("<svg xmlns=")
    assert Path(summary["viewer"]).is_file()


@docker
def test_a_copied_curve_task_records_manual_edits(tmp_path):
    task = tmp_path / "my-task"
    shutil.copytree(EXAMPLES / "curve-fitting", task, ignore=shutil.ignore_patterns("__pycache__"))
    output = tmp_path / "run"
    preview = ari("check", task, output, "--config", task / "local.toml")
    assert preview.returncode == 0, preview.stderr
    assert json.loads(preview.stdout)["budget"]["evaluations"]["used"] == 0
    baseline = ari("record", task, output, "--config", task / "local.toml")
    assert baseline.returncode == 0, baseline.stderr
    summary = json.loads(baseline.stdout)
    initial = summary["candidates"][0]["metrics"]["rmse"]
    workspace = Path(summary["workspace"])
    (workspace / "coefficients.json").write_text('{"coefficients": [2, 1.5, 0.5]}')
    improved = json.loads(ari("record", task, output, "--config", task / "local.toml").stdout)
    assert 0 < improved["candidates"][1]["metrics"]["rmse"] < initial / 10
    (workspace / "coefficients.json").write_text('{"coefficients": [1000]}')
    invalid = ari("record", task, output, "--config", task / "local.toml")
    assert invalid.returncode == 1
    assert json.loads(invalid.stdout)["candidates"][-1]["status"] == "invalid"
    assert json.loads((output / "research/state.json").read_text())["solver_trial_starts"] == 2


def test_curve_live_settings_open_without_credentials_or_starts(tmp_path):
    pytest.importorskip("harbor")
    result = ari("check", EXAMPLES / "curve-fitting", tmp_path / "live")  # no --image: the task's own container
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["starts"] == {"researcher": 0, "reviewer": 0, "reviewer_retry": 0}
    assert summary["review"] == "the task's validity function, then a reviewer"
    assert summary["budget"]["reviews"]["limit"] == 2
    plan = json.loads((tmp_path / "live/plan.json").read_text())
    assert plan["settings"]["limits"]["max_candidates"] == plan["settings"]["limits"]["max_evaluations"] == 2
    assert plan["settings"]["researcher"]["override_timeout_sec"] == 300
    assert plan["settings"]["reviewer"]["override_timeout_sec"] == 120
    assert "## Objective" in plan["instructions"] and "- Score: `rmse`. Lower is better." in plan["instructions"]
    assert "Also recorded, without affecting selection: `max_error`, `coefficients`." in plan["instructions"]
    assert "`/app/data/observations.json`" in plan["instructions"]
    assert "- Keep scripts and scratch files out of the candidate directory." in plan["instructions"]
    assert plan["settings"]["reviewer"]["name"] == "terminus-2"
    # Both agents get the Harbor task's container, with the observations and without the grader.
    assert summary["environments"] == {"researcher": "not prepared", "reviewer": "not prepared"}
    for role in ("environment", "review-environment"):
        staged = tmp_path / "live" / role
        assert len(json.loads((staged / "observations.json").read_text())["observations"]) == 13
        assert (staged / "Dockerfile").is_file() and not list(staged.rglob("grade.py"))
    assert not list((tmp_path / "live/context").iterdir())


def test_packing_demo_selects_the_best_selection_within_the_weight_limit(tmp_path):
    result = subprocess.run(
        [sys.executable, str(EXAMPLES / "packing/demo.py"), "--output", str(tmp_path / "packing")],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stderr
    summary = json.loads((tmp_path / "packing/summary.json").read_text())
    assert summary["selected"] == "candidate-0002"
    assert [row["status"] for row in summary["candidates"]] == ["valid", "valid", "invalid", "valid"]
    assert summary["candidates"][3]["metrics"] == {"value": 15.0, "weight": 9.0}


@pytest.mark.parametrize("name, kinds", [
    ("sum-instructions", ["researcher", "reviewer", "reviewer_retry", "solver_trial"]),
    ("sum-harness", ["researcher", "reviewer", "reviewer_retry", "solver_trial", "solver_session"]),
])
def test_sum_examples_are_self_contained_and_open_without_starts(tmp_path, name, kinds):
    pytest.importorskip("harbor")
    task = tmp_path / name
    shutil.copytree(EXAMPLES / name, task, ignore=shutil.ignore_patterns("__pycache__"))
    result = ari("check", task, tmp_path / "run", "--image", IMAGE)
    assert result.returncode == 0, result.stderr
    summary = json.loads(result.stdout)
    assert summary["starts"] == dict.fromkeys(kinds, 0)
    assert summary["budget"]["candidates"]["limit"] == 3 and summary["candidates"] == []
    workspace = Path(summary["workspace"])
    if name == "sum-instructions":
        assert [path.relative_to(workspace).as_posix() for path in workspace.rglob("*") if path.is_file()] == [
            "sum/instruction.md"]
    else:
        from autoresearch_infrastructure.programs.package import validate_program

        validate_program(workspace, "sum-solver", harness="codex")
        assert (tmp_path / "run/context/usage.md").is_file()


@pytest.mark.parametrize("name", ["sum-instructions", "sum-harness"])
def test_a_pinned_task_image_is_copied_once_and_must_not_change(tmp_path, name):
    pytest.importorskip("harbor")
    task = tmp_path / name
    shutil.copytree(EXAMPLES / name, task, ignore=shutil.ignore_patterns("__pycache__"))
    settings = task / "run.toml"
    settings.write_text(settings.read_text() + f'image = "{TASK_IMAGE}"\n')
    output = tmp_path / "run"
    assert ari("check", task, output, "--image", IMAGE).returncode == 0
    pinned = output / "harbor-tasks/sum"
    assert TASK_IMAGE in (pinned / "task.toml").read_text()
    assert (pinned / "tests/test.sh").read_bytes() == (task / "harbor-task/tests/test.sh").read_bytes()
    (pinned / "tests/test.sh").write_text("exit 0\n")
    changed = ari("check", task, output, "--image", IMAGE)
    assert changed.returncode == 2 and "Prepared task changed" in changed.stderr
    ledger = json.loads((output / "starts.json").read_text())
    assert ledger["starts"] == {} and not any(ledger["counts"].values())


def test_view_accepts_a_run_directory(tmp_path):
    task = EXAMPLES / "packing"
    assert ari("record", task, tmp_path / "run").returncode == 0
    page = tmp_path / "page.html"
    viewed = subprocess.run(
        [sys.executable, "-m", "autoresearch_infrastructure.cli", "view", str(tmp_path / "run"), "--html", str(page)],
        capture_output=True, text=True, timeout=60,
    )
    assert viewed.returncode == 0, viewed.stderr
    assert json.loads(viewed.stdout)["status"] == "written" and page.is_file()


def test_imported_instruction_examples_parse_and_render_with_task_facts():
    import autoresearch_infrastructure as ari
    from autoresearch_infrastructure.instructions import task_facts

    directory = Path(__file__).parents[1] / "skills/use-autoresearch-infrastructure/references/import-examples"
    files = sorted(directory.glob("*.md"))
    assert [path.stem for path in files] == ["attribution-speedup", "cpu-llm-decode-throughput"]
    for path in files:
        parsed = ari.Instructions.parse(path.read_text())
        assert parsed.title and len(parsed.rules) >= 4 and len(parsed.environment) >= 4
        assert all(heading in parsed.objective for heading in (
            "### Interface", "### How a run is measured", "### Measuring locally"))
        rendered = parsed.render(task_facts(ari.Metric("score"), ["a.py"], cases=3))
        assert rendered.index("### Measuring locally") < rendered.index("### What is measured") < rendered.index(
            "## Constraints")
        assert "- Score: `score`. Higher is better." in rendered
