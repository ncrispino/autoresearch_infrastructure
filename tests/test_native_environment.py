"""Real Docker and Harbor: a researcher in a Harbor environment with a sidecar, with memory and retained files.

Model responses are local constants, so no provider is called. Opt in with
COEVOLUTION_DOCKER_TESTS=1 and ARI_REVIEW_FIXTURE_IMAGE (the image built from
tests/fixtures/harbor-review). The sidecar image is pulled by digest.
"""

import json
import os
import shlex
import subprocess

import pytest

from autoresearch_infrastructure import harbor_environment, launch

pytestmark = pytest.mark.skipif(
    os.environ.get("COEVOLUTION_DOCKER_TESTS") != "1" or not os.environ.get("ARI_REVIEW_FIXTURE_IMAGE"),
    reason="opt-in native environment test; requires Docker and the review fixture image",
)
SIDECAR = "busybox@sha256:73aaf090f3d85aa34ee199857f03fa3a95c8ede2ffd4cc2cdb5b94e566b11662"
TASK = '''
import json
from pathlib import Path

import autoresearch_infrastructure as ari

ERROR = ari.Metric("error", "min")


def evaluate(candidate, context):
    return ari.Evaluation({ERROR: abs(10 - json.loads((candidate / "value.json").read_text()))})


def task(run):
    return ari.Task(
        objective=ERROR, evaluator=evaluate,
        instructions=ari.Instructions(objective="Move value.json toward ten.", rules=["Hold one integer."]),
        environment=ari.Environment(harbor="env-task"),
        memory=ari.Memory(conversations=1, log=True),
    )
'''
SCRIPT = '''import json, socket, subprocess
from pathlib import Path
start = START
notes = Path('/workspace/notes.txt')
if start >= 2:
    assert notes.read_text() == f'left by start {start - 1}', 'retained files were not restored'
    assert len(list(Path('/history/conversations').glob('*.json'))) == 1, 'no carried conversation'
    assert f'candidate-000{start}' in Path('/history/log.md').read_text(), 'no research log'
else:
    assert not notes.exists()
socket.gethostbyname('cache')  # the sidecar service is on the start's network
notes.write_text(f'left by start {start}')
Path('/workspace/candidate/value.json').write_text(json.dumps(4 * start))
Path('/tmp/proposal.json').write_text(json.dumps({'candidate': 'workspace',
    'proposal': {'hypothesis': f'start {start}', 'mechanism': 'fixture'}}))
out = subprocess.run(['python', '/opt/autoresearch/mcp_proxy.py', '--call', '/tmp/proposal.json'],
                     capture_output=True, text=True, check=True)
assert json.loads(out.stdout)['status'] == 'valid', out.stdout
Path('/workspace/ok-' + str(start)).write_text('ok')
'''


def pull_sidecar():
    if subprocess.run(["docker", "image", "inspect", SIDECAR], capture_output=True).returncode == 0:
        return
    for _ in range(4):
        if subprocess.run(["docker", "pull", "-q", SIDECAR], capture_output=True).returncode == 0:
            return
    pytest.fail(f"Could not pull {SIDECAR}")


def containers():
    result = subprocess.run(["docker", "ps", "-aq"], capture_output=True, text=True, check=True)
    return set(result.stdout.split())


