# Reference

This page describes the Python API, the `ari` command and the files they write.
To install ARI and run a first experiment, start with the [README](README.md). To
drive an experiment from your own Python, see
[Programmatic use](docs/programmatic.md).

```python
import autoresearch_infrastructure as ari
```

## Define a task

A task is a file, `task.py`, that defines `task(run)` and returns an `ari.Task`.

```python
import autoresearch_infrastructure as ari

LENGTH = ari.Metric("length", direction="min")
INSTRUCTIONS = ari.Instructions(
    objective="Improve answer.txt so that the answer is shorter.",
    rules=["The answer is not empty.", "answer.txt stays the only file."],
)


def validity(candidate, context):
    text = (candidate / "answer.txt").read_text()
    return ari.Validity() if text.strip() else ari.Validity("invalid", ("Empty answer",))


def evaluate(candidate, context):
    text = (candidate / "answer.txt").read_text()
    (context.artifacts / "answer-copy.txt").write_text(text)
    return ari.Evaluation({LENGTH: len(text)})


def task(run):
    return ari.Task(
        instructions=INSTRUCTIONS,
        objective=LENGTH,
        evaluator=evaluate,
        validity=validity,
    )
```

This task needs a `seed/answer.txt` beside it. A candidate is one saved version
of the seed files, and both functions receive its directory. The unchanged seed
files are the baseline, saved as `candidate-0001`.

A task has four parts:

