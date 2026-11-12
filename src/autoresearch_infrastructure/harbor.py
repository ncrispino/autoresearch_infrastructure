"""Convert a Codex session to ATIF and register it where Harbor's job viewer can show it."""

from __future__ import annotations

import fcntl
import hashlib
import os
import re
import shutil
import stat
import threading
from datetime import datetime
from pathlib import Path
from uuid import NAMESPACE_URL, uuid5

from harbor.agents.installed.codex import Codex
from harbor.models.job.config import JobConfig
from harbor.models.job.result import JobResult, JobStats
from harbor.models.task.id import LocalTaskId
from harbor.models.trajectories import Trajectory
from harbor.models.trial.config import AgentConfig, TaskConfig, TrialConfig, VerifierConfig
from harbor.models.trial.result import AgentInfo, ModelInfo, TrialResult
from harbor.utils.trajectory_utils import format_trajectory_json
from harbor.utils.trajectory_validator import TrajectoryValidator

from autoresearch_infrastructure.persistence import write_bytes, write_json

from .traces import MAX_ATIF_BYTES

_SAFE_LABEL = re.compile(r"[^A-Za-z0-9_.-]+")
_VIEWER_LOCK = threading.Lock()
MAX_NATIVE_FILES = 2_000


def _label(value: str) -> str:
    normalized = _SAFE_LABEL.sub("-", value).strip("-.")
    if not normalized:
        raise ValueError("ATIF observation requires a nonempty label")
    return normalized[:80]


def _validate_native_logs(native_logs: Path) -> None:
    """Reject model-controlled links, special files, and unbounded native logs."""
    sessions = native_logs / "sessions"
    for root in (native_logs, sessions):
        try:
            info = root.lstat()
        except FileNotFoundError as exc:
            raise RuntimeError("Codex did not persist a native sessions directory") from exc
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise RuntimeError("Codex native sessions path is not a safe directory")

    count = 0
    total = 0
    for current, directories, files in os.walk(native_logs, followlinks=False):
        current_path = Path(current)
        if len(current_path.relative_to(native_logs).parts) > 9:
            raise RuntimeError("Codex native session tree is too deep")
        for name in [*directories, *files]:
            item = current_path / name
            info = item.lstat()
            if stat.S_ISLNK(info.st_mode):
                raise RuntimeError("Codex native session tree contains a symlink")
            if name in directories:
                if not stat.S_ISDIR(info.st_mode):
                    raise RuntimeError("Codex native session tree contains a special path")
                continue
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise RuntimeError("Codex native session tree contains an unsafe file")
            count += 1
            total += info.st_size
            if count > MAX_NATIVE_FILES or total > MAX_ATIF_BYTES:
                raise RuntimeError("Codex native session tree exceeds its evidence limit")


def _model_info(model: str) -> ModelInfo:
    if "/" in model:
        provider, name = model.split("/", 1)
        return ModelInfo(provider=provider, name=name)
    return ModelInfo(name=model)


def _copy_images(source: Path, destination: Path) -> None:
    images = source / "images"
    if not images.is_dir() or images.is_symlink():
        return
    total = 0
    for item in images.rglob("*"):
        if not item.is_file() or item.is_symlink():
            continue
        total += item.stat().st_size
        if total > MAX_ATIF_BYTES:
            raise RuntimeError("Autoresearch ATIF media exceeds 50 MB")
        target = destination / "images" / item.relative_to(images)
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(item, target)


def _register_observation_unlocked(
    jobs_root: Path,
    role: str,
    scope: str,
    agent_name: str,
    viewer_agent_name: str,
    model: str,
    version: str,
    trajectory: bytes,
    native_assets: Path | None,
    started_at: datetime,
    finished_at: datetime,
) -> tuple[Path, str, str]:
    """Expose one controller-owned agent run in Harbor's jobs viewer."""
    role_label = _label(role)
    scope_label = _label(scope)
    sha256 = hashlib.sha256(trajectory).hexdigest()
    job_name = f"autoresearch-{role_label}"
    trial_name = f"{scope_label}-{sha256[:12]}"
    job_dir = jobs_root / job_name
    trial_dir = job_dir / trial_name
    agent_dir = trial_dir / "agent"
    write_bytes(agent_dir / "trajectory.json", trajectory)
    if native_assets is not None:
        _copy_images(native_assets, agent_dir)

    validator = TrajectoryValidator()
    if not validator.validate(agent_dir / "trajectory.json"):
        details = "; ".join(validator.get_errors()[:5])
        raise RuntimeError(f"Autoresearch agent produced invalid ATIF: {details}")

    task = TaskConfig(
        path=Path("/autoresearch") / role_label / scope_label,
        source=f"autoresearch-{role_label}",
    )
    job_id = uuid5(NAMESPACE_URL, job_dir.resolve().as_uri())
    trial_id = uuid5(NAMESPACE_URL, trial_dir.resolve().as_uri())
    agent = AgentConfig(name=viewer_agent_name, model_name=model)
    verifier = VerifierConfig(disable=True)
    trial_config = TrialConfig(
        task=task,
        trial_name=trial_name,
        trials_dir=job_dir,
        agent=agent,
        verifier=verifier,
        job_id=job_id,
    )
    result = TrialResult(
        id=trial_id,
        task_name=f"autoresearch-{role_label}",
        trial_name=trial_name,
        trial_uri=trial_dir.resolve().as_uri(),
        task_id=LocalTaskId(path=task.path),
        source=f"autoresearch-{role_label}",
        task_checksum=sha256,
        config=trial_config,
        agent_info=AgentInfo(
            name=agent_name,
            version=version,
            model_info=_model_info(model),
        ),
        started_at=started_at,
        finished_at=finished_at,
    )
    write_json(trial_dir / "config.json", trial_config)
    write_json(trial_dir / "result.json", result)
    trial_results = [
        TrialResult.model_validate_json(path.read_text(encoding="utf-8"))
        for path in sorted(job_dir.glob("*/result.json"))
        if path.is_file() and not path.is_symlink()
    ]
    tasks = {
        (str(item.config.task.path), item.config.task.source): item.config.task for item in trial_results
    }
    agents = {
        (item.config.agent.name, item.config.agent.model_name): item.config.agent for item in trial_results
    }
    write_json(
        job_dir / "config.json",
        JobConfig(
            job_name=job_name,
            jobs_dir=jobs_root,
            n_attempts=1,
            n_concurrent_trials=1,
            agents=list(agents.values()),
            tasks=list(tasks.values()),
            verifier=verifier,
        ),
    )
    write_json(
        job_dir / "result.json",
        JobResult(
            id=job_id,
            started_at=min(item.started_at for item in trial_results if item.started_at),
            updated_at=finished_at,
            finished_at=max(item.finished_at for item in trial_results if item.finished_at),
            n_total_trials=len(trial_results),
            stats=JobStats.from_trial_results(trial_results),
            trial_results=trial_results,
        ),
    )
    return agent_dir / "trajectory.json", job_name, trial_name


