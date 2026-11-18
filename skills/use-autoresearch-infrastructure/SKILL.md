---
name: use-autoresearch-infrastructure
description: Set up an autoresearch run with ARI. Configure the agents and run settings for an existing task, or design a new task with its instructions and evaluator, then hand the user a checked launch command.
---

# Set up an autoresearch run

Use this skill when a user wants to optimize something with ARI
(`autoresearch_infrastructure`). You prepare the files and the command. The user
decides whether to spend money.

Never launch `ari run`, a Harbor trial or any other paid command without the
user's explicit approval of the budget. `ari check` is free and starts nothing.

## Terms

| Term | Meaning |
| --- | --- |
| task | The optimization problem, in four parts: the starting files, the instructions, the objective and the evaluator. A `task.py` file defines it. |
| instructions | One text for the researcher. It has two sections: Objective (which files to change and which metric should move) and Constraints (the Rules a candidate must obey, and what the Environment enforces regardless). The researcher is given it, and the reviewer judges each candidate against it. |
| run settings | The `run.toml` file: limits and agents. |
| candidate | One saved version of the editable files. The baseline is the unchanged starting files, saved as `candidate-0001`. |
| researcher | The agent that proposes candidates. |
| reviewer | The agent that judges a candidate against the instructions. |
| solver | The agent being evaluated, when the task evaluates one. |
| start | One launch of an agent or Harbor trial. Starts are counted before launch and never refunded. |
| run directory | The `--output` directory, which holds everything a run saves. |

## The workflow

1. Write or reuse a `task.py` that defines `task(run) -> ari.Task`.
2. Put limits and agents in `run.toml` beside it.
3. Verify with `ari check`.
4. Give the user the `ari run` command, or `ari record` when they make the edits themselves.
5. Inspect results with `ari view`.

```sh
ari check  <task-directory> --output <run-directory> --image <image-id>
ari run    <task-directory> --output <run-directory> --image <image-id>
ari view   <run-directory> --html <new-file.html>
```

