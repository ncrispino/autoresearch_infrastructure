"""Fit a polynomial to thirteen fixed, noisy observations.

The candidate is ``coefficients.json``. The evaluator measures its error on the
observations and saves a plot of the fit.
"""

import json
import math
from statistics import mean

import autoresearch_infrastructure as ari
from autoresearch_infrastructure.persistence import write_json

RMSE = ari.Metric("rmse", direction="min")
MAX_ERROR = ari.Metric("max_error", direction="min")
COEFFICIENTS = ari.Metric("coefficients", direction="min")
OBJECTIVE = ari.Objective(
    RMSE, metrics=(RMSE, MAX_ERROR, COEFFICIENTS),
    description="Fit a polynomial to the thirteen public observations",
)
OBSERVATIONS = (
    (-3.0, 2.15), (-2.5, 1.325), (-2.0, 1.2), (-1.5, 0.775),
    (-1.0, 0.85), (-0.5, 1.455), (0.0, 1.9), (0.5, 2.925),
    (1.0, 4.1), (1.5, 5.255), (2.0, 7.2), (2.5, 8.775), (3.0, 11.15),
)


def read_coefficients(candidate):
    """Return the candidate's coefficients, or raise ValueError naming the broken rule."""
    if {path.relative_to(candidate).as_posix() for path in candidate.rglob("*") if path.is_file()} != {
        "coefficients.json"
    }:
        raise ValueError("Edit only coefficients.json")
    payload = json.loads((candidate / "coefficients.json").read_text())
    if not isinstance(payload, dict) or set(payload) != {"coefficients"}:
        raise ValueError("Provide a JSON object containing only coefficients")
    coefficients = payload["coefficients"]
    if not isinstance(coefficients, list) or not 1 <= len(coefficients) <= 3 or any(
        type(value) not in (int, float) or not math.isfinite(value) or abs(value) > 10
        for value in coefficients
    ):
        raise ValueError("Use one to three finite coefficients between -10 and 10")
    return coefficients


def validity(candidate, context):
    """The constraints in instructions.md that code can decide exactly."""
    try:
        read_coefficients(candidate)
    except (ValueError, OSError, TypeError) as exc:
        return ari.Validity("invalid", (str(exc),))
    return ari.Validity()


def predict(coefficients, x):
    return sum(value * x ** power for power, value in enumerate(coefficients))


def plot(coefficients, rmse):
    """An SVG of the observations and the candidate's curve."""
    curve = [(x / 20, predict(coefficients, x / 20)) for x in range(-60, 61)]
    low = min(y for _, y in (*OBSERVATIONS, *curve)) - 0.5
    high = max(y for _, y in (*OBSERVATIONS, *curve)) + 0.5

    def point(x, y):
        return 64 + (x + 3) / 6 * 650, 340 - (y - low) / (high - low) * 260

    points = " ".join(f"{px:.2f},{py:.2f}" for px, py in (point(x, y) for x, y in curve))
    marks = "".join(
        f'<circle cx="{px:.2f}" cy="{py:.2f}" r="5"><title>x={x:g}, observed y={y:g}</title></circle>'
        for (x, y), (px, py) in zip(OBSERVATIONS, (point(x, y) for x, y in OBSERVATIONS))
    )
    return f'''<svg xmlns="http://www.w3.org/2000/svg" width="760" height="410" viewBox="0 0 760 410"
role="img" aria-labelledby="title description">
<title id="title">Polynomial fit: RMSE {rmse:.4f}</title>
<desc id="description">Black dots are the thirteen fixed observations. The blue line is this candidate's
polynomial prediction. Root mean squared error is measured on these same observations, not held-out data.</desc>
<rect width="760" height="410" fill="#fff" rx="12"/>
<g font-family="system-ui, sans-serif" fill="#172a40">
<text x="28" y="34" font-size="23" font-weight="600">Polynomial fit</text>
<text x="28" y="60" font-size="15">RMSE {rmse:.4f} · {len(coefficients)} coefficient(s) · 13 fixed observations</text>
<path d="M64 80V340H714" fill="none" stroke="#93a4b8"/>
<text x="28" y="89" font-size="13">{high:.1f}</text>
<text x="28" y="340" font-size="13">{low:.1f}</text>
<text x="58" y="362" font-size="13">−3</text><text x="382" y="362" font-size="13">0</text>
<text x="708" y="362" font-size="13">3</text><text x="738" y="342" font-size="14">x</text>
<polyline points="{points}" fill="none" stroke="#275ed2" stroke-width="3"/>
<g fill="#172a40" stroke="#fff" stroke-width="1.5">{marks}</g>
<circle cx="165" cy="389" r="4" fill="#172a40"/><text x="177" y="394" font-size="14">Observed data</text>
<path d="M405 389h25" stroke="#275ed2" stroke-width="3"/>
<text x="440" y="394" font-size="14">Candidate prediction</text></g></svg>'''


def evaluate(candidate, context):
    coefficients = read_coefficients(candidate)
    residuals = [{"x": x, "observed": y, "predicted": predict(coefficients, x),
                  "residual": predict(coefficients, x) - y} for x, y in OBSERVATIONS]
    rmse = math.sqrt(mean(row["residual"] ** 2 for row in residuals))
    maximum = max(abs(row["residual"]) for row in residuals)
    write_json(context.artifacts / "residuals.json", {"coefficients": coefficients, "observations": residuals})
    (context.artifacts / "fit.svg").write_text(plot(coefficients, rmse))
    return ari.Evaluation({RMSE: rmse, MAX_ERROR: maximum, COEFFICIENTS: len(coefficients)})


def task(run):
    return ari.Task(
        objective=OBJECTIVE,
        evaluator=evaluate,
        validity=validity,
        context={"task.json": {
            "observations": OBSERVATIONS,
            "formula": "y = sum(c[i] * x**i)",
            "coefficient_bounds": [-10, 10],
            "maximum_coefficients": 3,
        }},
    )
