"""Declarative LLM validity uses isolated-review wiring and strict files; no models."""

import json
import os
from pathlib import Path
from types import SimpleNamespace

import pytest

import autoresearch_infrastructure as ari
from autoresearch_infrastructure.ledger import StartLedger
from autoresearch_infrastructure.persistence import write_json

QUALITY = ari.Metric('quality')
IMAGE = 'sha256:' + 'a' * 64
pytest.importorskip('harbor')
AGENT = {'name': 'codex', 'model_name': 'gpt-6-luna'}
RULES = {'scope': 'Only edit the candidate value.', 'generality': 'Do not use hidden answers.'}


def evaluate(candidate, context):
    return ari.Evaluation({QUALITY: float((candidate / 'value').read_text())})


def make(root, validity):
    seed = root / 'seed'
    seed.mkdir(parents=True, exist_ok=True)
    (seed / 'value').write_text('1')
    return ari.Research(root / 'run', seed=seed, objective=QUALITY, validity=validity,
                        evaluator=evaluate, evaluation_config={})


@pytest.fixture
def agent(monkeypatch):
    calls = []
    outcomes = ['valid']

    from harbor.job import Job

    class FixtureJob:
        def __init__(self, config):
            self.config = config

        async def run(self):
            config = self.config
            assert config.n_attempts == config.n_concurrent_trials == 1
            assert config.retry.max_retries == 0 and config.verifier.disable
            from harbor.agents.factory import AgentFactory
            assert config.agents[0].name in AgentFactory.registered_names()
            mounts = config.environment.mounts
            workspace = Path(next(m['source'] for m in mounts if m['target'] == '/workspace'))
            readonly = {Path(m['source']): m['target'] for m in mounts if m.get('read_only')}
            assert set(readonly.values()) == {
                '/review/candidate', '/review/baseline', '/review/assignment.json', '/review/verdict-template.json',
            }
            task = config.tasks[0].path
            from harbor.models.task.task import Task
            assert Task.is_valid_dir(task, disable_verification=True)
            prompt = (task / 'instruction.md').read_text()
            assert 'Objective quality and task completion are separate' in prompt
            assert 'do not execute candidate code' in prompt
            assert 'Load /review/verdict-template.json as JSON' in prompt
            assert 'valid|invalid|inconclusive' not in prompt
            isolation = json.loads(config.environment.extra_docker_compose[0].read_text())['services']['main']
            assert isolation['read_only'] and isolation['cap_drop'] == ['ALL']
            assert isolation['user'] == f'{os.getuid()}:{os.getgid()}'
            calls.append({'config': config, 'workspace': workspace, 'readonly': readonly})
            assignment = json.loads((workspace.parent / 'assignment.json').read_text())
            template_path = next(source for source, target in readonly.items()
                                 if target == '/review/verdict-template.json')
            verdict = json.loads(template_path.read_text())
            assert verdict == {
                'candidate_hash': assignment['candidate_hash'],
                'contract_hash': assignment['contract_hash'],
                'verdict': 'invalid',
                'reasons': ['Review not completed.'],
                'checks': [{'criterion': name, 'status': 'fail', 'evidence_paths': []}
                           for name in assignment['required_criteria']],
            }
            outcome = outcomes.pop(0) if outcomes else 'valid'
            from harbor.models.trajectories import Trajectory
            trajectory = Trajectory(
                agent={'name': config.agents[0].name, 'version': 'fixture'},
                steps=[{'step_id': 1, 'source': 'agent', 'message': 'Fixture review'}],
            )
            native = config.jobs_dir / config.job_name / 'review/agent/trajectory.json'
            write_json(native, trajectory)
            trial = SimpleNamespace(trial_name='review', agent_result=SimpleNamespace(cost_usd=0.01),
                                    exception_info=None)
            if outcome == 'failure':
                trial.exception_info = SimpleNamespace(exception_type='FixtureTransportFailure')
            elif outcome == 'malformed':
                (workspace / 'validity/verdict.json').write_text('{')
            elif outcome != 'missing':
                if outcome != 'unreviewed':
                    verdict['verdict'] = 'invalid' if outcome == 'invalid' else 'valid'
                    verdict['reasons'] = ['Fixture independent review']
                    for check in verdict['checks']:
                        check['status'] = 'fail' if outcome == 'invalid' else 'pass'
                        check['evidence_paths'] = ['/review/candidate/value']
                if outcome == 'wrong-hash':
                    verdict['candidate_hash'] = '0' * 64
                if outcome == 'bad-evidence':
                    verdict['checks'][0]['evidence_paths'] = ['/controller/hidden-file']
                write_json(workspace / 'validity/verdict.json', verdict)
            return SimpleNamespace(trial_results=[trial])

    async def create(config):
        return FixtureJob(config)

    monkeypatch.setattr(Job, 'create', create)
    return calls, outcomes


