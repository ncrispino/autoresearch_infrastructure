"""Starting solver, copied to the candidate's solve.py before research begins."""

from autoresearch_infrastructure.programs import SolverOutput


async def solve(task, runtime):
    result = await runtime.run_harness(input=task.instruction)
    if not result.completed:
        raise RuntimeError("Solver did not complete")
    return SolverOutput(result.text)
