import json

import pytest

from autoresearch_infrastructure import Evaluation, Limits, Metric, Objective, Research, Validity
from autoresearch_infrastructure.persistence import write_json
from autoresearch_infrastructure.viewer import build_index, write_html_preview

QUALITY = Metric("quality")
COST = Metric("cost", "min", maximum=10)


def validity(path, context):
    value = (path / "number").read_text()
    return Validity("invalid", ("Reserved value",)) if value == "invalid" else Validity()


def evaluate(path, context):
    value = (path / "number").read_text()
    (context.artifacts / "receipt.txt").write_text(f"{value=} {context.repeat=}")
    if value == "error":
        raise RuntimeError("deliberate evaluator failure")
    if value == "interrupt":
        raise KeyboardInterrupt("deliberate interruption")
    metrics = {QUALITY: float(value), COST: float(value) * 2}
    return Evaluation(metrics, completed=value != "0")


def make(tmp_path, *, evaluator=evaluate, validator=validity, limits=None, config=None):
    seed = tmp_path / "seed"
    seed.mkdir(exist_ok=True)
    (seed / "number").write_text("1")
    return Research(
        tmp_path / "run",
        seed=seed,
        objective=Objective(
            QUALITY, tie_breakers=(COST,), description="Improve quality under a cost ceiling"
        ),
        evaluator=evaluator,
        validity=validator,
        evaluation_config=config or {"dataset": "v1"},
        limits=limits or Limits(max_evaluations=12, selection_repeats=2),
    )


def submit(research, value, **kwargs):
    (research.workspace / "number").write_text(value)
    return research.record_candidate(**kwargs)


def test_record_lineage_validity_metrics_constraints_completion_and_viewer(tmp_path):
    research = make(tmp_path)
    a = submit(research, "2", total_repeats=2)
    b = submit(research, "4", parent_id=a["candidate_id"], total_repeats=2)
    invalid = submit(research, "invalid")
    expensive = submit(research, "6", total_repeats=2)
    incomplete = submit(research, "0", total_repeats=2)
    assert invalid["status"] == "invalid" and invalid["evidence"]["evaluation"] == "not_started"
    assert expensive["status"] == "valid" and expensive["evidence"]["feasible"] is False
    assert incomplete["evidence"]["task_completed"] is False
    assert b["selected"] == b["candidate_id"]
    assert research.state["solver_trial_starts"] == 8
    index = build_index(research.output)
    candidates = index["sessions"][0]["candidates"]
    assert candidates[1]["parent_id"] == a["candidate_id"]
    assert "+4" in candidates[1]["diff_patch"] and "-2" in candidates[1]["diff_patch"]
    assert len(candidates[1]["comparisons"]) == 2
    assert candidates[1]["evidence"][0]["metrics"] == {"quality": 4, "cost": 8}
    preview = tmp_path / "preview.html"
    write_html_preview(research.output, preview)
    assert "Compare candidate" in preview.read_text()
    assert "quality" in preview.read_text()
    assert not index["warnings"]
    research.close()
    saved = make(tmp_path)
    again = saved.record_candidate(b["candidate_id"], total_repeats=2)
    assert again["remaining_evaluations"] == 4
    assert again["selected"] == b["candidate_id"]


