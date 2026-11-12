# Solver programs

A solver program is a directory of Python code that runs one solver for one trial.
It decides what to send to the coding-agent CLI, which skills to select and what
happens before and after each tool call. In a harness task, the solver program is
the candidate.

## Package layout

```text
program.toml    id, description, entrypoint = "solve.py:solve", optional [kwargs]
solve.py        async def solve(task, runtime)
skills/...      optional local skills
```

The [starter solver](starter.py) is the smallest working program. The harness
study (`pilot`) copies it into the baseline's `solve.py`.

```python
from autoresearch_infrastructure.programs import SolverOutput


async def solve(task, runtime):
    result = await runtime.run_harness(input=task.instruction)
    if not result.completed:
        raise RuntimeError("Solver did not complete")
    return SolverOutput(result.text)
```

`write_researcher_context` in `harbor_harness.py` gives the researcher read-only
copies of the starter, the [usage guide](usage.md), and the types in
[api.py](api.py) and [parsing.py](parsing.py).

## Validation

`validate_program` in [package.py](package.py) checks the manifest fields, the
entrypoint path and that the entrypoint is an async function. With `harness=` it
also checks `[kwargs]` against that harness's settings. It parses the source and
never imports it. A caller may use another manifest file name through
`manifest_name=`. The coevolution project uses `defense.toml`.

## Execution

`ProgramBridge` in [bridge.py](bridge.py) runs the program in a worker container
that is removed after the trial. The container has no network, a read-only root
filesystem, no capabilities and a non-root user. It holds no provider key, no
grader and no Docker socket.

The worker reaches the outside through two RPC calls that the bridge serves:
start a solver session, and run a task tool. The bridge enforces these limits for
the whole trial:

| Limit | `ProgramBridge` default |
| --- | --- |
| Sessions | 32 |
| Helper session depth | 2 |
| Task tool calls | 512 |
| Time for one hook | 30 seconds |
| Trial duration | Set by the caller |

Every session uses the harness and model that the caller fixed. A `pre_tool` or
`post_tool` hook sees task tool calls and the skill files that a session reads. A
helper session never has task tools. `runtime.state` is separate for each session by default.
`state_scope="trial"` shares it across the trial.

## Results

`HarnessResult.completed` means the CLI session ended normally. It says nothing
about whether the grader passed.

Token and cost totals are reported only when every started session reported that
field. Otherwise the total is `None`, and the `observed_*` fields give the known
part together with the number of sessions it covers.

If the bridge cannot confirm that a session or the worker container was removed,
the trial fails.

Hook support differs between the Codex and Claude Code CLIs. See the
[harness sessions README](../harnesses/README.md). A result transformation that
the selected CLI does not support raises an error.

`parse_json_object` is a strict JSON-object parser for model output. The caller
still checks types and meaning.