def _register_observation(
    jobs_root: Path,
    role: str,
    scope: str,
    agent_name: str,
    viewer_agent_name: str,
    model: str,
    version: str,
    trajectory: bytes,
    native_assets: Path | None,
    started_at: datetime,
    finished_at: datetime,
) -> tuple[Path, str, str]:
    job_dir = jobs_root / f"autoresearch-{_label(role)}"
    job_dir.mkdir(parents=True, exist_ok=True)
    with _VIEWER_LOCK, (job_dir / ".publication.lock").open("a+") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        return _register_observation_unlocked(
            jobs_root,
            role,
            scope,
            agent_name,
            viewer_agent_name,
            model,
            version,
            trajectory,
            native_assets,
            started_at,
            finished_at,
        )


def publish_atif(
    document: Trajectory,
    evidence_path: Path,
    jobs_root: Path,
    *,
    role: str,
    scope: str,
    started_at: datetime,
    finished_at: datetime,
    native_assets: Path | None = None,
    viewer_agent_name: str | None = None,
) -> dict[str, str | int]:
    """Publish one validated ATIF document as evidence and a Harbor trial."""
    data = format_trajectory_json(document.to_json_dict()).encode("utf-8")
    if len(data) > MAX_ATIF_BYTES:
        raise RuntimeError("Autoresearch ATIF trajectory exceeds 50 MB")
    write_bytes(evidence_path, data)
    if native_assets is not None:
        _copy_images(native_assets, evidence_path.parent)
    evidence_validator = TrajectoryValidator()
    if not evidence_validator.validate(evidence_path):
        details = "; ".join(evidence_validator.get_errors()[:5])
        raise RuntimeError(f"Autoresearch evidence ATIF is invalid: {details}")

    agent = document.agent
    viewer_path, job_name, trial_name = _register_observation(
        jobs_root,
        role,
        scope,
        agent.name,
        viewer_agent_name or agent.name,
        agent.model_name or "unknown",
        agent.version,
        data,
        native_assets,
        started_at,
        finished_at,
    )
    if evidence_path.read_bytes() != viewer_path.read_bytes():
        raise RuntimeError("ATIF evidence differs from the viewer trajectory")
    return {
        "format": "ATIF",
        "schema_version": document.schema_version,
        "steps": len(document.steps),
        "sha256": hashlib.sha256(data).hexdigest(),
        "viewer_job": job_name,
        "viewer_trial": trial_name,
    }


def publish_codex_atif(
    native_logs: Path,
    evidence_path: Path,
    jobs_root: Path,
    *,
    role: str,
    scope: str,
    model: str,
    version: str,
    started_at: datetime,
    finished_at: datetime,
) -> dict[str, str | int]:
    """Convert the actual Codex session and publish the same ATIF bytes everywhere."""
    _validate_native_logs(native_logs)
    converter = Codex(native_logs, model_name=model, version=version)
    document = converter.convert_trajectory(native_logs)
    if document is None:
        raise RuntimeError("Codex did not persist a session trajectory")
    raw = document.to_json_dict()
    if raw.get("agent", {}).get("name") != "codex":
        raise RuntimeError("Codex conversion produced an unexpected agent identity")
    return publish_atif(
        document,
        evidence_path,
        jobs_root,
        role=role,
        scope=scope,
        started_at=started_at,
        finished_at=finished_at,
        native_assets=native_logs,
        viewer_agent_name=f"autoresearch-{_label(role)}",
    )