def test_public_history_lists_prior_solutions_and_outcomes_without_private_state(tmp_path):
    research = make(tmp_path, config={"heldout_data": "private-confirmation-input"})
    index_path = research.root / "history/index.json"
    initial = json.loads(index_path.read_text())
    assert initial["objective"]["description"] == "Improve quality under a cost ceiling"
    assert initial["objective"]["score"] == {"metric": "quality"}
    assert initial["objective"]["direction"] == "max"
    assert initial["selected_candidate"] is None and initial["candidates"] == []
    proposal = {"hypothesis": "A larger number improves quality", "mechanism": "Change the number"}
    first = submit(research, "2", total_repeats=2, proposal=proposal)
    best = submit(research, "4", total_repeats=2, parent_id=first["candidate_id"])
    submit(research, "invalid")
    submit(research, "error")
    submit(research, "0")
    research.state["sessions"]["hidden-session"] = {"private": "private-session-data"}
    record = research.state["sessions"]["research"]["candidates"]["candidate-0001"]
    record["private"] = "private-record-data"
    record["evidence"]["hidden-contract"] = {"private": "private-other-contract-data"}
    research._save_state()
    saved = json.loads(index_path.read_text())
    assert saved["selected_candidate"] == best["candidate_id"]
    assert len(saved["candidates"]) == 5
    first, best, invalid, failed, incomplete = saved["candidates"]
    assert first["proposal"] == proposal
    assert best["parent_id"] == first["id"]
    assert best["evaluation"] == {
        "status": "completed", "metrics": {"quality": 4, "cost": 8},
        "score": 4, "completed": True, "feasible": True,
    }
    assert invalid["validity"] == {"verdict": "invalid", "reasons": ["Reserved value"]}
    assert invalid["evaluation"]["status"] == "not_started"
    assert invalid["evaluation"]["metrics"] is None
    assert failed["evaluation"]["status"] == "incomplete"
    assert failed["evaluation"]["metrics"] is None
    assert incomplete["evaluation"]["status"] == "completed"
    assert incomplete["evaluation"]["completed"] is False
    assert incomplete["evaluation"]["feasible"] is False
    for candidate in saved["candidates"]:
        paths = candidate["paths"]
        assert (index_path.parent / paths["snapshot"] / "number").is_file()
        assert (index_path.parent / paths["diff"]).is_file()
        assert (index_path.parent / paths["validity"]).is_dir()
    assert (index_path.parent / best["paths"]["evaluations"]).is_dir()
    public_text = index_path.read_text()
    assert "private-confirmation-input" not in public_text
    assert "private-session-data" not in public_text
    assert "private-record-data" not in public_text
    assert "private-other-contract-data" not in public_text
    assert "solver_trial_starts" not in public_text


def test_public_history_is_rebuilt_and_never_controls_selection_or_accounting(tmp_path):
    research = make(tmp_path)
    result = submit(research, "2", total_repeats=2)
    index_path = research.root / "history/index.json"
    forged = {"selected_candidate": "candidate-9999", "candidates": [], "solver_trial_starts": 0}
    write_json(index_path, forged)
    research.close()
    resumed = make(tmp_path)
    assert resumed.state["solver_trial_starts"] == 2
    restored = json.loads(index_path.read_text())
    assert restored["selected_candidate"] == result["candidate_id"]
    assert len(restored["candidates"]) == 1
    write_json(index_path, forged)
    repeated = resumed.record_candidate(result["candidate_id"], total_repeats=2)
    assert repeated["selected"] == result["candidate_id"]
    assert repeated["remaining_evaluations"] == 10
    assert json.loads(index_path.read_text())["selected_candidate"] == result["candidate_id"]


def test_public_history_does_not_present_previous_score_as_an_unfinished_panel(tmp_path):
    research = make(tmp_path, limits=Limits(max_evaluations=2, max_repeats=3))
    first = submit(research, "2", total_repeats=2)
    assert first["evidence"]["score"] == 2
    unfinished = research.record_candidate(first["candidate_id"], total_repeats=3)
    assert unfinished["evidence"]["evaluation"] == "incomplete"
    assert research.state["solver_trial_starts"] == 2
    saved = json.loads((research.root / "history/index.json").read_text())
    assert saved["candidates"][0]["evaluation"] == {
        "status": "incomplete", "metrics": None, "score": None, "completed": None, "feasible": None,
    }


def test_failed_retry_and_partial_results_do_not_reset_budget(tmp_path):
    fail = {2}

    def flaky(path, context):
        if context.repeat in fail:
            raise RuntimeError("retryable")
        return evaluate(path, context)

    research = make(tmp_path, evaluator=flaky)
    first = submit(research, "2", total_repeats=2)
    assert first["evidence"]["evaluation"] == "incomplete"
    assert first["evidence"]["metrics"] is None
    assert first["remaining_evaluations"] == 10
    fail.clear()
    research.close()
    resumed = make(tmp_path, evaluator=flaky)
    repeated = resumed.record_candidate(first["candidate_id"], total_repeats=2)
    assert repeated["evidence"]["metrics"]["quality"] == 2
    assert resumed.state["solver_trial_starts"] == 3
    cells = repeated["evidence"]["cells"]
    assert [a["status"] for a in cells["default/default/2"]["trial_starts"]] == [
        "infrastructure_error",
        "completed",
    ]
    assert len(cells["default/default/1"]["trial_starts"]) == 1


