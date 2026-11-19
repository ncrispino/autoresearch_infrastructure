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
from types import SimpleNamespace

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
    monkeypatch.setattr(HarborGraderEvaluator, "prepare", lambda self: None)  # no image to compare the seed with
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

    # The results page says a sealed score exists, and shows nothing of it.
    from autoresearch_infrastructure.viewer import build_index

    index = json.dumps(build_index(tmp_path / "run/research"))
    assert '"sealed_recorded": true' in index and "0.123456" not in index


def test_a_failed_sealed_script_is_recorded_privately_and_does_not_change_the_measurement(tmp_path, monkeypatch):
    def grade(self, candidate, script, evidence, case):
        evidence.mkdir(parents=True)
        (evidence / "output.txt").write_text("")
        if script == "sealed":
            raise RuntimeError("sealed data missing")
        return {"reward": 0.5}

    monkeypatch.setattr(HarborGraderEvaluator, "grade", grade)
    monkeypatch.setattr(HarborGraderEvaluator, "image", lambda self: "sha256:" + "c" * 64)
    monkeypatch.setattr(HarborGraderEvaluator, "prepare", lambda self: None)  # no image to compare the seed with
    run = open_copy(tmp_path, "reviewer = false\n")
    summary, = record(run, '{"threshold": 0.5}')
    assert summary["candidates"][0]["metrics"] == {"reward": 0.5}
    assert sealed_rewards(tmp_path / "run")[0]["error"] == "RuntimeError: sealed data missing"


def test_instruction_md_becomes_the_objective_with_the_added_rules(tmp_path):
    run = open_copy(tmp_path, "reviewer = false\n")
    plan = json.loads((tmp_path / "run/plan.json").read_text())["instructions"]
    assert "Write `/workspace/candidate/model.json` holding one JSON object" in plan
    assert "- The deliverable is the single file `model.json`." in plan
    assert "- Score: `reward`. Higher is better." in plan and "- Starting files: `model.json`." in plan
    assert "instead" not in plan  # the task already names the path the researcher edits
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
    assert "Only `/workspace` and `/tmp` are writable." in run.inputs.specification()  # the convention: one path
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
        # The script lives in the grader image's own /tests, beside a task that has a top-level tests/ too.
        "environment/grader/Dockerfile": f"FROM {BASE}\nCOPY hidden.txt /hidden.txt\nCOPY grade.sh /tests/grade.sh\n",
        "environment/grader/hidden.txt": "7\n",
        "environment/grader/grade.sh": (
            'touch /app/output/written 2>/dev/null && echo "writable" >&2\n'
            'wget -q -T 2 -O /dev/null http://example.com 2>/dev/null && echo "network" >&2\n'
            'echo "{\\"matches\\": $([ "$(cat /app/output/answer.txt)" = "$(cat /hidden.txt)" ] && echo 1 || echo 0),'
            ' \\"length\\": $(wc -c < /app/output/answer.txt)}" > /logs/verifier/reward.json\n'
            'echo "the hidden value is $(cat /hidden.txt)"\n'),
    })
    matches, length = ari.Metric("matches"), ari.Metric("length", "min")
    grader = evaluator(tmp_path, task=task, grader="environment/grader", feedback="/tests/grade.sh", sealed=None,
                       rewards={matches: "matches", length: "length"})
    candidate = tmp_path / "candidate"
    candidate.mkdir()
    (candidate / "answer.txt").write_text("7\n")
    grader.prepare()  # the script is found inside the grader image
    assert json.loads((tmp_path / "state/grader.json").read_text())["scripts_checked"] is True
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
            "        sealed='/tests/test.sh', cases=('small', 'large'), check_seed=False,\n"
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


def imported(tmp_path, call, settings="reviewer = false\n", image=None, **files):
    """A task directory around a Harbor task from elsewhere, opened with no agents unless the settings name one."""
    inside = {name: text for name, text in files.items() if name.startswith("harbor-task/")}
    harbor_task(tmp_path / "task/harbor-task", **{name[len("harbor-task/"):]: text for name, text in inside.items()})
    beside = {
        "task.py": ("import autoresearch_infrastructure as ari\n\n\n"
                    f"def task(run):\n    return ari.bases.harbor_deliverable(run, harbor_task='harbor-task', {call})\n"),
        "settings.toml": settings,
        **{name: text for name, text in files.items() if name not in inside},
    }
    for name, text in beside.items():
        (tmp_path / "task" / name).parent.mkdir(parents=True, exist_ok=True)
        (tmp_path / "task" / name).write_text(text)
    return launch.open_run(tmp_path / "task", tmp_path / "task/settings.toml", tmp_path / "run", image)


def test_a_task_from_elsewhere_keeps_its_own_deliverable_path_for_the_grader_and_the_researcher(tmp_path):
    run = imported(tmp_path, "deliverable='/app/output', feedback='/tests/feedback.sh'",
                   **{"seed/answer.txt": "x\n"})
    assert ("`/app/output`, the deliverable directory the text above names, is also mounted at "
            "`/workspace/candidate/`. The two paths are one directory") in run.inputs.specification()
    assert run.task.evaluator.deliverable.as_posix() == "/app/output"
    assert run.task.environment.candidate_at == "/app/output"
    plan = json.loads((tmp_path / "run/plan.json").read_text())
    assert plan["inputs"]["environment"]["candidate_at"] == "/app/output"