def policy(**kwargs):
    return ari.LLMValidity(RULES, agent=AGENT, image=IMAGE, **kwargs)


@pytest.mark.parametrize('changes', [
    {'model_name': None},
    {'kwargs': {'unknown_native_option': True}},
    {'override_timeout_sec': 7201},
    {'override_setup_timeout_sec': float('inf')},
    {'override_setup_timeout_sec': 0},
    {'max_timeout_sec': float('nan')},
])
def test_invalid_native_reviewer_options_fail_before_charging_a_review(tmp_path, agent, changes):
    calls, _ = agent
    ledger = StartLedger(tmp_path / 'starts.json', limits={'reviewer': 1}, identity={'fixture': 'early-validation'})
    with pytest.raises(ValueError):
        ari.LLMValidity(RULES, agent={**AGENT, **changes}, image=IMAGE, ledger=ledger)
    assert calls == []
    assert ledger.snapshot()['counts'] == {'reviewer': 0}
    assert ledger.snapshot()['starts'] == {}


def test_json_policy_budget_independent_mounts_and_file_authority(tmp_path, agent):
    calls, _ = agent
    path = tmp_path / 'validity.json'
    write_json(path, RULES)
    validity = ari.LLMValidity(path, agent=AGENT, image=IMAGE, max_reviews=2)
    with make(tmp_path, validity) as research:
        research.check_metrics(ari.Evaluation({QUALITY: 1}))
        assert research.budget_summary()["reviews"] == {
            "limit": 2, "used": 0, "remaining": 2, "reserved": 0, "available_here": 2}
        assert calls == [] and not (research.output / 'validity-starts.json').exists()
        result = research.record_candidate()
        assert research.budget_summary()["reviews"] == {
            "limit": 2, "used": 1, "remaining": 1, "reserved": 0, "available_here": 1}
        assert result['status'] == 'valid' and result['evidence']['evaluation'] == 'completed'
        assert research.state['solver_trial_starts'] == 1
        ledger = json.loads((research.output / 'validity-starts.json').read_text())
        assert ledger['counts'] == {'reviewer': 1}
        assert next(iter(ledger['starts'].values()))['cost_usd'] == 0.01
        assert IMAGE in (calls[0]['config'].tasks[0].path / 'task.toml').read_text()
        assert calls[0]['config'].agents[0].model_name == 'gpt-6-luna'
        from hashlib import sha256
        metadata = json.loads((calls[0]['workspace'].parent / 'agent/trajectory-metadata.json').read_text())
        native = research.output / 'harbor-jobs' / metadata['viewer_job'] / metadata['viewer_trial'] / 'agent/trajectory.json'
        assert metadata['sha256'] == sha256(native.read_bytes()).hexdigest()
        assert json.loads(native.read_text())['agent']['name'] == 'codex'
        assert calls[0]['readonly'] == {
            research._verified_candidate_path('research', 'candidate-0001'): '/review/candidate',
            research.root / 'seed': '/review/baseline',
            calls[0]['workspace'].parent / 'assignment.json': '/review/assignment.json',
            calls[0]['workspace'].parent / 'verdict-template.json': '/review/verdict-template.json',
        }
    with make(tmp_path, validity) as resumed:
        resumed.record_candidate('candidate-0001')
        assert len(calls) == 1  # Cached accepted validity and evaluation are reused.


@pytest.mark.parametrize('outcome', ['invalid', 'unreviewed', 'missing', 'malformed', 'wrong-hash', 'bad-evidence'])
def test_rejected_or_malformed_review_never_starts_evaluator(tmp_path, agent, outcome):
    calls, outcomes = agent
    outcomes[:] = [outcome]
    with make(tmp_path, policy()) as research:
        result = research.record_candidate()
        assert result['status'] == 'invalid'
        assert research.state['solver_trial_starts'] == 0
        assert len(calls) == 1


