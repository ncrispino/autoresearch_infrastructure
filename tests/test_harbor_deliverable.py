"""The Harbor deliverable base task.

The first tests are fixture evidence: no Docker, with the grader container
replaced by a function. The last ones run real grader containers and builds;
opt in with COEVOLUTION_DOCKER_TESTS=1. They pull one small pinned image.
"""

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import autoresearch_infrastructure as ari
from autoresearch_infrastructure import harbor_deliverable, launch
from autoresearch_infrastructure.harbor_deliverable import HarborGraderEvaluator, sealed_rewards
from autoresearch_infrastructure.source_tree import materialize

EXAMPLE = Path(__file__).parents[1] / "examples/threshold-deliverable"
BASE = "busybox@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"
docker = pytest.mark.skipif(os.environ.get("COEVOLUTION_DOCKER_TESTS") != "1",
                            reason="opt-in test of real grader containers; requires Docker")
RESEARCHER = ('[researcher]\nname = "terminus-2"\nmodel_name = "openrouter/openai/gpt-6-luna"\n'
              "override_timeout_sec = 86400\n")


def harbor_task(root, **extra):
    materialize(root, {
        "task.toml": '[environment]\nworkdir = "/app"\ncpus = 1\nmemory_mb = 256\n[verifier]\ntimeout_sec = 30\n',
        "instruction.md": "Write /app/output/answer.txt.",
        "environment/Dockerfile": f"FROM {BASE}\nCOPY public.txt /app/public.txt\n",
        "environment/public.txt": "public\n",
        "tests/feedback.sh": "echo 0.25 > /logs/verifier/reward.txt\n",
        "tests/test.sh": "echo 0.75 > /logs/verifier/reward.txt\n",
        **extra,
    })
    return root


def evaluator(tmp_path, **options):
    options = {"deliverable": "/app/output", "feedback": "/tests/feedback.sh", "sealed": "/tests/test.sh", **options}
    task = options.pop("task", None) or harbor_task(tmp_path / "harbor-task")
    return HarborGraderEvaluator(task=task, state=tmp_path / "state", **options)


def open_copy(tmp_path, settings):
    tmp_path.mkdir(exist_ok=True)
    task = tmp_path / "task"
    shutil.copytree(EXAMPLE, task, ignore=shutil.ignore_patterns("__pycache__"))
    (task / "settings.toml").write_text(settings)
    return launch.open_run(task, task / "settings.toml", tmp_path / "run", None)


def record(run, *models):
    """Record each model as a candidate and return the summaries."""
    summaries = []
    with run.research:
        for model in models:
            Path(run.research.workspace, "model.json").write_text(model)
            summaries.append(launch.record_workspace(run))
    return summaries


@pytest.mark.parametrize("options, message", [
    ({"deliverable": "app/output"}, "absolute path"),
    ({"deliverable": "/tests"}, "cannot be mounted"),
    ({"deliverable": "/"}, "cannot be mounted"),
    ({"feedback": "/tests/missing.sh"}, "not in the Harbor task's tests/"),
    ({"sealed": "/tests/feedback.sh"}, "must differ"),
    ({"grader": "environment/absent"}, "Dockerfile"),
    ({"grader": "../elsewhere"}, "inside the Harbor task"),
    ({"capabilities": ["sys_admin; rm"]}, "capability names"),
])
def test_a_grader_that_cannot_keep_its_promises_is_refused_before_docker(tmp_path, options, message):
    with pytest.raises(ValueError, match=message):
        evaluator(tmp_path, **options)


def test_feedback_is_the_public_score_and_the_sealed_score_stays_in_private_evidence(tmp_path, monkeypatch):
    graded = []

    def grade(self, candidate, script, evidence, case):
        graded.append(script)
        evidence.mkdir(parents=True)
        (evidence / "output.txt").write_text(f"hidden {script} details")
        if script == "sealed":
            return {"reward": 0.123456}
        return {"reward": float(json.loads((candidate / "model.json").read_text())["threshold"])}

    monkeypatch.setattr(HarborGraderEvaluator, "grade", grade)
    monkeypatch.setattr(HarborGraderEvaluator, "image", lambda self: "sha256:" + "c" * 64)
    run = open_copy(tmp_path, "reviewer = false\n[limits]\ncandidates = 3\nevaluations = 3\n")
    first, second = record(run, '{"threshold": 0.5}', '{"threshold": 0.9}')
    assert [row["metrics"] for row in second["candidates"]] == [{"reward": 0.5}, {"reward": 0.9}]
    assert first["selected"] == "candidate-0001" and second["selected"] == "candidate-0002"
    assert graded == ["feedback", "sealed"] * 2

    assert [(entry["candidate_id"], entry["rewards"]) for entry in sealed_rewards(tmp_path / "run")] == [
        ("candidate-0001", {"reward": 0.123456}), ("candidate-0002", {"reward": 0.123456})]
    # Nothing a researcher can read, and nothing a person shares, holds the sealed score or a grader log.
    root = tmp_path / "run/research/sessions/research"
    public = [tmp_path / "run/summary.json", tmp_path / "run/viewer.html", *(root / "history").rglob("*")]
    text = "".join(path.read_text(errors="replace") for path in public if path.is_file())
    assert "0.123456" not in text and "hidden" not in text
    assert "0.123456" in "".join(path.read_text() for path in (root / "private").rglob("sealed.json"))