def test_the_researcher_is_started_with_the_candidate_at_the_tasks_own_path(tmp_path, monkeypatch):
    pytest.importorskip("harbor")
    started = []

    class Agent:
        def __init__(self, **options):
            started.append(options)

        def run(self, prompt, **options):
            raise RuntimeError("fixture: no container is started")

    monkeypatch.setattr("autoresearch_infrastructure.coding_agent.CodingAgent", Agent)
    monkeypatch.setattr(HarborGraderEvaluator, "grade", lambda *arguments: {"reward": 0.5})
    monkeypatch.setattr(HarborGraderEvaluator, "image", lambda self: "sha256:" + "c" * 64)
    monkeypatch.setattr(HarborGraderEvaluator, "prepare", lambda self: None)  # no image to compare the seed with
    agents = "sha256:" + "a" * 64
    call = "feedback='/tests/feedback.sh', sealed=None, task_environment=False"
    settings = "reviewer = false\n" + RESEARCHER.replace("86400", "600")
    for name, own in (("elsewhere", "/app/output"), ("convention", None)):
        run = imported(tmp_path / name, call + (f", deliverable='{own}'" if own else ""), settings, agents,
                       **{"seed/answer.txt": "x\n"})
        with run.research:
            launch.run_researchers(run)
        assert started.pop() == {"agent": run.settings.researcher, "image": agents,
                                 **({"candidate_at": own} if own else {})}


@pytest.mark.parametrize("path", ["/workspace/candidate", "/workspace", "/", "/tmp/x", "/etc/app", "app/output",
                                  "/app/../etc", "/history/x", "/context"])
def test_the_candidate_is_never_also_mounted_over_a_path_the_run_depends_on(path):
    with pytest.raises(ValueError, match="cannot also be mounted"):
        ari.Environment(candidate_at=path)


@docker
def test_the_researchers_container_sees_one_candidate_directory_at_both_paths(tmp_path, monkeypatch):
    pytest.importorskip("harbor")
    from harbor.job import Job

    from autoresearch_infrastructure.coding_agent import CodingAgent

    configs = []

    async def create(config):
        configs.append(config)

        async def run():
            return SimpleNamespace(trial_results=[SimpleNamespace(exception_info=None, trial_name="fixture")])

        return SimpleNamespace(run=run)

    monkeypatch.setattr(Job, "create", create)
    task = harbor_task(tmp_path / "harbor-task")
    image = evaluator(tmp_path, task=task).image()  # /app/public.txt is in the task's image
    workspace = tmp_path / "workspace"
    (workspace / "candidate").mkdir(parents=True)
    (workspace / "candidate/answer.txt").write_text("first\n")
    recorder = SimpleNamespace(dispatch_record_candidate=lambda *arguments: {})
    agent = CodingAgent(agent={"name": "terminus-2", "model_name": "fixture"}, image=image,
                        candidate_at="/app/output")
    with pytest.raises(RuntimeError, match="ATIF trajectory"):  # the fixture job runs no agent
        agent.run("Fixture", workspace=workspace, evidence=tmp_path / "evidence", recorder=recorder,
                  jobs_root=tmp_path / "jobs")
    mounts = {mount["target"]: mount for mount in configs[0].environment.mounts}
    assert mounts["/app/output"]["source"] == str(workspace / "candidate")
    assert not mounts["/app/output"].get("read_only") and not mounts["/workspace"].get("read_only")

    # The same two mounts on real Docker: one directory, and the task's relative paths resolve as in grading.
    name = "ari-candidate-" + tmp_path.name[-12:].lower().replace("_", "-")
    arguments = [flag for target in ("/workspace", "/app/output")
                 for flag in ("--mount", f"type=bind,source={mounts[target]['source']},target={target}")]
    subprocess.run(["docker", "run", "--detach", "--name", name, "--read-only", "--workdir", "/app/output",
                    *arguments, image, "sleep", "60"], check=True, capture_output=True)

    def inside(command):
        return subprocess.run(["docker", "exec", name, "sh", "-c", command], capture_output=True, text=True,
                              check=True).stdout

    try:
        assert inside("cat ../public.txt answer.txt /workspace/candidate/answer.txt") == "public\nfirst\nfirst\n"
        inside("echo edited > /app/output/answer.txt")
        assert (workspace / "candidate/answer.txt").read_text() == "edited\n"
        assert inside("cat /workspace/candidate/answer.txt") == "edited\n"
        # Between submissions ARI replaces the candidate's files in place; a running container sees the new ones.
        (workspace / "candidate/answer.txt").unlink()
        (workspace / "candidate/next.txt").write_text("fresh\n")
        assert inside("ls /app/output; cat /app/output/next.txt") == "next.txt\nfresh\n"
    finally:
        subprocess.run(["docker", "rm", "--force", name], capture_output=True)


