"""Evaluate edited Harbor task files while the agent that solves the task stays fixed."""

from __future__ import annotations

import re
import shutil
from pathlib import Path
from uuid import uuid4

from .harbor_filesystem import immutable_image
from .harbor_trial import OFFLINE_ENVIRONMENT, reward_mapping, run_trial
from .ledger import StartLedger
from .persistence import write_json
from .source_tree import evidence_digest, materialize, snapshot


def task_with_image(source: Path, image: str, destination: Path) -> Path:
    """Copy a Harbor task to ``destination`` with its container image pinned to ``image``.

    The copy is the trusted task for one run. Reusing a destination whose files
    no longer match is refused.
    """
    from harbor.models.task.config import TaskConfig

    payload = snapshot(Path(source))
    config = TaskConfig.model_validate_toml(payload["task.toml"])
    config.environment.docker_image = immutable_image(image, "A pinned Harbor task")
    payload["task.toml"] = config.model_dump_toml()
    if not destination.exists():
        materialize(destination, payload)
    elif snapshot(destination) != payload:
        raise ValueError("Prepared task changed; use fresh output or restore the trusted files")
    return destination


class HarborTaskEvaluator:
    """Run a fixed Harbor agent on candidate task files.

    A candidate is laid out as ``TASK_ID/path/inside/task``. ``editable`` names
    the exact files a candidate may contain for each task; they replace the same
    files in a copy of the baseline task. Without ``editable`` the candidate is
    the whole task source, including its tests, so the validity check must
    establish that the task and its grader still mean the same thing.

    ``task.toml`` and Compose configuration can never change. Each evaluation
    costs one ``solver_trial`` start from ``ledger``.
    """

    def __init__(self, *, tasks, agent, ledger: StartLedger, editable=None, metrics=None):
        from harbor.agents.factory import AgentFactory
        from harbor.models.task.config import TaskConfig
        from harbor.models.trial.config import AgentConfig

        if not tasks or any(not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", k) for k in tasks):
            raise ValueError("Declare a nonempty mapping of safe task IDs to baseline tasks")
        if not isinstance(ledger, StartLedger) or "solver_trial" not in ledger.limits:
            raise ValueError("A StartLedger with a solver_trial ceiling is required")
        self.agent = AgentConfig.model_validate(agent).model_copy(deep=True)
        AgentFactory.get_agent_class_from_config(self.agent)
        if self.agent.override_timeout_sec is None:
            self.agent.override_timeout_sec = 120
        if not 0 < self.agent.override_timeout_sec <= 7200:
            raise ValueError("The fixed agent timeout must be positive and at most 7200 seconds")
        self.tasks = {k: Path(v).resolve() for k, v in tasks.items()}
        self.task_hashes = {k: evidence_digest(p, max_total_bytes=100_000_000)
                            for k, p in self.tasks.items()}
        self.configs = {k: TaskConfig.model_validate_toml((p / "task.toml").read_text())
                        for k, p in self.tasks.items()}
        if any((p / "environment/docker-compose.yaml").exists() for p in self.tasks.values()):
            raise ValueError("Only single-container Harbor tasks are supported, without task Compose files")
        for config in self.configs.values():
            workdir = config.environment.workdir
            if (config.steps or config.environment.mcp_servers or config.environment.os != "linux"
                    or not workdir or not workdir.startswith("/") or len(Path(workdir).parts) < 2
                    or ".." in Path(workdir).parts or Path(workdir).parts[1] in {"proc", "sys", "dev", "etc"}):
                raise ValueError("Use single-step Linux tasks without task MCP servers and an explicit workdir")
        self.editable = None if editable is None else {k: tuple(v) for k, v in editable.items()}
        if self.editable is not None:
            if set(self.editable) != set(self.tasks):
                raise ValueError("Declare editable files for every baseline task")
            for task, names in self.editable.items():
                if not names or len(set(names)) != len(names):
                    raise ValueError("Each task needs unique editable file paths")
                for name in names:
                    path = Path(name)
                    if (path.is_absolute() or ".." in path.parts or not path.parts or path.as_posix() != name
                            or self._protected(name) or not (self.tasks[task] / path).is_file()):
                        raise ValueError("Editable files must be existing task files outside runtime configuration")
        self.metrics = reward_mapping(metrics)
        self.ledger = ledger

    def _protected(self, name):
        return (name == "task.toml" or name == "environment/docker-compose.yaml"
                or (self.agent.name == "oracle" and name.startswith("solution/"))
                or Path(name).name == ".env")

    @property
    def identity(self):
        import importlib.metadata

        return {
            "harbor": importlib.metadata.version("harbor"), "task_hashes": self.task_hashes,
            "agent": self.agent.model_dump(mode="json"), "editable": self.editable,
            "metrics": None if self.metrics is None else {m.name: key for m, key in self.metrics.items()},
            "allocation": self.ledger.identity_hash, "limits": self.ledger.limits,
        }

    def seed(self, destination: Path) -> Path:
        """Write the starting files for a candidate and return their directory.

        Calling it again with the same destination checks that the files are unchanged.
        """
        destination = Path(destination)
        payload = {}
        for task, root in self.tasks.items():
            if evidence_digest(root, max_total_bytes=100_000_000) != self.task_hashes[task]:
                raise ValueError("Baseline task bytes changed")
            files = snapshot(root) if self.editable is None else {
                name: (root / name).read_text(encoding="utf-8") for name in self.editable[task]
            }
            payload.update({task + "/" + name: value for name, value in files.items()})
        if not destination.exists():
            materialize(destination, payload)
        elif snapshot(destination) != payload:
            raise ValueError("Starting files changed; use a fresh output")
        return destination

    def reserve_evaluations(self, count):
        self.ledger.require_capacity({"solver_trial": count})

    def prepare_task(self, candidate: Path, task: str, destination: Path):
        """Build the Harbor task for one evaluation from the saved candidate files.

        Files are only copied. No candidate code runs on the host.
        """
        if task not in self.tasks:
            raise ValueError("Task is outside the frozen panel")
        if any(evidence_digest(p, max_total_bytes=100_000_000) != self.task_hashes[k]
               for k, p in self.tasks.items()):
            raise ValueError("Baseline task bytes changed")
        payload = snapshot(Path(candidate))
        if any(name.split("/", 1)[0] not in self.tasks for name in payload):
            raise ValueError("Candidate contains an unknown task")
        for key, root in self.tasks.items():
            files = {n[len(key) + 1:]: v for n, v in payload.items() if n.startswith(key + "/")}
            if self.editable is not None and set(files) != set(self.editable[key]):
                raise ValueError("Candidate must contain exactly the declared editable files")
            if self.editable is None:
                protected = {"task.toml", "environment/docker-compose.yaml"}
                protected.update(n for n in snapshot(root) if self._protected(n))
                protected.update(n for n in files if self._protected(n))
                for name in protected:
                    baseline = root / name
                    expected = baseline.read_text() if baseline.exists() else None
                    if files.get(name) != expected:
                        raise ValueError("Candidate changed protected runtime configuration: " + name)
                if not files.get("instruction.md"):
                    raise ValueError("A task needs a nonempty instruction.md")
        files = {n[len(task) + 1:]: v for n, v in payload.items() if n.startswith(task + "/")}
        if self.editable is None:
            materialize(destination, files)
        else:
            shutil.copytree(self.tasks[task], destination)
            for name, value in files.items():
                (destination / name).write_text(value, encoding="utf-8")
        # Snapshots hold text, not file modes, and Harbor runs these scripts directly.
        for name in ("tests/test.sh", "solution/solve.sh"):
            entrypoint = destination / name
            if entrypoint.is_file():
                entrypoint.chmod(0o755)
        return destination

    def __call__(self, candidate, context):
        from harbor.models.trial.config import EnvironmentConfig, TaskConfig, TrialConfig

        context.artifacts.mkdir(parents=True, exist_ok=True)
        task = self.prepare_task(candidate, context.task, context.artifacts / "task")
        environment = self.configs[context.task].environment
        baseline = self.tasks[context.task]
        environment_changed = evidence_digest(task / "environment", max_total_bytes=100_000_000) != (
            evidence_digest(baseline / "environment", max_total_bytes=100_000_000)
        )
        if environment_changed and not (task / "environment/Dockerfile").is_file():
            raise ValueError("Environment changes need a Dockerfile to rebuild the task image")
        # The root filesystem is read-only. Named volumes copy the image's working
        # directory and home so the trial can write there and nowhere on the host.
        service = {
            "read_only": True, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
            "privileged": False, "pids_limit": 512, "init": True,
            "tmpfs": ["/tmp:rw,exec,nosuid,size=1g,mode=1777", "/installed-agent:rw,exec,nosuid,size=128m,mode=1777",
                      "/tests:rw,exec,nosuid,size=128m,mode=1777", "/solution:rw,exec,nosuid,size=128m,mode=1777"],
            "volumes": [f"ari-work:{environment.workdir}", "ari-home:/root"],
        }
        if environment_changed or environment.docker_image is None:
            service["build"] = {"network": "none"}
        overlay = context.artifacts / "isolation.json"
        write_json(overlay, {"services": {"main": service}, "volumes": {"ari-work": {}, "ari-home": {}}})
        offline = environment.resolve_baseline().network_mode == "no-network"
        config = TrialConfig(
            task=TaskConfig(path=task),
            trial_name="evaluation-" + uuid4().hex[:12],
            trials_dir=context.artifacts / "harbor-jobs",
            agent=self.agent.model_copy(deep=True),
            environment=EnvironmentConfig(
                # Changed environment files force a rebuild, so a cached image
                # cannot silently ignore the proposal.
                type="docker", force_build=environment_changed, extra_docker_compose=[overlay],
                import_path=OFFLINE_ENVIRONMENT if offline else None,
            ),
        )
        write_json(context.artifacts / "task-source.json", {
            "baseline_hash": self.task_hashes[context.task], "candidate_hash": context.candidate_hash,
            "materialized_hash": evidence_digest(task, max_total_bytes=100_000_000),
        })
        return run_trial(
            config, context.artifacts, self.ledger, mapping=self.metrics,
            metadata={"baseline_hash": self.task_hashes[context.task], "fidelity": context.condition},
        )
