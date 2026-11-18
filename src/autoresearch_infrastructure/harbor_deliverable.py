"""Grade a deliverable with a Harbor task's own grader, with no solver agent.

A deliverable task is a Harbor task of one shape: the agent writes files into
one directory, and the task's grader scores that directory. Here a candidate is
that directory. Each evaluation starts a fresh grader container, mounts a saved
candidate read-only at the deliverable path, runs one named script and reads the
reward file the script writes, as Harbor's verifier does.

This uses Harbor's task format, not Harbor's run. In a Harbor run the test
script executes once, after the agent stops. Here a grader script executes after
every submission, in its own container, and the controller stands between the
researcher and the grader.

Two scripts keep a held-out score held out:

- the ``feedback`` script's rewards become the candidate's metrics, which the
  researcher sees and selection ranks;
- the ``sealed`` script's rewards are written only to the evaluation's private
  evidence. No researcher input and no selection step reads them.

Only Docker is needed to grade. The ``harbor`` package is needed only to derive
the researcher's environment.
"""

from __future__ import annotations

import json
import math
import re
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path, PurePosixPath
from uuid import uuid4

from .api import Evaluation, Metric, MetricSchema
from .persistence import digest, write_json
from .source_tree import task_context_digest

REWARD = Metric("reward")
SEALED_RECORD = "sealed.json"

_PINNED = re.compile(r"(?:[^@\s]+@)?sha256:[0-9a-f]{64}")
_TESTS = PurePosixPath("/tests")
_LOGS = PurePosixPath("/logs/verifier")
_ARTIFACTS = PurePosixPath("/logs/artifacts")
CASE_VARIABLE = "ARI_CASE"
_SYSTEM = {"proc", "sys", "dev", "etc", "tests", "logs", "tmp"}
_LOG_LIMIT = 1_000_000
# Harbor test scripts are bash; a minimal grader image may have only sh.
_RUN_SCRIPT = 'if command -v bash >/dev/null 2>&1; then exec bash "$0"; else exec sh "$0"; fi'


def _container_path(value, label: str) -> PurePosixPath:
    path = PurePosixPath(value) if isinstance(value, str) else None
    if path is None or not path.is_absolute() or ".." in path.parts or path.as_posix() != value:
        raise ValueError(f"{label} is an absolute path inside the container, such as /app/output")
    return path


def _inside(task: Path, name, label: str) -> Path:
    """A path inside the Harbor task directory, named relative to it."""
    relative = Path(name)
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        raise ValueError(f"{label} is a path inside the Harbor task directory: {name}")
    return task / relative