def test_the_starting_deliverable_defaults_to_the_directory_the_task_ships(tmp_path):
    run = imported(tmp_path, "feedback='/tests/feedback.sh'",
                   **{"harbor-task/environment/candidate/answer.txt": "shipped\n", "seed/answer.txt": "beside\n"})
    assert Path(run.task.seed, "answer.txt").read_text() == "shipped\n"
    assert run.task.evaluator.deliverable.as_posix() == "/workspace/candidate"


def test_a_task_that_keeps_files_under_the_researchers_workspace_is_refused(tmp_path):
    files = {"seed/answer.txt": "x\n", "harbor-task/task.toml": '[environment]\nworkdir = "/workspace"\n'}
    call = "feedback='/tests/feedback.sh'"
    with pytest.raises(ValueError, match="workdir is /workspace"):
        imported(tmp_path / "workdir", call, **files)
    run = imported(tmp_path / "host", call + ", task_environment=False", **files)  # agents in --image: no clash
    assert run.task.environment.harbor is None
    for options in ("", ", task_environment=False"):  # a deliverable there clashes wherever the agents run
        with pytest.raises(ValueError, match="deliverable is /workspace/out"):
            imported(tmp_path / ("out" + options[-6:]), call + ", deliverable='/workspace/out'" + options,
                     **{"seed/answer.txt": "x\n"})


def test_a_task_without_context_files_is_told_of_no_context_directory():
    from autoresearch_infrastructure.search import researcher_prompt

    start = ari.Start(1, "candidate-0001", 1)
    shown = researcher_prompt("Goal", parent_id="candidate-0001", repeats=1, start=start)
    hidden = researcher_prompt("Goal", parent_id="candidate-0001", repeats=1,
                               start=ari.Start(1, "candidate-0001", 1, context=False))
    assert "| `/context/` | Read-only task material. |" in shown
    assert "`/context` and `/history` are mounted read-only." in shown
    assert "/context" not in hidden and "`/history` is mounted read-only." in hidden and "$" not in hidden


def test_a_seed_that_differs_from_what_the_image_ships_is_named_file_by_file(tmp_path, monkeypatch):
    shipped_files = tmp_path / "image-files"
    materialize(shipped_files, {"model.json": '{"threshold": 0.2}\n', "notes/readme.txt": "kept\n"})

    def fake_shipped(image, path, destination, **_):
        shutil.copytree(shipped_files, destination)
        return destination

    monkeypatch.setattr(harbor_deliverable, "shipped", fake_shipped)
    monkeypatch.setattr(HarborGraderEvaluator, "image", lambda self: "sha256:" + "c" * 64)
    seed = tmp_path / "seed"
    materialize(seed, {"model.json": '{"threshold": 0.9}\n', "extra.txt": "mine\n"})
    grader = evaluator(tmp_path, deliverable="/app/output", seed=seed)
    write_json_record = tmp_path / "state/grader.json"
    write_json_record.parent.mkdir(parents=True)
    write_json_record.write_text(json.dumps({"definition": grader.definition, "image": "sha256:" + "c" * 64}))
    with pytest.raises(ValueError) as refused:
        grader.prepare()
    message = str(refused.value)
    assert "notes/readme.txt is only in the image" in message and "extra.txt is only in the seed" in message
    assert "model.json differs" in message and "ari extract-seed" in message and "check_seed=False" in message

    shutil.rmtree(seed)
    shutil.copytree(shipped_files, seed)
    grader.prepare()
    recorded = json.loads(write_json_record.read_text())
    assert recorded["seed_checked_against"] == "sha256:" + "c" * 64
    monkeypatch.setattr(harbor_deliverable, "shipped", lambda *arguments: pytest.fail("checked twice"))
    grader.prepare()  # recorded once, so later evaluations do not create a container


