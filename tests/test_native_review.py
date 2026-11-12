"""Opt-in native Harbor review/terminal fixture; model responses are local constants."""

import json
import os
import shlex

import pytest

import autoresearch_infrastructure as ari

pytestmark = pytest.mark.skipif(
    os.environ.get("COEVOLUTION_DOCKER_TESTS") != "1" or not os.environ.get("ARI_REVIEW_FIXTURE_IMAGE"),
    reason="opt-in Docker review: build fixtures/harbor-review and set ARI_REVIEW_FIXTURE_IMAGE",
)
QUALITY = ari.Metric("quality")


def evaluate(candidate, context):
    return ari.Evaluation({QUALITY: 1})


def test_native_harbor_reviewer_writes_and_binds_the_file_verdict(tmp_path, monkeypatch):
    pytest.importorskip("harbor")
    import litellm
    from harbor.llms.base import LLMResponse
    from harbor.llms.lite_llm import LiteLLM
    from harbor.models.metric import UsageInfo

    image = os.environ["ARI_REVIEW_FIXTURE_IMAGE"]
    monkeypatch.setenv("OPENROUTER_API_KEY", "local-fixture-not-a-provider-credential")

    async def forbidden(*args, **kwargs):
        pytest.fail("Native review fixture must never call a provider")

    monkeypatch.setattr(litellm, "acompletion", forbidden)
    calls = []
    script = '''import json
from pathlib import Path
v=json.loads(Path('/review/verdict-template.json').read_text())
assert Path('/review/candidate/value').read_text() == 'fixture'
assert Path('/review/baseline/value').read_text() == 'fixture'
v['verdict']='valid'
v['reasons']=['Local native-terminal fixture']
for check in v['checks']:
    check['status']='pass'
    check['evidence_paths']=['/review/candidate/value']
p=Path('/workspace/validity/verdict.json'); p.parent.mkdir(parents=True,exist_ok=True)
p.write_text(json.dumps(v))
'''

    async def respond(self, prompt, **kwargs):
        assert self._model_name == "openrouter/openai/gpt-6-luna"
        calls.append(prompt)
        commands = [{"keystrokes": "python -c " + shlex.quote(script) + "\n", "duration": 0.5}] if len(calls) == 1 else []
        return LLMResponse(content=json.dumps({"analysis": "Local fixture", "plan": "Write the verdict",
                                             "commands": commands, "task_complete": len(calls) > 1}),
                           usage=UsageInfo(prompt_tokens=1, completion_tokens=1, cache_tokens=0, cost_usd=0))

    monkeypatch.setattr(LiteLLM, "call", respond)
    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "value").write_text("fixture")
    policy = ari.LLMValidity({"scope": "The candidate contains the fixture value."}, image=image,
        agent={"name": "terminus-2", "model_name": "openrouter/openai/gpt-6-luna",
               "override_timeout_sec": 60, "override_setup_timeout_sec": 60,
               "kwargs": {"record_terminal_session": False}}, max_reviews=1)
    with ari.Research(tmp_path / "run", seed=seed, objective=QUALITY, evaluator=evaluate,
                      validity=policy, evaluation_config={}) as research:
        recorded = research.record_candidate()
        assert recorded["status"] == "valid", recorded
        assert recorded["evidence"]["evaluation"] == "completed"
        assert calls
        metadata = list(research.root.rglob("trajectory-metadata.json"))
        assert len(metadata) == 1
        record = json.loads(metadata[0].read_text())
        native = research.output / "harbor-jobs" / record["viewer_job"] / record["viewer_trial"] / "agent/trajectory.json"
        assert json.loads(native.read_text())["agent"]["name"] == "terminus-2"
