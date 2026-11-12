# SpreadsheetBench Verified: calibration and harness studies

This directory records four pieces of work on Harbor's SpreadsheetBench Verified
tasks at revision `0ad7209b09780e78c6c80f194e49e353191be1cc`. All of them use the
Luna model (`gpt-6-luna` through OpenRouter) as the solver.

| Plan | What it runs | Outcome |
| --- | --- | --- |
| [plan.toml](plan.toml) | Baseline on three tasks | 3/3 passed |
| [expanded.toml](expanded.toml) | Baseline on six more tasks | 4/6 passed |
| [search.toml](search.toml) | Harness study: six proposals, 180-second solver deadline | Baseline selected. No improvement established. |
| [long.toml](long.toml) | Harness study: eight proposals, 600-second solver deadline | Run evidence lost during confirmation. No improvement established. |

The first two are calibrations. A calibration measures only the unchanged baseline
program, to learn the success rate, token usage and time per trial before spending
a budget on a harness study. A calibration cannot show a harness improvement.

In every plan the task instructions, the Python dependencies and the
LibreOffice-based verifier are unchanged. Input workbooks stay in the Harbor task's
container. Answers and reference scripts are outside the solver's reach until
verification. The public CA bundle is mounted with TLS verification on, and task
files are not edited.

## Three-task calibration

[plan.toml](plan.toml) pins the first three task IDs in the registry export:
`10452`, `105-24` and `10747`. They were chosen before any model result existed.

The baseline program calls `runtime.run_harness(input=task.instruction)` once and
returns the answer. The check accepts only those exact bytes, so no other program
can be evaluated. No researcher or reviewer runs. The solver image is identified
by its immutable image ID.

The solver deadline is 180 seconds, chosen for this study. The benchmark's default
is 600 seconds. The verifier and build timeouts keep their original 600 seconds,
so a whole trial can take longer than three minutes.

### Budget

| Plan key | Limit | Meaning |
| --- | --- | --- |
| `max_solver_trial_starts` | 3 | One trial per task, one repeat |
| `max_harness_session_starts` | 3 | One solver session per trial |
| `max_reference_trial_starts` | 8 | Four control trials are planned. The rest allow for setup failures. |

A failed start stays counted, and nothing resets the counts. Before any model
runs, the upstream oracle must pass all three tasks and the Nop agent must fail
task `10452`. Each control result is stored with hashes of the task bytes, the
build overlay and the Harbor result. A failed or missing control blocks the paid
run.

### Run

Install the package with its `harbor` extra. Fetch the three pinned Harbor task
directories and set `TASKS` to their parent directory. Build the
[solver image](../../../docker/solver/README.md). Use a new `--output` directory.

```sh
PLAN=autoresearch_infrastructure/examples/task-bank/spreadsheet-canary/plan.toml

python -m autoresearch_infrastructure.canary --check \
  --plan "$PLAN" --tasks-root "$TASKS" --output /tmp/ari-spreadsheet-canary --image ari-solver:local

python -m autoresearch_infrastructure.canary --reference \
  --plan "$PLAN" --tasks-root "$TASKS" --output /tmp/ari-spreadsheet-canary --image ari-solver:local

# Paid model calls. Requires OPENROUTER_API_KEY.
python -m autoresearch_infrastructure.canary --baseline \
  --plan "$PLAN" --tasks-root "$TASKS" --output /tmp/ari-spreadsheet-canary --image ari-solver:local
```

If the code changes, start again with a new `--output` and pass
`--allocation-ledger` with the path of the original `starts.json`. The original
start limits then still apply, provided the plan, the task bytes and the image are
the same.

The `canary` module imports nothing from the coevolution project. A project that
requires a source lock holds that lock itself while it runs the module, for
example through `runpy.run_module("autoresearch_infrastructure.canary",
run_name="__main__", alter_sys=True)`.

### Read the output

| File | Content |
| --- | --- |
| `summary.json` | Verifier reward, solver tokens and timing per task, and the ledger counts |
| `baseline.html` | Viewer with the native ATIF trajectories |
| `baseline/` | The `Research` output. Each trial keeps `timing.json` and the full Harbor result. |
| `starts.json` | The ledger |
| `references.json`, `controls/` | Reference results |

End-to-end time covers environment setup, model work, verification and cleanup. A
missing phase timestamp stays unknown. Report whether image builds were cold or
warm, and keep reference timings separate from model timings.