@docker
def test_extract_seed_copies_a_starter_the_dockerfile_generates_and_the_run_checks_against_it(tmp_path):
    task = harbor_task(tmp_path / "harbor-task", **{
        "environment/Dockerfile": (f"FROM {BASE}\nCOPY public.txt /app/public.txt\n"
                                   "RUN mkdir -p /app/output/sub && echo generated > /app/output/start.txt"
                                   " && echo deeper > /app/output/sub/more.txt && ln -s /etc/passwd /app/output/link\n"),
    })

    def cli(*arguments):
        return subprocess.run([sys.executable, "-m", "autoresearch_infrastructure.cli", *map(str, arguments)],
                              capture_output=True, text=True, timeout=300)

    extracted = cli("extract-seed", task, "--deliverable", "/app/output", "--output", tmp_path / "task/seed")
    assert extracted.returncode == 0, extracted.stderr
    result = json.loads(extracted.stdout)
    assert result["shipped"] is True and result["problem"] is None
    assert result["files"] == ["start.txt", "sub/more.txt"]  # the link is dropped
    assert (tmp_path / "task/seed/start.txt").read_text() == "generated\n"
    again = cli("extract-seed", task, "--deliverable", "/app/output", "--output", tmp_path / "task/seed")
    assert again.returncode == 2 and "already exists" in again.stderr
    nothing = cli("extract-seed", task, "--deliverable", "/app/nothing", "--output", tmp_path / "empty")
    assert json.loads(nothing.stdout)["shipped"] is False and not list((tmp_path / "empty").iterdir())

    # A run with that seed passes the check before its baseline; an edited seed is refused before any candidate.
    shutil.move(str(task), tmp_path / "task/harbor-task")
    (tmp_path / "task/task.py").write_text(
        "import autoresearch_infrastructure as ari\n\n\n"
        "def task(run):\n    return ari.bases.harbor_deliverable(run, harbor_task='harbor-task',"
        " deliverable='/app/output', feedback='/tests/feedback.sh')\n")
    (tmp_path / "task/local.toml").write_text("reviewer = false\n")
    recorded = cli("record", tmp_path / "task", "--output", tmp_path / "run", "--config", tmp_path / "task/local.toml",
                   "--json")
    assert recorded.returncode == 0, recorded.stderr
    assert json.loads(recorded.stdout)["candidates"][0]["metrics"] == {"reward": 0.25}
    (tmp_path / "task/seed/start.txt").write_text("edited\n")
    refused = cli("record", tmp_path / "task", "--output", tmp_path / "other", "--config",
                  tmp_path / "task/local.toml", "--json")
    assert refused.returncode == 2 and "start.txt differs" in refused.stderr
    assert not list((tmp_path / "other").rglob("candidate-0001"))  # opened, but nothing recorded or graded


def test_the_file_table_names_the_tasks_own_path_first_when_the_candidate_has_two():
    from autoresearch_infrastructure.search import researcher_prompt

    plain = researcher_prompt("Goal", parent_id="candidate-0001", repeats=1, start=ari.Start(1, "candidate-0001", 1))
    both = researcher_prompt("Goal", parent_id="candidate-0001", repeats=1,
                             start=ari.Start(1, "candidate-0001", 1, candidate_at="/app/output"))
    assert "| `/workspace/candidate/` | Your editable solution, copied from `candidate-0001`." in plain
    assert "/app/output" not in plain and "$" not in both
    row = next(line for line in both.splitlines() if line.startswith("| `/app/output/`"))
    assert "also at `/workspace/candidate/`" in row and "one directory, not two copies" in row
    assert "do not copy between the two paths" in row and "keep scratch files out of it" in row


def test_an_empty_seed_is_described_as_empty():
    from autoresearch_infrastructure.instructions import task_facts

    assert task_facts(ari.Metric("reward"), [])[0] == "Starting files: none. The candidate starts empty."


def test_a_separate_grader_image_keeps_its_own_tests_directory_and_its_scripts_are_checked_inside_it(
        tmp_path, monkeypatch):
    probed = []
    monkeypatch.setattr(harbor_deliverable, "_has_file", lambda image, path, **_: probed.append(path.as_posix())
                        or path.as_posix() != "/tests/missing.sh")
    monkeypatch.setattr(HarborGraderEvaluator, "image", lambda self: "sha256:" + "c" * 64)
    pinned = "sha256:" + "e" * 64
    # The task has a top-level tests/ without these scripts; the grader image has them in its own /tests.
    grader = evaluator(tmp_path, grader=pinned, feedback="/tests/intermediate.sh", sealed="/tests/final.sh")
    assert grader.mount_tests is False
    (tmp_path / "state").mkdir()
    (tmp_path / "state/grader.json").write_text(json.dumps({"definition": grader.definition, "image": pinned}))
    grader.prepare()
    assert probed == ["/tests/intermediate.sh", "/tests/final.sh"]
    grader.prepare()  # recorded once
    assert len(probed) == 2
    missing = evaluator(tmp_path / "missing", grader=pinned, feedback="/tests/missing.sh", sealed=None)
    (tmp_path / "missing/state").mkdir(parents=True)
    (tmp_path / "missing/state/grader.json").write_text(json.dumps({"definition": missing.definition,
                                                                    "image": pinned}))
    with pytest.raises(ValueError, match="grader image has no /tests/missing.sh"):
        missing.prepare()
    # The task's main image is graded with the task's tests/ mounted, and checked against it at once.
    assert evaluator(tmp_path / "main").mount_tests is True
    with pytest.raises(ValueError, match="not in the Harbor task's tests/"):
        evaluator(tmp_path / "absent", feedback="/tests/intermediate.sh")


def test_the_seed_is_read_from_the_agent_ready_image_when_the_settings_name_one(tmp_path, monkeypatch):
    read = []

    def fake_shipped(image, path, destination, **_):
        read.append(image)
        destination.mkdir()
        return destination

    monkeypatch.setattr(harbor_deliverable, "shipped", fake_shipped)
    monkeypatch.setattr(harbor_deliverable, "_has_file", lambda image, path, **_: True)
    monkeypatch.setattr(HarborGraderEvaluator, "image", lambda self: "sha256:" + "c" * 64)
    agents = "sha256:" + "f" * 64
    (tmp_path / "seed").mkdir()
    grader = evaluator(tmp_path, grader="sha256:" + "e" * 64, seed=tmp_path / "seed", task_image=agents)
    (tmp_path / "state").mkdir()
    (tmp_path / "state/grader.json").write_text(json.dumps({"definition": grader.definition, "image": "x"}))
    grader.prepare()
    assert read == [agents]
    with pytest.raises(ValueError, match="task_image must be an image ID"):
        evaluator(tmp_path / "tagged", task_image="python:3.13")


