"""Fixed-program admission recovery: deterministic fixtures, never paid models."""

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from autoresearch_infrastructure import Evaluation, Limits, Research, Validity, ValidityContext, pilot
from autoresearch_infrastructure.harbor_trial import SOLVER_TOKENS, SUCCESS_RATE
from autoresearch_infrastructure.ledger import StartLedger, StartLimitError
from autoresearch_infrastructure.persistence import digest, write_json
from autoresearch_infrastructure.source_tree import materialize, snapshot

pytest.importorskip("harbor")


def evaluate_program(candidate, context):
    return Evaluation({SUCCESS_RATE: 1})


def test_pilot_uses_shared_reviewer_with_trusted_context_and_frozen_baseline(tmp_path, monkeypatch):
    pytest.importorskip("harbor")
    seed = tmp_path / "seed"
    materialize(seed, {
        "program.toml": 'id="fixture"\ndescription="Fixture program"\nentrypoint="solve.py:solve"\n',
        "solve.py": "from autoresearch_infrastructure.programs import SolverOutput\nasync def solve(task, runtime):\n    return SolverOutput('fixture')\n",
    })
    calls = []

    def review(**kwargs):
        calls.append(kwargs)
        assignment_path = next(p for p, target in kwargs["readonly"].items()
                               if target == "/review/assignment.json")
        assignment = json.loads(assignment_path.read_text())
        assert assignment["context"] == {"benchmark_hashes": "opaque-frozen-hash"}
        assert "do not require benchmark copies" in kwargs["instruction"].lower()
        assert kwargs["agent"].name == "terminus-2"
        assert kwargs["agent"].model_name == "openrouter/openai/gpt-6-luna"
        write_json(kwargs["workspace"] / "validity/verdict.json", {
            "candidate_hash": assignment["candidate_hash"], "contract_hash": assignment["contract_hash"],
            "verdict": "valid", "reasons": ["Fixture review"],
            "checks": [{"criterion": name, "status": "pass", "evidence_paths": ["/review/candidate/solve.py"]}
                       for name in assignment["required_criteria"]],
        })
        return SimpleNamespace(agent_result=SimpleNamespace(cost_usd=0.01), exception_info=None)

    monkeypatch.setattr("autoresearch_infrastructure.review.run_filesystem_agent", review)
    ledger = StartLedger(tmp_path / "starts.json", limits={"reviewer": 2}, identity={})
    facts = {"benchmark_hashes": "opaque-frozen-hash"}
    policy = pilot.ProgramValidity(agent={"name": "terminus-2", "model_name": "openrouter/openai/gpt-6-luna"},
                                  image="sha256:" + "a" * 64, ledger=ledger, context=facts)
    facts["benchmark_hashes"] = "caller mutation"
    with Research(tmp_path / "run", seed=seed, objective=SUCCESS_RATE, evaluator=evaluate_program,
                  validity=policy, evaluation_config={}, limits=Limits(max_evaluations=2)) as research:
        assert research.record_candidate()["status"] == "valid"
        assert calls == []  # Exact trusted baseline still needs no model review.
        with (research.workspace / "solve.py").open("a") as f:
            f.write("# Generic change\n")
        assert research.record_candidate()["status"] == "valid"
        bound = research.validity
        assert calls[0]["readonly"][research.root / "seed"] == "/review/baseline"
        assert ledger.snapshot()["counts"]["reviewer"] == 1
        original_identity = bound.identity
        bound.context = {"benchmark_hashes": "changed-boundary"}
        assert bound.identity != original_identity
        baseline = research.root / "seed/solve.py"
        baseline.chmod(0o644)
        baseline.write_text("tampered baseline")
        with pytest.raises(ValueError, match="baseline changed"):
            bound(research.workspace, ValidityContext(digest(snapshot(research.workspace)),
                                                     research.contract_hash, tmp_path / "other"))
        assert ledger.snapshot()["counts"]["reviewer"] == 1


