# Harness study on QuixBugs

This study asks whether a researcher can improve a solver program on an existing
Harbor benchmark while the model, the Harbor tasks and their verifier stay fixed.
It uses a small subset of Harbor's QuixBugs adaptation. Its scores are not official
benchmark scores.

One live run completed. It found no improvement: the baseline stayed selected. See
[What was observed](#what-was-observed).

## Plans

| Plan | Benchmark | Status |
| --- | --- | --- |
| [quixbugs.toml](quixbugs.toml) | Upstream QuixBugs Python tasks at revision `37db108843a49bb31a592e37a75e2c40dc3f9749` | Runnable |
| [plan.toml](plan.toml) | BigCodeBench-Hard, complete split, at revision `4f398de8aacaf392d6ff63da1f3977075c9738b1` | Blocked, `runnable = false` |

`quixbugs.toml` pins two screen tasks (`quixbugs-python-knapsack`,
`quixbugs-python-levenshtein`) and four different confirmation tasks. The solver
deadline is 120 seconds, chosen for this study. In the reference run, all six
upstream oracle solutions passed the unchanged verifiers.

`plan.toml` is the first plan written for this study. Building the BigCodeBench-Hard
images filled this workspace's 32 GB Docker VFS filesystem during the oracle build,
before any paid request. Retry it on a builder with more storage. Do not trim the
image dependencies or rewrite the benchmark to make it fit.

SciCode is a possible later benchmark. Its Harbor adaptation and resource needs
have not been checked. OfficeQA data access and Harbor packaging have not been
verified here.

## Candidate

A candidate is a solver program:

- `program.toml` with the ID `benchmark-harness` and the entrypoint `solve.py:solve`
- `solve.py`, which defines `async def solve(task, runtime)` and returns a `SolverOutput`
- optional local modules and skills

All model inference goes through `runtime.run_harness`. The one task tool, `exec`,
runs commands inside the Harbor task's container. The program sees the task
instruction, the tool and its results. It cannot see the verifier, the reference
solutions, hidden test cases, the plan or the provider settings.

The baseline is the [starter solver](../../../src/autoresearch_infrastructure/programs/starter.py):
one solver session that receives the task instruction.

## How a candidate is measured

The objective is the mean verifier success over the panel. Ties go to the
candidate that uses fewer solver input plus output tokens.

A task the solver fails is a measured reward of zero. An infrastructure error or
missing token usage makes the evaluation incomplete, and an incomplete evaluation
has no score. An unknown cost stays unknown and is never recorded as zero.

Each candidate passes two validity steps before evaluation:

1. A deterministic check of the program package. It also rejects a
   `reasoning_effort` override in `program.toml`.
2. A reviewer agent that judges the candidate against the rules. The exact
   baseline skips this step.

The rules forbid tables of task IDs or answers, access to hidden tests, changes to
the evaluator, direct provider calls, helper sessions that are not counted, and
delegation by the coding-agent CLI itself.

## Budget

These are the limits in `quixbugs.toml`. Each is a count of starts across the
whole study, retries included.

| Plan key | Limit | Used for |
| --- | --- | --- |
| `max_solver_trial_starts` | 24 | 8 screen and 16 confirmation trials |
| `max_harness_session_starts` | 48 | At most 2 sessions per trial (`max_sessions_per_trial`) |
| `max_researcher_invocations` | 4 | Researcher starts, including failed ones |
| `max_reviewer_invocations` | 3 | Reviewer starts |
| `max_reference_trial_starts` | 14 | Oracle and Nop control trials |

The screen panel has two cases and one repeat. The confirmation panel has four
cases and two repeats for the baseline and for the selected program. The module
holds one reviewer start back for confirmation, so at most two new proposals are
reviewed during the screen.

The confirmation panel is never mounted for the researcher. Do not repair a
candidate using confirmation results and then report those results as a fresh
confirmation.

## Reproduce

Install the package with its `harbor` extra. Fetch the upstream QuixBugs export at
the revision above and set `TASKS` to its `datasets/quixbugs` directory. Build the
[solver image](../../../docker/solver/README.md). Use a new `--output` directory.

```sh
PLAN=autoresearch_infrastructure/examples/task-bank/harbor-harness/quixbugs.toml

python -m autoresearch_infrastructure.pilot --check \
  --plan "$PLAN" --tasks-root "$TASKS" --output /tmp/ari-quixbugs --image ari-solver:local

python -m autoresearch_infrastructure.pilot --reference \
  --plan "$PLAN" --tasks-root "$TASKS" --output /tmp/ari-quixbugs --image ari-solver:local

# Paid model calls. Requires OPENROUTER_API_KEY and an approved budget.
python -m autoresearch_infrastructure.pilot --run \
  --plan "$PLAN" --tasks-root "$TASKS" --output /tmp/ari-quixbugs --image ari-solver:local
```

`--check` validates the plan and writes `plan.json`. It calls no model.
`--reference` runs the upstream oracle solutions and the Nop control on
`quixbugs-python-knapsack`. It records task hashes, build hashes and the Harbor
results in `references.json` and `controls/`. `--run` refuses to start unless
those results passed for the same task and build bytes.

The module does not edit the upstream task files. It builds each task through a
generated Compose overlay in `build-overlays/`, which changes two things:

- The public CA bundle is mounted as a build secret and as a read-only file at
  run time, so TLS verification stays on. The bundle is never written into an
  image layer.
- The QuixBugs line `RUN curl -LsSf https://astral.sh/uv/install.sh | sh` is
  replaced with `RUN python -m pip install uv==0.12.23` (`uv_install_version`
  in the plan), so the build needs only the permitted package registry. The
  overlay records the hashes of the original and the derived Dockerfile.

### Files in the run directory

| File | Content |
| --- | --- |
| `plan.json` | The plan, task hashes and image IDs this output is bound to |
| `paid-starts.json` | The ledger |
| `references.json`, `controls/` | Reference results |
| `screen/`, `confirmation/` | One `Research` output per panel |
| `promotion.json` | The selected screen candidate and its source hash |
| `summary.json` | Confirmation state and ledger counts |
| `screen.html`, `confirmation.html` | Viewers |

### Continuing after a failure

Rerunning with the same `--output` continues the study only if the plan, the code
and the images are unchanged. After a code repair, use a new `--output` and pass
`--allocation-ledger` with the path of the original `paid-starts.json`. The
original counts then still apply. Never copy or reset a ledger.

When you report results, label each as fixture, reference, transport or live model
evidence. Give sample counts, per-task regressions, token usage and incomplete
evaluations.

## What was observed

Live model evidence, from one run. The full record is in the
[run report](https://github.com/ncrispino/prompt-injection-coevolution/blob/main/docs/results/harbor-harness-pilot-20261027.md).

- The run completed after a credential problem was fixed. The two starts that
  failed on authentication stayed counted.
- The baseline passed both screen cases and all eight confirmation evaluations.
- One proposal was rejected as invalid because it overrode the reasoning budget.
- The other proposal, a workflow prompt, was valid. It matched the baseline's
  screen success and used 19.2% more tokens, so the baseline stayed selected.
- Starts used: 14 trials, 14 solver sessions, two researchers and two reviewers.
- The unique native ATIF trajectories report $0.03327982 of observed model cost.
  The cost of the two authentication failures is unknown.

This run shows that the study procedure works end to end. It found no harness
improvement on this small panel.

To try a different starting program, see the
[sample solver programs](../../harness-programs/README.md).
