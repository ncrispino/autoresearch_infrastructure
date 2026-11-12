from autoresearch_infrastructure.programs import SolverOutput


async def solve(task, runtime):
    runtime.state["task_tool_calls"] = 0

    async def count_call(call, observation):
        if call.origin == "task":
            runtime.state["task_tool_calls"] += 1
        return observation

    result = await runtime.run_harness(
        input=task.instruction, post_tool=count_call, state_scope="trial",
    )
    if not result.completed:
        raise RuntimeError("Incomplete solver session")
    print(f"Task tool calls: {runtime.state['task_tool_calls']}")
    return SolverOutput(answer=result.text)
