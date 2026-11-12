"""Metric wiring, custom-score contracts, preflight and real panel selection; no models."""

import pytest

import autoresearch_infrastructure as ari
from autoresearch_infrastructure.viewer import build_index

QUALITY = ari.Metric("quality")
COST = ari.Metric("cost", "min")
LENGTH = ari.Metric("length", "min")
UNKNOWN = ari.Metric("unknown")


def valid(candidate, context):
    return ari.Validity()


def evaluate(candidate, context):
    value = float((candidate / "value").read_text())
    return ari.Evaluation({QUALITY: value, COST: 2, LENGTH: 100 - value})


def score(m):
    return m[QUALITY] - 0.1 * m[COST]


def make(tmp_path, *, objective=score, evaluator=evaluate, metrics=(QUALITY, COST, LENGTH), **kwargs):
    seed = tmp_path / "seed"
    seed.mkdir(exist_ok=True)
    (seed / "value").write_text("1")
    return ari.Research(
        tmp_path / "run", seed=seed, objective=objective, metrics=metrics,
        evaluator=evaluator, validity=valid, evaluation_config={}, **kwargs,
    )


def test_callable_score_and_diagnostics_preflight_selection_resume_and_viewer(tmp_path):
    with make(tmp_path) as research:
        before = research.state["solver_trial_starts"]
        checked = research.check_metrics(ari.Evaluation({QUALITY: 2, COST: 3, LENGTH: 99}))
        assert checked["score"] == pytest.approx(1.7)
        assert checked["referenced_metrics"] == ["quality", "cost"]
        assert checked["diagnostic_metrics"] == ["length"]
        assert research.state["solver_trial_starts"] == before == 0
        assert research.state["sessions"]["research"]["candidates"] == {}
        baseline = research.record_candidate()
        (research.workspace / "value").write_text("2")
        better = research.record_candidate(parent_id=baseline["candidate_id"])
        assert better["selected"] == better["candidate_id"]
        assert better["evidence"]["score"] == pytest.approx(1.8)
        assert better["evidence"]["objective_metrics"] == ["quality", "cost"]
        assert better["evidence"]["metrics"]["length"] == 98
        index = build_index(research.output)
        assert index["sessions"][0]["metrics"][2]["name"] == "length"
        assert index["sessions"][0]["candidates"][1]["evidence"][0]["score"] == pytest.approx(1.8)
    with make(tmp_path) as resumed:
        repeated = resumed.record_candidate(better["candidate_id"])
        assert repeated["remaining_evaluations"] == 18


def test_metric_shorthand_infers_schema_and_honors_direction(tmp_path):
    def measured(candidate, context):
        return ari.Evaluation({LENGTH: int((candidate / "value").read_text())})

    with make(tmp_path, objective=LENGTH, evaluator=measured, metrics=None) as research:
        assert research.metrics == (LENGTH,)
        a = research.record_candidate()
        (research.workspace / "value").write_text("10")
        b = research.record_candidate()
        assert b["selected"] == a["candidate_id"]


@pytest.mark.parametrize("sample", [
    {"quality": 1, COST: 2, LENGTH: 3},
    {QUALITY: 1, COST: 2},
    {QUALITY: 1, COST: 2, LENGTH: 3, UNKNOWN: 4},
    {ari.Metric("quality", "min"): 1, COST: 2, LENGTH: 3},
    {QUALITY: True, COST: 2, LENGTH: 3},
    {QUALITY: 1, COST: float("nan"), LENGTH: 3},
])
def test_preflight_rejects_invalid_measurements_without_spending(tmp_path, sample):
    with make(tmp_path) as research:
        with pytest.raises(ValueError):
            research.check_metrics(ari.Evaluation(sample))
        assert research.state["solver_trial_starts"] == 0