def test_a_grader_needing_more_cpus_than_the_host_is_refused_unless_fewer_are_accepted(tmp_path, monkeypatch):
    monkeypatch.setattr(harbor_deliverable.os, "cpu_count", lambda: 4)
    task = harbor_task(tmp_path / "harbor-task", **{"task.toml": '[environment]\ncpus = 8\n'})
    with pytest.raises(ValueError, match="asks for 8 CPUs and this machine has 4.*cpus = 4 under"):
        evaluator(tmp_path, task=task)
    assert evaluator(tmp_path / "accepted", task=task, cpus=4).cpus == 4
    with pytest.raises(ValueError, match="at most this machine's 4 CPUs"):
        evaluator(tmp_path / "more", task=task, cpus=6)
    small = harbor_task(tmp_path / "small", **{"task.toml": '[environment]\ncpus = 2\n'})
    assert evaluator(tmp_path / "s", task=small).cpus == 2


def test_a_prebuilt_grader_image_still_leaves_the_graders_directory_out_of_the_agents_container(tmp_path):
    pytest.importorskip("harbor")
    files = {"seed/answer.txt": "x\n",
             "harbor-task/environment/grader/Dockerfile": f"FROM {BASE}\nCOPY hidden.txt /tests/hidden.txt\n",
             "harbor-task/environment/grader/hidden.txt": "the answers\n"}
    prebuilt = "sha256:" + "e" * 64
    settings = ("reviewer = false\n" + RESEARCHER.replace("86400", "600")
                + f'[task]\ngrader_image = "{prebuilt}"\n')
    run = imported(tmp_path, "feedback='/tests/feedback.sh', grader='environment/grader', check_seed=False",
                   settings, **files)
    assert run.task.evaluator.prebuilt == prebuilt and run.task.evaluator.context.name == "grader"
    staged = sorted(path.relative_to(tmp_path / "run/environment").as_posix()
                    for path in (tmp_path / "run/environment").rglob("*") if path.is_file())
    assert staged == ["Dockerfile", "public.txt"]
    with pytest.raises(ValueError, match="name that directory as grader"):
        evaluator(tmp_path / "image-only", grader=prebuilt, grader_image=prebuilt)


def test_an_imported_task_tells_the_researcher_its_deliverable_is_writable(tmp_path):
    pytest.importorskip("harbor")
    settings = "reviewer = false\n" + RESEARCHER.replace("86400", "600")
    run = imported(tmp_path, "deliverable='/app/output', feedback='/tests/feedback.sh', check_seed=False", settings,
                   **{"seed/answer.txt": "x\n"})
    assert "Only `/app/output`, `/workspace` and `/tmp` are writable." in run.inputs.specification()


AGENT_ALLOWLIST = ('[agent]\nnetwork_mode = "allowlist"\nallowed_hosts = ["api.anthropic.com", "grader"]\n')


def test_the_agents_keep_the_tasks_agent_allowlist_without_its_own_services(tmp_path):
    pytest.importorskip("harbor")
    task = harbor_task(tmp_path / "harbor-task", **{
        "task.toml": '[environment]\nworkdir = "/app"\nnetwork_mode = "public"\n' + AGENT_ALLOWLIST,
        "environment/docker-compose.yaml": f"services:\n  grader:\n    image: {BASE}\n",
    })
    derived = harbor_deliverable.researcher_environment(task, tmp_path / "derived", allowed_hosts=["openrouter.ai"])
    loaded = launch.harbor_environment.load(derived)
    assert loaded.agent_network == {"network_mode": "allowlist",
                                    "allowed_hosts": ["api.anthropic.com", "openrouter.ai"]}
    assert loaded.config["network_mode"] == "public"  # setup keeps the task's own baseline
    public = harbor_task(tmp_path / "public", **{"task.toml": '[environment]\nworkdir = "/app"\n'})
    assert launch.harbor_environment.load(
        harbor_deliverable.researcher_environment(public, tmp_path / "plain")).agent_network is None
    with pytest.raises(ValueError, match="adds to the task's agent allowlist"):
        harbor_deliverable.researcher_environment(public, tmp_path / "extra", allowed_hosts=["openrouter.ai"])