def test_a_failed_sealed_script_is_recorded_privately_and_does_not_change_the_measurement(tmp_path, monkeypatch):
    def grade(self, candidate, script, evidence, case):
        evidence.mkdir(parents=True)
        (evidence / "output.txt").write_text("")
        if script == "sealed":
            raise RuntimeError("sealed data missing")
        return {"reward": 0.5}

    monkeypatch.setattr(HarborGraderEvaluator, "grade", grade)
    monkeypatch.setattr(HarborGraderEvaluator, "image", lambda self: "sha256:" + "c" * 64)
    run = open_copy(tmp_path, "reviewer = false\n")
    summary, = record(run, '{"threshold": 0.5}')
    assert summary["candidates"][0]["metrics"] == {"reward": 0.5}
    assert sealed_rewards(tmp_path / "run")[0]["error"] == "RuntimeError: sealed data missing"


def test_instruction_md_becomes_the_objective_with_the_added_rules(tmp_path):
    run = open_copy(tmp_path, "reviewer = false\n")
    plan = json.loads((tmp_path / "run/plan.json").read_text())["instructions"]
    assert "Write `/app/output/model.json` holding one JSON object" in plan
    assert "- The deliverable is the single file `model.json`." in plan
    assert "use `/workspace/candidate/` instead" in plan and "- Score: `reward`. Higher is better." in plan
    assert run.task.environment.harbor is None  # no researcher is configured, so nothing is derived
    assert not (tmp_path / "run/harbor-deliverable").exists()  # and nothing was built


def test_the_researcher_environment_leaves_out_the_grader_and_the_benchmark_services(tmp_path):
    pytest.importorskip("harbor")
    task = harbor_task(tmp_path / "harbor-task", **{
        "task.toml": ('[environment]\nworkdir = "/app"\ncpus = 2\n'
                      '[[environment.mcp_servers]]\nname = "submit"\ntransport = "streamable-http"\n'
                      'url = "http://grader:8000/mcp"\n'),
        "environment/docker-compose.yaml": "services:\n  grader:\n    build: grader\n    cap_add: [SYS_ADMIN]\n",
        "environment/grader/Dockerfile": f"FROM {BASE}\nCOPY hidden.txt /hidden.txt\n",
        "environment/grader/hidden.txt": "the answers\n",
        "environment/notes/answers.txt": "more answers\n",
    })
    target = harbor_deliverable.researcher_environment(
        task, tmp_path / "derived", private=["environment/grader", "environment/notes/answers.txt"])
    files = sorted(path.relative_to(target).as_posix() for path in target.rglob("*") if path.is_file())
    assert files == ["environment/Dockerfile", "environment/public.txt", "task.toml"]
    environment = launch.harbor_environment.load(target)  # the checked definition a researcher start uses
    assert environment.config["cpus"] == 2 and not environment.config.get("mcp_servers")
    assert environment.sidecars == {}
    assert harbor_deliverable.researcher_environment(
        task, tmp_path / "derived", private=["environment/grader", "environment/notes/answers.txt"]) == target
    (task / "environment/public.txt").write_text("changed\n")
    with pytest.raises(ValueError, match="environment changed"):
        harbor_deliverable.researcher_environment(task, tmp_path / "derived", private=["environment/grader"])
    with pytest.raises(ValueError, match="not in the Harbor task"):
        harbor_deliverable.researcher_environment(task, tmp_path / "other", private=["environment/absent"])