class HarborGraderEvaluator:
    """Run a Harbor task's grader script on one saved candidate.

    ``task``
        The Harbor task directory.
    ``deliverable``
        The container path the task's grader reads the deliverable from, such
        as ``/app/output``. The candidate is mounted there read-only.
    ``feedback``
        The container path of the script whose rewards the researcher sees. The
        task's ``tests/`` directory is mounted at ``/tests``, so the usual value
        is a file there, such as ``/tests/feedback.sh``.
    ``sealed``
        The container path of the script whose rewards stay private, or None
        for a task with no held-out score.
    ``grader``
        Where the scripts run. None is the task's main image: its pinned
        ``docker_image``, or ``environment/Dockerfile`` built once. A directory
        inside the task that holds a Dockerfile, such as
        ``environment/grader``, is built once instead. An image pinned by
        digest is used as it is.
    ``state``
        A directory in the run for the record of the grader image.
    ``rewards``
        A mapping from each metric to the reward key that supplies it. Without
        it, the feedback script must write exactly one reward, reported as
        ``REWARD``.
    ``capabilities``
        Linux capabilities the grader container keeps, such as ``SYS_ADMIN``
        for a grader that sandboxes the deliverable itself. The default is none.
    ``network``
        True gives the grader container the default Docker network. The default
        is no network.
    ``feedback_log``
        True publishes the feedback script's output as ``grader-log.txt`` in the
        evaluation's public artifacts, where the researcher can read it. Leave
        it off unless that output is known to reveal no hidden data.
    ``feedback_artifacts``
        True publishes the files the feedback script writes under
        ``/logs/artifacts``, such as a plot, in the same public artifacts. Off,
        they are kept with the private evidence. The sealed script's files are
        always private.

    Each script is told which case it is grading in the environment variable
    ``ARI_CASE``. A task with one case can ignore it.

    Every evaluation gets a new grader container, which is deleted with
    everything it wrote when the script ends. It has no capabilities beyond
    those named, and the Docker socket and the run directory are never mounted.
    The deliverable is the only thing it receives from the researcher, and it
    cannot change it. A grader that executes the deliverable does so inside
    this container, beside its own hidden data, so the rewards and an opted-in
    log are the only ways that data can reach the researcher.
    """

    def __init__(self, *, task, deliverable: str, feedback: str, sealed: str | None = None,
                 grader: str | None = None, state: Path, rewards: dict[Metric, str] | None = None,
                 capabilities=(), network: bool = False, feedback_log: bool = False,
                 feedback_artifacts: bool = False):
        self.task = Path(task).resolve()
        manifest = self.task / "task.toml"
        if not manifest.is_file():
            raise ValueError(f"No Harbor task.toml at {manifest}")
        config = tomllib.loads(manifest.read_text(encoding="utf-8"))
        self.environment = config.get("environment") or {}
        verifier = config.get("verifier") or {}
        if self.environment.get("os", "linux") != "linux":
            raise ValueError("A grader runs in a Linux container")
        self.deliverable = _container_path(deliverable, "deliverable")
        if len(self.deliverable.parts) < 2 or self.deliverable.parts[1] in _SYSTEM:
            raise ValueError(f"The deliverable cannot be mounted at {deliverable}")
        self.scripts = {"feedback": self._script(feedback, "feedback")}
        if sealed is not None:
            self.scripts["sealed"] = self._script(sealed, "sealed")
            if sealed == feedback:
                raise ValueError("The sealed script must differ from the feedback script, or the "
                                 "researcher would be shown the sealed score")
        self.grader, self.context = self._grader(grader)
        self.timeout = verifier.get("timeout_sec", 600)
        self.env = {**verifier.get("env", {})}
        if any(not isinstance(value, str) or "${" in value for value in self.env.values()):
            raise ValueError("[verifier.env] values must be literal text: a grader gets no host variables")
        if type(self.timeout) not in (int, float) or not 0 < self.timeout <= 86400:
            raise ValueError("[verifier] timeout_sec must be positive and at most 86400 seconds")
        self.capabilities = tuple(capabilities)
        if any(not isinstance(name, str) or not re.fullmatch(r"[A-Z_]+", name) for name in self.capabilities):
            raise ValueError("capabilities are Linux capability names, such as SYS_ADMIN")
        if any(type(flag) is not bool for flag in (network, feedback_log, feedback_artifacts)):
            raise ValueError("network, feedback_log and feedback_artifacts are True or False")
        self.network, self.feedback_log, self.feedback_artifacts = network, feedback_log, feedback_artifacts
        self.rewards = None if rewards is None else dict(rewards)
        if self.rewards is not None:
            MetricSchema(tuple(self.rewards))
            if any(not isinstance(key, str) or not key for key in self.rewards.values()):
                raise ValueError("rewards maps each Metric to a reward key")
        self.record = Path(state) / "grader.json"
        self.definition = digest({
            "task.toml": manifest.read_text(encoding="utf-8"),
            "tests": task_context_digest(self.task / "tests") if (self.task / "tests").is_dir() else None,
            "grader": self.grader,
            "context": task_context_digest(self.context) if self.context else None,
        })

    def _script(self, value, label: str) -> PurePosixPath:
        path = _container_path(value, label)
        if _TESTS in path.parents and not (self.task / "tests" / path.relative_to(_TESTS)).is_file():
            raise ValueError(f"The {label} script {value} is not in the Harbor task's tests/ directory")
        return path

    def _grader(self, grader):
        """The pinned image to use, or the build context to build once."""
        if grader is None:
            image = self.environment.get("docker_image")
            if image is not None:
                if not _PINNED.fullmatch(str(image)):
                    raise ValueError("[environment] docker_image must be pinned by digest to grade with it")
                return image, None
            grader = "environment"
        elif _PINNED.fullmatch(grader):
            return grader, None
        context = _inside(self.task, grader, "grader")
        if not (context / "Dockerfile").is_file():
            raise ValueError(f"The grader needs {context}/Dockerfile, or an image pinned by digest")
        return None, context

    @property
    def identity(self):
        return {
            "definition": self.definition, "deliverable": self.deliverable.as_posix(),
            "scripts": {name: path.as_posix() for name, path in self.scripts.items()},
            "rewards": None if self.rewards is None else {m.name: key for m, key in self.rewards.items()},
            "capabilities": self.capabilities, "network": self.network, "feedback_log": self.feedback_log,
            "feedback_artifacts": self.feedback_artifacts,
        }

    @property
    def metrics(self) -> tuple[Metric, ...]:
        return (REWARD,) if self.rewards is None else tuple(self.rewards)

    def image(self) -> str:
        """The grader image, built on first use and recorded so that every evaluation uses the same one."""
        if self.record.is_file():
            saved = json.loads(self.record.read_text())
            if saved.get("definition") != self.definition:
                raise ValueError(f"{self.record} records a different grader; use a fresh output")
            if not _image_present(saved["image"]):
                raise ValueError(f"The grader image {saved['image']} is no longer available; "
                                 "load it again (docker load) or use a fresh output")
            return saved["image"]
        if self.context is None:
            image, built = self.grader, False
            if not _image_present(image):
                _docker(["pull", "--quiet", image])
        else:
            with tempfile.TemporaryDirectory(prefix="ari-grader-build-") as scratch:
                iid = Path(scratch) / "image-id"
                _docker(["build", "--quiet", "--iidfile", str(iid), str(self.context)])
                image, built = iid.read_text().strip(), True
            if not _PINNED.fullmatch(image):
                raise RuntimeError(f"docker build reported no image ID: {image!r}")
        write_json(self.record, {"definition": self.definition, "image": image, "built": built})
        return image

    def grade(self, candidate: Path, script: str, evidence: Path, case: str = "default") -> dict:
        """Run one script on ``candidate`` for ``case`` and return its rewards.

        The script's output, reward file and artifacts are saved under ``evidence``.
        """
        image = self.image()
        name = "ari-grader-" + uuid4().hex[:12]
        command = [
            "create", "--name", name, "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--pids-limit", "512", "--init",
            "--cpus", str(self.environment.get("cpus", 1)),
            "--memory", f"{self.environment.get('memory_mb', 4096)}m",
            "--volume", _LOGS.as_posix(), "--volume", _ARTIFACTS.as_posix(),  # anonymous; removed with the container
            "--env", f"{CASE_VARIABLE}={case}",
            "--mount", f"type=bind,source={Path(candidate).resolve()},target={self.deliverable},readonly",
        ]
        if not self.network:
            command += ["--network", "none"]
        for capability in self.capabilities:
            command += ["--cap-add", capability]
        if (self.task / "tests").is_dir():
            command += ["--mount", f"type=bind,source={self.task / 'tests'},target={_TESTS},readonly"]
        for key, value in self.env.items():
            command += ["--env", f"{key}={value}"]
        _docker(command + [image, "sh", "-c", _RUN_SCRIPT, self.scripts[script].as_posix()])
        evidence.mkdir(parents=True, exist_ok=True)
        try:
            try:
                result = subprocess.run(["docker", "start", "--attach", name], capture_output=True,
                                        timeout=self.timeout)
            except subprocess.TimeoutExpired as exc:
                raise RuntimeError(f"The {script} grader script ran past {self.timeout} seconds") from exc
            output = (result.stdout + result.stderr)[-_LOG_LIMIT:]
            (evidence / "output.txt").write_bytes(output)
            logs = evidence / "verifier"
            for source, target in ((_LOGS, logs), (_ARTIFACTS, evidence / "artifacts")):
                subprocess.run(["docker", "cp", f"{name}:{source}/.", str(target)], capture_output=True)
                _regular_files_only(target)
            rewards = _rewards(logs)
            if rewards is None:
                raise RuntimeError(f"The {script} grader script exited {result.returncode} and wrote no "
                                   f"reward.txt or reward.json under {_LOGS}")
            return rewards
        finally:
            subprocess.run(["docker", "rm", "--force", "--volumes", name], capture_output=True)

    def __call__(self, candidate, context):
        private = context.private_artifacts
        if private is None:
            raise ValueError("Grading needs a private evidence directory for the grader's logs")
        rewards = self.grade(candidate, "feedback", private / "feedback", context.task)
        if self.rewards is None:
            if len(rewards) != 1:
                raise ValueError(f"The feedback script wrote the rewards {sorted(rewards)}; "
                                 "pass objective and rewards to name the ones to use")
            metrics = {REWARD: float(next(iter(rewards.values())))}
        else:
            if set(self.rewards.values()) - rewards.keys():
                raise ValueError("The feedback script did not write every mapped reward")
            metrics = {metric: float(rewards[key]) for metric, key in self.rewards.items()}
        if self.feedback_log:
            shutil.copyfile(private / "feedback/output.txt", context.artifacts / "grader-log.txt")
        produced = private / "feedback/artifacts"
        if self.feedback_artifacts and produced.is_dir():
            shutil.copytree(produced, context.artifacts, dirs_exist_ok=True)
            shutil.rmtree(produced)
        if "sealed" in self.scripts:
            # A failure here is recorded privately and never changes what the researcher sees.
            try:
                sealed = {"rewards": self.grade(candidate, "sealed", private / "sealed", context.task)}
            except (RuntimeError, ValueError, OSError) as error:
                sealed = {"error": f"{type(error).__name__}: {error}"}
            write_json(private / SEALED_RECORD, {"candidate_hash": context.candidate_hash, "case": context.task,
                                                 "repeat": context.repeat, **sealed})
        return Evaluation(metrics, metadata={"feedback_rewards": rewards, "grader_image": self.image()})


