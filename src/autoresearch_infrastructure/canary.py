"""Measure only the unchanged baseline solver on Harbor benchmark tasks.

Use this before a harness study to check the Harbor tasks with Harbor's reference
agents (``--reference``) and to record the baseline's rewards, token use and timing
(``--baseline``). No candidate other than the exact baseline is accepted.
"""

from __future__ import annotations

import argparse
import asyncio
import importlib.metadata
import json
import subprocess
import time
import tomllib
from pathlib import Path

from .api import Limits, Objective, Validity
from .harbor_build import ca_build_overlay
from .harbor_harness import HarborHarnessEvaluator
from .harbor_trial import SOLVER_TOKENS, SUCCESS_RATE, trial_timings
from .ledger import StartLedger
from .persistence import digest, write_json
from .programs.package import validate_program
from .providers import provider_for
from .research import Research
from .source_tree import evidence_digest, materialize, python_tree_hash, snapshot
from .viewer import write_html_preview

BASELINE = {
    "program.toml": 'id="benchmark-harness"\ndescription="Unchanged one-session baseline"\nentrypoint="solve.py:solve"\n',
    "solve.py": 'from autoresearch_infrastructure.programs import SolverOutput\n\nasync def solve(task, runtime):\n    result = await runtime.run_harness(input=task.instruction)\n    if not result.completed:\n        raise RuntimeError("Solver did not complete")\n    return SolverOutput(result.text)\n',
}


class BaselineValidity:
    """Accepts only the exact baseline solver program."""

    def __init__(self, harness="codex"):
        self.harness = harness

    @property
    def identity(self):
        return {"baseline_hash": digest(BASELINE), "harness": self.harness}

    def __call__(self, candidate, context):
        if snapshot(candidate) != BASELINE or context.candidate_hash != digest(BASELINE):
            return Validity("invalid", ("Canary admits only the exact trusted baseline",))
        validate_program(candidate, "benchmark-harness", harness=self.harness)
        return Validity("valid", ("Exact controller-owned one-session baseline",))