@pytest.mark.parametrize("objective", [
    lambda m: m[UNKNOWN],
    lambda m: m.get(UNKNOWN, 0),
    lambda m: m["quality"],
    lambda m: m[QUALITY] > 0,
    lambda m: m[QUALITY] * float("inf"),
    lambda m: (m[QUALITY], m[COST]),
    lambda m: 1,
])
def test_preflight_and_evaluation_share_score_checks(tmp_path, objective):
    with make(tmp_path, objective=objective) as research:
        with pytest.raises((ValueError, TypeError)):
            research.check_metrics(ari.Evaluation({QUALITY: 1, COST: 2, LENGTH: 3}))
        result = research.record_candidate()
        assert result["evidence"]["evaluation"] == "incomplete"
        assert result["selected"] is None
        assert research.state["solver_trial_starts"] == 1


def test_objective_cannot_mutate_measurements(tmp_path):
    def mutate(m):
        m[QUALITY] = 0
        return m[QUALITY]

    with make(tmp_path, objective=mutate) as research:
        with pytest.raises(TypeError):
            research.check_metrics(ari.Evaluation({QUALITY: 1, COST: 2, LENGTH: 3}))


def test_preflight_is_one_path_and_later_branch_is_still_checked(tmp_path):
    def objective(m):
        return m[UNKNOWN] if m[QUALITY] > 0 else m[QUALITY]
    with make(tmp_path, objective=objective) as research:
        assert research.check_metrics(ari.Evaluation({QUALITY: 0, COST: 2, LENGTH: 3}))["status"] == "valid"
        result = research.record_candidate()
        assert result["evidence"]["evaluation"] == "incomplete"
        assert result["selected"] is None


def test_nonlinear_objective_scores_aggregate_means_not_mean_of_scores(tmp_path):
    def measured(candidate, context):
        q, cost = (2, 1) if context.repeat == 1 else (12, 3)
        return ari.Evaluation({QUALITY: q, COST: cost, LENGTH: 5})

    with make(tmp_path, evaluator=measured, objective=lambda m: m[QUALITY] / m[COST]) as research:
        result = research.record_candidate(total_repeats=2)
        assert result["evidence"]["score"] == 3.5  # mean(2,12) / mean(1,3), not mean(2,4).


def test_ratio_of_means_allows_an_individual_zero_denominator(tmp_path):
    def measured(candidate, context):
        return ari.Evaluation({QUALITY: 2 if context.task == "free" else 6,
                               COST: 0 if context.task == "free" else 2})

    with make(tmp_path, evaluator=measured, objective=lambda m: m[QUALITY] / m[COST],
              metrics=(QUALITY, COST), tasks=("free", "paid")) as research:
        result = research.record_candidate()
        assert result["evidence"]["evaluation"] == "completed"
        assert result["evidence"]["score"] == 4
        assert result["selected"] == result["candidate_id"]


def test_known_limit_failure_in_partial_repeat_remains_ineligible(tmp_path):
    memory = ari.Metric("memory_mb", maximum=100)

    def measured(candidate, context):
        if context.repeat == 2 and context.task == "b":
            raise RuntimeError("Fixture missing measurement")
        return ari.Evaluation({QUALITY: 1, memory: 160 if context.repeat == 2 else 80})

    with make(tmp_path, evaluator=measured, objective=QUALITY, metrics=(QUALITY, memory),
              tasks=("a", "b")) as research:
        initial = research.record_candidate()
        assert initial["selected"] == initial["candidate_id"]
        partial = research.record_candidate(initial["candidate_id"], total_repeats=2)
        assert partial["evidence"]["evaluation"] == "incomplete"
        # Asking for the complete first repeat cannot erase the already observed violation.
        again = research.record_candidate(initial["candidate_id"], total_repeats=1)
        assert again["evidence"]["evaluation"] == "completed"
        assert again["evidence"]["metrics"]["memory_mb"] == 80
        assert again["evidence"]["constraint_failures"] and not again["evidence"]["feasible"]
        assert again["selected"] is None


def test_aggregate_only_score_error_preserves_cells_but_cannot_select(tmp_path):
    def measured(candidate, context):
        return ari.Evaluation({QUALITY: context.repeat, COST: 2, LENGTH: 3})

    def objective(m):
        return m[UNKNOWN] if m[QUALITY] == 1.5 else m[QUALITY]
    with make(tmp_path, evaluator=measured, objective=objective) as research:
        result = research.record_candidate(total_repeats=2)
        assert result["evidence"]["evaluation"] == "incomplete"
        assert "objective_error" in result["evidence"]["error"]
        assert all(c["status"] == "completed" for c in result["evidence"]["cells"].values())
        assert result["selected"] is None and result["evidence"]["score"] is None
        again = research.record_candidate(result["candidate_id"], total_repeats=2)
        assert again["remaining_evaluations"] == 18