def test_a_configured_researcher_runs_in_the_derived_environment_and_may_take_a_day(tmp_path):
    pytest.importorskip("harbor")
    run = open_copy(tmp_path, "reviewer = false\n" + RESEARCHER)
    assert run.settings.researcher["override_timeout_sec"] == 86400
    assert run.image is None and run.environment is not None and run.review_environment is None
    staged = tmp_path / "run/environment"
    assert (staged / "Dockerfile").is_file() and (staged / "train.csv").is_file()
    assert not list(staged.rglob("*.sh")) and not list(staged.rglob("sealed.csv"))
    # An image named under [task] replaces the build for the agents, and only for them.
    pinned = "sha256:" + "d" * 64
    other = open_copy(tmp_path / "pinned", "reviewer = false\n" + RESEARCHER + f'[task]\nimage = "{pinned}"\n')
    assert other.environment.config["docker_image"] == pinned and other.task.evaluator.grader is None
    with pytest.raises(ValueError, match="image ID or digest"):
        open_copy(tmp_path / "tagged", "reviewer = false\n" + RESEARCHER + '[task]\nimage = "python:3.12"\n')
    with pytest.raises(ValueError, match="at most 86400 seconds"):
        ari.RunSettings.parse({"reviewer": False, "researcher": {
            "name": "terminus-2", "model_name": "openrouter/openai/gpt-6-luna", "override_timeout_sec": 86401}})
    with pytest.raises(ValueError, match="at most 7200 seconds"):
        ari.RunSettings.parse({"reviewer": {
            "name": "terminus-2", "model_name": "openrouter/openai/gpt-6-luna", "override_timeout_sec": 7201}})


def test_the_reviewer_also_runs_in_the_task_container_and_needs_rules_to_judge(tmp_path):
    pytest.importorskip("harbor")
    with pytest.raises(ValueError, match=r"Add a \[reviewer\] table"):
        open_copy(tmp_path / "day-long", RESEARCHER)  # a reviewer may not inherit a day-long timeout
    settings = RESEARCHER.replace("86400", "600")
    reviewed = open_copy(tmp_path / "reviewed", settings)  # no --image: both agents use the task's container
    assert reviewed.review_environment is not None and reviewed.review == "a reviewer"
    assert (tmp_path / "reviewed/run/review-environment/Dockerfile").is_file()
    task = tmp_path / "task"
    shutil.copytree(EXAMPLE, task, ignore=shutil.ignore_patterns("__pycache__"))
    source = (task / "task.py").read_text()
    start, end = source.index("        rules=["), source.index("    )\n", source.index("        rules=["))
    (task / "task.py").write_text(source[:start] + source[end:])
    (task / "settings.toml").write_text(settings)
    with pytest.raises(ValueError, match="list no rules"):
        launch.open_run(task, task / "settings.toml", tmp_path / "run", None)


@docker
def test_the_example_is_graded_by_its_own_harbor_grader_in_real_containers(tmp_path):
    task = tmp_path / "task"
    shutil.copytree(EXAMPLE, task, ignore=shutil.ignore_patterns("__pycache__"))

    def ari_command(command, *extra):
        result = subprocess.run(
            [sys.executable, "-m", "autoresearch_infrastructure.cli", command, *map(str, extra)],
            capture_output=True, text=True, timeout=600)
        assert result.returncode == 0, result.stderr
        return json.loads(result.stdout)

    before = subprocess.run(["docker", "ps", "-aq"], capture_output=True, text=True).stdout
    arguments = (task, "--output", tmp_path / "run", "--config", task / "local.toml", "--json")
    baseline = ari_command("record", *arguments)
    Path(baseline["workspace"], "model.json").write_text('{"threshold": 0.62}')
    improved = ari_command("record", *arguments)
    scores = [row["metrics"]["reward"] for row in improved["candidates"]]
    assert scores[0] < scores[1] and improved["selected"] == "candidate-0002"
    sealed = ari_command("sealed", tmp_path / "run")
    assert [entry["candidate_id"] for entry in sealed] == ["candidate-0001", "candidate-0002"]
    assert sealed[0]["rewards"]["reward"] < sealed[1]["rewards"]["reward"]
    assert sealed[1]["rewards"]["reward"] != scores[1]  # a different split
    record = json.loads((tmp_path / "run/harbor-deliverable/grader.json").read_text())
    assert record["built"] is True and record["image"].startswith("sha256:")
    assert subprocess.run(["docker", "ps", "-aq"], capture_output=True, text=True).stdout == before