To go past a finished run's limits, or to judge its candidates again under a
changed task, see [Continue or start again](references/running-autoresearch.md#continue-or-start-again).

## Decide which case you are in

If the task exists, read its `task.py`, `run.toml` and README. Keep the
starting files, instructions, objective and evaluator as they are. Your work is
the run settings: agents, models, limits, image and credentials. Go to
[Write the run settings](#write-the-run-settings).

If the task is new, work through the next section first. Ask the user only for
decisions you cannot find in their project, such as an ambiguous goal or a
missing dataset. Propose defaults for the rest and state them.

## Design a new task

When the task already exists somewhere else, as a benchmark task or a written
description, follow [Break an existing task down](references/importing-tasks.md).
It gives the method for sorting a source task into ARI's parts and three worked
case studies.

Write the task as a Harbor task: a directory with an instruction, a container
and a grader, and a short `task.py` beside it that calls
`ari.bases.harbor_deliverable`. Answer every row before writing code.

| Part | Where it goes | Write down |
| --- | --- | --- |
| Objective | `instruction.md` | Which files change, what form they take, which number should move and how it is measured. |
| Starting files | A directory in `environment/`, copied by the Dockerfile to the deliverable path, and named as `seed` | The unimproved deliverable. Unchanged, it is the baseline. |
| Container | `environment/Dockerfile` | What the researcher needs beside the deliverable: data, tools. Pin `FROM` by digest. Nothing of the grader. |
| Grader | A script in `tests/` | It reads the deliverable, and writes one number to `/logs/verifier/reward.txt` or several to `reward.json`. Name every fixed input it uses. |
| Held-out score | A second script in `tests/` | Only if some data must stay hidden from the researcher. Then `feedback` scores the public split and `sealed` the hidden one. Otherwise pass the one script as `feedback` and `sealed=None`. |
| Rules | `rules=[...]` in `task.py` | Everything a candidate must and must not do; the reviewer checks each one separately. |
| Metrics | `objective` and `rewards` in `task.py` | Only when the grader writes more than one reward: which one ranks candidates and its direction. |
| `validity` (optional) | A function in `task.py` | The rules a function can decide exactly: file names, schema, value ranges. |
| `cases` (optional) | `cases=(...)` in `task.py` | The names of the fixed inputs every candidate is graded on. The scripts read the name from `ARI_CASE`. |

[Harbor tasks](references/harbor-harness.md#improve-a-harbor-tasks-deliverable)
describes this base task and how to check it before a paid run.

The base task builds an `ari.Task`. Write one directly, with a Python function
as the evaluator, only when no container is needed or no base task fits. It has
four parts and several optional ones:

| `Task` part | Write down |
| --- | --- |
| `seed` | The directory of starting files to improve. Unchanged, they are the baseline. |
| `instructions` | Two sections: Objective, then Constraints with its Rules and Environment lists. Write every rule under Rules; the reviewer checks each one separately. ARI adds the metric, its direction and the starting files itself. Optional advice comes last. |
| `objective` | The metric that measures the goal and whether higher or lower is better. Name any tie-breaker and any other metric the evaluator records. |
| `evaluator` | The trusted code that measures one candidate, and every fixed input it uses: data, models, versions. |
| `validity` (optional) | The constraints a function can decide exactly: file names, schema, value ranges. |
| `cases` (optional) | The names of the fixed inputs that every candidate is evaluated on. The default is one case. |
| `context` (optional) | Read-only files for the researcher. Keep graders, hidden answers and credentials out of the context and the starting files. |

Five more optional parts change what the researcher is given. Leave them out
unless the user asks for that behaviour; the defaults are the standard run.
The [reference](../../reference.md#researcher-inputs) describes each.

| `Task` part | Use it when the user wants |
| --- | --- |
| `memory=ari.Memory(...)` | Researchers to see fewer earlier candidates (`history`), records from an earlier run (`preload`), earlier researchers' conversations as files, or one researcher session that makes several proposals in a row (`conversations`, `conversation_mode`; raise the researcher's time limit to fit), or a research log or summary (`log`, `summarize`). By default each researcher is a fresh conversation that sees every earlier candidate. |
| `environment=ari.Environment(...)` | The researcher to run in a Harbor environment with its own Dockerfile or services (`harbor`), or to start with no files left by earlier researchers (`workspace="reset"`). |
| `review_environment` | The reviewer to run in its own Harbor environment. |
| `transforms=[...]` | A study that changes what the researcher is told, such as `ari.transforms.omit_rule(2)`. The reviewer judges the changed rules; the plan keeps the original text. |
| `presenter` | A different wording or order of the prompt. |

Tell the user when a task uses any of these, because they change what a result
means.

Review is on by default: a reviewer agent judges every candidate against the
instructions, after the validity function. The reviewer enforces only what the instructions
say, so write every constraint into them, including the ones the validity function also
enforces. A constraint that appears nowhere else, such as "do not hard-code
answers for specific cases", is enforced by the reviewer alone.

Then copy the closest example directory and edit it.

| What changes | What stays fixed | Copy | Built with |
| --- | --- | --- | --- |
| A configuration, data file or implementation, graded by a script | The Harbor task and its grader | [examples/curve-fitting](../../examples/curve-fitting/README.md) | `ari.bases.harbor_deliverable` |
| The same, with a score that must stay hidden from the researcher, such as an imported benchmark task | The Harbor task, its grader and both splits | [examples/threshold-deliverable](../../examples/threshold-deliverable/README.md) | `ari.bases.harbor_deliverable` |
| Files of a Harbor task, such as its instructions | The solver and the Harbor task's grader | [examples/sum-instructions](../../examples/sum-instructions/README.md) | `ari.bases.harbor_task_files` |
| The solver harness: its program, prompts, skills and hooks | The Harbor tasks, their graders and the model | [examples/sum-harness](../../examples/sum-harness/README.md) | `ari.bases.harbor_harness` |
| Something small that a few lines of Python can score with no container | The evaluator function | [examples/packing](../../examples/packing/task.py) | `ari.Task` |

A "Harbor task" is Harbor's own unit: a directory with `task.toml`,
`instruction.md`, an environment and tests. One task can evaluate many Harbor
tasks as its cases. The three functions in `ari.bases` each return a complete
`ari.Task` from your Harbor tasks, so a Harbor `task.py` is a few lines.

[Design a task](references/task-integration.md) explains these decisions and
how to test a task before a search.
[Harbor tasks](references/harbor-harness.md) covers the three functions in
`ari.bases`.

Validity and quality are separate questions. The validity function and the reviewer decide
whether a candidate is allowed. The evaluator decides how good it is. An allowed
candidate can score badly, and a candidate that breaks a constraint gets no
score.

## Write the run settings

`run.toml` holds everything about a run that the task does not fix, in four
tables:

| Table | Holds |
| --- | --- |
| `[limits]` | `candidates`, `evaluations`, `repeats` and `parallel` |
| `[researcher]` | The agent that proposes candidates |
| `[reviewer]` | The agent that judges candidates. It defaults to the researcher's agent. |
| `[task]` | Values the task reads, such as the solver in `[task.solver]` |

[Configure and launch a run](references/running-autoresearch.md) covers the
agent tables, the budget arithmetic, the image each role needs and how
credentials are passed.

- Use the harnesses and models the user asked for, or the ones the project
  already uses. Otherwise propose a choice and say so.
- Size the budget from the task. The baseline counts as a candidate, and each
  measured candidate needs one evaluation for every case and repeat. Agent
  starts follow from the limits: one reviewer start for each candidate and one
  researcher start for each candidate after the baseline.
- Leave review on. Write `reviewer = false` at the top of `run.toml` only when
  the user accepts that the task's validity function alone decides validity.
- The stopping rule is the budget: `ari run` stops when any limit is reached.
  Agree the limits with the user.
- To test a result on cases the researcher never saw, plan a second run. See
  [Screen and confirm](references/low-fidelity.md).

## Check before any paid start

1. Run `ari check` with the real settings and image. It loads the task, validates
   every agent table, saves the plan and prints the budget. It starts nothing.
2. Read the `Review:` line. It says `the task's validity function, then a reviewer` or
   `a reviewer` when review is on. If it says `the task's validity function` or `off`, the
   settings turned review off, and no one judges the constraints that the validity function
   does not cover.
3. For a new task, [test the evaluator and the validity function](references/task-integration.md#test-the-task-before-a-search)
   with `ari record` and run settings without agents. With a Harbor task this
   uses `solver_trial` starts, so it needs approval.

## Report to the user

Before asking for approval, tell the user:

- The budget: every value in `[limits]`, the agent starts that follow from
  them, and the models each role uses. Starts are counts. They do not limit
  tokens or cost.
- The exact `ari run` command, with the image ID and a fresh run directory.
- Where results go: the run directory, its `summary.json` and its `viewer.html`.
- What `ari check` printed, and what you could not test without paid starts.

After a run, read `summary.json` or run `ari inspect <run-directory>`. Report the
selected candidate, each candidate's validity and metrics, the starts used, and
whether the total cost is known. Say which kind of evidence each statement rests
on: fixture (fakes, no Docker or models), reference (Harbor Oracle or Nop
controls, no models), transport (real container and CLI with a fake provider), or
live model. A passing `ari check` shows that the files load. It says nothing about
whether a search will find an improvement.
