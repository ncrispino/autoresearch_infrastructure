from autoresearch_infrastructure.programs import SolverOutput


async def solve(task, runtime):
    result = await runtime.run_harness(
        input=task.instruction,
        skills=["skills/check-output"],
        system="Use the check-output skill before finishing a file-producing task.",
    )
    if not result.completed:
        raise RuntimeError("Incomplete solver session")
    return SolverOutput(answer=result.text)