def test_the_researcher_is_told_what_its_network_reaches_and_the_policy_is_in_the_plan(tmp_path):
    pytest.importorskip("harbor")
    settings = "reviewer = false\n" + RESEARCHER.replace("86400", "600") + '[task]\nallowed_hosts = ["openrouter.ai"]\n'
    toml = '[environment]\nworkdir = "/app"\n' + AGENT_ALLOWLIST
    run = imported(tmp_path, "deliverable='/app/output', feedback='/tests/feedback.sh', check_seed=False", settings,
                   **{"seed/answer.txt": "x\n", "harbor-task/task.toml": toml,
                      "harbor-task/environment/docker-compose.yaml": f"services:\n  grader:\n    image: {BASE}\n"})
    assert ("While you work, this container's network reaches only: `api.anthropic.com`, `openrouter.ai`."
            in run.inputs.specification())
    assert run.environment.agent_network["allowed_hosts"] == ["api.anthropic.com", "openrouter.ai"]
    plan = json.loads((tmp_path / "run/plan.json").read_text())
    other = launch.harbor_environment.HarborEnvironment(run.environment.config, run.environment.files)
    assert run.environment.identity != other.identity  # a changed policy is a changed environment
    assert plan["inputs"]["environment"]["harbor"] is not None


def test_each_agent_start_writes_the_agent_network_policy_into_its_harbor_task(tmp_path, monkeypatch):
    pytest.importorskip("harbor")
    from harbor.job import Job

    from autoresearch_infrastructure.harbor_filesystem import run_filesystem_agent

    async def create(config):
        async def run():
            return SimpleNamespace(trial_results=[SimpleNamespace(exception_info=None, trial_name="fixture")])

        return SimpleNamespace(run=run)

    monkeypatch.setattr(Job, "create", create)
    environment = launch.harbor_environment.HarborEnvironment(
        {"docker_image": "sha256:" + "a" * 64}, None, tmp_path / "prepared.json",
        agent_network={"network_mode": "allowlist", "allowed_hosts": ["web"]})
    monkeypatch.setattr(launch.harbor_environment, "_image_present", lambda image: True)
    run_filesystem_agent(agent={"name": "nop", "model_name": "fixture"}, image=None, instruction="Fixture",
                         readonly={}, workspace=tmp_path / "workspace", evidence=tmp_path / "evidence",
                         jobs_root=tmp_path / "jobs", environment=environment)
    import tomllib

    written = tomllib.loads((tmp_path / "evidence/filesystem-task/task.toml").read_text())
    assert written["agent"]["network_mode"] == "allowlist" and written["agent"]["allowed_hosts"] == ["web"]


PROBE_AGENT = '''
from harbor.agents.base import BaseAgent
from harbor.agents.capabilities import AgentCapabilities

# Each server answers with its own name; anything else means the request was blocked.
PROBE = ("python -c \\"import sys, urllib.request\\n"
         "for host in ('web', 'other'):\\n"
         "    try:\\n"
         "        body = urllib.request.urlopen('http://' + host + '/', timeout=4).read().decode().strip()\\n"
         "    except Exception as error:\\n"
         "        body = type(error).__name__\\n"
         "    print(host, 'open' if body == 'hello-' + host else 'closed')\\" > /workspace/{phase}.txt")


class Probe(BaseAgent):
    capabilities = AgentCapabilities(skills=True)  # ARI's generated task always names a skills directory

    @staticmethod
    def name():
        return "network-probe"

    def version(self):
        return "1"

    async def setup(self, environment):
        await environment.exec(PROBE.format(phase="setup"))

    async def run(self, instruction, environment, context):
        await environment.exec(PROBE.format(phase="run"))
'''


@docker
def test_the_agent_allowlist_is_enforced_while_the_agent_runs_and_not_during_setup(tmp_path, monkeypatch):
    pytest.importorskip("harbor")
    from autoresearch_infrastructure.harbor_filesystem import run_filesystem_agent

    python = "python@sha256:a6e34c598f2467ed0e9a8d349809fcd8b5c603269512df273a0bb1784edc11b1"
    servers = "".join(
        f"  {name}:\n    image: {BASE}\n    networks: [default]\n"  # explicit networks: not egress-controlled
        f"    command: [\"sh\", \"-c\", \"echo hello-{name} > /tmp/index.html && httpd -f -p 80 -h /tmp\"]\n"
        for name in ("web", "other"))
    materialize(tmp_path / "env-task", {
        "task.toml": (f'[environment]\ndocker_image = "{python}"\ncpus = 1\n'
                      '[agent]\nnetwork_mode = "allowlist"\nallowed_hosts = ["web"]\n'),
        "environment/docker-compose.yaml": "services:\n" + servers,
    })
    loaded = launch.harbor_environment.load(tmp_path / "env-task")
    assert loaded.agent_network == {"network_mode": "allowlist", "allowed_hosts": ["web"]}
    environment = launch.harbor_environment.HarborEnvironment(
        loaded.config, loaded.files, tmp_path / "prepared.json", agent_network=loaded.agent_network)
    (tmp_path / "agents").mkdir()
    (tmp_path / "agents/probe_agent.py").write_text(PROBE_AGENT)
    monkeypatch.syspath_prepend(str(tmp_path / "agents"))
    before = subprocess.run(["docker", "ps", "-aq"], capture_output=True, text=True).stdout
    workspace = tmp_path / "workspace"
    result = run_filesystem_agent(
        agent={"import_path": "probe_agent:Probe", "model_name": "fixture"}, image=None, instruction="Probe",
        readonly={}, workspace=workspace, evidence=tmp_path / "evidence", jobs_root=tmp_path / "jobs",
        environment=environment, role="network-probe")
    assert result.exception_info is None, result.exception_info
    assert (workspace / "setup.txt").read_text().split() == ["web", "open", "other", "open"]
    assert (workspace / "run.txt").read_text().split() == ["web", "open", "other", "closed"]
    assert subprocess.run(["docker", "ps", "-aq"], capture_output=True, text=True).stdout == before


