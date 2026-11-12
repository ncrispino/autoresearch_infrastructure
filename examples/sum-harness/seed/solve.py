from pathlib import Path

from autoresearch_infrastructure.programs import SolverOutput


async def solve(task, runtime):
    prompt = Path(__file__).with_name("prompt.txt").read_text()
    result = await runtime.run_harness(input=task.instruction, system=prompt)
    if not result.completed:
        raise RuntimeError("Solver session did not finish")
    return SolverOutput(result.text)
