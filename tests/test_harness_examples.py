"""Real worker/RPC fixtures for trusted examples; no containers or model calls."""

import asyncio
import re
import sys
from pathlib import Path

import pytest

from autoresearch_infrastructure.programs import SolverInput, starter
from autoresearch_infrastructure.programs.bridge import ProgramBridge, ProgramExecutionError
from autoresearch_infrastructure.source_tree import materialize

EXAMPLES = Path(__file__).parents[1] / "examples/harness-programs"
TOOLS = ({"name": "exec", "parameters": {"type": "object"}},)


@pytest.mark.parametrize("harness", ["codex", "claude-code"])
@pytest.mark.parametrize("example,helper_completed", [
    ("tool-hook", True), ("output-skill", True),
    ("helper-session", True), ("helper-session", False),
    ("starter", True), ("usage-hook", True),
])
def test_example_through_worker_and_session_bridge(
    tmp_path, monkeypatch, example, harness, helper_completed,
):
    monkeypatch.setenv("PYTHONPATH", str(Path(__file__).parents[1] / "src"))
    seen = []
    effects = []
    package = EXAMPLES / example
    if example in {"starter", "usage-hook"}:
        package = tmp_path / "candidate"
        source = Path(starter.__file__).read_text()
        if example == "usage-hook":
            guide = Path(starter.__file__).with_name("usage.md").read_text()
            source, = (code for code in re.findall(r"```python\n(.*?)```", guide, re.S)
                       if "async def solve" in code)
        materialize(package, {
            "program.toml": f'id="{example}"\ndescription="Template fixture"\nentrypoint="solve.py:solve"\n',
            "solve.py": source,
        })

    async def execute(call):
        effects.append(call["args"]["command"])
        return {
            "result": {"stdout": call["args"]["command"]},
            "error": "fixture command error" if call["args"]["command"] == "second" else None,
            "denied": False,
        }

    async def session(**kwargs):
        seen.append(kwargs)
        if kwargs["parent_id"] is not None:
            assert example == "helper-session"
            assert kwargs["settings"]["tools"] == {"allow": []}
            assert kwargs["settings"]["skills"] == []
            assert "first" in kwargs["input"]
            return {"text": "Reopen the output file.", "completed": helper_completed, "usage": {}}
        if example == "output-skill":
            assert kwargs["settings"]["skills"] == ["skills/check-output"]
            skill = EXAMPLES / example / "skills/check-output/SKILL.md"
            assert skill.read_text().startswith("---\nname: check-output\n")
        first = await kwargs["tool_handler"]({"name": "exec", "args": {"command": "first"}})
        if example == "helper-session":
            assert first["result"] == {
                "original": {"stdout": "first"}, "next_step_hint": "Reopen the output file.",
            }
        else:
            assert first["result"] == {"stdout": "first"}
        assert first["error"] is None and first["denied"] is False
        assert first["call_id"]
        second = await kwargs["tool_handler"]({"name": "exec", "args": {"command": "second"}})
        assert second["result"] == {"stdout": "second"}
        assert second["error"] == "fixture command error" and second["denied"] is False
        assert second["call_id"] != first["call_id"]
        return {"text": "fixture answer", "completed": True, "usage": {}}

    bridge = ProgramBridge(
        package, example, harness, "fixture-model",
        SolverInput("Produce the requested output file.", TOOLS), session, execute,
        tmp_path / "evidence", 10, max_sessions=2, max_depth=1,
    )

    async def run():
        return await bridge._run_process(
            [sys.executable, "-m", "autoresearch_infrastructure.programs.worker"],
            str(package),
        )

    if not helper_completed:
        with pytest.raises(ProgramExecutionError):
            asyncio.run(run())
        assert effects == ["first"]
        assert len(bridge.sessions) == 2
        assert bridge.sessions[1]["status"] == "incomplete"
        return

    result = asyncio.run(run())
    assert result["answer"] == "fixture answer"
    assert effects == ["first", "second"]
    assert len(seen) == (2 if example == "helper-session" else 1)
    if example == "helper-session":
        assert seen[1]["parent_id"] == seen[0]["session_id"]
    if example == "tool-hook":
        assert "Task tool calls: 2" in (tmp_path / "evidence/worker-stderr.log").read_text()
