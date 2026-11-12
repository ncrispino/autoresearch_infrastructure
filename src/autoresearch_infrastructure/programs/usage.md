# Edit the solver

`/workspace/candidate/solve.py` is a working starting point. It sends the task
instructions to the model and returns its answer. The imports and return value
are already set up. An unchanged copy is at `/context/starter.py`.

You can change the instructions, add skills, or add steps around tool calls.
Keep the `solve(task, runtime)` entrypoint and the ID in `program.toml`.
The evaluator runs your saved solver in the task's environment and scores what
it does. Your researcher container is for editing and inspecting results.

## Add instructions or a skill

In the starter, replace the `run_harness` call with:

```python
result = await runtime.run_harness(
    input=task.instruction,
    system="Check the requested output before finishing.",
    skills=["skills/check-output"],
)
```

Put the skill at `skills/check-output/SKILL.md` inside the candidate. Use only
options supported by the chosen harness; see `/context/harness-kwargs.md`.

## Add a tool hook

This complete example counts tool calls without changing their results:

```python
from autoresearch_infrastructure.programs import SolverOutput


async def solve(task, runtime):
    runtime.state["tool_calls"] = 0

    async def after_tool(call, observation):
        runtime.state["tool_calls"] += 1
        return observation

    result = await runtime.run_harness(
        input=task.instruction, post_tool=after_tool, state_scope="trial",
    )
    if not result.completed:
        raise RuntimeError("Solver did not complete")
    return SolverOutput(result.text)
```

Hooks run before or after a tool call. They must be async functions; returning
the original observation preserves its result, errors and call ID. More detailed
types and signatures are in `/context/api.py`. This example uses trial-scoped
state so the counter is shared between the solver and its tool hook. Model calls go through
`runtime.run_harness` and count toward the run's session limit. The only task tool
in this study is `exec`, which runs commands in the task environment. Helper
sessions, when allowed, use the same model and have no task tools.
