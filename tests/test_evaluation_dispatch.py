"""Reserved panels count dispatched attempts, including loss before a result."""

import json
import multiprocessing
import threading

import pytest

from autoresearch_infrastructure import Evaluation, Limits, Metric, Research, Validity

SCORE = Metric("score")


def valid(candidate, context):
    return Validity()


class Evaluator:
    def __init__(self, entered=None, release=None):
        self.entered, self.release = entered, release

    def __call__(self, candidate, context):
        if self.entered is not None and context.task == "second":
            self.entered.set()
            assert self.release.wait(15)
        (context.artifacts / "receipt.txt").write_text(context.task)
        return Evaluation({SCORE: 1})


def make(tmp_path, evaluator):
    seed = tmp_path / "seed"
    seed.mkdir(exist_ok=True)
    (seed / "value").write_text("1")
    return Research(
        tmp_path / "run", seed=seed, objective=SCORE, evaluator=evaluator,
        validity=valid, evaluation_config={"fixture": "dispatch-accounting"},
        tasks=("first", "second", "third"),
        limits=Limits(max_evaluations=4, parallel_evaluations=1),
    )


def cells(state):
    candidate = state["sessions"]["research"]["candidates"]["candidate-0001"]
    return next(iter(candidate["evidence"].values()))["cells"]


def test_queued_cells_keep_their_full_reservation_without_trial_starts(tmp_path):
    entered, release = threading.Event(), threading.Event()
    results, errors = [], []
    with make(tmp_path, Evaluator(entered, release)) as research:
        def submit():
            try:
                results.append(research.record_candidate())
            except BaseException as exc:
                errors.append(exc)

        thread = threading.Thread(target=submit)
        thread.start()
        try:
            assert entered.wait(10)
            with research._lock:
                assert research.state["solver_trial_starts"] == 2
                assert research.remaining_trial_starts == 2
                assert research.available_trial_start_capacity == 1
                assert research.available_instance_trial_start_capacity("research") == 1
                attempts = cells(research.state)
                assert len(attempts["first/default/1"]["trial_starts"]) == 1
                assert len(attempts["second/default/1"]["trial_starts"]) == 1
                assert attempts["third/default/1"]["trial_starts"] == []
                assert attempts["third/default/1"]["status"] == "missing"
                reservation = next(iter(research.state["reservations"].values()))
                assert reservation["status"] == "active"
                assert reservation["remaining_cells"] == ["third/default/1"]
                saved = json.loads(research.state_path.read_text())
                assert saved["solver_trial_starts"] == 2
                assert cells(saved)["third/default/1"]["trial_starts"] == []
        finally:
            release.set()
            thread.join(10)
        assert not thread.is_alive()
        assert not errors
        assert results[0]["evidence"]["evaluation"] == "completed"
        assert research.state["solver_trial_starts"] == 3
        assert research.available_trial_start_capacity == 1


@pytest.mark.skipif("fork" not in multiprocessing.get_all_start_methods(), reason="requires process-loss fixture")
def test_process_loss_reuses_durable_cells_and_does_not_charge_queued_attempts(tmp_path):
    process_context = multiprocessing.get_context("fork")
    entered, release = process_context.Event(), process_context.Event()

    def run():
        with make(tmp_path, Evaluator(entered, release)) as research:
            research.record_candidate()

    process = process_context.Process(target=run)
    process.start()
    try:
        assert entered.wait(10)
    finally:
        process.terminate()
        process.join(10)
    assert not process.is_alive()
    assert process.exitcode != 0

    with make(tmp_path, Evaluator()) as resumed:
        assert resumed.state["solver_trial_starts"] == 2
        recovered = cells(resumed.state)
        assert recovered["first/default/1"]["status"] == "completed"
        assert recovered["second/default/1"]["trial_starts"][0]["status"] == "interrupted"
        assert recovered["third/default/1"]["trial_starts"] == []
        assert resumed.available_trial_start_capacity == 2
        assert not any(r["status"] == "active" for r in resumed.state["reservations"].values())

        result = resumed.record_candidate("candidate-0001")
        assert result["evidence"]["evaluation"] == "completed"
        assert resumed.state["solver_trial_starts"] == 4
        attempts = result["evidence"]["cells"]
        assert len(attempts["first/default/1"]["trial_starts"]) == 1
        assert [a["status"] for a in attempts["second/default/1"]["trial_starts"]] == [
            "interrupted", "completed",
        ]
        assert len(attempts["third/default/1"]["trial_starts"]) == 1
        assert attempts["third/default/1"]["trial_starts"][0]["number"] == 1
        assert resumed.available_trial_start_capacity == 0
