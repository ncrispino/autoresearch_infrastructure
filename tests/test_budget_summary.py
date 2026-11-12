"""Visible budgets and automatic shared-budget ownership, without model calls."""

import multiprocessing
from concurrent.futures import ThreadPoolExecutor

import pytest

import autoresearch_infrastructure as ari
from autoresearch_infrastructure.ledger import StartLedger

QUALITY = ari.Metric("quality")


def valid(candidate, context):
    return ari.Validity()


class Measured:
    def __init__(self, ledger, *, fail=False):
        self.ledger, self.fail = ledger, fail

    @property
    def identity(self):
        return {"allocation": self.ledger.identity_hash, "fail": self.fail}

    def __call__(self, candidate, context):
        key = self.ledger.start("solver_trial", str(context.artifacts))
        self.ledger.finish(key, status="failed" if self.fail else "completed")
        if self.fail:
            raise RuntimeError("Fixture failure")
        return ari.Evaluation({QUALITY: 1})


def make(root, ledger, *, fail=False, validity=valid):
    seed = root / "seed"
    seed.mkdir(parents=True, exist_ok=True)
    (seed / "value").write_text("fixture")
    return ari.Research(root / "run", seed=seed, objective=QUALITY, evaluator=Measured(ledger, fail=fail),
                        validity=validity, evaluation_config={}, tasks=("a", "b"),
                        limits=ari.Limits(max_candidates=3, max_evaluations=4, selection_repeats=2))


def own(ledger):
    with ledger.allocation_owner():
        return "owned"


def test_summary_reports_cases_repeats_baseline_and_failed_starts(tmp_path):
    ledger = StartLedger(tmp_path / "starts.json", limits={"solver_trial": 8}, identity={})
    with make(tmp_path, ledger, fail=True) as research:
        before = research.budget_summary()
        assert before["cases_per_repeat"] == 2 and before["selection_repeats"] == 2
        assert before["max_repeats"] == 3 and before["evaluations_for_selection"] == 4
        assert before["candidates"] == {"limit": 3, "used": 0, "remaining": 3, "including_baseline": True}
        assert before["shared_budgets"][0]["starts"]["solver_trial"] == {"limit": 8, "used": 0, "remaining": 8}
        result = research.record_candidate()
        assert result["evidence"]["evaluation"] == "incomplete"
        after = research.budget_summary()
        assert after["evaluations"] == {"limit": 4, "used": 2, "reserved": 0, "remaining": 2}
        assert after["candidates"]["used"] == 1
        assert after["shared_budgets"][0]["starts"]["solver_trial"]["used"] == 2
    with make(tmp_path, ledger, fail=True) as resumed:
        assert resumed.budget_summary()["evaluations"]["used"] == 2


def test_research_owns_shared_budget_and_releases_it_on_close_and_constructor_failure(tmp_path):
    ledger = StartLedger(tmp_path / "starts.json", limits={"solver_trial": 8}, identity={})
    with make(tmp_path, ledger):
        with ThreadPoolExecutor(max_workers=1) as pool:
            with pytest.raises(RuntimeError, match="owns this shared budget"):
                pool.submit(own, ledger).result()
        # Failure to own the same output must release its nested budget ownership.
        with pytest.raises(BlockingIOError):
            make(tmp_path, ledger)
    with ThreadPoolExecutor(max_workers=1) as pool:
        assert pool.submit(own, ledger).result() == "owned"


def test_summary_distinguishes_reserved_reviews_from_available_reviews(tmp_path):
    pytest.importorskip("harbor")
    from autoresearch_infrastructure.pilot import ProgramValidity

    ledger = StartLedger(tmp_path / "starts.json", limits={"solver_trial": 8, "reviewer": 2}, identity={})
    ledger.start("reviewer", "Earlier review")
    policy = ProgramValidity(agent={"name": "nop", "model_name": "fixture-model"}, image="sha256:" + "a" * 64,
                             ledger=ledger, reserved_reviews=1)
    with make(tmp_path, ledger, validity=policy) as research:
        assert research.budget_summary()["reviews"] == {
            "limit": 2, "used": 1, "remaining": 1, "reserved": 1, "available_here": 0}


def child_owner(path, connection):
    ledger = StartLedger(path, limits={"solver_trial": 1}, identity={})
    try:
        own(ledger)
    except RuntimeError:
        connection.send("blocked")
    else:
        connection.send("owned")
    finally:
        connection.close()


def test_nested_outputs_share_ownership_but_a_forked_launcher_cannot_inherit_it(tmp_path):
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("fork is unavailable")
    ledger = StartLedger(tmp_path / "starts.json", limits={"solver_trial": 1}, identity={})
    same = StartLedger(ledger.path.parent / "unused/../starts.json", limits=ledger.limits, identity={})
    with ledger.allocation_owner(), same.allocation_owner():
        ctx = multiprocessing.get_context("fork")
        parent, child = ctx.Pipe()
        process = ctx.Process(target=child_owner, args=(ledger.path, child))
        process.start()
        child.close()
        try:
            assert parent.poll(5), "Child did not finish its lock check"
            assert parent.recv() == "blocked"
            process.join(5)
            assert process.exitcode == 0
        finally:
            parent.close()
            if process.is_alive():
                process.terminate()
                process.join(5)
    assert own(ledger) == "owned"


def inherited_controller(research, connection):
    try:
        research.record_candidate()
    except RuntimeError as exc:
        connection.send(str(exc))
    finally:
        research.close()
        connection.close()


def test_forked_child_cannot_dispatch_an_inherited_controller(tmp_path):
    if "fork" not in multiprocessing.get_all_start_methods():
        pytest.skip("fork is unavailable")
    ledger = StartLedger(tmp_path / "starts.json", limits={"solver_trial": 8}, identity={})
    with make(tmp_path, ledger) as research:
        ctx = multiprocessing.get_context("fork")
        parent, child = ctx.Pipe()
        process = ctx.Process(target=inherited_controller, args=(research, child))
        process.start()
        child.close()
        try:
            assert parent.poll(5), "Child did not finish its controller check"
            assert "another process" in parent.recv()
            process.join(5)
            assert process.exitcode == 0
        finally:
            parent.close()
            if process.is_alive():
                process.terminate()
                process.join(5)
        assert ledger.snapshot()["counts"]["solver_trial"] == 0
        assert research.record_candidate()["status"] == "valid"