def test_researcher_runs_in_a_prepared_harbor_environment_with_memory_and_retained_files(tmp_path, monkeypatch):
    pytest.importorskip("harbor")
    import litellm
    from harbor.llms.base import LLMResponse
    from harbor.llms.lite_llm import LiteLLM
    from harbor.models.metric import UsageInfo

    image = os.environ["ARI_REVIEW_FIXTURE_IMAGE"]
    monkeypatch.setenv("OPENROUTER_API_KEY", "local-fixture-not-a-provider-credential")

    async def forbidden(*args, **kwargs):
        pytest.fail("The native environment fixture must not call a provider")

    monkeypatch.setattr(litellm, "acompletion", forbidden)
    starts = []

    async def respond(self, prompt, **kwargs):
        first = "## Objective" in prompt
        if first:
            starts.append(prompt)
        script = SCRIPT.replace("START", str(len(starts)))
        commands = [{"keystrokes": "python -c " + shlex.quote(script) + "\n", "duration": 1.0}] if first else []
        return LLMResponse(
            content=json.dumps({"analysis": "Local fixture", "plan": "Edit and record",
                                "commands": commands, "task_complete": not first}),
            usage=UsageInfo(prompt_tokens=1, completion_tokens=1, cache_tokens=0, cost_usd=0),
        )

    monkeypatch.setattr(LiteLLM, "call", respond)
    pull_sidecar()
    directory = tmp_path / "task"
    (directory / "seed").mkdir(parents=True)
    (directory / "seed/value.json").write_text("0")
    (directory / "task.py").write_text(TASK)
    (directory / "run.toml").write_text(
        'reviewer = false\n[limits]\ncandidates = 3\nevaluations = 3\n[researcher]\nname = "terminus-2"\n'
        'model_name = "openrouter/openai/gpt-6-luna"\noverride_timeout_sec = 120\n'
        'override_setup_timeout_sec = 120\nkwargs = { record_terminal_session = false }\n')
    environment = directory / "env-task"
    (environment / "environment").mkdir(parents=True)
    (environment / "task.toml").write_text(f'[environment]\ndocker_image = "{image}"\ncpus = 1\n')
    (environment / "environment/docker-compose.yaml").write_text(
        f"services:\n  cache:\n    image: {SIDECAR}\n    command: [\"sleep\", \"600\"]\n")
    before = containers()

    opened = launch.open_run(directory, directory / "run.toml", tmp_path / "run", None)
    with opened.research:
        summary = launch.run_researchers(opened)
    assert summary["status"] == "completed", summary.get("error")
    assert len(starts) == 2
    assert [row["metrics"] for row in summary["candidates"]] == [{"error": 10}, {"error": 6}, {"error": 2}]
    prepared = json.loads((tmp_path / "run/prepared/environment.json").read_text())
    assert prepared == {"definition": opened.environment.identity, "main": image, "built": False,
                        "sidecars": {"cache": SIDECAR}}
    root = tmp_path / "run/research/sessions/research"
    records = sorted((root / "agent/inputs").glob("*/inputs.json"), key=lambda p: p.stat().st_mtime_ns)
    second = json.loads(records[1].read_text())
    assert second["restored"]["path"].startswith("agent/retained/")
    assert second["environment"]["sidecars"] == {"cache": SIDECAR}
    assert (root / "workspace/ok-2").is_file()
    assert containers() == before  # every start's containers and services were torn down

    # Extend the finished run by one start: memory and retained files carry over, nothing is re-run.
    (directory / "more.toml").write_text((directory / "run.toml").read_text().replace(
        "candidates = 3\nevaluations = 3", "candidates = 4\nevaluations = 4"))
    extended = launch.open_run(directory, directory / "more.toml", tmp_path / "more", None,
                               extends=tmp_path / "run")
    with extended.research:
        more = launch.run_researchers(extended)
    assert more["status"] == "completed", more.get("error")
    assert len(starts) == 3 and more["candidates"][-1]["metrics"] == {"error": 2}
    assert [row["candidate_id"] for row in more["candidates"]] == [f"candidate-000{n}" for n in range(1, 5)]
    assert more["starts"] == {"researcher": 3}
    assert containers() == before

    # A conversation ARI carries is a trajectory Harbor's own loader accepts for native continuation.
    from harbor.agents.factory import AgentFactory
    from harbor.models.trial.config import AgentConfig

    log = json.loads((root / "agent/researcher-starts.json").read_text())["starts"]
    saved = root / log[0]["trajectory"]
    # Harbor's trial passes load_trajectory to the agent this way when it starts one.
    agent = AgentFactory.create_agent_from_config(
        AgentConfig(name="claude-code", model_name="anthropic/fixture", load_trajectory=str(saved)),
        logs_dir=tmp_path / "logs", load_trajectory=str(saved))
    assert agent._atif_load_trajectory is not None and agent._atif_load_trajectory.steps