def test_panel_is_reserved_completely_before_evaluation(tmp_path):
    research = make(tmp_path, limits=Limits(max_evaluations=1))
    result = submit(research, "2", total_repeats=2)
    assert result["evidence"]["error"] == "global_solver_trial_start_limit"
    assert research.state["solver_trial_starts"] == 0
    assert not result["evidence"]["cells"]


def test_interruption_recovery_retains_starts_and_durable_completed_cells(tmp_path):
    interrupted = {True}

    def crash(path, context):
        value = evaluate(path, context)
        if interrupted and context.repeat == 2:
            raise KeyboardInterrupt()
        return value

    research = make(tmp_path, evaluator=crash)
    with pytest.raises(KeyboardInterrupt):
        submit(research, "2", total_repeats=2)
    assert research.state["solver_trial_starts"] == 2
    interrupted.clear()
    research.close()
    resumed = make(tmp_path, evaluator=crash)
    restored_history = json.loads((resumed.root / "history/index.json").read_text())
    assert restored_history["candidates"][0]["evaluation"]["status"] == "incomplete"
    result = resumed.record_candidate("candidate-0001", total_repeats=2)
    assert resumed.state["solver_trial_starts"] == 3
    assert result["evidence"]["evaluation"] == "completed"
    assert result["evidence"]["cells"]["default/default/2"]["trial_starts"][0]["status"] == "interrupted"


@pytest.mark.parametrize(
    "metrics",
    [
        {QUALITY: True, COST: 2},
        {QUALITY: float("nan"), COST: 1},
        {QUALITY: float("inf"), COST: 1},
        {QUALITY: 1},
        {QUALITY: 1, COST: 1, "extra": 0},
    ],
)
def test_bad_metrics_are_evaluation_failures_not_low_scores(tmp_path, metrics):
    def bad(path, context):
        return Evaluation(metrics)

    research = make(tmp_path, evaluator=bad)
    result = submit(research, "2")
    assert result["status"] == "valid"
    assert result["evidence"]["evaluation"] == "incomplete"
    assert result["evidence"]["objective"] is None
    assert result["remaining_evaluations"] == 11


def test_validity_exception_is_invalid_and_does_not_consume_evaluations(tmp_path):
    def broken(path, context):
        raise OSError("validator unavailable")

    result = submit(make(tmp_path, validator=broken), "2")
    assert result["status"] == "invalid"
    assert result["remaining_evaluations"] == 12


def test_changed_contract_rejects_resume(tmp_path):
    make(tmp_path).close()
    with pytest.raises(ValueError, match="setup changed"):
        make(tmp_path, config={"dataset": "v2"})


def test_candidate_and_artifact_tampering_rejected_on_repeat(tmp_path):
    research = make(tmp_path)
    result = submit(research, "2")
    cell = result["evidence"]["cells"]["default/default/1"]
    receipt = research.output / cell["trial_starts"][0]["result_path"]
    (receipt.parent / "artifacts/receipt.txt").write_text("tampered")
    with pytest.raises(ValueError, match="artifacts changed"):
        research.record_candidate(result["candidate_id"])
    candidate = research._candidate_dir("research", result["candidate_id"]) / "snapshot/number"
    candidate.chmod(0o644)
    candidate.write_text("99")
    with pytest.raises(ValueError, match="snapshot changed"):
        research.record_candidate(result["candidate_id"])


