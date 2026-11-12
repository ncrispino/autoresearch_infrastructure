# Benchmark studies

This directory holds studies that ran ARI against existing Harbor benchmarks, and
one planned study. Each study has a `TASK.md` that describes the task, the budget
and what was observed.

| Study | Candidate | Scored by | Status |
| --- | --- | --- | --- |
| [QuixBugs harness](harbor-harness/TASK.md) | A solver program with its prompts, skills and hooks | The unchanged Harbor task verifier | Run once with live models. The baseline stayed selected. |
| [SpreadsheetBench Verified](spreadsheet-canary/TASK.md) | The fixed baseline program, then a solver program | The unchanged SpreadsheetBench Verified verifier | Baseline calibrations and harness studies run with live models. No improvement established. |
| [SQLite indexes](sqlite-indexes/TASK.md) | Up to three indexes over a fixed schema | Query results and SQLite work counters | Planned. The evaluator is not built. |

The small runnable examples (curve fitting, packing, sum instructions and sum
harness) are listed in the [package README](../../README.md).

## Which tool reads which file

The studies here use plan files such as `quixbugs.toml`. Two modules read them:

- `python -m autoresearch_infrastructure.pilot` runs a harness study. Researchers
  propose solver programs on a screen panel of Harbor tasks. The selected program
  and the baseline are then measured on a separate confirmation panel.
- `python -m autoresearch_infrastructure.canary` runs a calibration. It measures
  only the unchanged baseline program.

A plan file is a different, older format from the `run.toml` run settings that
`ari run` reads. `ari` does not read plan files, and these two modules do not read
`run.toml`.

Both modules take `--plan`, `--tasks-root`, `--output` and `--image`, and one mode:

| Mode | What it does | Starts |
| --- | --- | --- |
| `--check` | Validates the plan, hashes the Harbor tasks and the image, and writes `plan.json`. Needs Docker and the task files. | None |
| `--reference` | Runs the Harbor Oracle agent on every task and the Nop agent on the plan's `negative_control_task_ids`. | `reference_trial` |
| `--run` (`pilot`) or `--baseline` (`canary`) | Runs live models. Refuses to start without passing reference results for the same task and build bytes. | `solver_trial`, `solver_session`, and for `pilot` also `researcher` and `reviewer` |

Harbor benchmark tasks and the solver image are not in this repository. Each
`TASK.md` names the upstream revision to fetch. Build the image from
[docker/solver](../../docker/solver/README.md).

## Protocol the studies share

1. Fix the tasks, verifier, model, image and budget in the plan before any model
   runs. The plan pins every task ID. Panels are never chosen from model scores.
2. Run `--reference`. The Oracle agent must pass every task and the Nop agent must
   score zero on the negative-control tasks.
3. Screen proposals. The researcher can write only the candidate and can see only
   screen results. Each researcher start records at most one candidate.
4. Confirm. The module copies the selected program by its source hash, writes
   `promotion.json`, and measures the baseline and that program again on the
   confirmation panel. If the baseline won the screen, it is confirmed alone.
5. Report the screen panel and the confirmation panel separately. A screen leader
   is not a confirmed improvement. Keep rejected proposals, failures and unknown
   costs in the report.

The screen panel and the confirmation panel are separate `Research` outputs, in
`screen/` and `confirmation/` under the run directory. One `StartLedger` counts
the starts of both. A start is counted before the agent or trial begins and is not
refunded if it fails. Starts are counts. They do not limit tokens or cost.

## Keep the run directory and its ledger

Put the run directory for a paid study in persistent storage outside the source
checkout, and back it up together with its ledger. The ledger is the only record
of how many starts a study has used. If it is lost, stop paid runs until the
original file is restored. Do not create a replacement ledger from a remembered
count.

The [longer SpreadsheetBench study](https://github.com/ncrispino/prompt-injection-coevolution/blob/main/docs/results/spreadsheet-long-budget-search.md)
lost its `/tmp` run directory during confirmation. Its report records the
incomplete checkpoint.
