# Design a task

A task is a Python file, `task.py`, whose `task(run)` function returns an
`ari.Task`. A task has four parts:

| Part | What it is |
| --- | --- |
| `seed` | The directory of starting files. Each saved version of them is a candidate. |
| `instructions` | The goal and every constraint, as one text |
| `objective` | The metric to improve |
| `evaluator` | Trusted code that measures one candidate on one case |

Three optional parts refine them: `validity`, `cases` and `context`. The
[API reference](../../../reference.md) lists every signature. This guide is
about the decisions behind them. Start from the closest example:

- [packing/task.py](../../../examples/packing/task.py): the shortest complete task.
- [curve-fitting/task.py](../../../examples/curve-fitting/task.py): keeps its
  instructions in a file, records extra metrics and saves a plot.
- [sum-instructions/task.py](../../../examples/sum-instructions/task.py) and
  [sum-harness/task.py](../../../examples/sum-harness/task.py): tasks measured on
  Harbor tasks, each built with one call to `ari.bases`. See
  [Harbor tasks](harbor-harness.md).

## The objective

State the goal in one sentence, then name the one metric that measures it and
its direction. Pass that sentence as the objective's description, so that the
saved run and the viewer show the goal.

```python
import autoresearch_infrastructure as ari

RMSE = ari.Metric("rmse", direction="min")
MAX_ERROR = ari.Metric("max_error", direction="min")
OBJECTIVE = ari.Objective(
    RMSE, metrics=(RMSE, MAX_ERROR),
    description="Fit a polynomial to the observations",
)
```

- A single `ari.Metric` is a complete objective when the evaluator reports
  nothing else.
- To record other useful numbers, list every metric the evaluator reports in
  `metrics`. Only the first argument and the tie-breakers affect ranking.
- Use a tie-breaker for a secondary goal, such as fewer tokens at equal success.
- Use a bound (`minimum`, `maximum`) for a hard limit. A candidate outside a
  bound is measured but can never be selected. The packing task does this for
  its weight limit.

ARI averages each metric over all cases and repeats, then ranks candidates by
the score of those averages.

## The evaluator

The evaluator is trusted code that measures one saved candidate on one case.
Decide how it ends in each situation:

| Situation | What to do | What ARI records |
| --- | --- | --- |
| The candidate was measured, however badly it did | Return an `ari.Evaluation` | A completed measurement. A wrong answer is a low score. |
| The measurement could not be made | Raise an exception | An incomplete evaluation with no score. The candidate cannot be selected. |

ARI never imports or runs candidate files. If your evaluator executes candidate
code, it must isolate that execution itself, for example in a container. Parsing
a candidate's JSON on the host, as the curve-fitting evaluator does, is safe.

Save files that explain a score, such as plots or logs, under
`context.artifacts`. The researcher can read them in later sessions.

## Validity: the function and the reviewer

Validity answers one question: is this candidate allowed? ARI decides it before
any evaluation and never evaluates a candidate that is not allowed. Quality is
the evaluator's separate question, so an allowed candidate can score badly.

Two mechanisms decide validity, in this order:

| Mechanism | What it enforces | Cost |
| --- | --- | --- |
| The task's `validity` function, optional | The constraints a function can decide exactly: file names, schema, value ranges | None |
| The reviewer, an agent | Every rule in the instructions, including those that need judgment, such as "does not hard-code answers for specific cases" | One `reviewer` start for each candidate that passes the validity function, including the baseline |

Review is on by default. The reviewer is given each rule under Constraints,
Rules as a separate check, and its verdict names the rule a candidate broke. It
also reads the whole instructions the researcher read, to understand what each
rule refers to, but it enforces only the rules. Write every rule into the instructions, including the ones
the validity function also enforces. A rule that exists only in your head or only in a README
is not enforced. With review on, instructions that list no rules are refused.

A list of item names can be fully checked by a function. Code needs the
reviewer, because valid syntax does not show that the code avoids hard-coded
answers.

Write constraints about facts the reviewer can see in the candidate and baseline
files. The reviewer cannot see graders or hidden data. A missing, malformed or
uncertain verdict counts as invalid.

Run settings can turn review off with `reviewer = false`. The validity function alone then
decides validity, and without a validity function every candidate is valid.

## Cases and repeats

Each name in `cases` is one fixed input, and every candidate is evaluated on
every case. Use more than one repeat (`repeats` in the run settings' `[limits]`)
when the measurement is noisy, as it is with a model-driven solver.

The researcher can study the results of these cases. To test a result on cases
the researcher never saw, see [Screen and confirm](low-fidelity.md).

## The instructions and the context

Write the instructions in two sections.

- **Objective** says which files to change, which metric should move and in
  which direction, and how a run is measured. A suggestion about approach can
  close it.
- **Constraints** has two lists. **Rules** are what a candidate must and must not
  do; the reviewer judges these. **Environment** is what the sandbox enforces
  whatever the researcher does.

[curve-fitting/instructions.md](../../../examples/curve-fitting/instructions.md)
is a complete example. The same three things can be given in Python as
`ari.Instructions(objective=..., rules=[...], environment=[...])`.

ARI lays the instructions out the same way for every task and adds what the task already knows: the starting files, the metric and its direction, tie-breakers, bounds, and the number of cases and repeats. Do not restate the direction in prose. Any other heading at those levels is refused, so a misplaced
section is never silently ignored.

Leave out the file layout and how to submit. ARI adds
[that part](../../../src/autoresearch_infrastructure/researcher-protocol.md) for
every researcher.

The researcher can read everything in `context` and in the starting files. Keep
graders, expected answers and credentials out of both. Telling the
researcher not to read a file does not prevent it.

## Fixed inputs

ARI refuses to reopen a run directory after the task file, the run settings, the
instructions, the context, the starting files or the evaluator's source file
change. A change then requires a fresh run directory, so old and new
measurements are never mixed.

An input that lives elsewhere, such as a dataset or a dependency, is not
detected. Write its version or hash as a constant in `task.py`. Updating the
constant changes the task file, which ARI does detect.

## Test the task before a search

1. Run `ari check`. It loads the task and the run settings and starts nothing.
2. Run `ari record` with run settings that have no agents and say
   `reviewer = false`, like
   [curve-fitting/local.toml](../../../examples/curve-fitting/local.toml). The
   first call measures the baseline and prints the workspace directory.
3. Edit the workspace and record again for each of these. A known good change
   should score better. A forbidden change should be rejected by the validity function. A
   poor but allowed change should be measured with a low score.
4. If the evaluator can fail for reasons outside the candidate, make it fail once
   and confirm that the evaluation is recorded as incomplete.

These are fixture tests of your own code. They do not test the constraints
that only the reviewer judges. The first run with real agents tests the
instructions, the image and the reviewer, so keep its budget small.
