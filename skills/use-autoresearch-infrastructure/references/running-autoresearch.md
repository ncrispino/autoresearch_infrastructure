# Configure and launch a run

A run needs a task directory containing `task.py`, run settings in `run.toml`,
and a container image if it uses agents. The
[API reference](../../../reference.md) lists every run setting, command option
and saved file. This guide covers the choices you make: agents, budget, image and
credentials, and the order of commands. [Design a task](task-integration.md)
covers `task.py`.

Copy the run settings of the closest example:
[curve-fitting/run.toml](../../../examples/curve-fitting/run.toml) (researcher and reviewer),
[curve-fitting/local.toml](../../../examples/curve-fitting/local.toml) (no agents),
[sum-instructions/run.toml](../../../examples/sum-instructions/run.toml) and
[sum-harness/run.toml](../../../examples/sum-harness/run.toml) (with a solver).

Run settings have four tables:

| Table | Holds |
| --- | --- |
| `[limits]` | `candidates`, `evaluations`, `repeats` and `parallel` |
| `[researcher]` | The agent that proposes candidates. Leave it out to propose them yourself with `ari record`. |
| `[reviewer]` | The agent that judges each candidate against the task's instructions |
| `[task]` | Values the task reads, such as the solver in `[task.solver]` |

## Where each part runs

| Part | Where it runs | What its image must contain |
| --- | --- | --- |
| `ari`, the validity function and a Python evaluator | The host process | |
| Researcher | A container from `--image` | `python` on the path, for the submission client, and what the chosen agent needs: `tmux` for Terminus-2, the CLI for Codex or Claude Code |
| Reviewer | A container from `--image` | What the chosen agent needs |
| Solver of an `ari.bases.harbor_harness` task | Its CLI sessions run in containers from `--image` | The `codex` or `claude` CLI |
| Solver of an `ari.bases.harbor_task_files` task | The Harbor task's own container | What the solver agent needs. Pin a prepared image with `[task] image`. |

The agent containers have a read-only root file system and no added privileges.
The researcher can write to `/workspace` and `/tmp`. It reads the task's context
at `/context` and earlier candidates at `/history`. Graders, the ledger and the
Docker socket stay on the host.

## Configure the agents

`[researcher]` and `[reviewer]` are ordinary Harbor `AgentConfig` tables.

```toml
[researcher]
name = "terminus-2"
model_name = "openrouter/openai/gpt-6-luna"
override_timeout_sec = 300
[researcher.kwargs]
record_terminal_session = false
```

Review is on by default. The reviewer judges each candidate against the task's
instructions, after the task's validity function if it has one.

- Without a `[reviewer]` table, the reviewer uses the researcher's agent. Add
  the table to give the reviewer its own harness, model or timeout.
- To turn review off, write `reviewer = false` at the top of `run.toml`. The
  task's validity function alone then decides validity. Without a validity function, every candidate is
  valid.
- ARI refuses settings that have no `[researcher]`, no `[reviewer]` and no
  `reviewer = false`. It also refuses to run without a settings file.
- The researcher must be a Harbor agent that supports MCP servers and writes
  ATIF trajectories. `codex`, `claude-code` and `terminus-2` qualify.
- `kwargs` holds the agent's own options. An unknown option is refused when the
  run opens, before any start.
- The researcher submits a candidate and then waits while ARI reviews and
  evaluates it. Set its timeout long enough to cover that wait. The default is
  600 seconds and the most is 7200.
- A long evaluation can also outlast the tool-call timeout of the agent's own
  client, which Harbor does not set for Codex. If the connection breaks, ARI
  still finishes and saves the candidate.

Each task defines what it reads from `[task]`. The two Harbor examples read
the solver from `[task.solver]` with different keys, described in
[Harbor tasks](harbor-harness.md).

## Size the budget

`[limits]` bounds candidates and evaluations in one run directory. Let `P` be
the number of proposals you want and `C` the number of cases.

| `[limits]` key | Set it to |
| --- | --- |
| `candidates` | At least `P + 1`, for the baseline. Rejected candidates count too. |
| `evaluations` | At least `candidates × C × repeats` |
| `repeats` | How many times each candidate is measured on each case. The default is 1. |
| `parallel` | How many evaluations run at a time. The default is 1. |

```toml
[limits]             # the baseline plus two proposals, on one case
candidates = 3
evaluations = 3
```

A start is one launch of an agent or a Harbor trial. ARI counts it in
`starts.json` before the launch and does not refund it if the launch fails.
Starts are counts. They do not limit tokens or cost. You do not set them. They
follow from the limits:

| Start kind | Most starts in one run directory |
| --- | --- |
| `researcher` | `candidates - 1` |
| `reviewer` | `candidates`, because the baseline is reviewed too |
| `solver_trial` | `evaluations`, in a task built with `ari.bases` |
| `solver_session` | `evaluations` × `max_sessions`, in an `ari.bases.harbor_harness` task |

A researcher session that ends without a candidate still uses its start. Raise
`candidates` by the number of failed sessions you want to allow for.

Before evaluating a candidate, ARI checks that all of its missing evaluations fit
the remaining budget. If they do not fit, it starts none of them and records the
evaluation as incomplete. `ari run` stops before a proposal it could not
evaluate.

## Prepare the image

Pass an immutable reference with `--image`: an image ID such as
`sha256:<64 hex digits>`, or `repository@sha256:<digest>`. A tag is refused,
because a tag can be moved to a different image during a study.

```sh
docker build --secret id=session_ca,src=/etc/ssl/certs/ca-certificates.crt \
  -t ari-solver:local autoresearch_infrastructure/docker/solver
docker image inspect ari-solver:local --format '{{.Id}}'
```

That [packaged image](../../../docker/solver/README.md) contains Python, tmux,
Codex and Claude Code. Runs without agents need no image.

## Credentials

Set each provider's API key in the environment of the process that runs `ari`.
For `openrouter/...` model names the variable is `OPENROUTER_API_KEY`. The solver
of `HarborHarnessEvaluator` reads `OPENAI_API_KEY`, `OPENROUTER_API_KEY` or
`ANTHROPIC_API_KEY`, depending on its provider.

Do not write keys in `run.toml` or `task.py`. The settings are saved in
`plan.json`, and the task's context is shown to the researcher. Treat a key that
is passed into a container as readable by anything that runs in that container.

## Check, then launch

Run `ari check` first. It opens the task and the settings, saves the plan and
prints the budget. It starts nothing. This example needs no Docker and no models:

```sh
uv run ari check autoresearch_infrastructure/examples/curve-fitting \
  --config autoresearch_infrastructure/examples/curve-fitting/local.toml \
  --output /tmp/curve-check
```

```text
Status: checked
Selected: None
Candidates: 0 of 5 used
Evaluations: 0 of 4 used
Review: the task's validity function
Workspace: /tmp/curve-check/research/sessions/research/workspace/candidate
Viewer: ari view /tmp/curve-check --html <new-file.html>
```

With agents configured, the output also lists `Agent starts`: those used and the limit for each kind.
`Review:` names who decides validity: `the task's validity function, then a reviewer`,
`a reviewer`, `the task's validity function`, or `off`. A mistake in the settings, a missing
image or an unsupported agent makes the command fail before it creates anything.

Then use one of two commands:

- `ari record` saves the current workspace as a candidate, then reviews and
  evaluates it. Use it for edits you make yourself or from a script. The first
  call records the baseline.
- `ari run` measures the baseline, then starts researchers until a budget runs
  out. Each researcher starts from the selected candidate, which is the best
  valid, fully measured candidate so far, and may submit one candidate. This
  command may incur charges, so run it only with the user's approval.

```sh
ari run <task-directory> --output <run-directory> --image <image-id>
```

## Continue or start again

Running the same command again on the same run directory continues with the
budget that is left. Completed measurements are kept.

If the task, the run settings or the image changed, ARI refuses with
`Run plan changed; use a fresh output`. A new run directory has a new
`starts.json`, so its starts begin at zero. Add up the budgets of all the run
directories you report on.

## Read the results

`ari run` and `ari record` write `summary.json` and `viewer.html` in the run
directory. The summary lists each candidate's status, reasons, metrics and
score, the selected candidate, and the budget and starts used. Its `stopped`
field names the budget that ended the run. `observed_cost_usd` is the sum of the
costs that agents reported. `total_cost_usd` is `null` when any start reported no
cost, because an unreported cost is unknown.

```sh
ari view <run-directory> --html <new-file.html>   # one HTML page
ari inspect <run-directory>                       # the same data as JSON
```

The page shows candidates, diffs, verdicts, scores and the original trajectories
of the researcher, the reviewer and any model-driven solver.

<a id="another-optimization-task"></a>

## Drive the loop from your own Python

`ari run` starts one researcher at a time from the selected candidate. For a
different loop, call the same functions from Python:
[`launch.open_run`](../../../src/autoresearch_infrastructure/launch.py) opens a
task with its settings, and
[`search.propose`](../../../src/autoresearch_infrastructure/search.py) runs one
researcher start. [Programmatic use](../../../docs/programmatic.md) describes the
Python interface.