### What was observed

Reference evidence:

- The verifier installs LibreOffice on every trial. An exploratory oracle trial
  passed in about 153 seconds: 49 seconds of environment setup and 92 seconds of
  verification, with no model work.
- All three oracle controls passed and the Nop control scored zero. With warm
  image builds, each control took 107 to 120 seconds, of which LibreOffice
  verification took about 86 to 101 seconds.

Fixture evidence at the time of the build: 854 passing tests in the combined
repository and 59 passing package tests without Harbor.

Live model evidence, run after explicit launch approval:

- All three baseline trials passed the verifier.
- Solver execution took 47 to 55 seconds. Full trials took 165 to 173 seconds
  (2:45 to 2:53), including 95 to 98 seconds of verification.
- Recorded model cost totalled $0.005286195.
- Exactly three trials and three sessions ran, with no retries.
- The baseline viewer passed a Chromium check with all three ATIF trajectories.

With 3/3 passing, these cases leave no room to observe a higher success rate. They
are now development data. A later confirmation needs different task IDs.

The [build report](https://github.com/ncrispino/prompt-injection-coevolution/blob/main/docs/results/spreadsheet-canary-build-20261026.md)
has the full record.

## Six-task calibration

[expanded.toml](expanded.toml) pins six more cases and excludes the first three.
The remaining registry IDs were sorted by the SHA256 of
`ari-spreadsheet-calibration-v2:` followed by the ID, and the first six were taken
before any model result. Four are cell-level tasks and two are sheet-level tasks.
They cover dynamic references, row deduplication and deletion, working-day
formulas and transposition. This is a small reproducible development sample. It
does not estimate the benchmark score, and it is not a confirmation panel.

Run the same three `canary` commands with this plan, its six Harbor task
directories and a new `--output` such as `/tmp/ari-spreadsheet-expanded`. The
budget is six trials and six sessions, one repeat per task, under the same
180-second solver deadline. Six oracle controls and one Nop control are required.
`max_reference_trial_starts` is 14 to allow for setup failures.

### What was observed

Live model evidence:

- 4/6 trials passed the verifier. There were no transport failures.
- One failure left a listed holiday out of a formula range.
- The other failure produced a blank formula result after the verifier
  recalculated the workbook. It still needs a check of spreadsheet-engine
  compatibility and of the evaluation.
- Solver execution took 44 to 110 seconds. Full trials took 2:43 to 3:43.
- Recorded model cost was $0.016673385, for six trials and six sessions. Seven
  control trials ran without a model.

The result shows that the baseline can be improved on these cases. It does not
show an improved harness. These six cases are also development data now. See the
[calibration report](https://github.com/ncrispino/prompt-injection-coevolution/blob/main/docs/results/spreadsheet-expanded-calibration.md).

## Harness study with a 180-second deadline

[search.toml](search.toml) is read by the `pilot` module. A candidate is a solver
program: `program.toml` with the ID `benchmark-harness`, `solve.py`, and optional
prompts, skills and tool hooks. The researcher may edit general spreadsheet
workflows, skills and hooks. The model, reasoning effort, benchmark, verifier and
session limit are fixed.

The objective is the mean verifier reward. Ties go to fewer solver input plus
output tokens.

| Panel | Cases | Design | Planned trials |
| --- | --- | --- | --- |
| Screen | The six development cases | Baseline plus six proposals, one repeat | 42 |
| Confirmation | `192-22`, `49036`, `34033`, `53994` | Baseline and the selected program, two repeats | 16 |

The confirmation cases were chosen before any result and exclude all nine
calibration cases. The researcher records one candidate per start and sees only
screen results.

Budget, retries included: 66 trial starts and 66 session starts, with one Luna
session per trial. Eight researcher starts and eight reviewer starts. Eleven
control trials are planned and `max_reference_trial_starts` is 22. Trials run one
at a time. Allow several hours, because verification alone takes about 90 seconds
per trial. Starts are counts. They do not limit tokens or cost.

Run `python -m autoresearch_infrastructure.pilot` with `--check`, then
`--reference`, then `--run`. Pass this plan, a `--tasks-root` that holds all ten
Harbor task directories, and a new `--output`. The recorded study used
`/tmp/ari-sheets-harness-search`. A project that requires a source lock holds it
around each command.

When you read results, keep the raw verifier rewards, and separate reasoning or
data mistakes from adapter incompatibility and infrastructure failures.

### Re-evaluating a fixed program

The `pilot` option `--fixed-candidate DIRECTORY` evaluates the exact files in
`DIRECTORY` against a new baseline on the screen panel, then confirms the selected
program on the same confirmation panel. It starts no researcher.

Use it to repeat a measurement after repairing the code around a candidate. Keep
the original run directory and its verdicts. Add the option to the `--check`,
`--reference` and `--run` commands alike, use a new `--output`, and pass the
original ledger with `--allocation-ledger`. Earlier starts still count against
the same limits. The option does not add starts. Before the first paid call, the
module requires room in the ledger for both programs on both panels.

### What was observed

Live model evidence. The full record is in the
[search report](https://github.com/ncrispino/prompt-injection-coevolution/blob/main/docs/results/spreadsheet-harness-search.md).

- The study was run twice: the original study, and a new study after a repair to
  the review context and the hook API. The baseline was selected in both.
- In the repair study, the repaired candidate (a compactor hook) matched its
  baseline's 5/6 screen success and used 7.97% more tokens. The hook did not
  activate in any development case.
- Baseline confirmation was 4/8 in the repair study and 5/8 in the original study.
- Totals: 58 trial starts, 52 solver sessions, six researcher starts, seven
  reviewer starts, eleven control trials, and $0.353499255 of recorded native
  model cost.

No harness improvement is established.

### The tested program

[frozen-compactor.json](frozen-compactor.json) holds the exact two-file program
that was tested (`program.toml` and `solve.py`) and its `source_hash`. The files
are stored as measured, without lint formatting. To rebuild the program:

```python
import json
from pathlib import Path

from autoresearch_infrastructure.persistence import digest
from autoresearch_infrastructure.source_tree import materialize, snapshot

frozen = json.loads(Path("frozen-compactor.json").read_text())
target = Path("/tmp/frozen-compactor")  # must not exist yet
materialize(target, frozen["files"])
assert digest(snapshot(target)) == frozen["source_hash"]
```

To repeat the protocol, pass that directory to `--fixed-candidate` with the same
plan and benchmark revision, a new `--output`, and a ledger that has room.

## Harness study with a 600-second deadline

[long.toml](long.toml) was registered before the run as a separate study. The
solver deadline is 600 seconds, which matches the benchmark's declared agent
limit. The model, reasoning effort, session limit, Harbor tasks and verifier are
the same as before.

| Panel | Cases | Design | Trials |
| --- | --- | --- | --- |
| Screen | The same six development cases | Baseline plus eight proposals, one repeat | 54 planned, limit 60 |
| Confirmation | Eight new cases | Baseline and a different selected program, two repeats | At most 32, limit 36 |

The eight confirmation cases were chosen before any result and exclude all
thirteen task IDs used earlier.

Budget, retries included: 96 trial starts and 96 session starts. Ten researcher
starts and ten reviewer starts. Fourteen oracle controls and one Nop control are
planned and `max_reference_trial_starts` is 30. Trials run one at a time and the
study takes several hours.

The owner authorized this as a new study. It has its own ledger and its own
evaluations. The earlier studies and
their ledgers are unchanged. The 180-second scores are background only and are
not reused as 600-second measurements. The plan's `research_guidance` gives the
researcher the earlier lessons about workflows, the hook API and hook activation.
It does not reveal confirmation task IDs or answers.

Run the `pilot` module as above with this plan and a `--tasks-root` that holds all
fourteen Harbor task directories. The recorded study used the output
`/tmp/ari-sheets-harness-long`.

### What was observed

The study recorded eight proposals and had partly completed confirmation when a
cloud reconnection lost its `/tmp` run directory.

- The last checkpoint written in the session recorded 12/16 baseline passes and
  12 passes among 15 graded trials of the selected candidate.
- The final candidate result and the total cost are unknown.
- These numbers cannot be audited again, because the files are lost.

No harness improvement is established. See the
[interruption report](https://github.com/ncrispino/prompt-injection-coevolution/blob/main/docs/results/spreadsheet-long-budget-search.md).

Do not continue this study by creating a replacement ledger. Keep the run
directory and ledger of any later paid study in persistent storage with backups.
