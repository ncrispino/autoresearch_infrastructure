"""Fit a polynomial to thirteen fixed, noisy observations.

The task is the Harbor task in ``harbor-task/``. A candidate is the contents of
its ``/workspace/candidate``: the one file ``coefficients.json``. The Harbor task's own
grader measures the error on the observations and saves a plot of the fit.
"""

import json
import math

import autoresearch_infrastructure as ari

RMSE = ari.Metric("rmse", direction="min")
MAX_ERROR = ari.Metric("max_error", direction="min")
COEFFICIENTS = ari.Metric("coefficients", direction="min")
RULES = (
    "Submit only `coefficients.json`, containing an object with just the `coefficients` key.",
    "Use one to three finite numbers between -10 and 10: a constant, a straight line or a quadratic.",
    "Keep scripts and scratch files out of the candidate directory.",
)


def validity(candidate, context):
    """The rules that code can decide exactly. It runs before the reviewer and before the grader."""
    files = {path.relative_to(candidate).as_posix() for path in candidate.rglob("*") if path.is_file()}
    if files != {"coefficients.json"}:
        return ari.Validity("invalid", ("Edit only coefficients.json",))
    try:
        payload = json.loads((candidate / "coefficients.json").read_text())
    except (ValueError, OSError):
        return ari.Validity("invalid", ("coefficients.json must be JSON",))
    if not isinstance(payload, dict) or set(payload) != {"coefficients"}:
        return ari.Validity("invalid", ("Provide a JSON object containing only coefficients",))
    coefficients = payload["coefficients"]
    if not isinstance(coefficients, list) or not 1 <= len(coefficients) <= 3 or any(
        type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 10
        for value in coefficients
    ):
        return ari.Validity("invalid", ("Use one to three finite coefficients between -10 and 10",))
    return ari.Validity()


def task(run):
    return ari.bases.harbor_deliverable(
        run,
        harbor_task="harbor-task",
        feedback="/tests/test.sh",               # the task's one grader script
        sealed=None,                             # nothing is held out: the observations are public
        rules=RULES,
        validity=validity,
        objective=ari.Objective(
            RMSE, metrics=(RMSE, MAX_ERROR, COEFFICIENTS),
            description="Fit a polynomial to the thirteen public observations",
        ),
        rewards={RMSE: "rmse", MAX_ERROR: "max_error", COEFFICIENTS: "coefficients"},
        feedback_artifacts=True,                 # the researcher may read the plot and the residuals
    )