def test_completed_evaluation_artifacts_are_verified_before_exposure(tmp_path):
    research = make(tmp_path)
    result = submit(research, "2", total_repeats=2)
    artifacts = research.evaluation_artifacts(result["candidate_id"])
    assert [(item.task_id, item.condition, item.repeat, item.attempt)
            for item in artifacts] == [
        ("default", "default", 1, 1),
        ("default", "default", 2, 1),
    ]
    assert all(item.candidate_id == result["candidate_id"] for item in artifacts)
    assert [item.artifacts.joinpath("receipt.txt").read_text() for item in artifacts] == [
        "value='2' context.repeat=1", "value='2' context.repeat=2",
    ]

    artifacts[0].artifacts.joinpath("receipt.txt").write_text("tampered")
    with pytest.raises(ValueError, match="evaluation or artifacts changed"):
        research.evaluation_artifacts(result["candidate_id"])


def test_private_evaluation_artifacts_are_sealed_but_not_in_researcher_history(tmp_path):
    def evaluate_with_private_evidence(path, context):
        assert context.private_artifacts is not None
        (context.artifacts / "public.txt").write_text("visible")
        (context.private_artifacts / "secret.txt").write_text("trusted-only")
        value = float((path / "number").read_text())
        return Evaluation({QUALITY: value, COST: value})

    research = make(tmp_path, evaluator=evaluate_with_private_evidence)
    result = submit(research, "2")
    artifact, = research.evaluation_artifacts(result["candidate_id"])
    assert artifact.private_artifacts is not None
    assert artifact.private_artifacts.joinpath("secret.txt").read_text() == "trusted-only"
    assert not artifact.private_artifacts.is_relative_to(research.root / "history")
    assert not list((research.root / "history").rglob("secret.txt"))
    assert "trusted-only" not in "".join(
        path.read_text(errors="ignore")
        for path in (research.root / "history").rglob("*")
        if path.is_file()
    )

    artifact.private_artifacts.joinpath("secret.txt").write_text("tampered")
    with pytest.raises(ValueError, match="evaluation or artifacts changed"):
        research.evaluation_artifacts(result["candidate_id"])


def test_evaluation_artifact_envelope_accepts_large_rollout_files(tmp_path):
    def evaluate_large_rollout(path, context):
        context.artifacts.joinpath("rollout.json").write_bytes(b"x" * 8_000_001)
        assert context.private_artifacts is not None
        context.private_artifacts.joinpath("corpus.jsonl").write_text("private\n")
        value = float((path / "number").read_text())
        return Evaluation({QUALITY: value, COST: value})

    research = make(tmp_path, evaluator=evaluate_large_rollout)
    result = submit(research, "2")
    artifact, = research.evaluation_artifacts(result["candidate_id"])
    assert artifact.artifacts.joinpath("rollout.json").stat().st_size == 8_000_001


def test_evaluation_artifacts_reject_forged_paths_and_skip_failed_attempts(tmp_path):
    fail = {True}

    def flaky(path, context):
        if fail:
            raise RuntimeError("retryable")
        return evaluate(path, context)

    research = make(tmp_path, evaluator=flaky)
    first = submit(research, "2")
    assert research.evaluation_artifacts(first["candidate_id"]) == ()
    fail.clear()
    retried = research.record_candidate(first["candidate_id"])
    artifacts = research.evaluation_artifacts(first["candidate_id"])
    assert len(artifacts) == 1 and artifacts[0].attempt == 2

    cell = retried["evidence"]["cells"]["default/default/1"]
    cell["status"] = "missing"
    research._save_state()
    with pytest.raises(ValueError, match="evaluation or artifacts changed"):
        research.evaluation_artifacts(first["candidate_id"])
    cell["status"] = "completed"

    cell["trial_starts"][0]["number"] = 2
    research._save_state()
    with pytest.raises(ValueError, match="evaluation or artifacts changed"):
        research.evaluation_artifacts(first["candidate_id"])
    cell["trial_starts"][0]["number"] = 1

    cell["trial_starts"][-1]["result_path"] = "../outside.json"
    research._save_state()
    with pytest.raises(ValueError, match="evaluation or artifacts changed"):
        research.evaluation_artifacts(first["candidate_id"])


