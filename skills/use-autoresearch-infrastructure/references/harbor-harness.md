# Harbor tasks

A "Harbor task" is Harbor's own unit: a directory with `task.toml`,
`instruction.md`, an environment and tests. To optimize something that is
measured on Harbor tasks, build your task with one of the two functions in
[`ari.bases`](../../../src/autoresearch_infrastructure/bases.py). Each returns a
complete `ari.Task`, so `task.py` is a few lines.

| You want to change | What stays fixed | Function | Copy |
| --- | --- | --- | --- |
| Files of the Harbor tasks, such as their instructions | The solver | `ari.bases.harbor_task_files` | [sum-instructions](../../../examples/sum-instructions/README.md) |
| The solver harness | The Harbor tasks and their graders | `ari.bases.harbor_harness` | [sum-harness](../../../examples/sum-harness/README.md) |

The solver is the agent being evaluated. The harness is the program, prompts,
skills and hooks that drive a coding-agent CLI.

Both functions work the same way in these respects:

- `harbor_tasks` maps a case name to a Harbor task directory. Each Harbor task
  is one case of your task.
- The solver comes from `[task.solver]` in the run settings.
- `[task] image`, if set, pins a prepared image for the Harbor tasks, which
  otherwise build their own Dockerfile.
- Each evaluation is one Harbor trial and uses one `solver_trial` start, whether
  or not the trial succeeds. The function sets the most `solver_trial` starts to
  the `evaluations` limit.
- The Harbor task's own verifier produces the reward. A trial that finishes with
  a low reward is a completed measurement. A trial that Harbor could not finish
  leaves the evaluation incomplete.
- The Harbor task files are hashed when the run opens. The evaluator refuses to
  run if they change.

Both need Docker and the package's `harbor` extra, which installs Harbor 0.24.0.

## Edit Harbor task files with a fixed solver

[sum-instructions/task.py](../../../examples/sum-instructions/task.py) lets a
candidate change one file, `sum/instruction.md`:

```python
def task(run):
    return ari.bases.harbor_task_files(
        run,
        harbor_tasks={"sum": "harbor-task"},
        editable=["instruction.md"],
        validity=validity,
    )
```

| Argument | Meaning |
| --- | --- |
| `instructions` | The goal and every constraint, as text. The researcher is given it and the reviewer judges each candidate against it. |
| `editable` | The files of each Harbor task that a candidate may change. The default is `["instruction.md"]`. |
| `validity` | Optional. A function for the constraints that code can decide exactly. |
| `objective`, `rewards` | Optional. The default objective is the success rate from each Harbor task's own grader. For other rewards, pass an objective and a mapping from each metric to the Harbor reward key that supplies it. |

Decide how much of the Harbor task a candidate may change:

- With a list of `editable` files, a candidate contains exactly those files. The
  grader cannot change. Prefer this.
- With `editable=None`, the candidate is the whole Harbor task, including its
  tests. The candidate can then change the grader. The instructions must require
  that the task and its grader keep their meaning, and review must stay on.

In both cases a candidate can never change `task.toml`, a Compose file or a
`.env` file.

Before writing the task, check that each Harbor task fits: a single container
without a Compose file, a single step on Linux, no task MCP servers and an
absolute `workdir`.

`[task.solver]` is an ordinary Harbor `AgentConfig` table:

```toml
[task.solver]
name = "terminus-2"
model_name = "openrouter/openai/gpt-6-luna"
override_timeout_sec = 120
```

The solver works in the Harbor task's container, so that image must contain
what the solver agent needs. The sum task's own Dockerfile has only Python, and
Terminus-2 also needs `tmux`. Build an image that adds it and set `[task] image`
to its ID.

[controls.py](../../../examples/sum-instructions/controls.py) runs the same
Harbor task with Harbor's Oracle and Nop agents, without a model. Do the same
for a new Harbor task to confirm that it passes with its reference solution and
fails when the solver does nothing. Those results are reference evidence.

## Edit the solver harness on fixed Harbor tasks

[sum-harness/task.py](../../../examples/sum-harness/task.py) lets a candidate
change a solver program:

```python
def task(run):
    return ari.bases.harbor_harness(
        run,
        harbor_tasks={"sum": "harbor-task"},
        advice="Do not hard-code the input numbers or the expected total.",
    )
```

| Argument | Meaning |
| --- | --- |
| `seed` | The starting solver program: `program.toml`, `solve.py` and any prompts, skills or hooks. |
| `advice` | Optional text added to the instructions. |
| `validity` | Optional. A function that runs after the built-in check. |

The function supplies the rest of the task:

- The instructions are the package's
  [standard text for a harness researcher](../../../src/autoresearch_infrastructure/harness-researcher.md),
  followed by your `advice`. They forbid hard-coded answers, direct provider
  calls and changes to the model or limits. The reviewer judges each candidate
  against them, so put any further constraint in `advice`.
- The built-in check requires a solver program that loads and keeps the seed's
  ID.
- The context holds the researcher's reference files for the solver interface.

`[task.solver]` names the coding-agent CLI and its model:

```toml
[task.solver]
harness = "codex"          # or "claude-code"
provider = "openrouter"
model = "openai/gpt-6-luna"
timeout_seconds = 120      # optional: for the solver program in one trial
max_sessions = 1           # optional: model sessions in one trial, helpers included
```

- `codex` works with the providers `openai` and `openrouter`. `claude-code` works
  with `anthropic` and `openrouter`. Another CLI needs a new adapter in
  [`harnesses`](../../../src/autoresearch_infrastructure/harnesses/README.md).
- The CLI runs in containers from `--image`, so that image must contain it.
- Each evaluation uses one `solver_trial` start and at most `max_sessions`
  `solver_session` starts.

### The solver program

A candidate is a directory with a manifest, `program.toml`, and the code it
names. The baseline of the example is
[sum-harness/seed](../../../examples/sum-harness/seed/solve.py):

```python
from pathlib import Path

from autoresearch_infrastructure.programs import SolverOutput


async def solve(task, runtime):
    prompt = Path(__file__).with_name("prompt.txt").read_text()
    result = await runtime.run_harness(input=task.instruction, system=prompt)
    if not result.completed:
        raise RuntimeError("Solver session did not finish")
    return SolverOutput(result.text)
```

- Every model call goes through `runtime.run_harness`. Each call is one session.
- `run_harness` accepts `system`, `skills`, `pre_tool`, `post_tool` and the
  options listed in the researcher's `/context/harness-kwargs.md`. An unknown
  option is refused.
- Hooks are async functions. `pre_tool(call)` returns `Allow()` or
  `Deny(reason)`. `post_tool(call, observation)` returns a `ToolObservation`.
  Import these types from `autoresearch_infrastructure.programs`.
- The solver has one task tool, `exec`, which runs a shell command in the Harbor
  task's container for at most 30 seconds.
- A skill is a directory with a `SKILL.md` whose front matter has `name` and
  `description`.
- `result.completed` means the session ended normally. It says nothing about the
  reward.

The program runs in its own container without network access, the provider key
or the grader. ARI does not import it on the host.

[usage.md](../../../src/autoresearch_infrastructure/programs/usage.md) has worked
examples of instructions, skills and hooks.
[examples/harness-programs](../../../examples/harness-programs/README.md) has
three small programs: a tool hook, a skill and a helper session.

## The evaluator classes

The two functions are built on two evaluator classes:
[`HarborTaskEvaluator`](../../../src/autoresearch_infrastructure/harbor_task.py)
and
[`HarborHarnessEvaluator`](../../../src/autoresearch_infrastructure/harbor_harness.py).
Construct one yourself and pass it as the `evaluator` of an `ari.Task` only when
a function in `ari.bases` does not fit. Examples are a different list of
editable files for each Harbor task, or solver tokens as a tie-breaker. The
[API reference](../../../reference.md) lists their constructor arguments, and
[bases.py](../../../src/autoresearch_infrastructure/bases.py) shows how each
function fills them.

## The benchmark harness study