| Part | Meaning |
| --- | --- |
| `seed` | Directory of starting files. Paths are relative to the task file; the default is `seed/` beside it. |
| `instructions` | What the researcher is told: an `ari.Instructions(objective=..., rules=[...], environment=[...])`, or Markdown with `## Objective` and `## Constraints` (`### Rules`, `### Environment`). A `Path` names a Markdown file; the default is `instructions.md` beside the task file. ARI lays the instructions out the same way for every task and adds what the task already knows: the starting files, the metric and its direction, tie-breakers, bounds, and the number of cases and repeats. Do not restate the direction in prose. |
| `objective` | The `Metric` to improve, or an `Objective`: what is measured and how candidates are ranked. This is not the written goal in `Instructions.objective`. See [Metrics, objectives and bounds](#metrics-objectives-and-bounds). |
| `evaluator` | Measures one candidate on one case. See [Write an evaluator](#write-an-evaluator). |

Six more are optional:

| Optional part | Meaning |
| --- | --- |
| `validity` | A function that decides the constraints code can decide exactly. It runs before the reviewer. See [Validity](#validity). |
| `cases` | Names of the cases: the fixed inputs every candidate is evaluated on. The default is one case named `default`. |
| `context` | Read-only files for the researcher, by file name under `/context`. A `Path` is copied, a string is written as text, and any other value is saved as JSON. Context is bounded to 256 MiB per file, 512 MiB total and 2,000 regular files; symbolic links, hard-linked files and special files are rejected. |
| `environment` | An `ari.Environment`: a Harbor environment definition for the researcher's container, and whether files outside the candidate survive between starts. See [Researcher inputs](#researcher-inputs). |
| `memory` | An `ari.Memory`: which earlier candidates a researcher sees, preloaded records, and earlier researcher conversations. See [Researcher inputs](#researcher-inputs). |
| `presenter` | A function `(ResearcherInputs, Start) -> str` that writes the prompt of one start. The default, `ari.present`, is the standard layout. |

`task(run)` can read the run before it builds its evaluator. `run.settings` holds
the parsed run settings, `run.output` is the run directory and `run.image` is the
`--image` value. `task.py` can import modules beside it.

To build a task on Harbor tasks, call a [Harbor base task](#harbor-base-tasks)
in place of `ari.Task`.

## Run settings

Run settings are one TOML file, `run.toml`, with four tables and no others:

| Table | Contents |
| --- | --- |
| `[limits]` | How much the run may do. The keys are listed below. |
| `[researcher]` | The Harbor agent that proposes candidates, as a Harbor `AgentConfig` table with a `model_name`. Leave it out to propose candidates yourself with `ari record`. |
| `[reviewer]` | The Harbor agent that judges each candidate against the task's instructions. Leave it out to use the researcher's agent. |
| `[task]` | Values the task reads from `run.settings.task`. `[task.solver]` is the solver: the agent being evaluated, in tasks that evaluate one. |

| `[limits]` key | Default | Meaning |
| --- | --- | --- |
| `candidates` | 10 | Candidates the run may save, including the baseline. |
| `evaluations` | 20 | Evaluations the run may start. |
| `repeats` | 1 | Times each candidate is evaluated on each case. A candidate needs this many complete repeats before it can be selected. |
| `parallel` | 1 | Evaluations that run at the same time. |

Review is on by default. To turn it off, write `reviewer = false` above the
tables. Then the task's `validity` function alone decides validity. Settings that have no
`[researcher]`, no `[reviewer]` and no `reviewer = false` are refused, and so is
running without a settings file.

The limits also fix the agent starts: one reviewer start for each candidate, and
one researcher start for each candidate after the baseline. See
[Starts and the ledger](#starts-and-the-ledger).

The researcher's timeout (`override_timeout_sec`) defaults to 600 seconds and the
reviewer's to 240. The maximum is 7200. The researcher must be a Harbor agent
that supports MCP servers and ATIF trajectories. Credentials and agent options
use Harbor's own configuration. The
[curve-fitting settings](examples/curve-fitting/run.toml) are a complete example
with agents, and [local.toml](examples/curve-fitting/local.toml) is one without.

## Commands

| Command | What it does |
| --- | --- |
| `ari check TASK --output DIR` | Opens the task and prints its budget and who reviews. Starts nothing. |
| `ari record TASK --output DIR` | Saves the workspace as a candidate, then reviews and evaluates it. Use it for your own edits. |
| `ari run TASK --output DIR` | Evaluates the baseline, then starts researchers until a budget runs out. This calls your models. |
| `ari extend TASK --from SRC --output DIR` | Continues the finished run `SRC` in a new directory with the larger limits of `--config`. Starts nothing; run `ari run` on `DIR` next. See [Extend a finished run](#extend-a-finished-run). |
| `ari import TASK --from SRC --output DIR` | Starts a new run that reviews and evaluates the candidates of `SRC` again under this task. Starts nothing; `ari run` on `DIR` does the work. |
| `ari prepare TASK --output DIR` | Builds or resolves the task's Harbor environments and records their images. Starts no agent. `run` and `record` do this first themselves. |
| `ari view DIR --html FILE` | Writes the results as one HTML page. `FILE` must not exist yet. |
| `ari inspect DIR` | Prints the results as JSON. |

`TASK` is a directory containing `task.py`, or the file itself. `DIR` is the run
directory. The commands that open a task also take:

- `--config FILE`: the run settings. The default is `run.toml` beside the task.
- `--image ID`: the container image for the researcher and reviewer, required
  when the settings have either. A researcher with a Harbor `environment`, or a
  reviewer with a `review_environment`, does not use it.
- `--reviewer-image ID`: a separate image for the reviewer. Without it the
  reviewer uses `--image`, as before. It must be an image ID (`sha256:...`) or a digest
  (`repository@sha256:...`), so a moved tag cannot change a run.
- `--json`: print one JSON summary and turn off the progress display.

These commands print a `Review:` line that says what decides validity in this
run: `the task's validity function, then a reviewer`, `the task's validity function`,
`a reviewer` or `off`.

If the baseline is invalid or not fully measured, `ari run` starts no researcher.
Each researcher works on a copy of the selected candidate and may submit one
candidate. The run stops when the candidates, the evaluations or the starts of
either agent run out. Running the command again continues with the budget that
is left.

The first command to open a run directory writes `plan.json`: the settings, the
image, the instructions, and hashes of the task file, the context files and the
package source. Opening the directory again after any of these changed fails
with `Run plan changed; use a fresh output`, so editing the settings cannot reset
a budget. ARI also rehashes the copied `context/` tree before reusing it, so
tampering with a run's staged context blocks resume.

The exit status is 0 on success, 1 when a recorded candidate was not measured or
a run failed, and 2 when the task or settings could not be opened.

### Extend a finished run

Three ways to reuse a run differ in what they keep:

| You want | Use | What happens |
| --- | --- | --- |
| To finish the budget a run already has | `ari run` on the same directory | Same-plan resume. Any change to the plan is refused. |
| More candidates or evaluations than a finished run allowed | `ari extend` into a new directory | The source is copied and verified. Its candidates keep their IDs, parents, reviews, measurements and selection, and their evidence is reused without new calls. |
| To judge saved candidates under a changed evaluator or rules | `ari import` into a new directory | The source's candidates are recorded again with the same IDs, parents and proposals, then reviewed and evaluated under the new task before any researcher starts. No evidence is reused, and the new run's limits pay for it. |

```sh
ari extend examples/curve-fitting --from runs/curve --output runs/curve-more \
  --config more.toml --image <image-id>
ari run examples/curve-fitting --output runs/curve-more --image <image-id>
```

The limits in `more.toml` are cumulative totals, not additions, and may not be
lower than the source's. Every start the source used, including failed starts
and starts of unknown cost, stays counted. The next researcher starts from the
imported selected candidate and sees the imported history under `/history`.

Evidence is reused only if everything it depends on is unchanged: the task file,
the image, the `[reviewer]` and `[task]` tables, the repeat limits, the
instructions, the context and the package source. Otherwise `ari extend` names
the fields that changed and creates nothing. The researcher's own settings,
`[researcher]`, the memory, the environment and the presenter may change.

Before the new directory is usable, ARI checks every snapshot, result and
artifact of the source against its saved hash. A tampered source, or one with an
interrupted evaluation, is refused and leaves no new directory. The source is
only read. Its `plan.json` and `starts.json` are bound into the new plan, so
reopening the extension after the source changed is refused. The viewer shows
the source and marks imported candidates.

`ari import` needs the source's baseline to equal the task's starting files and
limits large enough for every source candidate. Each snapshot is checked against
its saved hash first. Re-recorded candidates keep the source's proposal, with
`reevaluated_from` added. Reopening the new directory finds its source in
`plan.json`, refuses a changed source, and continues any re-evaluation that was
interrupted.

## Run directory

```text
DIR/
  plan.json          the task, settings and researcher inputs this run was opened with
  starts.json        the ledger, when the run has agents
  context/           the researcher's read-only files
  memory/preloaded/  preloaded records, when the memory has them
  environment/       the researcher's Harbor environment, when the task has one
  review-environment/  the reviewer's Harbor environment, when the task has one
  prepared/          the image identities each environment was prepared with
  research/          the ari.Research output
    state.json
    sessions/research/
      seed/                  the starting files
      workspace/candidate/   the workspace
      history/index.json     a summary of every candidate
      history/candidates/candidate-0001/
        snapshot/            the saved files
        diff.patch           changes from the parent
        validity/            verdicts
        evaluations/         results and artifacts
      agent/invocations/     researcher logs and trajectories
      agent/inputs/          each start's exact prompt and input manifest
      agent/retained/        files exported after each start, when they are kept
      agent/researcher-starts.json   every researcher start, oldest first
  summary.json       the outcome of the last run or record
  viewer.html        the same results as a page
```

Keep the run directory of a paid run in storage that persists, outside the source
checkout.

## Define a small experiment

`ari.Research` is the object under every run. It works without a task file or the
`ari` command.

```python
from pathlib import Path

import autoresearch_infrastructure as ari
from task import LENGTH, evaluate, validity  # the functions from "Define a task"

with ari.Research(
    Path("runs/short-answer"),
    seed=Path("seed"),
    objective=LENGTH,
    validity=validity,
    evaluator=evaluate,
    evaluation_config={"measurement_version": 1},
    limits=ari.Limits(max_candidates=5, max_evaluations=12),
) as research:
    baseline = research.record_candidate()
    (research.workspace / "answer.txt").write_text("A short answer")
    result = research.record_candidate(parent_id=baseline["candidate_id"])
    print(result["candidate_id"], result["status"], result["evidence"]["metrics"])
```

`seed`, `objective` and `evaluator` are the task parts of the same names.
`Research` has no `instructions`, because it starts no agent by itself. Its other
arguments are:

| Argument | Meaning |
| --- | --- |
| `validity` | A validity function, an `LLMValidity` or a `FileValidity`. See [Validity](#validity). |
| `evaluation_config` | JSON data that names fixed inputs that are not code, such as a dataset version. |
| `limits` | An `ari.Limits`. Its fields are `max_candidates`, `max_evaluations`, `selection_repeats`, `parallel_evaluations` and `max_repeats`, the most repeats one candidate may have (default 3). |
| `tasks` | The case names. |
| `conditions` | Optional names of a second dimension. Every case is evaluated under every condition. |
| `metrics` | Every `Metric` the evaluator returns. Optional when the objective names them all. |

Define the evaluator and validity functions in a `.py` file. ARI hashes their
source file, which a REPL does not have.

### Record a candidate

`research.workspace` is the workspace: the editable copy of the files.
`record_candidate(candidate="workspace", total_repeats=1, parent_id=None, proposal=None)`
saves it as the next candidate, checks validity and evaluates every case.
`parent_id` names the candidate the edit started from and is used for the saved
diff. `proposal` is a dictionary of your notes, such as a hypothesis.

| Result key | Meaning |
| --- | --- |
| `candidate_id` | For example `candidate-0002`. |
| `status` | `valid` or `invalid`. |
| `selected` | The selected candidate after this call, or `None`. |
| `remaining_evaluations` | Evaluations left in the limit. |
| `evidence["validity"]` | The verdict and its reasons. |
| `evidence["evaluation"]` | `not_started`, `completed` or `incomplete`. |
| `evidence["metrics"]` | The mean of each metric, by metric name. |
| `evidence["score"]` | The objective's value. |
| `evidence["feasible"]` | Whether every bound holds. `evidence["constraint_failures"]` lists the measurements that broke one. |
| `evidence["total_repeats"]` | The number of complete repeats. |

An invalid candidate is saved and not evaluated. Reaching the candidate limit
raises `ValueError`. If the missing evaluations do not fit the remaining budget, none
starts and `evaluation` is `incomplete`.

The selected candidate is the valid, feasible candidate with the best objective
among those with at least `selection_repeats` complete repeats (`repeats` in the
run settings). An exact tie
keeps the earlier candidate.

### Add repeats

Pass a saved candidate's ID to evaluate it again:

```python
research.record_candidate("candidate-0002", total_repeats=2)
```

`total_repeats` is a target. ARI runs only the evaluations the candidate is
missing, so this call adds one repeat to a candidate that has one. The same call
retries evaluations that failed or were interrupted.

### Read results

| Member | Returns |
| --- | --- |
| `research.candidates` | Saved candidate records by ID, in order. |
| `research.proposals` | The number of saved candidates, including the baseline. |
| `research.selected` | The selected candidate's ID, or `None`. |
| `research.evidence(id)` | The evidence dictionary of one candidate. |
| `research.measured(id)` | Whether a candidate is valid and fully measured. |
| `research.candidate_files(id)` | The read-only directory of a saved candidate. |
| `research.evaluation_artifacts(id)` | A tuple of integrity-checked `EvaluationArtifact` records for completed attempts. Each record names its case, condition, repeat, attempt number, researcher-visible `artifacts`, and optional trusted `private_artifacts`. Private evidence is sealed with the result but never mounted in `/history`. Failed or interrupted attempts are omitted; changed evidence raises `ValueError`. |
| `research.reset_workspace(id)` | Replaces the workspace with a saved candidate's files. |
| `research.budget_summary()` | Limits, use and remainder for candidates, evaluations, reviews and ledgers. |

### Resume

Open the same output with the same setup to continue. Completed evaluations are
reused. `record_candidate()` with no ID always saves a new candidate, so a script
that resumes should read `research.candidates` first.

The setup is the objective, metrics, limits, cases, conditions, starting files,
`evaluation_config`, the source files of the evaluator and validity functions, the
package source and the Python version. If any of them changes, opening fails with
`Experiment setup changed; use a fresh output`. ARI cannot see inputs that your
functions load at run time, so put the versions of datasets, models and
dependencies in `evaluation_config`.

Evidence is keyed by an evaluation contract that leaves out allocation: the
candidate, evaluation and parallel limits and the review budget. Resuming the
same output with different limits is still refused. To continue a finished
experiment under larger limits, pass `extends=` the finished output when
creating a new one; `ari extend` does this for a run directory.

One process writes an output at a time. A forked child must open its own
`Research`.

## Metrics, objectives and bounds

Declare each metric once and use the same object as the key everywhere.

```python
QUALITY = ari.Metric("quality")
TOKENS = ari.Metric("tokens", direction="min")
LATENCY = ari.Metric("latency_seconds", direction="min", maximum=30)


def score(m):
    return m[QUALITY] - 0.001 * m[TOKENS]


objective = ari.Objective(score, tie_breakers=(LATENCY,), metrics=(QUALITY, TOKENS, LATENCY))
```

`ari.Metric(name, direction="max", minimum=None, maximum=None, bounds_scope="each")`
declares one measurement and, optionally, a bound on it.

`ari.Objective(score, direction=None, tie_breakers=(), description="", metrics=None)`
ranks candidates. `score` is a metric, or a function of the metric values that
returns one finite number. A function is maximized unless `direction="min"`. When
scores tie, the tie-breaker metrics decide in order. A bare `Metric` works as an
objective.

`metrics` lists every metric the evaluator returns. Pass it when `score` is a
function, or when the evaluator returns metrics that the score and the
tie-breakers do not name. The extra metrics are recorded and shown, and do not
affect selection unless they carry a bound.

- Each metric is averaged with equal weight over every case, condition and
  complete repeat.
- The objective is computed from those means. A ratio objective is therefore a
  ratio of means.
- A bound applies to each evaluation. One repeat over a memory limit makes the
  candidate infeasible even when the mean is under the limit. A completed
  evaluation that breaks a bound counts even if its repeat is not complete.
- `bounds_scope="mean"` applies the bound to the mean, as in
  `ari.Metric("accuracy", minimum=0.8, bounds_scope="mean")`.
- A candidate that breaks a bound is measured and shown, and cannot be selected.

A wrong answer that was graded normally is a completed measurement with a low
metric, and it competes. A missing or unfinished evaluation has no value. It is
never treated as zero, and its candidate cannot be selected.

`research.check_metrics(ari.Evaluation({...}))` scores sample values without
running validity or the evaluator, and uses no budget. It reports the score,
whether the sample is feasible and which metrics the objective read. It tests one
sample, so it does not prove every branch of a score function.

## Validity

Validity decides whether a candidate follows the task's constraints. It is
separate from how well the candidate scores. An invalid candidate is saved with
its reasons and is not evaluated.

`ari` decides validity in two steps. The task's `validity` function runs first, if the task
has one. The reviewer then judges the candidates that pass. With review turned
off, the validity function alone decides, and a task without a validity function accepts every candidate.

### Check

A validity function is a function `(candidate, context) -> ari.Validity`. Return
`ari.Validity()` to accept or `ari.Validity("invalid", ("reason",))` to reject. Use
a validity function for anything a program can decide exactly, such as file names, schemas
and numeric ranges.

If a validity function raises, the candidate is recorded as invalid with the error as its
reason. Recording that saved candidate again reruns the validity function.

### The reviewer

The reviewer is a Harbor agent that reads a candidate and judges whether it
obeys each rule listed under Constraints, Rules in the task's instructions. Each
rule is a separate check, named `rule_1`, `rule_2` and so on, and the verdict says
which one failed. Write rules about facts the reviewer can see in the candidate
and the baseline. With review on, instructions that list no rules are refused.

What the reviewer is given:

| Given | Where |
| --- | --- |
| The rules, one criterion each | `required_criteria` in the assignment |
| The task specification: the exact text the researcher read, with the Objective, the generated "What is measured" list and both Constraints lists | `context.task_specification` in the assignment |
| The candidate's files and the baseline's files | `/review/candidate`, `/review/baseline` |

The specification is there so the reviewer can tell what a rule refers to. It
adds no criteria. The reviewer is told not to fail a candidate for a low score
or for anything outside the listed rules, and to apply a rule and explain the
conflict if a rule and the specification seem to disagree.

The reviewer is not given the researcher's submission steps, the `context`
files, earlier candidates, the evaluator, its data or any score. The
specification is part of the reviewer's identity, so editing only the Objective
or the Environment list still requires a fresh output, and an old verdict is
never reused for a changed specification.

Each review is one Harbor job with one attempt and uses one `reviewer` start. A
candidate that fails the validity function uses none. The reviewer reads the candidate at
`/review/candidate`, the baseline at `/review/baseline` and its assignment at
`/review/assignment.json`. It must write `/workspace/validity/verdict.json`. ARI
accepts the verdict only if all of these hold:

- It carries the hashes of this candidate and this experiment setup.
- It gives `pass` or `fail` for every criterion.
- Every `pass` cites at least one file that exists in the candidate.
- It says `valid` only when every criterion passes.

A missing, malformed or mismatched verdict file makes the candidate invalid. The
reviewer's messages are never read as a verdict. A saved verdict is reused when
repeats are added. A verdict is a judgment. It does not prove that a candidate is
correct or better.

### Build a reviewer yourself

`ari` builds its reviewer as an `ari.LLMValidity` with one criterion, the task's
instructions. With `ari.Research`, build one yourself and pass it as `validity`:

```python
validity = ari.LLMValidity(
    {"scope": "The candidate contains only answer.txt."},
    agent={"name": "terminus-2", "model_name": "openrouter/openai/gpt-6-luna"},
    image="<image-id>",
    validity=validity,
)
```

- The first argument holds the criteria: 1 to 32 names with the text the
  reviewer judges. It is a mapping, or the path of a `.toml` or `.json` file.
- `agent` is a Harbor `AgentConfig` or its dictionary form. `image` is an image ID
  or digest that contains the agent.
- `precheck` is an optional validity function that runs before the reviewer.
- `context` is optional JSON data holding facts the reviewer may trust, such as
  which inputs are fixed.
- `max_reviews` is the number of reviewer starts allowed for this output. The
  default is 10, counted in `validity-starts.json`. Pass `ledger=` instead to
  count reviews in a shared `StartLedger` that has a `reviewer` limit.

Creating an `LLMValidity` starts nothing.

`FileValidity(reviewer, criteria=...)` in `autoresearch_infrastructure.review`
applies the same verdict conditions to your own `reviewer(candidate,
verdict_path, prompt)` function. You are responsible for isolating it.

### Relabel a finished run

The reviewer is part of the run: it judges against the rules and text the
researcher was given. A study that changes those, for example by rewording or
leaving out a rule, may also want every candidate judged against the original
rules afterwards. ARI has no command for this, but a run saves what it needs.
Paths are relative to the run directory:

| Saved output | What it holds |
| --- | --- |
| `plan.json`, key `instructions` | The exact text the researcher read |
| `research/sessions/research/history/candidates/<id>/snapshot/` | Each candidate's files, frozen. The baseline is `candidate-0001`. |
| `research/sessions/research/history/candidates/<id>/validity/<contract>/attempt-NNNN/assignment.json` | The criteria and task specification each review used |
| `research/sessions/research/history/index.json` | Every candidate's parent, proposal, verdict and scores |

Candidates the reviewer rejected were not evaluated, so they have no scores.

## Write an evaluator

An evaluator is a function or callable object `(candidate, context) ->
ari.Evaluation`. ARI calls it once for each evaluation: one candidate on one case
and repeat.

| `context` attribute | Meaning |
| --- | --- |
| `task`, `condition`, `repeat` | The case name, the condition name and the repeat number, from 1. |
| `artifacts` | An empty directory for files to keep with this evaluation. |
| `candidate_hash`, `contract_hash` | Hashes of the candidate's files and of the experiment setup. |

| `ari.Evaluation` field | Meaning |
| --- | --- |
| `metrics` | A dictionary with exactly the declared metrics as keys and finite numbers as values. |
| `completed` | `False` when the measurement did not finish. A graded failure is still `True`, the default. |
| `trajectory` | Optional path of an ATIF trajectory file inside `context.artifacts`. |
| `metadata` | Optional JSON data to save with the result. |

If the evaluator raises, the evaluation is recorded as missing and still counts
against the evaluation limit. Files in `context.artifacts` are hashed and saved with
the result.

The evaluator runs in the controller process, as do the validity function and the objective
function. ARI never imports or executes candidate files. An evaluator that
executes them must isolate that execution.

An evaluator object may also define `identity` (JSON data describing its
configuration, which becomes part of the setup), `ledger` (a `StartLedger` that
`Research` holds while it is open) and `reserve_evaluations(count)`. ARI calls the
last one with the number of missing evaluations before any starts, and the
evaluator raises to refuse them all.

## Starts and the ledger

A start is one launch of an agent or a Harbor trial. `StartLedger` is a file that
counts starts against fixed limits.

```python
from pathlib import Path

from autoresearch_infrastructure.ledger import StartLedger

ledger = StartLedger(
    Path("runs/study/starts.json"),
    limits={"researcher": 2, "reviewer": 3},
    identity={"study": "example"},
)
key = ledger.start("researcher", "first proposal")
ledger.finish(key, status="completed", cost_usd=None)
print(ledger.remaining("researcher"))  # 1
```

A start is counted before the agent or trial begins and is not refunded if it
fails. `start` raises `StartLimitError` at the limit.
`require_capacity({"reviewer": 3})` raises unless that many starts still fit.

Starts are counts. They do not limit tokens or cost. `finish` records a start's
cost when the agent reports one. A start with no reported cost makes the total
unknown.

`ari` creates one ledger for a run that has agents, in `starts.json`. The limits
follow from the run settings:

| Kind | Used by | Limit |
| --- | --- | --- |
| `researcher` | Each researcher start. | `candidates` minus one, because the baseline needs no researcher. |
| `reviewer` | Each review. | `candidates`. |
| `solver_trial` | Each Harbor trial that a Harbor evaluator runs. | Named by the task. |
| `solver_session` | Each solver session inside a `HarborHarnessEvaluator` trial. | Named by the task. |

A task whose evaluator starts agents names those kinds in one call,
`run.starts(solver_trial=20)`. The call returns the run's ledger to pass to the
evaluator. The [Harbor base tasks](#harbor-base-tasks) make this call for you.

Several `Research` outputs can share one ledger file. Reopening a ledger with
different limits or a different `identity` fails, and one controller process holds
a ledger at a time. The ledger is the only record of the starts used. If the file
is lost, do not recreate it from a remembered count.

The candidate and evaluation limits are separate from the ledger. They bound one
`Research` output.

## Human terminal progress

`Research` prints nothing by default. Attach `TerminalProgress` to show two status
lines on an interactive terminal:

```python
from autoresearch_infrastructure.progress import TerminalProgress

with TerminalProgress() as progress:
    with ari.Research(..., progress=progress) as research:
        research.record_candidate()
```

The display shows the current candidate, the stage, evaluations started and
measured, and the latest metrics. It writes to stderr, and only when stderr is a
terminal and `TERM` is not `dumb`. It does not show an agent's messages.

The `ari` commands attach this display unless you pass `--json`. An agent that
runs `ari` in a terminal should pass `--json`. In Python, pass
`TerminalProgress(enabled=False)` or omit `progress`. Any function that takes one
event dictionary can be the observer, and an error in it does not change what is
recorded.

## Harbor base tasks

A Harbor task is Harbor's unit: an instruction, a container and tests. `ari.bases`
holds two base tasks for experiments on Harbor tasks. Each is a function that
returns a complete `ari.Task`, so a task file is a few lines. Start here for any
experiment that a Harbor task's own grader scores.

| Base task | A candidate is | What stays fixed |
| --- | --- | --- |
| `ari.bases.harbor_task_files` | Files of the Harbor tasks, such as `instruction.md`. | The solver. |
| `ari.bases.harbor_harness` | The solver's harness. | The Harbor tasks. |

Both base tasks do the following:

- They read the solver from `[task.solver]` in the run settings.
- They use each key of `harbor_tasks` as a case name.
- They use one `solver_trial` start for each evaluation and allow as many as
  `evaluations` in `[limits]`.
- They build each Harbor task's image from its Dockerfile. To use a prepared
  image, write `image = "sha256:..."` under `[task]`.

They need Docker and the `harbor` extra, which installs Harbor 0.24.0.

### Edit Harbor task files and keep the solver fixed

```python
import autoresearch_infrastructure as ari


def task(run):
    return ari.bases.harbor_task_files(
        run,
        harbor_tasks={"sum": "harbor-task"},
        editable=["instruction.md"],
    )
```

```toml
[task.solver]        # a Harbor AgentConfig table
name = "terminus-2"
model_name = "openrouter/openai/gpt-6-luna"
```

| Argument | Meaning |
| --- | --- |
| `harbor_tasks` | A mapping from case name to Harbor task directory. |
| `instructions` | The task's instructions, as in `ari.Task`. |
| `editable` | The files of each Harbor task that a candidate may change. The default is `["instruction.md"]`. `None` makes the whole Harbor task editable, including its tests. |
| `validity` | An optional validity function. |
| `objective`, `rewards` | The default objective is `SUCCESS_RATE`, read from each Harbor task's single reward. For other rewards, pass an objective and `rewards={METRIC: "reward_key"}` together. |

A candidate is laid out as `CASE/path/inside/task`, so this one holds
`sum/instruction.md`. The solver's timeout defaults to 120 seconds. See the
[sum-instructions example](examples/sum-instructions/README.md).

### Edit the solver's harness and keep the Harbor tasks fixed

A harness is the program, prompts, skills and hooks that drive a coding-agent
CLI. The candidate is a solver program: `program.toml`, `solve.py` and any prompts
or skills.

```python
def task(run):
    return ari.bases.harbor_harness(
        run,
        harbor_tasks={"sum": "harbor-task"},
        advice="Do not hard-code the input numbers or the expected total.",
    )
```

```toml
[task.solver]
harness = "codex"          # or "claude-code"
provider = "openrouter"
model = "openai/gpt-6-luna"
```

| Argument | Meaning |
| --- | --- |
| `harbor_tasks` | A mapping from case name to Harbor task directory. |
| `seed` | The directory of the starting solver program. |
| `advice` | Text added to the package's standard instructions for a harness researcher. |
| `validity` | An optional validity function. It runs after the built-in check, which requires a loadable solver program that keeps the seed's ID. |

`[task.solver]` also accepts `timeout_seconds` for each trial (default 120) and
`max_sessions`, the solver sessions one trial may start (default 1). The
coding-agent CLI comes from the `--image` container, so this base task needs
`--image` even with review turned off. The objective is `SUCCESS_RATE`. The
researcher's `/context` holds the solver program interface and an example. See
the [sum-harness example](examples/sum-harness/README.md).

### Fork a trajectory and its environment

`ari.ContinuationPoint` is the stable JSON contract for branching after a
settled tool call. It stores provenance, the source rollout ID and call count,
plus separate agent and environment states. Each state has an adapter identity,
and the whole record has a verified `sha256:` ID and `format_version`.

Use `await ari.capture_continuation(...)` only while tool execution is quiescent.
Use `await ari.load_continuation(...)` to branch: it checks the format,
identities and Harbor-style agent capability (`load_native_trajectory` or
`load_atif_trajectory`) before changing either side, restores the environment,
then calls the agent adapter's load-and-resume operation. An agent failure after
environment restore raises `ari.ContinuationRestoreError` with
`environment_restored=True`; restoration is not claimed to be transactional.

Adapters implement `ari.AgentContinuationAdapter` and
`ari.EnvironmentContinuationAdapter`. The two protocols are intentionally
independent because a loaded conversation cannot reconstruct files, services,
locks or processes. ARI supplies the contract and ordering, not an unsupported
capability for a Harbor agent.

## Harbor evaluators

The base tasks are built on two evaluators, which you can construct yourself
when a base task does not fit. Each runs Harbor trials and reads the rewards of
the Harbor task's own grader. Each evaluation uses one `solver_trial` start from
the ledger you pass.

A single reward between 0 and 1 becomes the `SUCCESS_RATE` metric from
`autoresearch_infrastructure.harbor_trial`. For other rewards, pass
`metrics={METRIC: "reward_key"}`. A trial that the grader scored zero is a
completed measurement. A trial that Harbor could not finish leaves the evaluation
missing.

### `HarborTaskEvaluator`

```python
import autoresearch_infrastructure as ari
from autoresearch_infrastructure.harbor_task import HarborTaskEvaluator
from autoresearch_infrastructure.harbor_trial import SUCCESS_RATE


def task(run):
    evaluator = HarborTaskEvaluator(
        tasks={"sum": run.directory / "harbor-task"},     # case name -> Harbor task directory
        agent=run.settings.task["solver"],       # the solver, a Harbor AgentConfig
        ledger=run.starts(solver_trial=run.settings.limits.max_evaluations),
        editable={"sum": ["instruction.md"]},    # the files a candidate contains
    )
    return ari.Task(
        seed=evaluator.seed(run.output / "seed"),   # writes the starting files
        objective=SUCCESS_RATE,
        evaluator=evaluator,
        cases=("sum",),
    )
```

A candidate's files replace the same files in a copy of the Harbor task. Without
`editable`, the candidate is the whole Harbor task including its tests, so the
instructions must require that the Harbor task and its grader still mean the
same thing.

A candidate can never change `task.toml`, a Compose file or a `.env` file. The
evaluator supports single-container, single-step Linux Harbor tasks without task
MCP servers. A change under `environment/` forces an image rebuild, which runs
without network access. `task_with_image(source, image, destination)` copies a
Harbor task with its image pinned to an image ID.

### `HarborHarnessEvaluator`

```python
from autoresearch_infrastructure.harbor_harness import HarborHarnessEvaluator


def harness_evaluator(run):
    evaluations = run.settings.limits.max_evaluations
    return HarborHarnessEvaluator(
        tasks={"sum": run.directory / "harbor-task"},
        solver_image=run.image,           # image ID of the image with the CLI
        model="openai/gpt-6-luna",
        harness="codex",                  # or "claude-code"
        ledger=run.starts(solver_trial=evaluations, solver_session=evaluations),
        max_harness_sessions=1,           # solver sessions per trial, default 2
    )
```

The provider defaults to OpenRouter. `providers.provider_for(harness, name)` builds
another. `include_solver_tokens=True` adds the `SOLVER_TOKENS` metric, which is
input plus output tokens. `timeout_seconds` defaults to 120.

`program.toml` names `solve.py:solve` as the entry point. The solver program runs
in its own container without network access, and the controller never imports
it. All model calls go through `runtime.run_harness`. See the
[program guide](src/autoresearch_infrastructure/programs/README.md), the
[harness guide](src/autoresearch_infrastructure/harnesses/README.md) and the
[example solver programs](examples/harness-programs/README.md).

| Harness | Providers | Limits |
| --- | --- | --- |
| Codex 0.159.2 | OpenAI, OpenRouter | Post-tool hooks observe results and cannot replace them. |
| Claude Code 2.1.309 | Anthropic, OpenRouter | A hook on a skill read can replace the text content. Configuration and transport are tested. Live benchmark performance is not established. |

Another Harbor agent cannot drive a solver program until an adapter is registered
in `harnesses/adapters/__init__.py`. It can still be the researcher, the reviewer
or the solver of a `HarborTaskEvaluator`.

Token and cost totals need a report from every solver session a trial started.
If one session reports none, the total is unknown, and an evaluation that needs
`SOLVER_TOKENS` is left missing. The saved usage keeps the known part in
`observed_*` fields. A missing cost is never recorded as zero.

### Harness studies on benchmarks

`python -m autoresearch_infrastructure.pilot` runs a harness study: it screens
proposals on some Harbor tasks and confirms the selected program on others.
`python -m autoresearch_infrastructure.canary` measures only the baseline. Both
read plan files, which are a different format from run settings. The
[benchmark studies](examples/task-bank/README.md) describe them and record what
was run.

QuixBugs and SpreadsheetBench have recorded live model runs of the whole pipeline.
Neither established a reliable harness improvement. The BigCodeBench-Hard image
exceeded the earlier builder's storage budget. SciCode and OfficeQA are not
verified integrations.

## The researcher step

`ari run` is built from two pieces that you can call yourself.
`ari.CodingAgent(agent=..., image=...)` runs one Harbor agent as a researcher.
`search.propose` uses one `researcher` start, resets the workspace to the parent
candidate, runs the agent and accepts at most one submission.

```python
from autoresearch_infrastructure.search import propose, researcher_prompt

agent = ari.CodingAgent(
    agent={"name": "terminus-2", "model_name": "openrouter/openai/gpt-6-luna"},
    image="<image-id>",
)
parent = research.selected
outcome = propose(
    research,
    agent,
    researcher_prompt(INSTRUCTIONS, parent_id=parent, repeats=1),
    parent_id=parent,
    repeats=1,
    ledger=ledger,
    context=context_directory,
)
```

Here `research` is an open `ari.Research`, `INSTRUCTIONS` is the task's
instructions, `ledger` is a `StartLedger` with a `researcher` limit, and
`context_directory` holds the read-only files. `propose` returns `status`,
`submitted`, `error` and the path of the agent's logs. It reports a failed
researcher in that result and does not raise. Anything the researcher submitted
before failing stays recorded.

`researcher_prompt` appends text that tells the researcher where its files are
and how to submit. In its container, `/workspace/candidate/` holds the editable
files, `/context/` holds the task's read-only files, and `/history/`
holds every earlier candidate with its diff, verdict and results, summarized in
`/history/index.json`.

The researcher submits with the `record_candidate` tool, which ARI serves as an
MCP server named `autoresearch`. A terminal agent can run
`python /opt/autoresearch/mcp_proxy.py --call FILE.json` instead. A submission
needs a `proposal` with a `hypothesis` and a `mechanism`. The controller fixes the
parent and the repeat count.

Every proposal starts a new agent, whether the previous candidate was valid,
invalid or never submitted. By default no conversation carries over, and a
researcher learns from earlier work only through:

- its parent's files, copied into `/workspace/candidate/`;
- `/history/`, the run's memory, which ARI rewrites after each candidate;
- files it or an earlier researcher left elsewhere under `/workspace/`, which
  `propose` does not reset. `/tmp/` is emptied between starts.

The researchers' own trajectories are saved under `agent/invocations/` but are
not mounted into `/history/`. [Researcher inputs](#researcher-inputs) describes
how a task changes each of these.

The
[launch guide](skills/use-autoresearch-infrastructure/references/running-autoresearch.md)
describes where each process runs.

## Researcher inputs

A researcher is given four inputs. `open_run` resolves a task into one
`ari.ResearcherInputs` value and records it in `plan.json`:

| Input | Field | From the task |
| --- | --- | --- |
| Objective | `goal`, the written goal, and `measured`, the "What is measured" list | `Instructions.objective`, and the task's `objective` |
| Rules | `rules` | `Instructions.rules` |
| Environment | `environment_notes`, the description, and `environment`, the runtime | `Instructions.environment`, `seed`, `context` and `environment` |
| Memory | `memory`, the policy | `memory` |

`measured` is generated from the metric definition, so the text cannot disagree
with what is scored. The evaluator and the reviewer are not inputs and do not
change with them: the reviewer always reads the standard task text,
`inputs.specification()`, whatever a presenter shows the researcher.

### Memory

```python
ari.Task(..., memory=ari.Memory(history=3, conversations=2))
```

| Field | Values | Effect |
| --- | --- | --- |
| `history` | `"all"` (default), `"none"`, or `n` | Which earlier candidates appear under `/history`: all, none, or the `n` most recently recorded. |
| `preload` | A directory | Earlier experiment records, such as another run's `history/`, shown under `/history/preloaded/`. They are copied into the run and sealed in the plan. They never become candidates, results or spent budget. |
| `conversations` | `0` (default), `k`, or `"all"` | How much of earlier researcher conversations carries over. What `k` counts depends on `conversation_mode`. Starts are counted in time order, including failed starts and starts whose candidate was not selected. |
| `conversation_mode` | `"files"` (default) or `"continue"` | `"files"`: every start is a fresh conversation that can read the conversations of the last `k` starts that saved one, as ATIF JSON under `/history/conversations/`. `"continue"`: one conversation continues for `k` proposals, then a fresh one begins. See below. |
| `log` | `False` (default) or `True` | Adds `/history/log.md`, a research log ARI writes from its own records before each start: every visible candidate's parent, hypothesis, mechanism, verdict and score. |
| `summarize` | A function `(index) -> str` | Shown as `/history/summary.md`. It receives the visible `/history/index.json` content. It is trusted code: its identity goes into the plan and its output's hash into each start's record. |

With `conversation_mode="continue"` and `conversations=3`, starts 1 to 3 share
one conversation, starts 4 to 6 the next, and start 7 begins a third. `"all"`
never begins a fresh one. A continuing start loads the conversation saved by
the latest earlier start of its group through Harbor's `load_trajectory`, and
its prompt says that the workspace was reset to the current parent. Each
proposal is still one charged start with one submission from the parent the
controller chose. It needs `k` of at least 2 and a researcher agent that loads
ATIF trajectories, such as Claude Code or Codex; `ari check` refuses other
agents. Nothing shortens a long conversation, so `"all"` can outgrow a model's
context on a long run.

With the default memory, a start mounts the canonical history. Any other policy
builds a separate view for that start, so a hidden record is not reachable
through links, snapshots or trajectories, and the controller's evidence is never
edited. The view is deleted after the start. Whatever the memory shows, the
controller's selection, evidence and budgets are unchanged.

### Environment

```python
ari.Task(..., environment=ari.Environment(harbor="researcher-env", workspace="reset"))
```

A start's container, its services and their volumes are deleted when the start
ends. `workspace` says what survives besides the candidate and the memory:

| `workspace` | Before a start | After a start |
| --- | --- | --- |
| `"keep"` (default) | The workspace is cleared, then the previous start's export is restored after its seal is checked | Files left under `/workspace/` outside `candidate/` are copied to `agent/retained/<invocation>/` and sealed. Links and special files are dropped and listed |
| `"reset"` | The workspace is cleared | Nothing is kept |

A changed export is refused before the next start. An export larger than the
context limits is not kept, and its record says why.

`harbor` names a Harbor task directory. ARI uses only its `task.toml`
`[environment]` table and its `environment/` directory: a Dockerfile, an
optional `docker-compose.yaml` for services, or a prebuilt `docker_image`.
Resources, network policy, environment variables and healthchecks are Harbor's
own settings. The Harbor task's instruction, tests and solution are ignored.

ARI adds only what the experiment needs: the working directory, the skills
directory, its isolation overlay and the mounts for the candidate, context,
history and submission. A definition may not set `workdir` or `skills_dir`.
Every image, in `docker_image`, Dockerfile `FROM` lines and compose services,
must be pinned by digest. Compose may not change the main service's user,
mounts, capabilities or network, and services may not be privileged, bind host
paths, use the host network or be built. The definition is copied into the run
and its hash recorded.

An environment goes through three stages:

1. `ari check` validates the definition and starts nothing.
2. `ari prepare`, or the first step of `run` and `record`, builds the
   Dockerfile once with `docker build`, or resolves the pinned `docker_image`,
   and writes `prepared/<environment>.json` with the actual image ID of the main
   container and every sidecar's image. Every start's record repeats them. If
   the prepared image is later missing, ARI refuses rather than rebuild a
   possibly different image.
3. Each start uses Harbor's own lifecycle on the prepared image: start the
   services, wait for health checks, set up the agent, run it, then delete
   everything with its volumes.

Agents run on Harbor's local Docker backend. Submission uses a host socket and
bind mounts, which remote Harbor providers do not offer, so a definition that
needs another provider, such as a TPU, is refused.

The reviewer runs in `--reviewer-image`, in `--image`, or in its own
`review_environment`, a Harbor task directory prepared the same way. Changing
the researcher's environment never changes the reviewer's.

### Presentation and the record of each start

A presenter turns the inputs and one `ari.Start` into the prompt. `Start` holds
the start number, the parent, the repeats, the visible candidates, and whether
preloaded records or conversations are present. The default, `ari.present`,
writes the standard layout; with the default memory its text is unchanged.

A custom presenter can reword, reorder, omit or add to what the researcher is
told. That is an experimental change, so the presenter's code identity goes into
the plan.

To change the content instead, pass `Task(transforms=[...])`. Each transform
maps `ResearcherInputs` to `ResearcherInputs`, in order, before anything is
shown:

```python
ari.Task(..., transforms=[ari.transforms.omit_rule(2), ari.transforms.omit_measured()])
```

`ari.transforms` has `omit_rule`, `reword_rule`, `add_rule`, `replace_goal`,
`add_environment_note` and `omit_measured`. Rules are numbered from 1. A
transform may not change the memory or the environment. The monitor is part of
the run, so the reviewer judges the changed rules and reads the changed text.
The plan records each transform with its arguments, and the original task text
as `inputs.original_instructions`, for relabeling afterwards. Scoring never
changes. Every start saves, under `agent/inputs/<invocation>/`:

- `prompt.md`: the exact prompt.
- `inputs.json`: the parent and its hash, the prompt's hash, the memory policy,
  the visible candidates with their hashes, the history index's hash, the carried
  conversations with their hashes, the presenter and the environment's hash.

### What is configurable

| Control | Status | What ARI adds beyond Harbor |
| --- | --- | --- |
| Written goal, rules, environment notes | Now: `instructions` | The fixed layout and generated "What is measured" facts |
| Wording and order of the prompt | Now: `presenter` | Recorded per start, kept away from the reviewer |
| Changing the content the researcher is told | Now: `transforms`, `ari.transforms` | Recorded with the original text |
| Starting files, context | Now: `seed`, `context` | Candidates, baseline and sealed copies |
| Container, services, resources, network | Now: `environment.harbor` | Harbor's own definition; ARI adds isolation, mounts and submission |
| Files surviving between starts | Now: `environment.workspace` | Sealed export and restore around the controller-managed candidate |
| Building and recording images | Now: `ari prepare` | Actual main and sidecar image identities, reused by every start |
| Which earlier candidates are visible | Now: `memory.history` | Separate per-start views over trusted evidence |
| Preloaded records | Now: `memory.preload` | Sealed copies that never count as evidence |
| Earlier researcher conversations | Now: `memory.conversations`, `memory.conversation_mode` | Which starts' conversations are read as files, or how many proposals one conversation continues for; loading is Harbor's `load_trajectory` |
| Research log and summaries | Now: `memory.log`, `memory.summarize` | Written from trusted records; summaries recorded |
| Reviewer environment | Now: `--reviewer-image`, `review_environment` | Independent of the researcher's |
| Continuing a finished run | Now: `ari extend` | Verified import, cumulative budgets, lineage |
| Re-evaluating a finished run's candidates | Now: `ari import` | Same IDs, parents and proposals under a new contract |
| Remote Harbor providers | Not supported | Needs a submission path without a host socket; refused explicitly |

## Viewer

`ari view DIR --html FILE` writes one read-only HTML page. `ari record` and
`ari run` write the same page to `viewer.html` in the run directory. `DIR` may be
a run directory or a `Research` output. The page charts each metric by candidate
and shows each candidate's files, diff, verdict and evaluations. When reading
it, keep these in mind:

- A candidate with no completed measurement is left out of the chart and never
  plotted as zero.
- The validity counts exclude the baseline, and the valid percentage uses
  reviewed proposals only.
- SVG, PNG and JPEG files that the evaluator saved in `context.artifacts` are
  shown when their saved hash verifies. The page embeds images up to 1 MB each
  and 4 MB in total. A larger image stays in the run directory and the page
  shows a warning.
- Agent trajectories are shown in ATIF, when an agent ran. A trajectory is the
  agent's own record of its messages and tool calls.

The viewer reads saved results. It does not evaluate anything or change a run.

## Isolation

| What runs | Where | Protection |
| --- | --- | --- |
| The validity function, the evaluator and the objective function | The controller process | None. They are your trusted code. |
| The researcher and the reviewer | A Harbor job in a container from `--image`, or for the researcher, from the task's Harbor `environment` | Read-only root filesystem, all capabilities dropped, no new privileges, at most 512 processes. The history, the context and the review inputs are mounted read-only. |
| The solver of a `HarborTaskEvaluator` | The Harbor task's container | Read-only root filesystem, all capabilities dropped, no new privileges. A Harbor task that declares no network keeps that policy. |
| A candidate solver program | A separate container | No network, read-only and unprivileged. It has no provider key, grader, controller state or Docker socket. |

The Docker socket and the ledger are never mounted into a container. A researcher
can write only its workspace. Credentials that an agent needs are passed into its
container, and any process in that container can read them.

## Tests

```sh
uv pip install -e '.[test]'
python -m pytest -q
```

The core tests need neither Harbor nor Docker. They use fake agents, so they are
fixture evidence: they show that the plumbing works and say nothing about whether
a researcher improves a task.

`tests/test_native_review.py`, `tests/test_native_researcher.py` and
`tests/test_native_environment.py` run real Harbor containers with model
responses replaced by local constants, which is transport evidence. The last
runs a researcher in a Harbor environment with a sidecar, carried memory and
retained files, extends the run by one more start, builds and records an image,
and runs a reviewer in its own environment. It pulls a small pinned image. To run them, build `tests/fixtures/harbor-review/Dockerfile`,
then set `COEVOLUTION_DOCKER_TESTS=1` and `ARI_REVIEW_FIXTURE_IMAGE=sha256:...`.

`npm run test:viewer -- PATH_TO_PACKING_HTML` and
`npm run test:progress -- PATH_TO_HTML` drive the viewer in Chromium. They need
the locked Node dependencies and a Playwright Chromium, or
`CHROMIUM_EXECUTABLE_PATH`.

## What ARI does not do

- It does not choose a search strategy. A person, a script or a researcher
  proposes each candidate.
- Candidates are trees of UTF-8 text files: at most 2 MB for a file, 20 MB and
  2,000 files for a candidate. Evaluator artifacts may be binary.
- One controller process starts every evaluation. There is no distributed
  execution.
- Selection compares means. It does not estimate noise or confidence.

The [extraction notes](extraction.md) describe how this package relates to the
application it came from.
