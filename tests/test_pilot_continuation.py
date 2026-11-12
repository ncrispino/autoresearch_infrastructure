"""Fresh-output continuation exercises real state/accounting, with no model calls."""

import asyncio
import json
import tomllib
from pathlib import Path
from types import SimpleNamespace

import pytest

from autoresearch_infrastructure import Evaluation, Limits, Objective, Research, Validity, pilot
from autoresearch_infrastructure.harbor_trial import SOLVER_TOKENS, SUCCESS_RATE
from autoresearch_infrastructure.ledger import StartLimitError
from autoresearch_infrastructure.persistence import write_json
from autoresearch_infrastructure.source_tree import materialize

pytest.importorskip("harbor")


@pytest.mark.parametrize("separate_images", [False, True])
@pytest.mark.parametrize("interruption", [None, KeyboardInterrupt, asyncio.CancelledError])
def test_fresh_output_keeps_proposal_slots_charges_and_confirmation_capacity(
    tmp_path, monkeypatch, separate_images, interruption
):
    shipped = Path(__file__).parents[1] / "examples/task-bank/spreadsheet-canary/long.toml"
    plan = tomllib.loads(shipped.read_text())
    names = plan["benchmark"]["screen_task_ids"] + plan["benchmark"]["confirmation_task_ids"]
    tasks = tmp_path / "tasks"
    for name in names:
        materialize(tasks / name, {"instruction.md": f"Fixture task {name}"})
    def inspect_image(command, **kwargs):
        return SimpleNamespace(stdout=command[3] if command[3].startswith("sha256:") else "sha256:" + "a" * 64)

    monkeypatch.setattr(pilot.subprocess, "run", inspect_image)
    role_image = "sha256:" + ("b" if separate_images else "a") * 64
    image_options = {"researcher_image": role_image, "reviewer_image": role_image} if separate_images else {}

    def overlay(task, destination, *args, **kwargs):
        materialize(destination, {"compose.json": "fixture overlay"})
        return destination / "compose.json"

    monkeypatch.setattr(pilot, "ca_build_overlay", overlay)
    old = tmp_path / "old"
    _, _, _, _, ledger, _ = pilot.prepare(shipped, tasks, old, "fixture-image")

    class FixtureEvaluator:
        def __init__(self, **kwargs):
            self.ledger = kwargs["ledger"]
            self.max_harness_sessions = 1
            self.names = sorted(kwargs["tasks"])

        @property
        def identity(self):
            return {"fixture_tasks": self.names}

        def __call__(self, candidate, context):
            for kind in ["solver_trial", "solver_session"]:
                key = self.ledger.start(kind, str(context.artifacts))
                self.ledger.finish(key, status="completed", cost_usd=0)
            tokens = 1 if "native-fixture" in (candidate / "solve.py").read_text() else 2
            return Evaluation({SUCCESS_RATE: 1, SOLVER_TOKENS: tokens}, completed=True)

    evaluator = FixtureEvaluator(
        tasks={name: tasks / name for name in plan["benchmark"]["screen_task_ids"]},
        ledger=ledger,
    )
    seed = tmp_path / "seed"
    materialize(seed, {"solve.py": "fixture baseline"})

    def valid(candidate, context):
        return Validity("valid", ("Deterministic fixture",))

    with Research(
        old / "screen",
        seed=seed,
        objective=Objective(SUCCESS_RATE, tie_breakers=(SOLVER_TOKENS,)),
        evaluator=evaluator,
        validity=valid,
        evaluation_config={"fixture": "before-guidance-repair"},
        limits=Limits(max_candidates=9, max_evaluations=60),
        tasks=tuple(plan["benchmark"]["screen_task_ids"]),
        conditions=("screen",),
    ) as research:
        research.record_candidate()
        for index in range(4):
            key = ledger.start("researcher", str(research.output))
            (research.workspace / "solve.py").write_text(f"old proposal {index}")
            research.record_candidate()
            ledger.finish(key, status="completed", cost_usd=0)
        # A lost invocation consumes a role start but does not consume a proposal slot.
        key = ledger.start("researcher", str(research.output))
        ledger.finish(key, status="failed")
    assert ledger.snapshot()["counts"]["solver_trial"] == 30

    output = tmp_path / "continuation"
    _, _, _, identity, _, _ = pilot.prepare(shipped, tasks, output, "fixture-image", ledger.path, **image_options)
    assert identity["agent_images"] == {"researcher": role_image, "reviewer": role_image}
    if separate_images:
        with pytest.raises(ValueError, match="plan/runtime changed"):
            pilot.prepare(shipped, tasks, output, "fixture-image", ledger.path)
    assert pilot.prior_new_candidates(ledger, output / "screen") == 4
    import autoresearch_infrastructure.canary as canary

    monkeypatch.setattr(canary, "verified_control", lambda output, agent, name, identity: {"fixture": name})
    controls = {f"oracle-{name}": {"fixture": name} for name in names}
    controls["nop-42515"] = {"fixture": "42515"}
    write_json(output / "references.json", {
        "all_passed": True, "task_hashes": identity["task_hashes"],
        "build_overlays": identity["build_overlays"], "controls": controls,
    })
    monkeypatch.setattr("autoresearch_infrastructure.providers.Provider.key", lambda self: "fixture-only-not-a-key")
    calls = []

    class FixtureAgent:
        def __init__(self, **kwargs):
            assert set(kwargs) == {"agent", "image"}
            assert kwargs["agent"] == plan["researcher"]
            assert kwargs["agent"]["name"] == "terminus-2"
            assert kwargs["agent"]["model_name"] == "openrouter/openai/gpt-6-luna"
            assert kwargs["image"] == role_image

        def run(self, *args, workspace, recorder=None, **kwargs):
            assert recorder is not None
            context = output / "research-context"
            assert (context / "starter.py").read_text() == Path(pilot.starter.__file__).read_text()
            assert (context / "usage.md").read_text() == Path(pilot.starter.__file__).with_name("usage.md").read_text()
            calls.append(ledger.snapshot()["counts"]["researcher"])
            if interruption is not None:
                # Retain a native partial ATIF, whose cost is not a final charge.
                evidence = kwargs["evidence"]
                write_json(evidence / "trajectory.json", {"final_metrics": {"total_cost_usd": 0.125}})
                write_json(evidence / "trajectory-metadata.json", {"fixture": "partial-researcher"})
                raise interruption("fixture interruption")
            (workspace / "candidate/solve.py").write_text(f"native-fixture {calls[-1]}")
            recorder.dispatch_record_candidate("research", {
                "candidate": "workspace",
                "proposal": {"hypothesis": "Fixture efficiency", "mechanism": "Fixture program"},
            })

    class FixtureValidity:
        def __init__(self, *args, **kwargs):
            assert kwargs["image"] == role_image
            self.criteria = {"fixture": "trusted fixture validity"}

        @property
        def identity(self):
            return {"fixture": "deterministic-validity"}

        def __call__(self, candidate, context):
            return valid(candidate, context)

    monkeypatch.setattr(pilot, "CodingAgent", FixtureAgent)
    monkeypatch.setattr(pilot, "ProgramValidity", FixtureValidity)
    monkeypatch.setattr(pilot, "HarborHarnessEvaluator", FixtureEvaluator)
    if interruption is not None:
        with pytest.raises(interruption, match="fixture interruption"):
            pilot.run(shipped, tasks, output, "fixture-image", allocation_ledger=ledger.path, **image_options)
        saved = ledger.snapshot()
        assert calls == [6]
        assert saved["counts"]["researcher"] == 6
        assert saved["counts"]["solver_trial"] == saved["counts"]["solver_session"] == 36
        starts = [s for s in saved["starts"].values() if s["kind"] == "researcher"]
        latest = max(starts, key=lambda s: s["started_at"])
        assert latest["status"] == "failed"
        assert latest["cost_usd"] is None
        assert "finished_at" in latest
        assert all(s["status"] != "started" for s in starts)
        index = json.loads((output / "screen/sessions/research/agent/trajectory-index.json").read_text())
        assert index["trajectories"] == [{"fixture": "partial-researcher"}]
        assert not (output / "confirmation").exists()
        return
    pilot.run(shipped, tasks, output, "fixture-image", allocation_ledger=ledger.path, **image_options)
    assert calls == [6, 7, 8, 9]
    counts = ledger.snapshot()["counts"]
    assert counts["solver_trial"] == counts["solver_session"] == 92
    assert counts["researcher"] == 9
    state = json.loads((output / "screen/state.json").read_text())["sessions"]["research"]
    assert state["new_proposals"] == 5  # Fresh baseline plus four remaining proposals.
    assert json.loads((output / "summary.json").read_text())["status"] == "completed"
    assert len(json.loads((output / "confirmation/state.json").read_text())["sessions"]["research"]["candidates"]) == 2

    with pytest.raises(StartLimitError, match="start budget"):
        pilot.run(shipped, tasks, tmp_path / "no-capacity", "fixture-image", check=True, allocation_ledger=ledger.path)
    assert ledger.snapshot()["counts"] == counts

    # Lost prior controller state must not silently restore spent proposal slots.
    (old / "screen/state.json").unlink()
    with pytest.raises(ValueError, match="Cannot establish prior proposal charges"):
        pilot.run(shipped, tasks, tmp_path / "missing-prior", "fixture-image", check=True, allocation_ledger=ledger.path)
    assert ledger.snapshot()["counts"] == counts
