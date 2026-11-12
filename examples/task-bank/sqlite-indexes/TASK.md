# SQLite index selection (planned)

This study is planned and has not been built or run. [plan.toml](plan.toml) holds
the intended numbers and is marked `runnable = false`. No module reads it yet. The
evaluator, the dataset generator, the launcher and the tests are still to be
written.

## Research question

Can a researcher agent choose a small set of indexes that reduces the work SQLite
does on a mixed query workload, within a fixed storage budget, without changing
any query result?

The study exercises candidate recording and evaluation on a task with no solver
agent. SQLite does the work that is measured, and the evaluator calls no model.
Luna (`gpt-6-luna`) is the researcher.

## Task and data

A seeded local dataset contains tenants, orders and order items. The query
families are selective lookup by tenant and date, a join with aggregation, and
ordered pagination. No production or user data is used.

Fixed before the study: the SQL text, the data-generation code, the schema, the
SQLite version and the workload weights.

| Researcher can read | Kept from the researcher |
| --- | --- |
| The schema | Confirmation parameter draws |
| Query plans for the public training queries | Expected result sets |
| | The evaluator implementation and database construction |

## Candidate

A candidate is one file, `indexes.json`. It declares at most three non-unique
indexes. Each index names a known table and an ordered list of known columns. The
evaluator turns the declaration into SQL.

The first study does not allow arbitrary SQL, expressions, collations, partial
indexes, query rewrites or candidate Python. A candidate cannot change table
contents, queries, pragmas, runtime settings or the measurement loop.

The baseline declares no indexes, so only the schema's primary-key and foreign-key
structures exist. A hand-chosen index set is also measured as a reference control.
A comparison against the bare baseline alone would be too weak.

## Validity and correctness

The check rejects malformed JSON, unknown or duplicate columns, unsupported
declarations and more than three indexes. The evaluator applies a valid
declaration to a new private copy of the database. If a well-formed index fails to
build, the candidate fails with the build diagnostic and receives no score.

For each query, the evaluator compares the result with the baseline's result.
Ordered queries must match as exact sequences. Unordered queries must match as
multisets, duplicates included. Any mismatch stops the evaluation and the evidence
is kept. Every measured candidate must have correctness 1.

## Objective

Minimize the total SQLite virtual-machine steps on the fixed weighted workload,
divided by the baseline's steps on the same panel. Steps are counted with SQLite's
progress callback at a declared interval, initially one instruction, under a fixed
instruction limit.

Index storage may be at most 25% of the baseline database's physical size
(`max_index_storage_ratio = 0.25`). The evaluator fixes how pages and bytes are
counted. If calibration shows that no useful index fits in 25%, change the budget
before the search starts and fix it again.

The evaluator also records per-query steps before normalization, query plans,
index bytes, build work and wall-clock time. Wall-clock time is a diagnostic only.
Page cache, machine load and callback overhead could dominate such small
comparisons, so the study makes no wall-clock speed claim.

The workload is read-only. The cost of maintaining indexes on writes is not
measured and must be added before the results are applied to a production
workload.

How each outcome is recorded:

| Outcome | Recorded as |
| --- | --- |
| Valid indexes that do not help | A measured poor result |
| Valid indexes over the storage budget | Infeasible |
| A query that reaches the step limit | A measured workload failure. The step count is a lower bound and is never reported as a completed cost. |
| I/O error, broken measurement or missing artifact | An evaluation failure |

A full-workload objective is comparable only when every query completed with a
correct result. Selection keeps the default requirement that evaluations are
complete.

## Panels and budget

| Panel | Data | Evaluations |
| --- | --- | --- |
| Training | Public query families and their plans. Its data and parameters are separate from both panels below. | None |
| Screen | 10,000 orders, eight fixed query instances, one repeat | Baseline plus three proposals: 4 |
| Confirmation | 100,000 orders, 32 different parameter instances that cover every query family, three data seeds declared in advance | Baseline plus the selected candidate on each seed: 6 |

One evaluation runs the whole workload once. In the confirmation panel, a repeat
identifies one data seed.

| Plan key | Limit | Meaning |
| --- | --- | --- |
| `max_search_evaluator_starts` | 10 | 4 screen and 6 confirmation evaluations |
| `max_reference_evaluator_starts` | 4 | The hand-chosen reference control, once at screen scale and once on each confirmation seed |
| `max_evaluator_starts` | 14 | The total. Retries count against it. |
| `max_researcher_invocations` | 4 | Researcher starts, including setup and repair |
| `max_solver_model_calls` | 0 | The evaluator and the check call no model |

Include the reference control's measurements in every published comparison. Do not
send SQLite measurements through a model judge, and do not invent a solver
trajectory for a deterministic run.

Confirmation runs at the larger scale because cardinality and selectivity can
change query plans and reverse a ranking. Scores normalized to different baselines
come from different evaluations and cannot be mixed. Claim an improvement only if
the confirmation run preserves every result, meets the storage limit and improves
the fixed workload. Report per-query regressions and all three seeds as well as
the mean.

## What has to be built

- A Python evaluator that returns `ari.Evaluation` with the declared metrics,
  completion status and artifacts. It needs no Harbor dependency. It stores the
  index declaration, the hashes of the schema, workload and data, the baseline
  comparison, query plans, counters, limits and build diagnostics.
- The workload, the dataset generator and a launcher.
- Tests to run before any paid use: malformed and irrelevant indexes, corrupted
  results, budget checks before an evaluation starts, interrupted and retried
  evaluations, separation of the confirmation panel from the researcher, and a
  ranking that reverses between scales.

The study will use the same candidate snapshots, validity, `record_candidate`,
budgets and viewer as the Harbor studies. A SQLite run has no model trajectory.
The researcher's own run still needs its native trajectory, tool calls and cost
record.

The package's support for a plain Python function as the evaluator is the only
part that exists today.
