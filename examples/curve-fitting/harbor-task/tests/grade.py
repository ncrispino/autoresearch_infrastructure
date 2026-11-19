"""Measure /workspace/candidate/coefficients.json on the observations.

Writes Harbor's reward file with three rewards, and two artifacts: a plot of the
fit and every residual.
"""

import json
import math
from pathlib import Path
from statistics import mean

OBSERVATIONS = [tuple(point) for point in json.loads(Path("/app/data/observations.json").read_text())["observations"]]
ARTIFACTS = Path("/logs/artifacts")


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


coefficients = json.loads(Path("/workspace/candidate/coefficients.json").read_text())["coefficients"]
residuals = [{"x": x, "observed": y, "predicted": predict(coefficients, x),
              "residual": predict(coefficients, x) - y} for x, y in OBSERVATIONS]
rmse = math.sqrt(mean(row["residual"] ** 2 for row in residuals))
(ARTIFACTS / "residuals.json").write_text(json.dumps({"coefficients": coefficients, "observations": residuals}, indent=2))
(ARTIFACTS / "fit.svg").write_text(plot(coefficients, rmse))
Path("/logs/verifier/reward.json").write_text(json.dumps({
    "rmse": rmse,
    "max_error": max(abs(row["residual"]) for row in residuals),
    "coefficients": len(coefficients),
}))