@pytest.mark.parametrize("screen_repeats", [1, 2])
def test_fixed_candidate_fresh_panels_share_prior_charges_without_proposer(tmp_path, monkeypatch, screen_repeats):
    shipped = Path(__file__).parents[1] / "examples/task-bank/spreadsheet-canary/search.toml"
    import tomllib

    original = shipped.read_text()
    shipped = tmp_path / "plan.toml"
    shipped.write_text(original.replace("[screen]\ncases = 6\nrepeats = 1",
        f"[screen]\ncases = 6\nrepeats = {screen_repeats}").replace(
        "candidates_including_baseline = 7", "candidates_including_baseline = 2"))
    plan = tomllib.loads(shipped.read_text())
    tasks_root = tmp_path / "tasks"
    names = plan["benchmark"]["screen_task_ids"] + plan["benchmark"]["confirmation_task_ids"]
    for name in names:
        materialize(tasks_root / name, {"instruction.md": f"Fixture task {name}"})
    monkeypatch.setattr(pilot.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="sha256:" + "a" * 64))

    def overlay(task, destination, *args, **kwargs):
        materialize(destination, {"compose.json": "fixture-build-overlay"})
        return destination / "compose.json"

    monkeypatch.setattr(pilot, "ca_build_overlay", overlay)
    old = tmp_path / "old"
    _, _, _, _, ledger, _ = pilot.prepare(shipped, tasks_root, old, "fixture-image")
    prior = ledger.start("solver_trial", "prior admitted trial")
    ledger.finish(prior, status="failed")
    fixed = tmp_path / "frozen"
    materialize(
        fixed,
        {
            "program.toml": 'id="benchmark-harness"\nentrypoint="solve.py:solve"\n',
            "solve.py": "frozen fixture source\n",
        },
    )
    output = tmp_path / "recovery"
    _, _, _, identity, reused, _ = pilot.prepare(
        shipped, tasks_root, output, "fixture-image", ledger.path, fixed
    )
    assert reused.snapshot()["counts"]["solver_trial"] == 1
    assert reused.identity_hash == ledger.identity_hash
    assert identity["fixed_candidate"]["hash"] == digest(snapshot(fixed))

    import autoresearch_infrastructure.canary as canary

    controls = {f"oracle-{n}": {"fixture": n} for n in names}
    controls["nop-42515"] = {"fixture": "42515"}
    monkeypatch.setattr(canary, "verified_control", lambda output, agent, name, identity: {"fixture": name})
    write_json(
        output / "references.json",
        {
            "all_passed": True,
            "task_hashes": identity["task_hashes"],
            "build_overlays": identity["build_overlays"],
            "controls": controls,
        },
    )
    monkeypatch.setattr("autoresearch_infrastructure.providers.Provider.key", lambda self: "fixture-only-not-a-key")

    class FixtureAgent:
        def __init__(self, **kwargs):
            pass

        def run(self, *args, **kwargs):
            pytest.fail("No native proposer or paid reviewer may launch in this fixture")

    class FixtureValidity:
        def __init__(self, *args, **kwargs):
            self.criteria = {"fixture": "trusted fixture validity"}

        @property
        def identity(self):
            return {"fixture": "deterministic-validity"}

        def __call__(self, candidate, context):
            return Validity("valid", ("Fixture package",))

    class FixtureEvaluator:
        def __init__(self, **kwargs):
            self.ledger = kwargs["ledger"]
            self.max_harness_sessions = kwargs["max_harness_sessions"]
            self.names = sorted(kwargs["tasks"])

        @property
        def identity(self):
            return {"fixture_tasks": self.names}

        def __call__(self, candidate, context):
            for kind in ["solver_trial", "solver_session"]:
                key = self.ledger.start(kind, str(context.artifacts))
                self.ledger.finish(key, status="completed", cost_usd=0)
            tokens = 1 if (candidate / "solve.py").read_text() == "frozen fixture source\n" else 2
            return Evaluation({SUCCESS_RATE: 1, SOLVER_TOKENS: tokens}, completed=True)

    monkeypatch.setattr(pilot, "CodingAgent", FixtureAgent)
    monkeypatch.setattr(pilot, "ProgramValidity", FixtureValidity)
    monkeypatch.setattr(pilot, "HarborHarnessEvaluator", FixtureEvaluator)
    pilot.run(
        shipped, tasks_root, output, "fixture-image", allocation_ledger=ledger.path, fixed_candidate=fixed
    )
    counts = ledger.snapshot()["counts"]
    assert counts["solver_trial"] == 1 + 12 * screen_repeats + 16
    assert counts["solver_session"] == 12 * screen_repeats + 16
    assert counts["researcher"] == 0
    summary = json.loads((output / "summary.json").read_text())
    assert summary["status"] == "completed"
    assert summary["promotion"]["source_hash"] == digest(snapshot(fixed))
    assert summary["fixed_candidate"]["hash"] == digest(snapshot(fixed))
    (fixed / "solve.py").write_text("changed input")
    with pytest.raises(ValueError, match="plan/runtime changed"):
        pilot.prepare(shipped, tasks_root, output, "fixture-image", ledger.path, fixed)

    limited = StartLedger(tmp_path / "limited.json", limits=ledger.limits, identity=ledger.identity_hash)
    for _ in range(40):
        limited.start("solver_trial", "prior allocation use")
    with pytest.raises(StartLimitError, match="start budget"):
        pilot.run(
            shipped,
            tasks_root,
            tmp_path / "insufficient-capacity",
            "fixture-image",
            check=True,
            allocation_ledger=limited.path,
            fixed_candidate=fixed,
        )
    assert limited.snapshot()["counts"]["solver_trial"] == 40
    assert limited.snapshot()["counts"]["solver_session"] == 0
    assert limited.snapshot()["counts"]["researcher"] == 0