def _rewards(logs: Path) -> dict | None:
    """Read the reward file as Harbor's verifier does: ``reward.txt`` is one number, ``reward.json`` a mapping."""
    text, mapping = logs / "reward.txt", logs / "reward.json"
    if text.is_file():
        rewards = {"reward": float(text.read_text())}
    elif mapping.is_file():
        rewards = json.loads(mapping.read_text())
    else:
        return None
    if not isinstance(rewards, dict) or not rewards or any(
        type(value) not in (int, float) or not math.isfinite(value) for value in rewards.values()
    ):
        raise ValueError("A grader must write finite numeric rewards")
    return rewards


def _regular_files_only(root: Path) -> None:
    """Drop links and special files a grader left, so nothing copied out can point back into the host."""
    for path in sorted(root.rglob("*"), reverse=True) if root.is_dir() else ():
        if path.is_symlink() or not (path.is_file() or path.is_dir()):
            path.unlink()


def _docker(args: list[str]) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed: {result.stderr.strip()[-2000:]}")
    return result.stdout


def _image_present(image: str) -> bool:
    return subprocess.run(["docker", "image", "inspect", image], capture_output=True).returncode == 0


def researcher_environment(task: Path, destination: Path, *, private=()) -> Path:
    """Write a Harbor environment for the researcher: the task's main container and nothing of its grader.

    The copy holds ``task.toml`` with only the ``[environment]`` table and the
    task's ``environment/`` directory. Left out are the task's
    ``docker-compose.yaml``, which starts the benchmark's own grader services
    and submission tool, the task's MCP servers, and every path in ``private``.
    The image is built from this copy, so a Dockerfile that copies a private
    path fails to build instead of handing the grader to the researcher.
    ``tests/`` and ``solution/`` lie outside ``environment/`` and are never
    copied.

    Calling it again checks that the copy is unchanged.
    """
    from harbor.models.task.config import TaskConfig

    task = Path(task)
    table = dict(tomllib.loads((task / "task.toml").read_text(encoding="utf-8")).get("environment") or {})
    for name in ("workdir", "skills_dir", "mcp_servers"):  # ARI sets the first two and replaces the submit tool
        table.pop(name, None)
    source = task / "environment"
    excluded = {(task / "environment/docker-compose.yaml").resolve()}
    for name in private:
        path = _inside(task, name, "private").resolve()
        if not path.exists():
            raise ValueError(f"private names {name}, which is not in the Harbor task")
        excluded.add(path)

    def ignore(directory, names):
        return [name for name in names if (Path(directory) / name).resolve() in excluded]

    with tempfile.TemporaryDirectory(prefix="ari-researcher-environment-") as scratch:
        staged = Path(scratch) / "task"
        staged.mkdir()
        (staged / "task.toml").write_text(TaskConfig(environment=table).model_dump_toml(), encoding="utf-8")
        if source.is_dir():
            shutil.copytree(source, staged / "environment", ignore=ignore)
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(staged, destination)
        elif task_context_digest(destination) != task_context_digest(staged):
            raise ValueError("The Harbor task's environment changed; use a fresh output")
    return destination


def sealed_rewards(output: Path) -> list[dict]:
    """The sealed rewards a run recorded, one entry for each evaluation, for reading after the run.

    ``output`` is a run directory. Each entry names the candidate, case and
    repeat, and holds either ``rewards`` or the ``error`` that prevented them.
    Candidates that were judged invalid were never graded and have no entry.
    """
    output = Path(output)
    root = output / "research" if (output / "research").is_dir() else output
    entries = []
    for path in sorted(root.glob("sessions/*/private/**/" + SEALED_RECORD)):
        candidate = next((part for part in path.parts if part.startswith("candidate-")), None)
        entries.append({"candidate_id": candidate, **json.loads(path.read_text())})
    return entries