`python -m autoresearch_infrastructure.pilot` is the harness study, a separate
launcher for improving a solver program on an existing Harbor benchmark. It does
two things that `ari run` does not. It requires reference controls before any
paid start. It confirms the result on Harbor tasks that the researcher never saw.

It reads a plan file, which is a different format from `run.toml`.
[Benchmark studies](../../../examples/task-bank/README.md) explains the plan
files and the three modes, and lists the studies that were run.

Run the modes in order, from the `autoresearch_infrastructure` directory, with
one run directory: `--check`, then `--reference`, then `--run`. Only `--run`
starts models, so run it only with the user's approval of the plan's limits.

```sh
python -m autoresearch_infrastructure.pilot --check \
  --plan examples/task-bank/harbor-harness/quixbugs.toml \
  --tasks-root <quixbugs-task-directory> --image <solver-image> \
  --output <run-directory>
```

`--image` names the solver image and may be a local tag, which the module
resolves to its image ID. `--researcher-image` and `--reviewer-image` default to
the solver image. Pass the same image options in every mode.

`--run` does the following:

1. It refuses to start unless `--reference` passed for the same Harbor task files.
2. It measures the baseline program on the screen panel.
3. It starts researchers, each from the selected candidate, with one submission
   each. The baseline is accepted without a review. Every other candidate is
   checked and then reviewed. One `reviewer` start is held back for confirmation.
4. It measures the baseline again on the confirmation panel, then the selected
   candidate by its exact files. If the baseline was selected, only the baseline
   is confirmed.

The run directory holds `plan.json`, the ledger `paid-starts.json`,
`references.json`, `screen/` and `confirmation/` (one `Research` output each),
`promotion.json`, `summary.json`, `screen.html` and `confirmation.html`. The
module shows no progress in the terminal while it runs.

Two options handle a study that needs more than one run directory:

- `--allocation-ledger <path>` counts starts in an existing ledger. Use it with a
  fresh run directory after you repair ARI's own code, so that the starts already
  used stay counted. The plan, Harbor tasks and solver image must be unchanged.
- `--fixed-candidate <directory>` measures a program you supply against a fresh
  baseline on the screen panel, without starting a researcher, and then confirms
  whichever of the two is selected. If you repaired the program by hand, report
  it as a different candidate from the one a researcher submitted.

### Add a new benchmark

1. Pin the upstream revision and the Harbor task names of both panels in a plan
   file. Keep the two panels disjoint.
2. Check that each Harbor task's image has Python and Bash, which the `exec` tool
   needs.
3. Run `--check`, then `--reference`. Every Harbor task must pass with its Oracle
   solution, and the plan's negative controls must fail with the Nop agent.
4. Measure the baseline alone with
   `python -m autoresearch_infrastructure.canary --plan ... --baseline`. It
   records rewards, token use and timing, and accepts no other candidate. Do not
   reuse those Harbor tasks as a confirmation panel.
5. Read the timing before setting limits. `timeout_seconds` limits the solver
   program. Image builds, verifier setup and cleanup take additional time, which
   each evaluation's `timing.json` reports.
6. Before a larger study, confirm on a few baseline trials that a new hook or
   skill is used at all. A hook that never runs cannot explain a change in score.

A subset of a benchmark is not an official benchmark score.

An adapter can change how a Harbor task is executed, for example by
recalculating spreadsheets with LibreOffice where the original used Excel. A
passing Oracle run then shows that the reference solution works. It does not show
that every output a solver produces is graded as the original would grade it.
Report that uncertainty with the results.

Known limits of the packaged plans:

- BigCodeBench-Hard has a plan, but its dependency image exceeded this
  workspace's 32 GB Docker file system before any model call.
- SciCode needs its own resource check before use.
- For OfficeQA, data access and a compatible Harbor export are not verified.

### Trials that did not finish

A trial that did not finish has no score. To display its partial trajectories,
pass `--partial-evidence <file>` to `ari view` or `ari inspect`. The file is a
JSON inventory that you write. It has `schema_version` 1, `run_state_sha256`
matching the saved run state, and `records` that name each result path with its
candidate hash, contract hash and solver sessions. The viewer attaches only what
the inventory lists and never turns a partial trajectory into a score.