def prepare(plan_path, tasks_root, output, image, allocation_ledger=None):
    plan = tomllib.loads(plan_path.read_text())
    if plan.get("runnable") is not True:
        raise ValueError("Canary requires a runnable plan")
    harness = plan.get("harness", {}).get("name", "codex")
    provider_for(harness, plan["solver_provider"])
    for key in (
        "solver_timeout_seconds",
        "max_solver_trial_starts",
        "max_harness_session_starts",
        "max_reference_trial_starts",
        "max_sessions_per_trial",
        "max_parallel_trials",
    ):
        if type(plan.get(key)) is not int or plan[key] <= 0:
            raise ValueError("Canary limits must be positive integers")
    names = plan["benchmark"]["task_ids"]
    if (
        not names
        or len(names) != len(set(names))
        or any(Path(n).name != n or n in {".", ".."} for n in names)
    ):
        raise ValueError("Canary requires unique task basenames")
    if plan["max_parallel_trials"] != 1 or plan["max_sessions_per_trial"] != 1:
        raise ValueError("Baseline canary uses sequential one-session trials")
    if plan["max_solver_trial_starts"] < len(names) or plan["max_harness_session_starts"] < len(names):
        raise ValueError("Entire baseline panel must fit the declared ceilings")
    negative = plan["benchmark"]["negative_control_task_ids"]
    if not negative or len(set(negative)) != len(negative) or not set(negative) <= set(names):
        raise ValueError("Pin negative controls within the selected task panel")
    if plan["max_reference_trial_starts"] < len(names) + len(negative):
        raise ValueError("Reserve all oracle and declared no-op controls")
    tasks = {name: tasks_root / name for name in names}
    hashes = {name: evidence_digest(task, max_total_bytes=100_000_000) for name, task in tasks.items()}
    image_id = subprocess.run(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()
    if not image_id.startswith("sha256:"):
        raise ValueError("Freeze an immutable solver image")
    allocation = {"plan": plan, "task_hashes": hashes, "solver_image_id": image_id}
    identity = {
        **allocation,
        "runtime": python_tree_hash(Path(__file__).parent),
        "harbor": importlib.metadata.version("harbor"),
    }
    output.mkdir(parents=True, exist_ok=True)
    receipt = output / "plan.json"
    if receipt.exists() and json.loads(receipt.read_text()) != identity:
        raise ValueError("Canary plan/runtime changed; use fresh evidence and retain its allocation ledger")
    write_json(receipt, identity)
    overlays = {}
    for name, task in tasks.items():
        destination = output / "build-overlays" / name
        overlays[name] = (
            destination / "compose.json"
            if destination.exists()
            else ca_build_overlay(
                task,
                destination,
                Path("/etc/ssl/certs/ca-certificates.crt"),
                uv_version=plan["benchmark"].get("uv_install_version"),
            )
        )
    identity["build_overlays"] = {name: evidence_digest(path.parent) for name, path in overlays.items()}
    ledger = StartLedger(
        allocation_ledger or output / "starts.json",
        limits={
            "solver_trial": plan["max_solver_trial_starts"],
            "solver_session": plan["max_harness_session_starts"],
            "reference_trial": plan["max_reference_trial_starts"],
        },
        identity=allocation,
    )
    return plan, tasks, overlays, identity, allocation, ledger


def verified_control(output, agent, name, identity):
    directory = output / "controls" / f"{agent}-{name}"
    row = json.loads((directory / "receipt.json").read_text())
    result = json.loads((directory / "harbor-result.json").read_text())
    expected = 1.0 if agent == "oracle" else 0.0
    rewards = (result.get("verifier_result") or {}).get("rewards")
    if (
        row.get("agent") != agent
        or row.get("task") != name
        or row.get("task_hash") != identity["task_hashes"][name]
        or row.get("build_overlay_hash") != identity["build_overlays"][name]
        or row.get("result_hash") != digest(result)
        or row.get("passed") is not True
        or result.get("exception_info") is not None
        or not isinstance(rewards, dict)
        or len(rewards) != 1
        or list(rewards.values()) != [expected]
    ):
        raise ValueError(f"Original {agent} control is failed, changed or mismatched for {name}")
    return row


def controls(tasks, overlays, identity, ledger, output, negative_tasks):
    from harbor.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig, TrialConfig
    from harbor.trial.trial import Trial

    pending = [
        (agent, name)
        for agent in ("oracle", "nop")
        for name in tasks
        if (agent == "oracle" or name in negative_tasks)
        if not (output / "controls" / f"{agent}-{name}" / "receipt.json").exists()
    ]
    ledger.require_capacity({"reference_trial": len(pending)})
    results = {}
    for agent in ("oracle", "nop"):
        for name, task in tasks.items():
            if agent == "nop" and name not in negative_tasks:
                continue
            trial_name = f"{agent}-{name}"
            receipt = output / "controls" / trial_name / "receipt.json"
            if receipt.exists():
                row = json.loads(receipt.read_text())
            else:
                key = ledger.start("reference_trial", str(receipt))
                config = TrialConfig(
                    task=TaskConfig(path=task),
                    trial_name=trial_name,
                    trials_dir=output / "controls",
                    agent=AgentConfig(name=agent),
                    environment=EnvironmentConfig(extra_docker_compose=[overlays[name]]),
                )
                started = time.monotonic()

                async def execute():
                    trial = await Trial.create(config)
                    return await trial.run()

                try:
                    result = asyncio.run(execute())
                    write_json(receipt.parent / "harbor-result.json", result)
                    rewards = result.verifier_result.rewards if result.verifier_result else None
                    expected = 1.0 if agent == "oracle" else 0.0
                    passed = (
                        result.exception_info is None
                        and isinstance(rewards, dict)
                        and len(rewards) == 1
                        and list(rewards.values()) == [expected]
                    )
                    row = {
                        "agent": agent,
                        "task": name,
                        "task_hash": identity["task_hashes"][name],
                        "build_overlay_hash": identity["build_overlays"][name],
                        "result_hash": digest(result.model_dump(mode="json")),
                        "passed": passed,
                        "rewards": rewards,
                        "timing": {"end_to_end_seconds": time.monotonic() - started, **trial_timings(result)},
                    }
                    write_json(receipt, row)
                    ledger.finish(key, status="completed" if passed else "failed")
                except BaseException:
                    ledger.finish(key, status="failed")
                    raise
            verified_control(output, agent, name, identity)
            results[trial_name] = row
    write_json(
        output / "references.json",
        {
            "task_hashes": identity["task_hashes"],
            "build_overlays": identity["build_overlays"],
            "all_passed": True,
            "controls": results,
        },
    )
    return results


def baseline(plan, tasks, overlays, identity, allocation, ledger, output):
    reference = json.loads((output / "references.json").read_text())
    if reference.get("all_passed") is not True or any(
        reference.get(k) != identity[k] for k in ("task_hashes", "build_overlays")
    ):
        raise ValueError("Passing original controls for these exact task/build bytes are required")
    controls = {
        f"{agent}-{name}": verified_control(output, agent, name, identity)
        for agent in ("oracle", "nop")
        for name in tasks
        if agent == "oracle" or name in plan["benchmark"]["negative_control_task_ids"]
    }
    if reference.get("controls") != controls:
        raise ValueError("Reference receipt differs from original control evidence")
    seed = output / "seed"
    if not seed.exists():
        materialize(seed, BASELINE)
    if snapshot(seed) != BASELINE:
        raise ValueError("Controller baseline changed")
    harness = plan.get("harness", {}).get("name", "codex")
    evaluator = HarborHarnessEvaluator(
        tasks=tasks,
        solver_image=identity["solver_image_id"],
        model=plan["solver_model"],
        harness=harness,
        provider=provider_for(harness, plan["solver_provider"]),
        ledger=ledger,
        timeout_seconds=plan["solver_timeout_seconds"],
        max_harness_sessions=1,
        compose_overlays=overlays,
        include_solver_tokens=True,
    )
    with Research(
        output / "baseline",
        seed=seed,
        objective=Objective(
            SUCCESS_RATE,
            tie_breakers=(SOLVER_TOKENS,),
            description="Calibrate original benchmark reward and native token usage",
        ),
        evaluator=evaluator,
        validity=BaselineValidity(harness),
        evaluation_config={"fidelity": "calibration", "canary": identity},
        limits=Limits(
            max_evaluations=plan["max_solver_trial_starts"],
            max_candidates=1,
            max_repeats=1,
            selection_repeats=1,
        ),
        tasks=tuple(tasks),
        conditions=("calibration",),
    ) as research:
        result = research.record_candidate("candidate-0001" if research.candidates else "workspace")
        evidence = result["evidence"]
        rows = []
        for cell in evidence["cells"].values():
            if cell["status"] == "completed":
                value = cell["result"]
                rows.append(
                    {
                        "task": cell["task_id"],
                        "reward": value["metrics"]["success_rate"],
                        "solver_tokens": value["metrics"]["solver_tokens"],
                        "completed": value["completed"],
                        **value.get("metadata", {}),
                    }
                )
        summary = {
            "status": evidence["evaluation"],
            "candidate_hash": result["source_hash"],
            "contract_hash": result["contract_hash"],
            "metrics": evidence.get("metrics"),
            "tasks": rows,
            "starts": ledger.snapshot(),
        }
        write_json(output / "summary.json", summary)
    if not (output / "baseline.html").exists():
        write_html_preview(output / "baseline", output / "baseline.html")
    if summary["status"] != "completed":
        raise RuntimeError("Baseline panel incomplete; starts and partial evidence retained")
    return summary


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--tasks-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", default="ari-solver:local")
    parser.add_argument("--allocation-ledger", type=Path)
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--check", action="store_true")
    choice.add_argument("--reference", action="store_true")
    choice.add_argument("--baseline", action="store_true")
    args = parser.parse_args()
    plan, tasks, overlays, identity, allocation, ledger = prepare(
        args.plan, args.tasks_root, args.output, args.image, args.allocation_ledger
    )
    with ledger.allocation_owner():
        if args.check:
            status = "ready_for_controls"
        elif args.reference:
            controls(
                tasks, overlays, identity, ledger, args.output, plan["benchmark"]["negative_control_task_ids"]
            )
            status = "original_controls_passed"
        else:
            status = baseline(plan, tasks, overlays, identity, allocation, ledger, args.output)["status"]
    print(json.dumps({"status": status, "output": str(args.output), "starts": ledger.snapshot()["counts"]}))


if __name__ == "__main__":
    main()