@docker
def test_a_grader_built_from_its_own_directory_cannot_change_the_candidate_or_reach_the_network(tmp_path):
    task = harbor_task(tmp_path / "harbor-task", **{
        "environment/grader/Dockerfile": f"FROM {BASE}\nCOPY hidden.txt /hidden.txt\nCOPY grade.sh /grade.sh\n",
        "environment/grader/hidden.txt": "7\n",
        "environment/grader/grade.sh": (
            'touch /app/output/written 2>/dev/null && echo "writable" >&2\n'
            'wget -q -T 2 -O /dev/null http://example.com 2>/dev/null && echo "network" >&2\n'
            'echo "{\\"matches\\": $([ "$(cat /app/output/answer.txt)" = "$(cat /hidden.txt)" ] && echo 1 || echo 0),'
            ' \\"length\\": $(wc -c < /app/output/answer.txt)}" > /logs/verifier/reward.json\n'
            'echo "the hidden value is $(cat /hidden.txt)"\n'),
    })
    matches, length = ari.Metric("matches"), ari.Metric("length", "min")
    grader = evaluator(tmp_path, task=task, grader="environment/grader", feedback="/grade.sh", sealed=None,
                       rewards={matches: "matches", length: "length"})
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "answer.txt").write_text("7\n")
    assert grader.grade(candidate, "feedback", tmp_path / "evidence") == {"matches": 1, "length": 2}
    assert sorted(path.name for path in candidate.iterdir()) == ["answer.txt"]
    output = (tmp_path / "evidence/output.txt").read_text()
    assert "the hidden value is 7" in output and "writable" not in output and "network" not in output
    assert grader.image() == json.loads((tmp_path / "state/grader.json").read_text())["image"]

    # The researcher's image is built without the grader's directory.
    pytest.importorskip("harbor")
    target = harbor_deliverable.researcher_environment(task, tmp_path / "derived", private=["environment/grader"])
    loaded = launch.harbor_environment.load(target)
    prepared = launch.harbor_environment.prepare(loaded)
    listing = subprocess.run(["docker", "run", "--rm", prepared["main"], "sh", "-c", "ls / /app"],
                             capture_output=True, text=True, check=True).stdout
    assert "public.txt" in listing and "hidden" not in listing and "grade.sh" not in listing


@docker
def test_a_script_that_writes_no_reward_leaves_the_evaluation_unfinished(tmp_path):
    task = harbor_task(tmp_path / "harbor-task", **{"tests/feedback.sh": "echo broken >&2\nexit 3\n"})
    grader = evaluator(tmp_path, task=task, sealed=None)
    (tmp_path / "candidate").mkdir()
    with pytest.raises(RuntimeError, match="exited 3 and wrote no reward"):
        grader.grade(tmp_path / "candidate", "feedback", tmp_path / "evidence")


@docker
def test_each_case_is_graded_separately_and_feedback_artifacts_reach_the_researcher_only_when_asked(tmp_path):
    script = (
        'case "$ARI_CASE" in small) echo 1 ;; large) echo 3 ;; esac > /logs/verifier/reward.txt\n'
        'echo "plot for $ARI_CASE" > /logs/artifacts/plot.txt\n'
        'ln -s /etc/passwd /logs/artifacts/link\n')
    materialize(tmp_path / "task", {
        "task.py": (
            "import autoresearch_infrastructure as ari\n\n\n"
            "def task(run):\n"
            "    return ari.bases.harbor_deliverable(\n"
            "        run, harbor_task='harbor-task', deliverable='/app/output', feedback='/tests/feedback.sh',\n"
            "        sealed='/tests/test.sh', cases=('small', 'large'),\n"
            "        feedback_artifacts=run.settings.task.get('publish', False))\n"),
        "seed/answer.txt": "x\n",
        "public.toml": "reviewer = false\n[limits]\nevaluations = 4\n[task]\npublish = true\n",
        "private.toml": "reviewer = false\n[limits]\nevaluations = 4\n",
    })
    harbor_task(tmp_path / "task/harbor-task", **{"tests/feedback.sh": script, "tests/test.sh": script})
    for settings, published in (("public", True), ("private", False)):
        run = launch.open_run(tmp_path / "task", tmp_path / f"task/{settings}.toml", tmp_path / settings, None)
        with run.research:
            summary = launch.record_workspace(run)
        assert summary["candidates"][0]["metrics"] == {"reward": 2.0}  # the mean of the two cases
        assert summary["budget"]["evaluations"]["used"] == 2
        root = tmp_path / settings / "research/sessions/research"
        shown = sorted(path.read_text() for path in (root / "history").rglob("plot.txt"))
        assert shown == (["plot for large\n", "plot for small\n"] if published else [])
        # The sealed script's files stay private either way, and a link a grader left is dropped.
        assert len(list((root / "private").rglob("sealed/artifacts/plot.txt"))) == 2
        assert not [path for path in (tmp_path / settings).rglob("link")]
        assert sorted(entry["case"] for entry in sealed_rewards(tmp_path / settings)) == ["large", "small"]