def test_the_grading_timeout_defaults_to_the_verifiers_and_can_be_set(tmp_path):
    assert evaluator(tmp_path).timeout == 30  # the fixture's [verifier] timeout_sec
    assert evaluator(tmp_path / "set", timeout=2700).timeout == 2700
    for bad in (0, -1, 86401, "600"):
        with pytest.raises(ValueError, match="grading timeout"):
            evaluator(tmp_path / f"bad{bad}", timeout=bad)


@docker
def test_a_script_past_its_timeout_leaves_the_evaluation_unfinished_and_no_container(tmp_path):
    task = harbor_task(tmp_path / "harbor-task", **{"tests/feedback.sh": "sleep 30\n"})
    grader = evaluator(tmp_path, task=task, sealed=None, timeout=2)
    (tmp_path / "candidate").mkdir()
    before = subprocess.run(["docker", "ps", "-aq"], capture_output=True, text=True).stdout
    with pytest.raises(RuntimeError, match="ran past 2 seconds"):
        grader.grade(tmp_path / "candidate", "feedback", tmp_path / "evidence")
    assert subprocess.run(["docker", "ps", "-aq"], capture_output=True, text=True).stdout == before


@docker
def test_containers_left_by_a_grader_process_that_died_are_removed_before_the_next_grading(tmp_path):
    task = harbor_task(tmp_path / "harbor-task")
    grader = evaluator(tmp_path, task=task, sealed=None)
    image = grader.image()
    other = evaluator(tmp_path / "another-run", task=task, sealed=None)
    assert grader.label != other.label  # each run has its own label

    def make(label):
        return subprocess.run(["docker", "create", "--label", label, image, "true"], capture_output=True,
                              text=True, check=True).stdout.strip()

    leftover, unrelated = make(grader.label), make(other.label)
    try:
        grader.prepare()
        listed = subprocess.run(["docker", "ps", "--all", "--quiet", "--no-trunc"], capture_output=True,
                                text=True).stdout.split()
        assert leftover not in listed and unrelated in listed
        again = make(grader.label)
        grader.prepare()  # only once per process: a container this process made is never swept
        assert again in subprocess.run(["docker", "ps", "--all", "--quiet", "--no-trunc"], capture_output=True,
                                       text=True).stdout.split()
        subprocess.run(["docker", "rm", "--force", again], capture_output=True)
        (tmp_path / "candidate").mkdir()
        (tmp_path / "candidate/answer.txt").write_text("x\n")
        assert grader.grade(tmp_path / "candidate", "feedback", tmp_path / "evidence") == {"reward": 0.25}
        assert not subprocess.run(["docker", "ps", "--all", "--quiet", "--filter", f"label={grader.label}"],
                                  capture_output=True, text=True).stdout.split()
    finally:
        subprocess.run(["docker", "rm", "--force", leftover, unrelated], capture_output=True)


def test_the_sweep_is_skipped_where_there_is_no_docker(tmp_path, monkeypatch):
    def missing(*arguments, **options):
        raise FileNotFoundError("docker")

    grader = evaluator(tmp_path)
    monkeypatch.setattr(harbor_deliverable.subprocess, "run", missing)
    grader._sweep()  # does not raise; the image build is what reports the missing docker
    assert grader._swept is False


def test_the_results_page_counts_an_empty_candidates_files(tmp_path, monkeypatch):
    from autoresearch_infrastructure.viewer import build_index

    monkeypatch.setattr(HarborGraderEvaluator, "grade", lambda self, candidate, script, evidence, case: {"reward": 0.0})
    monkeypatch.setattr(HarborGraderEvaluator, "image", lambda self: "sha256:" + "c" * 64)
    monkeypatch.setattr(HarborGraderEvaluator, "prepare", lambda self: None)
    harbor_task(tmp_path / "task/harbor-task")
    (tmp_path / "task/seed").mkdir()  # the starter is empty, as AutoResearchExam's often is
    (tmp_path / "task/task.py").write_text(
        "import autoresearch_infrastructure as ari\n\n\ndef task(run):\n"
        "    return ari.bases.harbor_deliverable(run, harbor_task='harbor-task', feedback='/tests/feedback.sh',"
        " sealed=None)\n")
    (tmp_path / "task/settings.toml").write_text("reviewer = false\n")
    run = launch.open_run(tmp_path / "task", tmp_path / "task/settings.toml", tmp_path / "run", None)
    with run.research:
        launch.record_workspace(run)
    session = next(entry for entry in build_index(tmp_path / "run/research")["sessions"] if entry["id"] == "research")
    assert session["candidates"][0]["file_count"] == 0


