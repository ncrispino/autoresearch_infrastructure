# Curve fitting

Fit a polynomial to thirteen fixed, noisy `(x, y)` observations. This is the
smallest complete task: a Python function measures each candidate, so no solver
agent is involved.

| Part | Definition |
| --- | --- |
| Candidate | `coefficients.json`, holding `{"coefficients": [c0, c1, c2]}` for `y = c0 + c1*x + c2*x²`. A shorter list is a line or a constant. |
| Baseline | `[0.0]`, which predicts zero everywhere. See [seed/](seed/coefficients.json). |
| Objective | Minimize root mean squared error (RMSE) on the thirteen observations. |
| Other metrics | Maximum absolute error and the number of coefficients. They are recorded and do not affect selection. |
| Rules | Only `coefficients.json`, with one to three finite numbers between -10 and 10. |
| Fixed inputs | The observations and the evaluator, both in [task.py](task.py). |

The error is measured on the same observations a researcher can read. It shows
how well the curve fits them, not how well it would predict new points. The
observations are noisy, so the best quadratic does not reach zero error.

## Files

| File | Purpose |
| --- | --- |
| [task.py](task.py) | The task: observations, metrics, the evaluator, a validity function, and `task(run)`. |
| [instructions.md](instructions.md) | What the researcher is told. The reviewer checks each rule under Constraints. |
| [local.toml](local.toml) | Run settings with no agents and review turned off: five candidates, four evaluations. |
| [run.toml](run.toml) | Run settings with a Harbor researcher and reviewer: one proposal. |
| [demo.py](demo.py) | Five scripted proposals, for a first look without agents. |

## Run the scripted demo

Commands run from the package root with ARI installed. Use a new output directory.

```sh
python examples/curve-fitting/demo.py --output runs/curve-demo
```

The demo records the zero baseline, then the least-squares constant, line and
quadratic, then a coefficient of 1000. The last one breaks a constraint, so it is
recorded as invalid and never evaluated. Open `runs/curve-demo/viewer.html` to
compare them. Each evaluation saves `fit.svg`, a plot of the observations and
the candidate's curve, and `residuals.json`, with every prediction and error.

## Make your own proposals

```sh
ari record examples/curve-fitting --config examples/curve-fitting/local.toml --output runs/my-fit
```

The first call records the baseline and prints the workspace path. Edit
`coefficients.json` there, for example to `{"coefficients": [2, 1.5, 0.5]}`, and
run the same command again to record and measure your version. With
`local.toml` review is off, so the task's own validity function decides validity. No agent or
container is used.

## Run a Harbor researcher

[run.toml](run.toml) configures one researcher proposal. A reviewer judges the
baseline and the proposal against [instructions.md](instructions.md), after the
task's validity function has passed them. The defaults use
Terminus-2 for both roles. Change `name` and `model_name` in either table to use
another Harbor agent, and replace its `kwargs` with options that agent accepts.

You need the Harbor extra installed, the providers' credentials in the
environment, and an immutable image that contains what both agents need. The
[launch guide](../../skills/use-autoresearch-infrastructure/references/running-autoresearch.md)
explains how to build one.

```sh
# Validate the task, settings and image reference. Starts nothing.
ari check examples/curve-fitting --image <image-id> --output runs/live-curve

# One researcher start and up to two reviewer starts. This costs money.
ari run examples/curve-fitting --image <image-id> --output runs/live-curve
```

With the supplied settings a run makes at most one researcher start of five
minutes and two reviewer starts of two minutes each. The settings also limit each
agent's turns and output tokens. None of these is a cap on cost. If you choose
another agent, set the limits that agent supports.

The researcher works on `/workspace/candidate/coefficients.json`. It can read the
observations in `/context/task.json` and earlier results in `/history`. It
submits once. The run directory keeps the agents' original logs and
trajectories next to the measurements, and `summary.json` records the outcome,
including a failed or unfinished one.