def test_score_function_and_metric_registry_are_bound_to_contract(tmp_path):
    first, second = lambda m: m[QUALITY], lambda m: -m[QUALITY]
    with make(tmp_path, objective=first):
        pass
    with pytest.raises(ValueError, match="setup changed"):
        make(tmp_path, objective=second)
    with pytest.raises(ValueError, match="setup changed"):
        make(tmp_path, objective=first, metrics=(QUALITY, COST))


def test_function_requires_registered_metrics_and_bad_directions_fail_early(tmp_path):
    with pytest.raises(ValueError, match="explicit metrics"):
        make(tmp_path, metrics=None)
    with pytest.raises(ValueError, match="explicit metrics"):
        make(tmp_path, objective=ari.Objective(score, tie_breakers=(COST,)), metrics=None)
    for direction in ("sideways", "", False):
        with pytest.raises(ValueError, match="direction"):
            ari.Objective(score, direction=direction)
    with pytest.raises(ValueError, match="unique names"):
        make(tmp_path, metrics=(QUALITY, ari.Metric("quality", "min")))
    with pytest.raises(ValueError, match="one metric-values mapping"):
        ari.Objective(lambda x, y: x + y)


def test_constraint_only_metric_and_explicit_tie_breaker(tmp_path):
    budget = ari.Metric("budget", maximum=2)

    def measured(candidate, context):
        return ari.Evaluation({QUALITY: 1, COST: 10, budget: 3})

    objective = ari.Objective(QUALITY, tie_breakers=(COST,))
    with make(tmp_path, objective=objective, evaluator=measured, metrics=(QUALITY, COST, budget)) as research:
        checked = research.check_metrics(ari.Evaluation({QUALITY: 1, COST: 10, budget: 3}))
        assert checked["constraint_metrics"] == ["budget"]
        assert checked["diagnostic_metrics"] == []
        result = research.record_candidate()
        assert result["evidence"]["evaluation"] == "completed"
        assert not result["evidence"]["feasible"] and result["selected"] is None


@pytest.mark.parametrize("scope,eligible", [("each", False), ("mean", True)])
def test_one_bad_repeat_cannot_hide_behind_a_good_average_unless_requested(tmp_path, scope, eligible):
    memory = ari.Metric("memory_mb", maximum=100, bounds_scope=scope)

    def measured(candidate, context):
        better = (candidate / "value").read_text() == "2"
        return ari.Evaluation({QUALITY: 2 if better else 1,
                               memory: (160 if context.repeat == 2 else 0) if better else 80})

    with make(tmp_path, objective=QUALITY, evaluator=measured, metrics=(QUALITY, memory),
              limits=ari.Limits(max_evaluations=4, selection_repeats=2)) as research:
        baseline = research.record_candidate(total_repeats=2)
        (research.workspace / "value").write_text("2")
        changed = research.record_candidate(total_repeats=2)
        assert changed["evidence"]["metrics"]["memory_mb"] == 80
        assert changed["evidence"]["feasible"] is eligible
        assert changed["selected"] == (changed if eligible else baseline)["candidate_id"]
        failures = changed["evidence"]["constraint_failures"]
        assert bool(failures) is not eligible
        if failures:
            assert "above maximum 100" in failures[0] and "repeat 2" in failures[0]
        viewed = build_index(research.output)["sessions"][0]["candidates"][1]["evidence"][0]
        assert viewed["constraint_failures"] == failures


@pytest.mark.parametrize("scope", [None, [], "percentile"])
def test_unknown_bound_scope_is_rejected_before_evaluation(scope):
    with pytest.raises(ValueError, match="bounds_scope"):
        ari.Metric("memory", maximum=100, bounds_scope=scope)