def test_the_agents_container_needing_more_cpus_than_the_host_is_refused_unless_fewer_are_accepted(
        tmp_path, monkeypatch):
    pytest.importorskip("harbor")
    monkeypatch.setattr(harbor_deliverable.os, "cpu_count", lambda: 4)
    task = harbor_task(tmp_path / "harbor-task", **{"task.toml": '[environment]\nworkdir = "/app"\ncpus = 8\n'})
    with pytest.raises(ValueError, match="container asks for 8 CPUs and this machine has 4.*cpus = 4"):
        harbor_deliverable.researcher_environment(task, tmp_path / "refused")
    derived = harbor_deliverable.researcher_environment(task, tmp_path / "accepted", cpus=4)
    assert launch.harbor_environment.load(derived).config["cpus"] == 4


def test_json_output_is_one_document_even_when_harbor_prints(tmp_path, monkeypatch, capsys):
    from autoresearch_infrastructure import cli

    def noisy(run):
        print("1/1 Mean: 0.000 progress from Harbor")
        return {"status": "checked", "candidates": []}

    monkeypatch.setattr(launch, "check_run", noisy)
    task = tmp_path / "task"
    shutil.copytree(EXAMPLE, task, ignore=shutil.ignore_patterns("__pycache__"))
    assert cli.main(["check", str(task), "--config", str(task / "local.toml"), "--output", str(tmp_path / "run"),
                     "--json"]) == 0
    captured = capsys.readouterr()
    assert json.loads(captured.out)["status"] == "checked" and "Mean" in captured.err


def test_a_missing_image_id_is_reported_as_missing_not_pulled(monkeypatch):
    calls = []
    monkeypatch.setattr(harbor_deliverable, "_image_present", lambda image: False)
    monkeypatch.setattr(harbor_deliverable, "_docker", lambda arguments: calls.append(arguments))
    with pytest.raises(ValueError, match="not on this machine, and an image ID cannot be pulled"):
        harbor_deliverable._resolve("sha256:" + "a" * 64, None)
    assert calls == []
    harbor_deliverable._resolve("registry.example/grader@sha256:" + "a" * 64, None)
    assert calls == [["pull", "--quiet", "registry.example/grader@sha256:" + "a" * 64]]


def are_task(root, *, gpus=0, paths="/app/output", timeout="1800"):
    """A minimal task in AutoResearchExam's layout."""
    materialize(root, {
        "task.toml": f'[environment]\ncpus = 8\ngpus = {gpus}\n[agent]\nnetwork_mode = "allowlist"\n'
                     'allowed_hosts = ["api.openai.com", "grader"]\n',
        "instruction.md": "Write /app/output/answer.txt.\n",
        "environment/Dockerfile": f"FROM {BASE}\nRUN mkdir -p /app/output\n",
        "environment/docker-compose.yaml": (
            "services:\n  main: {}\n  grader:\n    environment:\n"
            f"      ARE_ARTIFACT_PATHS: \"{paths}\"\n      ARE_GRADE_TIMEOUT_SEC: \"{timeout}\"\n"),
        "environment/are-grader-ctx/Dockerfile": f"FROM {BASE}\nCOPY . /tests\n",
        "environment/are-grader-ctx/intermediate.sh": "echo 0.5 > /logs/verifier/reward.txt\n",
        "environment/are-grader-ctx/test.sh": "echo 0.25 > /logs/verifier/reward.txt\n",
    })
    return root


def test_the_autoresearchexam_importer_derives_task_py_from_the_tasks_own_files(tmp_path, monkeypatch):
    sys.path.insert(0, str(Path(__file__).parents[1] / "examples/task-bank/autoresearchexam"))
    try:
        import import_task
    finally:
        sys.path.pop(0)
    source = are_task(tmp_path / "source")
    found = import_task.derive(source)
    assert (found["deliverable"], found["timeout"], found["problem"]) == ("/app/output", 1800, None)
    assert "needs 1 GPU" in import_task.derive(are_task(tmp_path / "gpu", gpus=1))["problem"]
    assert "names 2 deliverables" in import_task.derive(are_task(tmp_path / "two", paths="/a:/b"))["problem"]
    monkeypatch.setattr(import_task.os, "cpu_count", lambda: 4)
    assert import_task.main([str(source), str(tmp_path / "imported")]) == 0
    written = (tmp_path / "imported/task.py").read_text()
    for name in ("FOWNER", "KILL", "SETUID", "DAC_OVERRIDE"):  # the grader's root defaults, which it uses
        assert f'"{name}"' in written or f"'{name}'" in written
    assert "timeout=1800" in written and "deliverable='/app/output'" in written
    assert "cpus = 4" in (tmp_path / "imported/local.toml").read_text()
    (tmp_path / "imported/seed").mkdir()
    run = launch.open_run(tmp_path / "imported", tmp_path / "imported/local.toml", tmp_path / "run", None)
    assert run.task.evaluator.scripts["sealed"].as_posix() == "/tests/test.sh"
    assert run.task.evaluator.mount_tests is False  # the grader image keeps its own /tests
