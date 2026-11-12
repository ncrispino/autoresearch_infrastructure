# Sample solver programs

A solver program is the Python code that drives a solver's coding-agent sessions
during one Harbor trial. These three samples each show one mechanism of the
[solver program interface](../../src/autoresearch_infrastructure/programs/README.md).
Use one as the `seed` of a task that evaluates candidates with
`HarborHarnessEvaluator`. For a complete task built that way, see the
[sum-harness example](../sum-harness/README.md).

| Sample | What it shows | Sessions per trial |
| --- | --- | --- |
| [tool-hook](tool-hook/solve.py) | A hook that runs after each task tool call. It counts the calls and returns each result unchanged. | 1 |
| [output-skill](output-skill/solve.py) | One local skill, selected by path, that asks the solver to check its output. | 1 |
| [helper-session](helper-session/solve.py) | After the first task tool result, a second session with no tools suggests a next step. | At most 2 |

The samples contain no strategy for a particular benchmark, no scoring rule and no
answers. None of them has been shown to improve a score.

## Layout

```text
tool-hook/
  program.toml              id, description and entrypoint "solve.py:solve"
  solve.py                  the editable program and its hooks
output-skill/
  program.toml
  solve.py
  skills/check-output/
    SKILL.md
helper-session/
  program.toml
  solve.py
```

Each sample has its own ID in `program.toml`. `HarborHarnessEvaluator` accepts
any valid ID. The harness study (`pilot`) creates its own seed with the ID
`benchmark-harness`, so to use a sample there, copy the code and skills into that
candidate and keep its ID.

## How a program runs

The researcher edits the program in its workspace. Recording a candidate saves the
exact bytes. For each trial, a separate worker container loads that snapshot and
runs `solve`. Candidate code is never imported by the process that runs the
experiment. Task tools act on the Harbor task's container, which is separate from
the worker. A solver session can read only the skills that the program selects.

## What each sample does

### tool-hook

The hook counts calls whose origin is the task and keeps the count in
trial-scoped state. The program prints the count to the worker log. The count is
not a metric and does not reach the grader. The hook returns each observation
unchanged, including its result, error, denial flag and call ID.

### output-skill

The program selects `skills/check-output` and adds one system instruction that
tells the solver to use it. The skill asks for a check. It cannot make the model
perform one, and it grants no extra file or shell access.

### helper-session

The helper session starts inside the first session's post-tool hook, so its
recorded `parent_id` links it to the session that was running. It receives the
task instruction and an excerpt of the tool result. It has no task tools and no
skills. The hook returns the original result together with the hint, and leaves
the error and denial fields unchanged.

If the helper session does not complete, the program raises, the evaluation is
incomplete and the candidate gets no score for it.

To run this sample:

- Allow at least two sessions per trial (`max_harness_sessions=2`). Both
  sessions use the same model and harness and both count as `solver_session`
  starts.
- The helper session must finish within the hook deadline, which is 30 seconds
  under `HarborHarnessEvaluator`.

The program runtime starts the helper session. This is different from the
coding-agent CLI's own delegation feature, which stays disabled.

## What has been verified

Fixture evidence: [test_harness_examples.py](../../tests/test_harness_examples.py) runs each sample through the
real worker process and its RPC connection, with fake sessions and no Docker or
model calls. It checks hook registration, unchanged observations, skill selection
and the helper session's limits.

Transport evidence: separate checks ran the Codex and Claude Code CLIs in
containers against local fake providers. They passed, including a helper session
with a selected skill that was started from a hook.

There is no live model evidence for these samples.
