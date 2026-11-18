# Curve fitting

Fit a polynomial to thirteen fixed, noisy `(x, y)` observations. This is the
smallest complete task: a Harbor task with one grader script and nothing held
out. Copy it to start your own.

| Part | Definition |
| --- | --- |
| Candidate | The contents of the Harbor task's `/app/output`: `coefficients.json`, holding `{"coefficients": [c0, c1, c2]}` for `y = c0 + c1*x + c2*x²`. A shorter list is a line or a constant. |
| Baseline | `[0.0]`, which predicts zero everywhere. See [harbor-task/environment/output/](harbor-task/environment/output/coefficients.json). |
| Objective | Minimize root mean squared error (RMSE) on the thirteen observations. |
| Other metrics | Maximum absolute error and the number of coefficients. They are recorded and do not affect selection. |
| Rules | Only `coefficients.json`, with one to three finite numbers between -10 and 10. |
| Fixed inputs | The Harbor task: its observations, its container and its grader. |

The error is measured on the same observations a researcher can read. It shows
how well the curve fits them, not how well it would predict new points. The
observations are noisy, so the best quadratic does not reach zero error.

## Files

| File | Purpose |
| --- | --- |
| [task.py](task.py) | One call to `ari.bases.harbor_deliverable`, with the metrics, the rules and a validity function. |
| [harbor-task/instruction.md](harbor-task/instruction.md) | The objective the researcher reads. |
| [harbor-task/environment/](harbor-task/environment/Dockerfile) | The task's container: Python, the observations, and the starting `output/coefficients.json`. |
| [harbor-task/tests/](harbor-task/tests/grade.py) | The grader. `test.sh` runs `grade.py`, which writes three rewards, a plot and the residuals. |
| [harbor-task/solution/](harbor-task/solution/solve.sh) | A reference answer, the least-squares quadratic. ARI does not use it. |
| [local.toml](local.toml) | Run settings with no agents and review turned off: five candidates, four evaluations. |
| [run.toml](run.toml) | Run settings with a Harbor researcher and reviewer: one proposal. |
| [demo.py](demo.py) | Five scripted proposals, for a first look without agents. |

Nothing is held out here, so `task.py` passes the task's one script as
`feedback` and `sealed=None`. `feedback_artifacts=True` lets the researcher read
what the grader writes under `/logs/artifacts`.

## Run the scripted demo

Commands run from the package root with ARI installed. They need Docker and no
model. Use a new output directory.

```sh
python examples/curve-fitting/demo.py --output runs/curve-demo
```

The demo records the zero baseline, then the least-squares constant, line and
quadratic, then a coefficient of 1000. The last one breaks a rule that the
validity function decides, so it is recorded as invalid and never graded. The
first grading builds the task's image; each one after that starts a grader
container for about a second. Open `runs/curve-demo/viewer.html` to compare the
candidates. Each evaluation saves `fit.svg`, a plot of the observations and the
candidate's curve, and `residuals.json`, with every prediction and error.

## Make your own proposals

```sh
ari record examples/curve-fitting --config examples/curve-fitting/local.toml --output runs/my-fit
```

The first call records the baseline and prints the workspace path. Edit
`coefficients.json` there, for example to `{"coefficients": [2, 1.5, 0.5]}`, and
run the same command again to record and grade your version. With `local.toml`
review is off, so the task's own validity function decides validity. No agent
is used.

## Run a Harbor researcher

[run.toml](run.toml) configures one researcher proposal. A reviewer judges the
baseline and the proposal against the rules in [task.py](task.py), after the
validity function has passed them. The defaults use Terminus-2 for both roles.
Change `name` and `model_name` in either table to use another Harbor agent, and
replace its `kwargs` with options that agent accepts.

Both agents run in the Harbor task's container, where the observations are. The
task's Dockerfile has only Python, and Terminus-2 also needs `tmux`. Build an
image that adds it and name it in the settings; the grader keeps using the
task's own image:

```sh
docker build -t curve-task examples/curve-fitting/harbor-task/environment
printf 'FROM curve-task\nRUN apt-get update && apt-get install -y --no-install-recommends tmux\n' \
  | docker build -t curve-agents -
docker image inspect curve-agents --format '{{.Id}}'    # write this under [task] image in run.toml
```

You also need the Harbor extra installed and the providers' credentials in the
environment. The
[launch guide](../../skills/use-autoresearch-infrastructure/references/running-autoresearch.md)
has more on images and credentials.

```sh
# Validate the task and the settings. Starts and builds nothing.
ari check examples/curve-fitting --output runs/live-curve

# One researcher start and up to two reviewer starts. This costs money.
ari run examples/curve-fitting --output runs/live-curve
```

With the supplied settings a run makes at most one researcher start of five
minutes and two reviewer starts of two minutes each. The settings also limit each
agent's turns and output tokens. None of these is a cap on cost. If you choose
another agent, set the limits that agent supports.

The researcher edits `/workspace/candidate/coefficients.json`. It can read the
observations in `/app/data/observations.json` and earlier results, with each
plot, in `/history`. It submits once. The run directory keeps the agents'
original logs and trajectories next to the measurements, and `summary.json`
records the outcome, including a failed or unfinished one.