def test_example_atif_is_embedded_and_html_escaped(tmp_path):
    def traced(path, context):
        atif = context.artifacts / "trajectory.json"
        write_json(
            atif,
            {
                "schema_version": "ATIF-v1.7",
                "agent": {"name": "fixture"},
                "steps": [{"message": "</script><b>untrusted"}],
            },
        )
        return Evaluation({QUALITY: 1, COST: 2}, trajectory=atif)

    research = make(tmp_path, evaluator=traced)
    submit(research, "2")
    preview = tmp_path / "view.html"
    summary = write_html_preview(research.output, preview)
    assert summary["trajectories"] == 1
    assert "</script><b>untrusted" not in preview.read_text()
    assert "\\u003c/script>" in preview.read_text()
    history = json.loads((research.root / "history/index.json").read_text())
    attempts = history["candidates"][0]["attempts"]
    assert len(attempts) == 1 and attempts[0]["completed"] is True
    assert attempts[0]["metrics"] == {"quality": 1, "cost": 2}
    files = attempts[0]["paths"]
    history_root = research.root / "history"
    assert (history_root / files["result"]).is_file()
    assert (history_root / files["trajectory"]).read_text().find("</script><b>untrusted") >= 0


def test_output_has_one_writer_until_closed(tmp_path):
    research = make(tmp_path)
    with pytest.raises(BlockingIOError):
        make(tmp_path)
    submit(research, "2")
    research.close()
    resumed = make(tmp_path)
    assert resumed.state["solver_trial_starts"] == 1
    with pytest.raises(ValueError, match="closed"):
        research.record_candidate("candidate-0001")


def test_switching_callback_in_the_same_source_module_changes_contract(tmp_path):
    make(tmp_path).close()

    def other(path, context):
        return evaluate(path, context)

    with pytest.raises(ValueError, match="setup changed"):
        make(tmp_path, evaluator=other)


def test_file_validity_uses_bound_file_not_reviewer_prose(tmp_path):
    from autoresearch_infrastructure.review import FileValidity

    def reviewer(candidate, verdict_path, prompt):
        assignment = json.loads((verdict_path.parents[2] / "assignment.json").read_text())
        write_json(
            verdict_path,
            {
                "candidate_hash": assignment["candidate_hash"],
                "contract_hash": assignment["contract_hash"],
                "verdict": "valid",
                "reasons": ["Checked integer input"],
                "checks": [
                    {"criterion": "numeric", "status": "pass", "evidence_paths": ["/review/candidate/number"]}
                ],
            },
        )
        return "This prose is ignored"

    validator = FileValidity(reviewer, criteria={"numeric": "Input is a numeric string"})
    research = make(tmp_path, validator=validator)
    result = submit(research, "2")
    assert result["evidence"]["validity"]["checks"][0]["criterion"] == "numeric"
    assert result["status"] == "valid"


def test_file_validity_fails_closed_on_missing_output(tmp_path):
    from autoresearch_infrastructure.review import FileValidity

    def reviewer(candidate, verdict_path, prompt):
        return "valid"

    result = submit(
        make(tmp_path, validator=FileValidity(reviewer, criteria={"scope": "Unchanged scope"})), "2"
    )
    assert result["status"] == "invalid"
    assert result["remaining_evaluations"] == 12


def test_validity_retries_preserve_previous_verdict_and_do_not_reset_budget(tmp_path):
    unavailable = {True}

    def validator(path, context):
        if unavailable:
            raise OSError("reviewer unavailable")
        return Validity()

    research = make(tmp_path, validator=validator)
    result = submit(research, "2")
    assert result["status"] == "invalid" and result["remaining_evaluations"] == 12
    unavailable.clear()
    result = research.record_candidate(result["candidate_id"])
    assert result["status"] == "valid" and result["remaining_evaluations"] == 11
    attempts = research.state["sessions"]["research"]["candidates"][result["candidate_id"]][
        "validity_attempts"
    ]
    assert [a["status"] for a in attempts] == ["invalid", "valid"]
    root = research._candidate_dir("research", result["candidate_id"]) / "validity" / research.contract_hash
    assert (root / "attempt-0001/accepted-verdict.json").is_file()
    assert (root / "attempt-0002/accepted-verdict.json").is_file()
