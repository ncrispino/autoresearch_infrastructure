"""Run one Harbor trial as an evaluation: charge it, save its evidence, read its rewards."""

from __future__ import annotations

import asyncio
import math
import threading
import time
from datetime import datetime
from pathlib import Path

from .api import Evaluation, Metric, MetricSchema
from .persistence import write_json

SUCCESS_RATE = Metric("success_rate")
SOLVER_TOKENS = Metric("solver_tokens", "min")
OFFLINE_ENVIRONMENT = "autoresearch_infrastructure.harbor_offline:OfflineDockerEnvironment"

# Harbor's Docker backend keeps process-wide asyncio build locks. Each evaluation
# runs its own event loop, so trials in one process take turns.
_TRIAL_LOCK = threading.Lock()


def reward_mapping(metrics: dict[Metric, str] | None) -> dict[Metric, str] | None:
    """Check an optional mapping from metrics to the Harbor reward keys that supply them."""
    if metrics is None:
        return None
    metrics = dict(metrics)
    MetricSchema(tuple(metrics))
    if any(not isinstance(key, str) or not key for key in metrics.values()):
        raise ValueError("Metric mappings must name Harbor reward keys")
    return metrics


def reward_metrics(rewards, mapping: dict[Metric, str] | None) -> dict[Metric, float]:
    """Turn Harbor's rewards into metrics. Without a mapping, one reward in [0, 1] is the success rate."""
    if not isinstance(rewards, dict) or not rewards or any(
        type(value) not in (int, float) or not math.isfinite(value) for value in rewards.values()
    ):
        raise ValueError("Harbor must return finite numeric rewards")
    if mapping is None:
        if len(rewards) != 1 or not 0 <= next(iter(rewards.values())) <= 1:
            raise ValueError("Supply an explicit Metric-to-reward mapping for these rewards")
        return {SUCCESS_RATE: float(next(iter(rewards.values())))}
    if set(mapping.values()) - rewards.keys():
        raise ValueError("Harbor did not return every mapped reward")
    return {metric: rewards[name] for metric, name in mapping.items()}


def trial_timings(result) -> dict:
    """Seconds spent in each Harbor phase; a phase without timestamps is None."""
    raw = result.model_dump(mode="json") if hasattr(result, "model_dump") else result

    def seconds(window):
        if not window or not window.get("started_at") or not window.get("finished_at"):
            return None
        value = (
            datetime.fromisoformat(window["finished_at"]) - datetime.fromisoformat(window["started_at"])
        ).total_seconds()
        if value < 0:
            raise ValueError("Harbor phase finished before it started")
        return value

    return {
        "trial_seconds": seconds(raw),
        **{
            key + "_seconds": seconds(raw.get(key))
            for key in ("environment_setup", "agent_setup", "agent_execution", "verifier")
        },
    }


def run_trial(config, artifacts: Path, ledger, *, mapping=None, extra_metrics=None, metadata=None) -> Evaluation:
    """Run the trial in ``config`` and return its measurement.

    One ``solver_trial`` start is charged before the trial begins and is kept
    whatever happens. A trial that Harbor could not finish raises, which leaves
    the evaluation incomplete; a finished trial with a low reward is a
    measurement. ``extra_metrics(result)`` may add metrics read from the result.
    """
    from harbor.trial.trial import Trial

    write_json(artifacts / "trial-config.json", config)
    key = ledger.start("solver_trial", str(artifacts))

    async def execute():
        trial = await Trial.create(config)
        return await trial.run()

    started = time.monotonic()
    status, cost, timing = "failed", None, None
    try:
        with _TRIAL_LOCK:
            result = asyncio.run(execute())
        cost = result.agent_result.cost_usd if result.agent_result else None
        timing = {"end_to_end_seconds": time.monotonic() - started, **trial_timings(result)}
        write_json(artifacts / "harbor-result.json", result)
        if result.exception_info is not None:
            raise RuntimeError("Harbor trial failed: " + result.exception_info.exception_type)
        rewards = result.verifier_result.rewards if result.verifier_result else None
        metrics = reward_metrics(rewards, mapping)
        if extra_metrics is not None:
            metrics.update(extra_metrics(result))
        trajectory = Path(config.trials_dir) / config.trial_name / "agent/trajectory.json"
        status = "completed"
        return Evaluation(
            metrics,
            completed=True,
            trajectory=trajectory if trajectory.is_file() else None,
            metadata={"timing": timing, "benchmark_rewards": rewards, "cost_usd": cost, **(metadata or {})},
        )
    finally:
        write_json(artifacts / "timing.json", timing or {"end_to_end_seconds": time.monotonic() - started})
        ledger.finish(key, status=status, cost_usd=cost)
