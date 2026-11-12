# Improve the solver

## Objective

Improve the solver program in `/workspace/candidate/` so that it completes its
tasks more reliably. Make one improvement to your starting version.

The program is `solve.py` with its manifest `program.toml`. Prompts, skills and
code that runs before or after tools can live beside them. The starting
`solve.py` already has the imports and entrypoint set up, so build on it.

The score is the average task score. Evaluation keeps the tasks, environments
and scoring checks fixed.

Look for a repeated failure or wasted step, then change something that addresses
its cause. For example, improve planning, tool use, output checks or how a skill
is used. Prefer one clear, reusable change you can explain over many unrelated
edits. Write down what you expect to improve and why; the evaluator measures it.
$advice
## Constraints

### Rules

- Use the instructions, files and tools available to the solver during a task.
  Do not hard-code answers, add exceptions for specific test cases, inspect
  reference answers or hidden tests, or alter score files. Adapting the approach
  to the kind of task is fine.
- Keep the ID and entrypoint in `program.toml`. Use only the documented interface and
  standard-library dependencies. In the solver code, make every model call through
  `runtime.run_harness`; do not call providers directly or launch native delegation.
- Keep the chosen model/provider, reasoning effort, permissions and time limits.
  Each trial allows at most $sessions solver sessions total, including helpers and
  retries. With one session, use no helpers or retries.
- Do not try to reach tasks or files other than the ones described here.

### Environment

- The runtime fixes the model and provider, limits the available tools, and caps
  solver sessions and evaluation time.
- `/context` holds the reference material: `starter.py` is a working solver
  template, `usage.md` has complete examples for adding instructions, skills and
  hooks, `policy.md` lists the rules, and `api.py` and `harness-kwargs.md`
  describe the interface.
