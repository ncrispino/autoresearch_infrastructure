"""Fixture and local subprocess checks; no provider requests."""

import asyncio
import base64
import json
import shlex
import subprocess
from concurrent.futures import ThreadPoolExecutor
from types import SimpleNamespace

import pytest

from autoresearch_infrastructure.ledger import StartLedger, StartLimitError


def test_shared_paid_ledger_retains_failed_starts_and_enforces_atomic_limits(tmp_path):
    path = tmp_path / "ledger.json"
    limits = {"solver_session": 3, "researcher": 1}
    ledger = StartLedger(path, limits=limits, identity={"model": "fixture"})

    def admit(index):
        try:
            return ledger.start("solver_session", str(index))
        except StartLimitError:
            return None

    with ThreadPoolExecutor(max_workers=5) as pool:
        keys = [key for key in pool.map(admit, range(5)) if key]
    assert len(keys) == 3
    ledger.finish(keys[0], status="failed")
    resumed = StartLedger(path, limits=limits, identity={"model": "fixture"})
    assert resumed.snapshot()["counts"]["solver_session"] == 3
    assert resumed.snapshot()["starts"][keys[0]]["cost_usd"] is None
    with pytest.raises(StartLimitError):
        resumed.start("solver_session", "retry")
    with pytest.raises(ValueError, match="changed"):
        StartLedger(path, limits={"solver_session": 4}, identity={"model": "fixture"})


def test_generic_program_package_does_not_import_candidate(tmp_path):
    from autoresearch_infrastructure.programs.package import validate_program

    marker = tmp_path / "host-imported"
    (tmp_path / "program.toml").write_text('id="fixture"\ndescription="test"\nentrypoint="solve.py:solve"\n')
    (tmp_path / "solve.py").write_text(
        f"from pathlib import Path\nPath({str(marker)!r}).touch()\nasync def solve(task,runtime): pass\n"
    )
    validate_program(tmp_path, "fixture", harness="codex")
    assert not marker.exists()


def test_harbor_agent_loads_from_the_generic_import_path():
    pytest.importorskip("harbor")
    from harbor.agents.factory import AgentFactory
    from harbor.models.trial.config import AgentConfig

    from autoresearch_infrastructure.harbor_agent import HarnessProgramAgent

    assert (
        AgentFactory.get_agent_class_from_config(
            AgentConfig(import_path="autoresearch_infrastructure.harbor_agent:HarnessProgramAgent")
        )
        is HarnessProgramAgent
    )


def test_task_terminal_bridge_bounds_outputs_and_rejects_controller_options(tmp_path):
    pytest.importorskip("harbor")
    from autoresearch_infrastructure.harbor_agent import HarborTaskTools

    class TaskSandbox:
        async def exec(self, command, timeout_sec):
            result = await asyncio.to_thread(
                subprocess.run,
                ["bash", "-lc", command],
                cwd=tmp_path,
                capture_output=True,
                text=True,
                timeout=timeout_sec,
            )
            return SimpleNamespace(return_code=result.returncode, stdout=result.stdout, stderr=result.stderr)

    tools = HarborTaskTools(TaskSandbox(), max_output_bytes=10)

    async def check():
        success = await tools({"name": "exec", "args": {"command": "printf sandbox > marker; cat marker"}})
        assert success["result"]["stdout"] == "sandbox"
        assert (tmp_path / "marker").read_text() == "sandbox"
        bounded = await tools({"name": "exec", "args": {"command": "python -c 'print(\"x\"*100)'"}})
        assert bounded["error"] == "output_limit"
        assert len(bounded["result"]["stdout"]) <= 10
        timeout = await tools({"name": "exec", "args": {"command": "sleep 3", "timeout_seconds": 1}})
        assert timeout["error"] == "command_timeout"
        with pytest.raises(ValueError):
            await tools({"name": "exec", "args": {"command": "true", "user": "root"}})
        with pytest.raises(ValueError):
            await tools({"name": "controller_exec", "args": {"command": "true"}})

    asyncio.run(check())


