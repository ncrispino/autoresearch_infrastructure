"""Real Harbor researcher/MCP/container transport with local fixed model responses."""

import json
import os
import shlex

import pytest

import autoresearch_infrastructure as ari
from autoresearch_infrastructure.coding_agent import native_agent_cost
from autoresearch_infrastructure.ledger import StartLedger

pytestmark = pytest.mark.skipif(
    os.environ.get("COEVOLUTION_DOCKER_TESTS") != "1" or not os.environ.get("ARI_REVIEW_FIXTURE_IMAGE"),
    reason="opt-in native researcher transport; requires the Python/tmux review fixture image",
)
ERROR = ari.Metric("error", "min")


def valid(candidate, context):
    return ari.Validity()


def evaluate(candidate, context):
    return ari.Evaluation({ERROR: float((candidate / "value").read_text())})


def test_native_researcher_edits_and_records_with_original_atif(tmp_path, monkeypatch):
    pytest.importorskip("harbor")
    import litellm
    from harbor.llms.base import LLMResponse
    from harbor.llms.lite_llm import LiteLLM
    from harbor.models.metric import UsageInfo

    monkeypatch.setenv("OPENROUTER_API_KEY", "local-fixture-not-a-provider-credential")

    async def forbidden(*args, **kwargs):
        pytest.fail("Native researcher transport fixture must not call a provider")

    monkeypatch.setattr(litellm, "acompletion", forbidden)
    current = {"invocation": 0, "responses": 0}
    calls = []

    async def respond(self, prompt, **kwargs):
        assert self._model_name == "openrouter/openai/gpt-6-luna"
        calls.append(prompt)
        first = current["responses"] == 0
        current["responses"] += 1
        script = '''import json, subprocess
from pathlib import Path
assert Path('/context/rule.txt').read_text() == 'Read-only trusted guidance'
assert 'Native injected skill marker' in Path('/tmp/harbor-skills/fixture-skill/SKILL.md').read_text()
try:
    Path('/context/rule.txt').write_text('changed')
except OSError:
    pass
else:
    raise AssertionError('Researcher context must be read-only')
Path('/workspace/candidate/value').write_text(VALUE)
Path('/tmp/proposal.json').write_text(json.dumps({'candidate':'workspace',
    'proposal':{'hypothesis':"Baseline's native Harbor fixture edit"}}))
response=subprocess.run(['python','/opt/autoresearch/mcp_proxy.py',
    '--call','/tmp/proposal.json'],capture_output=True,text=True,check=True)
record=json.loads(response.stdout)
assert record['status']=='valid', record
assert record['evaluation']['evaluation']=='completed', record
Path('/workspace/receipt.json').write_text(json.dumps(record))
'''.replace("VALUE", repr(str(1 - current["invocation"])))
        commands = [{"keystrokes": "python -c " + shlex.quote(script) + "\n", "duration": 0.5}] if first else []
        return LLMResponse(
            content=json.dumps({"analysis": "Local transport fixture", "plan": "Edit and record",
                                "commands": commands, "task_complete": not first}),
            usage=UsageInfo(prompt_tokens=1, completion_tokens=1, cache_tokens=0, cost_usd=0),
        )

    monkeypatch.setattr(LiteLLM, "call", respond)
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "value").write_text("2")
    context = tmp_path / "context"
    context.mkdir()
    (context / "rule.txt").write_text("Read-only trusted guidance")
    skill = tmp_path / "fixture-skill"
    skill.mkdir()
    skill_text = ("---\nname: fixture-skill\ndescription: Native injected skill marker\n---\n"
                  "Inspect the candidate before submitting it.\n")
    (skill / "SKILL.md").write_text(skill_text)
    ledger = StartLedger(tmp_path / "researcher-starts.json", limits={"researcher": 2}, identity={"fixture": 1})
    agent = ari.CodingAgent(
        image=os.environ["ARI_REVIEW_FIXTURE_IMAGE"],
        agent={"name": "terminus-2", "model_name": "openrouter/openai/gpt-6-luna",
               "override_timeout_sec": 60, "override_setup_timeout_sec": 60,
               "skills": [skill],
               "kwargs": {"record_terminal_session": False}},
    )
    with ari.Research(
        tmp_path / "run", seed=seed, objective=ERROR, evaluator=evaluate, validity=valid,
        evaluation_config={"fixture": "native Harbor researcher transport"},
        limits=ari.Limits(max_candidates=3, max_evaluations=3, max_repeats=1),
    ) as research:
        research.record_candidate()
        for invocation in range(2):
            current.update(invocation=invocation, responses=0)
            evidence = research.root / "agent/invocations" / f"fixture-{invocation}"
            key = ledger.start("researcher", str(research.output))
            status = "failed"
            try:
                metadata = agent.run(
                    "Use the autoresearch MCP server to edit and record one candidate, then stop.",
                    workspace=research.root / "workspace", evidence=evidence,
                    readonly={context: "/context"}, recorder=research,
                    jobs_root=research.output / "harbor-jobs",
                )
                status = "completed"
            finally:
                ledger.finish(key, status=status, cost_usd=native_agent_cost(evidence))
            receipt = json.loads((research.root / "workspace/receipt.json").read_text())
            assert receipt["evaluation"]["metrics"] == {"error": 1 - invocation}
            assert metadata["format"] == "ATIF"
            native = research.output / "harbor-jobs" / metadata["viewer_job"] / metadata["viewer_trial"] / "agent/trajectory.json"
            assert json.loads(native.read_text())["agent"]["name"] == "terminus-2"
            assert (evidence / "trajectory.json").read_bytes() == native.read_bytes()
        assert len(calls) >= 4
        assert "Native injected skill marker" in json.dumps(calls)
        assert research.budget_summary()["evaluations"]["used"] == 3
        assert research.state["sessions"]["research"]["selected"][research.contract_hash] == "candidate-0003"
    assert ledger.snapshot()["counts"] == {"researcher": 2}
    assert (context / "rule.txt").read_text() == "Read-only trusted guidance"
    assert (skill / "SKILL.md").read_text() == skill_text