def test_failed_review_is_charged_and_retry_cannot_reset_review_budget(tmp_path, agent):
    calls, outcomes = agent
    outcomes[:] = ['failure']
    validity = policy(max_reviews=1)
    with make(tmp_path, validity) as research:
        result = research.record_candidate()
        assert result['status'] == 'invalid'
        ledger = json.loads((research.output / 'validity-starts.json').read_text())
        start = next(iter(ledger['starts'].values()))
        assert start['status'] == 'failed' and start['cost_usd'] == 0.01
    with make(tmp_path, validity) as resumed:
        result = resumed.record_candidate('candidate-0001')
        assert result['status'] == 'invalid'
        assert resumed.state['solver_trial_starts'] == 0
        assert len(calls) == 1
        assert json.loads((resumed.output / 'validity-starts.json').read_text())['counts']['reviewer'] == 1


def test_shared_review_ledger_enforces_one_allocation_across_outputs(tmp_path, agent):
    calls, _ = agent
    ledger = StartLedger(tmp_path / 'shared.json', limits={'reviewer': 1}, identity={'study': 'fixture'})
    validity = policy(ledger=ledger)
    with make(tmp_path / 'a', validity) as first, make(tmp_path / 'b', validity) as second:
        assert first.record_candidate()['status'] == 'valid'
        assert second.record_candidate()['status'] == 'invalid'
        assert second.state['solver_trial_starts'] == 0
    assert len(calls) == 1 and ledger.snapshot()['counts']['reviewer'] == 1


def test_reusing_policy_does_not_redirect_independent_output_budgets(tmp_path, agent):
    calls, _ = agent
    validity = policy(max_reviews=1)
    with make(tmp_path / 'a', validity) as first, make(tmp_path / 'b', validity) as second:
        assert first.record_candidate()['status'] == second.record_candidate()['status'] == 'valid'
        for research in (first, second):
            assert json.loads((research.output / 'validity-starts.json').read_text())['counts']['reviewer'] == 1
    assert len(calls) == 2


def test_policy_and_backend_settings_are_frozen_in_research_contract(tmp_path, agent):
    with make(tmp_path, policy()):
        pass
    changed = ari.LLMValidity({**RULES, 'scope': 'Changed rule'}, agent=AGENT, image=IMAGE)
    with pytest.raises(ValueError, match='setup changed'):
        make(tmp_path, changed)
    with pytest.raises(ValueError, match='setup changed'):
        make(tmp_path, ari.LLMValidity(RULES, agent=AGENT, image='sha256:' + 'b' * 64))


def test_mutating_reused_policy_agent_does_not_change_active_review_contract(tmp_path, agent):
    calls, _ = agent
    validity = ari.LLMValidity(
        RULES, agent={**AGENT, 'kwargs': {'reasoning_effort': 'low'}}, image=IMAGE,
    )
    with make(tmp_path, validity) as research:
        contract = (research.output / 'contract.json').read_bytes()
        contract_hash = research.contract_hash
        validity.agent.model_name = 'changed-after-contract'
        validity.agent.kwargs['reasoning_effort'] = 'high'
        assert research.record_candidate()['status'] == 'valid'
        actual = calls[0]['config'].agents[0]
        assert actual.model_name == AGENT['model_name']
        assert actual.kwargs == {'reasoning_effort': 'low'}
        assert research.contract_hash == contract_hash
        assert (research.output / 'contract.json').read_bytes() == contract


@pytest.mark.parametrize('rules', [{}, {'scope': ''}, {'scope': {'rule': 'nested'}}, {'scope': 1}, {1: 'rule'}])
def test_malformed_declarative_policy_fails_before_launch(rules):
    with pytest.raises(ValueError, match='criteria'):
        ari.LLMValidity(rules, agent=AGENT, image=IMAGE)


def test_json_policy_rejects_duplicate_keys_and_mutable_image(tmp_path):
    path = tmp_path / 'rules.json'
    path.write_text('{"scope":"first", "scope":"second"}')
    with pytest.raises(ValueError, match='Duplicate'):
        ari.LLMValidity(path, agent=AGENT, image=IMAGE)
    with pytest.raises(ValueError, match='immutable image'):
        ari.LLMValidity(RULES, agent=AGENT, image='ari-solver:latest')


@pytest.mark.parametrize('name', ['codex', 'claude-code', 'gemini-cli'])
def test_review_uses_the_requested_first_class_harbor_agent(tmp_path, agent, name):
    calls, _ = agent
    validity = ari.LLMValidity(RULES, agent={'name': name, 'model_name': 'fixture-model'}, image=IMAGE)
    with make(tmp_path, validity) as research:
        assert research.record_candidate()['status'] == 'valid'
    assert calls[0]['config'].agents[0].name == name