def test_task_terminal_rejects_millisecond_timeouts_and_reports_its_actual_bound():
    pytest.importorskip("harbor")
    from autoresearch_infrastructure.harbor_agent import HarborTaskTools

    received = []

    class TaskSandbox:
        async def exec(self, command, timeout_sec):
            payload = json.loads(base64.b64decode(shlex.split(command)[-1]))
            received.append((payload["timeout_seconds"], timeout_sec))
            response = {"stdout": "", "stderr": "", "return_code": 0, "failure": None}
            return SimpleNamespace(return_code=0, stdout=json.dumps(response), stderr="")

    tools = HarborTaskTools(TaskSandbox(), max_seconds=3)

    async def check():
        for invalid in (10000, 4, 0, -1, True, 1.5):
            with pytest.raises(ValueError, match="integer between 1 and 3 seconds"):
                await tools({"name": "exec", "args": {"command": "true", "timeout_seconds": invalid}})
        assert received == []
        await tools({"name": "exec", "args": {"command": "true"}})
        await tools({"name": "exec", "args": {"command": "true", "timeout_seconds": 1}})
        assert received == [(3, 8), (1, 6)]

    asyncio.run(check())
    timeout = HarborTaskTools.tools[0]["parameters"]["properties"]["timeout_seconds"]
    assert (timeout["minimum"], timeout["maximum"], timeout["default"]) == (1, 30, 30)
    assert "seconds" in timeout["description"]


def test_provider_selection_is_injected_and_session_baseline_can_be_frozen():
    pytest.importorskip("harbor")
    from autoresearch_infrastructure.harnesses.sessions import system_text

    assert system_text({}, "project baseline") == "project baseline"
    assert system_text({"system": "check output"}, "baseline") == "baseline\n\ncheck output"


def test_ca_build_overlay_keeps_upstream_files_unchanged_and_tls_enabled(tmp_path):
    from autoresearch_infrastructure.harbor_build import ca_build_overlay

    task = tmp_path / "task"
    (task / "environment").mkdir(parents=True)
    dockerfile = task / "environment/Dockerfile"
    original = "FROM python:3.12-slim\nRUN pip install pytest\nRUN echo ready\n"
    dockerfile.write_text(original)
    ca = tmp_path / "ca.pem"
    ca.write_text("public test certificate")
    overlay = ca_build_overlay(task, tmp_path / "overlay", ca)
    assert dockerfile.read_text() == original
    derived = (overlay.parent / "Dockerfile").read_text()
    assert derived.count("--mount=type=secret") == 2
    assert "PIP_CERT=/run/secrets/session_ca" in derived
    assert "trusted-host" not in derived and "COPY" not in derived
    receipt = json.loads((overlay.parent / "receipt.json").read_text())
    assert receipt["source_dockerfile_sha256"] != receipt["derived_dockerfile_sha256"]


def test_shared_allocation_rejects_whole_panel_and_parallel_launchers(tmp_path):
    ledger = StartLedger(tmp_path / "ledger.json", limits={"solver_trial": 3}, identity={})
    ledger.start("solver_trial", "failed earlier attempt")
    with ledger.allocation_owner():
        with ledger.allocation_owner():
            ledger.require_capacity({"solver_trial": 2})
        def other_launcher():
            with pytest.raises(RuntimeError, match="owns this shared budget"):
                with ledger.allocation_owner():
                    pass
        with ThreadPoolExecutor(max_workers=1) as pool:
            pool.submit(other_launcher).result()
        with pytest.raises(StartLimitError, match="start budget"):
            ledger.require_capacity({"solver_trial": 3})
        ledger.require_capacity({"solver_trial": 2})
    assert ledger.snapshot()["counts"]["solver_trial"] == 1


def test_pilot_reserves_external_capacity_before_dispatch(tmp_path):
    from autoresearch_infrastructure import Evaluation, Limits, Metric, Research, Validity

    ledger = StartLedger(
        tmp_path / "ledger.json", limits={"solver_trial": 1, "solver_session": 2}, identity={}
    )
    starts = []

    class Evaluator:
        max_harness_sessions = 2

        def reserve_evaluations(self, count):
            self.ledger.require_capacity({"solver_trial": count, "solver_session": count * 2})

        def __init__(self):
            self.ledger = ledger

        def __call__(self, candidate, context):
            starts.append(context.task)
            return Evaluation({Metric("success"): 1}, completed=True)

    seed = tmp_path / "seed"
    seed.mkdir()
    (seed / "candidate.txt").write_text("fixture")
    with Research(
        tmp_path / "output",
        seed=seed,
        objective=Metric("success"),
        evaluator=Evaluator(),
        validity=lambda *args: Validity("valid", ("Fixture passes",)),
        evaluation_config={},
        limits=Limits(max_evaluations=2),
        tasks=("a", "b"),
    ) as research:
        with pytest.raises(StartLimitError, match="start budget"):
            research.record_candidate()
    assert starts == []
    assert ledger.snapshot()["counts"]["solver_trial"] == 0
