# Sum instructions

Improve the instructions of a Harbor task while the agent that solves it stays
fixed. Use this example as the model for any task where the candidate is a
Harbor task's own files.

The Harbor task in [harbor-task/](harbor-task/instruction.md) asks an agent to read
the integers in `/workspace/numbers.txt`, add them, and write only the total to
`/workspace/answer.txt`.

| Part | Definition |
| --- | --- |
| Candidate | `sum/instruction.md`, the instructions the solver receives. |
| Baseline | The instructions in [harbor-task/instruction.md](harbor-task/instruction.md). |
| Objective | Maximize `success_rate`: the reward, 0 or 1, from the Harbor task's own grader. |
| Check | The instructions still name the sum, the input path and the output path. |
| Rules | The Harbor task keeps its goal and requirements, and gives away no answer. See [instructions.md](instructions.md). |
| Fixed inputs | The solver agent, the other task files, the grader and the budget. |

One easy Harbor task shows how the pieces connect. It cannot show that a wording
change helps in general. For that, use several Harbor tasks and repeats.

## Files

| File | Purpose |
| --- | --- |
| [task.py](task.py) | The task: a call to `ari.bases.harbor_task_files`, plus a validity function. |
| [instructions.md](instructions.md) | What the researcher is told. The reviewer checks each rule under Constraints. |
| [run.toml](run.toml) | Run settings: limits, the researcher, and the solver under `[task.solver]`. |
| [harbor-task/](harbor-task/task.toml) | The Harbor task: instructions, container, grader and reference solution. |
| [controls.py](controls.py) | Checks the Harbor task with Harbor's reference agents. Calls no model. |

## Check the Harbor task first

Commands run from the package root with ARI installed. Before spending on models,
confirm that the Harbor task and its grader behave. Build its image and note the ID:

```sh
docker build --network=none -t ari-sum-task examples/sum-instructions/harbor-task/environment
docker image inspect ari-sum-task --format '{{.Id}}'
```

Then run the controls, each with a new output directory:

```sh
python examples/sum-instructions/controls.py --image <task-image-id> --output runs/sum-oracle
python examples/sum-instructions/controls.py --image <task-image-id> --output runs/sum-nop --agent nop
python examples/sum-instructions/controls.py --image <task-image-id> --output runs/sum-whole-task --whole-task
```

Oracle runs the reference solution and must score 1. Nop does nothing and must
score 0. Each run evaluates the baseline and one edited candidate, so it uses
two trial starts. `--whole-task` makes every task file editable, changes
`numbers.txt`, and confirms that the new numbers reached the container.
[EVIDENCE.md](EVIDENCE.md) records the results of these controls.

Rewording cannot change what Oracle or Nop do. The controls show that the task
and the plumbing work. They say nothing about whether clearer instructions help
a model.

## Evaluate instructions with a model

[run.toml](run.toml) uses Terminus-2 as the solver. Its model requests come from
the controller and its terminal work happens in the task's container, so the
container needs no network. Terminus-2 does need tmux, which the task's Python
image lacks. Build a task image that adds tmux and keeps Python, Bash and
`/workspace/numbers.txt`, then set its ID under `[task]` in the settings, above the `[task.solver]` table:

```toml
[task]
image = "sha256:<task-image-id>"
```

`--image` is a second image, for the researcher and reviewer.

```sh
# Validate everything. Starts nothing.
ari check examples/sum-instructions --image <agent-image-id> --output runs/sum-instructions

# Record and evaluate the current workspace: one reviewer start and one solver trial.
ari record examples/sum-instructions --image <agent-image-id> --output runs/sum-instructions

# Or let researchers propose: up to two researcher starts, three reviews and three trials.
ari run examples/sum-instructions --image <agent-image-id> --output runs/sum-instructions
```

`ari record` suits manual work: record the baseline, edit
`sum/instruction.md` in the printed workspace, and record again.

In this repository `ari run` and `ari record` for this example are covered by
tests that use a fake agent and by `ari check`. They have not been run here with
Docker or a live model.

## What a candidate can change

`task.py` passes `editable=["instruction.md"]`. A candidate then holds exactly
that file, and each evaluation copies the frozen Harbor task and replaces it.
With `editable=None`, a candidate is the whole Harbor task source:

```text
candidate/
  sum/
    instruction.md
    environment/     container files; a change forces an image rebuild with no network
    tests/           the grader
    solution/        the reference solution; frozen when the solver is Oracle
    task.toml        always frozen
```

`task.toml`, `.env` files and Compose configuration can never change. When the
grader is editable, a passing score proves nothing by itself, so the validity
validity function and the instructions must establish that the task still means the same thing. Reject,
for example, a grader that accepts an empty answer.

The base task evaluates with `HarborTaskEvaluator`, which supports Linux Harbor tasks with one container, one step,
an absolute working directory and no task MCP servers. Candidates are text
files. Binary assets stay in the frozen Harbor task and are copied unchanged.

## What the solver can reach

Each trial runs in a fresh container with a read-only root filesystem, no added
capabilities and `no-new-privileges`. Its working directory, home and temporary
directories are writable and discarded afterwards. The researcher's workspace,
the run directory, the ledger and the Docker socket are outside the container.
Harbor mounts the trial's log directory so that results can be saved. Treat
those logs as untrusted output.

A trial that finishes with a wrong answer is a measurement with a reward of 0. A
trial that fails to set up, times out or returns no reward is incomplete and
produces no score. Both use a trial start.
