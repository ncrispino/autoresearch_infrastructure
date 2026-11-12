import json
from dataclasses import replace

from autoresearch_infrastructure.programs import SolverOutput


async def solve(task, runtime):
    helper_used = False

    async def add_hint(call, observation):
        nonlocal helper_used
        if call.origin != "task" or helper_used:
            return observation
        helper_used = True
        helper = await runtime.run_harness(
            input=("Suggest one next step; do not perform it.\nTask:\n"
                   + task.instruction + "\nTool output excerpt:\n"
                   + json.dumps(observation.result, ensure_ascii=False)[:8000]),
            tools={"allow": []}, skills=[],
        )
        if not helper.completed:
            raise RuntimeError("Incomplete helper session")
        return replace(observation, result={
            "original": observation.result, "next_step_hint": helper.text,
        })

    result = await runtime.run_harness(
        input=task.instruction, post_tool=add_hint,
        subagents={"enabled": True, "max_depth": 1},
    )
    if not result.completed:
        raise RuntimeError("Incomplete solver session")
    return SolverOutput(answer=result.text)