def test_prepare_builds_a_dockerfile_and_records_the_image_it_built(tmp_path):
    directory = tmp_path / "env-task"
    (directory / "environment").mkdir(parents=True)
    (directory / "task.toml").write_text("[environment]\ncpus = 1\n")
    (directory / "environment/Dockerfile").write_text(f"FROM {SIDECAR}\nRUN echo prepared > /prepared.txt\n")
    loaded = harbor_environment.load(directory)
    environment = harbor_environment.HarborEnvironment(loaded.config, loaded.files, tmp_path / "prepared.json")
    prepared = harbor_environment.prepare(environment)
    inspected = subprocess.run(["docker", "image", "inspect", "-f", "{{.Id}}", prepared["main"]],
                               capture_output=True, text=True, check=True).stdout.strip()
    assert prepared["built"] is True and inspected == prepared["main"]
    output = subprocess.run(["docker", "run", "--rm", prepared["main"], "cat", "/prepared.txt"],
                            capture_output=True, text=True, check=True).stdout
    assert output == "prepared\n"
    assert harbor_environment.prepare(environment) == prepared  # reused, not rebuilt


def test_reviewer_runs_in_its_own_prepared_harbor_environment(tmp_path, monkeypatch):
    pytest.importorskip("harbor")
    import litellm
    from harbor.llms.base import LLMResponse
    from harbor.llms.lite_llm import LiteLLM
    from harbor.models.metric import UsageInfo

    import autoresearch_infrastructure as ari

    image = os.environ["ARI_REVIEW_FIXTURE_IMAGE"]
    monkeypatch.setenv("OPENROUTER_API_KEY", "local-fixture-not-a-provider-credential")

    async def forbidden(*args, **kwargs):
        pytest.fail("The native review fixture must not call a provider")

    monkeypatch.setattr(litellm, "acompletion", forbidden)
    script = """import json, socket
from pathlib import Path
socket.gethostbyname('cache')  # the reviewer's own sidecar
v = json.loads(Path('/review/verdict-template.json').read_text())
v['verdict'] = 'valid'
v['reasons'] = ['Local fixture in a Harbor environment']
for check in v['checks']:
    check['status'] = 'pass'
    check['evidence_paths'] = ['/review/candidate/value']
p = Path('/workspace/validity/verdict.json'); p.parent.mkdir(parents=True, exist_ok=True)
p.write_text(json.dumps(v))
"""
    calls = []

    async def respond(self, prompt, **kwargs):
        calls.append(prompt)
        commands = [{"keystrokes": "python -c " + shlex.quote(script) + "\n", "duration": 1.0}] \
            if len(calls) == 1 else []
        return LLMResponse(content=json.dumps({"analysis": "Local fixture", "plan": "Write the verdict",
                                               "commands": commands, "task_complete": len(calls) > 1}),
                           usage=UsageInfo(prompt_tokens=1, completion_tokens=1, cache_tokens=0, cost_usd=0))

    monkeypatch.setattr(LiteLLM, "call", respond)
    pull_sidecar()
    definition = tmp_path / "review-env"
    (definition / "environment").mkdir(parents=True)
    (definition / "task.toml").write_text(f'[environment]\ndocker_image = "{image}"\n')
    (definition / "environment/docker-compose.yaml").write_text(
        f"services:\n  cache:\n    image: {SIDECAR}\n    command: [\"sleep\", \"600\"]\n")
    loaded = harbor_environment.load(definition)
    environment = harbor_environment.HarborEnvironment(loaded.config, loaded.files, tmp_path / "prepared.json")
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "value").write_text("fixture")
    policy = ari.LLMValidity({"scope": "The candidate contains the fixture value."}, environment=environment,
                             agent={"name": "terminus-2", "model_name": "openrouter/openai/gpt-6-luna",
                                    "override_timeout_sec": 60, "override_setup_timeout_sec": 60,
                                    "kwargs": {"record_terminal_session": False}}, max_reviews=1)
    before = containers()
    with ari.Research(tmp_path / "run", seed=seed, objective=ari.Metric("quality"),
                      evaluator=lambda candidate, context: ari.Evaluation({ari.Metric("quality"): 1}),
                      validity=policy, evaluation_config={}) as research:
        recorded = research.record_candidate()
    assert recorded["status"] == "valid", recorded
    assert json.loads((tmp_path / "prepared.json").read_text())["sidecars"] == {"cache": SIDECAR}
    assert containers() == before
