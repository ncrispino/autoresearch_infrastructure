# Sum harness

Improve the program that drives a solver agent, on a fixed Harbor task. Use this
example as the model for any task where the candidate is a *harness*: the
program, prompts, skills and hooks around a coding-agent CLI.

The Harbor task in [harbor-task/](harbor-task/instruction.md) asks an agent to read
the integers in `/workspace/numbers.txt`, add them, and write only the total to
`/workspace/answer.txt`. Here the Harbor task never changes.

| Part | Definition |
| --- | --- |
| Candidate | The solver program in [seed/](seed/solve.py): `solve.py`, `prompt.txt` and `program.toml`. Skills and hooks may be added. |
| Objective | Maximize `success_rate`: the reward, 0 or 1, from the Harbor task's own grader. |
| Check | The candidate is still a loadable solver program with the ID `sum-solver`. |
| Rules | The program is general and keeps the model, provider and session limit. The package's harness instructions state them. |
| Fixed inputs | The Harbor task and grader, the solver's model and provider, the images and the budget. |

One Harbor task with a pass-or-fail score cannot show that a harness is better.
Use several varied Harbor tasks and repeats for a real study, as the
[benchmark studies](../task-bank/README.md) do.

## Files

| File | Purpose |
| --- | --- |
| [task.py](task.py) | The task: one call to `ari.bases.harbor_harness`. |
| [run.toml](run.toml) | Run settings: limits, the researcher, and the solver's model under `[task.solver]`. |
| [seed/](seed/program.toml) | The starting solver program. |
| [harbor-task/](harbor-task/task.toml) | The Harbor task: instructions, container, grader and reference solution. |

The instructions come from the base task. It also places the solver interface
and worked examples in the researcher's `/context` directory. To build your own
harness task, copy this directory and point `harbor_tasks` at your Harbor tasks.

## Prepare the images

Commands run from the package root with ARI installed. Two images are involved.
Pass image IDs, not tags, so that a later rebuild cannot change a running study.

```sh
# The solver CLI, the researcher and the reviewer share this image.
docker build -t ari-solver docker/solver        # build options: docker/solver/README.md
docker image inspect ari-solver --format '{{.Id}}'

# The Harbor task's own container, with Python and Bash for the task's tools.
docker build --network=none -t ari-sum-task examples/sum-harness/harbor-task/environment
docker image inspect ari-sum-task --format '{{.Id}}'
```

Pass the first ID as `--image`. Set the second under `[task]` in
[run.toml](run.toml), above the `[task.solver]` table:

```toml
[task]
image = "sha256:<task-image-id>"
```

Without `[task] image`, Harbor builds
[harbor-task/environment/Dockerfile](harbor-task/environment/Dockerfile) itself.

## Run it

`[task.solver]` in [run.toml](run.toml) names the coding-agent CLI (`codex` or
`claude-code`), the provider and the model. The solver needs that provider's
credentials in the environment.

```sh
# Validate everything and print the budget. Starts nothing.
ari check examples/sum-harness --image <solver-image-id> --output runs/sum-harness

# Record and evaluate the current workspace: one reviewer start and one solver trial.
ari record examples/sum-harness --image <solver-image-id> --output runs/sum-harness

# Or let researchers propose: up to two researcher starts, three reviews, three trials.
ari run examples/sum-harness --image <solver-image-id> --output runs/sum-harness
```

For manual work, record the baseline with `ari record`, then edit `prompt.txt` in
the printed workspace. For example, add: "Before finishing, check that the
requested output file exists and contains only the requested result." Record
again to measure it. Whether that helps is what the evaluation tells you.

In this repository `ari run` and `ari record` for this example are covered by
tests that use a fake agent and by `ari check`. They have not been run here with
Docker or a live model.

## What a candidate may do

The grader reads the answer file, not the solver's final message. A candidate
that hard-codes the numbers or the total is invalid. Every model call in the
program goes through `runtime.run_harness`, and each trial allows one solver
session. The reviewer reads the candidate's files and never runs them. ARI never
imports candidate code on the host: the program runs in an isolated worker
container.

The [harness programs](../harness-programs/README.md) show a tool hook, a skill
and a helper session that a candidate can build on.
