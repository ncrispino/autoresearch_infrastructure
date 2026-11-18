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

import fnmatch
import hashlib
import json
import math
import os
import re
import shutil
import subprocess
import tempfile
import tomllib
from pathlib import Path, PurePosixPath
from uuid import uuid4

from .api import Evaluation, Metric, MetricSchema
from .persistence import digest, write_json
from .source_tree import IGNORED_DIRS, IGNORED_FILES, snapshot, task_context_digest

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
        The container path of the script whose rewards the researcher sees.
        When the grader runs in the task's main image, the task's ``tests/``
        directory is mounted at ``/tests``, so the usual value is a file there,
        such as ``/tests/feedback.sh``. A separate grader image is used as it
        is, with nothing mounted at ``/tests``: the scripts are paths in that
        image, and ``prepare`` checks that they exist.
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

    ``seed``
        The starting deliverable to compare with what the task's main image
        ships at ``deliverable``, or None to skip the comparison. ``prepare``
        compares them once and refuses a run whose seed differs.
    ``grader_image``
        An image pinned by digest that was built from the ``grader``
        directory, to use instead of building it. ``grader`` must then name
        that directory, so that it is still known to be the grader's.
    ``task_image``
        An image pinned by digest to read the shipped deliverable from, instead
        of the task's main image: the task's image with what an agent needs
        added, as the run settings name it under ``[task] image``.

    The grader container gets the CPUs and memory of the task's
    ``[environment]``. CPUs are capped at the host's count, and each
    evaluation records the number used.

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
                 feedback_artifacts: bool = False, seed: Path | None = None,
                 task_image: str | None = None, grader_image: str | None = None):
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
        self.separate_grader = grader is not None
        self.mount_tests = not self.separate_grader and (self.task / "tests").is_dir()
        self.scripts = {"feedback": self._script(feedback, "feedback")}
        if sealed is not None:
            self.scripts["sealed"] = self._script(sealed, "sealed")
            if sealed == feedback:
                raise ValueError("The sealed script must differ from the feedback script, or the "
                                 "researcher would be shown the sealed score")
        self.grader, self.context = self._grader(grader)
        if grader_image is not None and (self.context is None or not self.separate_grader):
            raise ValueError("grader_image replaces building the grader's own directory: name that directory as "
                             "grader, such as grader=\"environment/grader\"")
        if grader_image is not None and not _PINNED.fullmatch(str(grader_image)):
            raise ValueError("grader_image must be an image ID or digest (sha256:...)")
        self.prebuilt = grader_image
        self.seed = None if seed is None else Path(seed).resolve()
        if task_image is not None and not _PINNED.fullmatch(str(task_image)):
            raise ValueError("task_image must be an image ID or digest (sha256:...)")
        self.task_image = task_image
        requested = self.environment.get("cpus", 1)
        self.cpus = min(float(requested), float(os.cpu_count() or 1))
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
            "prebuilt": self.prebuilt,
        })

    def _script(self, value, label: str) -> PurePosixPath:
        path = _container_path(value, label)
        if self.mount_tests and _TESTS in path.parents and not (
                self.task / "tests" / path.relative_to(_TESTS)).is_file():
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
        image, built = _resolve(self.prebuilt, None) if self.prebuilt else _resolve(self.grader, self.context)
        write_json(self.record, {"definition": self.definition, "image": image, "built": built})
        return image

    def prepare(self) -> None:
        """Build the grader image, and check once that the seed is what the task's image ships.

        Under ``harbor run`` an agent starts from whatever the task's image has
        at the deliverable path. Here the researcher and the grader see only
        the seed, which hides those files, so a seed that differs would make
        this a different task. Called before the baseline, and again before
        each evaluation, where it costs nothing once the check is recorded.
        """
        image = self.image()
        saved = json.loads(self.record.read_text())
        if self.separate_grader and not saved.get("scripts_checked"):
            missing = [path.as_posix() for path in self.scripts.values() if not _has_file(image, path)]
            if missing:
                raise ValueError(f"The grader image has no {', '.join(missing)}. A separate grader image is used "
                                 "as it is, so name the scripts by their paths inside it")
            saved = {**saved, "scripts_checked": True}
            write_json(self.record, saved)
        if self.seed is None:
            return
        expected = _file_digests(self.seed)
        if saved.get("seed") == digest(expected):
            return
        if self.task_image is not None:
            main = self.task_image
        elif self.separate_grader:
            main = _resolve(*_main_image(self.task, self.environment))[0]
        else:
            main = image
        with tempfile.TemporaryDirectory(prefix="ari-shipped-") as scratch:
            found = shipped(main, self.deliverable, Path(scratch) / "shipped")
            differences = _differences(_file_digests(found) if found else {}, expected)
        if differences:
            shown = "; ".join(differences[:8]) + (f"; and {len(differences) - 8} more" if len(differences) > 8 else "")
            raise ValueError(
                f"The seed {self.seed} is not what the task's image ships at {self.deliverable}: {shown}. "
                f"Run `ari extract-seed {self.task} --deliverable {self.deliverable} --output DIR` and use DIR as "
                "the seed, or pass check_seed=False to start from files of your own")
        write_json(self.record, {**saved, "seed": digest(expected), "seed_checked_against": main})

    def grade(self, candidate: Path, script: str, evidence: Path, case: str = "default") -> dict:
        """Run one script on ``candidate`` for ``case`` and return its rewards.

        The script's output, reward file and artifacts are saved under ``evidence``.
        """
        image = self.image()
        name = "ari-grader-" + uuid4().hex[:12]
        command = [
            "create", "--name", name, "--cap-drop", "ALL", "--security-opt", "no-new-privileges:true",
            "--pids-limit", "512", "--init",
            "--cpus", f"{self.cpus:g}",
            "--memory", f"{self.environment.get('memory_mb', 4096)}m",
            "--volume", _LOGS.as_posix(), "--volume", _ARTIFACTS.as_posix(),  # anonymous; removed with the container
            "--env", f"{CASE_VARIABLE}={case}",
            "--mount", f"type=bind,source={Path(candidate).resolve()},target={self.deliverable},readonly",
        ]
        if not self.network:
            command += ["--network", "none"]
        for capability in self.capabilities:
            command += ["--cap-add", capability]
        if self.mount_tests:
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
        self.prepare()
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
        return Evaluation(metrics, metadata={"feedback_rewards": rewards, "grader_image": self.image(),
                                             "grader_cpus": self.cpus})


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


def _resolve(image: str | None, context: Path | None) -> tuple[str, bool]:
    """A pinned image, pulled if absent, or the image built from ``context``; and whether it was built."""
    if context is None:
        if not _image_present(image):
            _docker(["pull", "--quiet", image])
        return image, False
    with tempfile.TemporaryDirectory(prefix="ari-grader-build-") as scratch:
        iid = Path(scratch) / "image-id"
        _docker(["build", "--quiet", "--iidfile", str(iid), str(context)])
        built = iid.read_text().strip()
    if not _PINNED.fullmatch(built):
        raise RuntimeError(f"docker build reported no image ID: {built!r}")
    return built, True


def _main_image(task: Path, environment: dict) -> tuple[str | None, Path | None]:
    """The task's main image: its pinned ``docker_image``, or the build context ``environment/``."""
    image = environment.get("docker_image")
    if image is not None:
        if not _PINNED.fullmatch(str(image)):
            raise ValueError("[environment] docker_image must be pinned by digest (sha256:...)")
        return image, None
    if not (task / "environment/Dockerfile").is_file():
        raise ValueError(f"The Harbor task needs {task}/environment/Dockerfile or [environment] docker_image")
    return None, task / "environment"


def shipped(image: str, path: PurePosixPath, destination: Path) -> Path | None:
    """Copy what ``image`` holds at ``path`` into ``destination``; None when the image has nothing there.

    The image is never run: a container is created, copied from and removed.
    Links and special files are dropped from the copy.
    """
    name = "ari-shipped-" + uuid4().hex[:12]
    _docker(["create", "--name", name, "--entrypoint", "sh", image])
    try:
        copied = subprocess.run(["docker", "cp", f"{name}:{path}/.", str(destination)],
                                capture_output=True, text=True)
        if copied.returncode != 0:
            if "Could not find" in copied.stderr or "No such" in copied.stderr:
                return None
            raise RuntimeError(f"docker cp failed: {copied.stderr.strip()[-2000:]}")
    finally:
        subprocess.run(["docker", "rm", "--force", name], capture_output=True)
    _regular_files_only(destination)
    return destination


def _has_file(image: str, path: PurePosixPath) -> bool:
    """Whether ``image`` holds a regular file at ``path``. The image is never run."""
    with tempfile.TemporaryDirectory(prefix="ari-probe-") as scratch:
        name = "ari-probe-" + uuid4().hex[:12]
        _docker(["create", "--name", name, "--entrypoint", "sh", image])
        try:
            copied = subprocess.run(["docker", "cp", f"{name}:{path}", str(Path(scratch) / "file")],
                                    capture_output=True)
        finally:
            subprocess.run(["docker", "rm", "--force", name], capture_output=True)
        return copied.returncode == 0 and (Path(scratch) / "file").is_file()


def extract_seed(task: Path, deliverable: str, destination: Path, image: str | None = None) -> dict:
    """Write the starting deliverable a Harbor task's image ships at ``deliverable`` into ``destination``.

    This is the import step for a task whose starter exists only in its image,
    for example because the Dockerfile generates it. The image is built from
    ``environment/``, or the pinned ``docker_image`` is used; ``image``, an
    image pinned by digest, is read instead. ``destination`` must not exist yet.
    """
    task, destination = Path(task).resolve(), Path(destination)
    if destination.exists():
        raise ValueError(f"{destination} already exists; name a new directory")
    environment = tomllib.loads((task / "task.toml").read_text(encoding="utf-8")).get("environment") or {}
    if image is None:
        image, _ = _resolve(*_main_image(task, environment))
    elif not _PINNED.fullmatch(str(image)):
        raise ValueError("--image must be an image ID or digest (sha256:...)")
    with tempfile.TemporaryDirectory(prefix="ari-shipped-") as scratch:
        found = shipped(image, _container_path(deliverable, "deliverable"), Path(scratch) / "shipped")
        destination.parent.mkdir(parents=True, exist_ok=True)
        if found is None:
            destination.mkdir()
        else:
            shutil.copytree(found, destination)
    try:
        snapshot(destination)
        problem = None
    except ValueError as error:
        problem = f"{error}. A candidate is a tree of UTF-8 text files, so this starter cannot be one as it is"
    return {"seed": str(destination), "image": image, "shipped": found is not None,
            "files": sorted(path.relative_to(destination).as_posix()
                            for path in destination.rglob("*") if path.is_file()),
            "problem": problem}


def _file_digests(root: Path) -> dict[str, str]:
    """A hash of every regular file under ``root``, by relative path, skipping what a candidate snapshot skips."""
    digests = {}
    for path in sorted(root.rglob("*")):
        relative = path.relative_to(root)
        if any(part in IGNORED_DIRS for part in relative.parts[:-1]) or not path.is_file() or path.is_symlink():
            continue
        if any(fnmatch.fnmatchcase(path.name, pattern) for pattern in IGNORED_FILES):
            continue
        digests[relative.as_posix()] = hashlib.sha256(path.read_bytes()).hexdigest()
    return digests


def _differences(shipped_files: dict, seed_files: dict) -> list[str]:
    return ([f"{name} is only in the image" for name in sorted(shipped_files.keys() - seed_files.keys())]
            + [f"{name} is only in the seed" for name in sorted(seed_files.keys() - shipped_files.keys())]
            + [f"{name} differs" for name in sorted(shipped_files.keys() & seed_files.keys())
               if shipped_files[name] != seed_files[name]])


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


def researcher_environment(task: Path, destination: Path, *, private=(), image: str | None = None,
                           allowed_hosts=()) -> Path:
    """Write a Harbor environment for the researcher: the task's main container and nothing of its grader.

    The copy holds ``task.toml`` with only the ``[environment]`` table and the
    task's ``environment/`` directory. Left out are the task's
    ``docker-compose.yaml``, which starts the benchmark's own grader services
    and submission tool, the task's MCP servers, and every path in ``private``.
    The image is built from this copy, so a Dockerfile that copies a private
    path fails to build instead of handing the grader to the researcher.
    ``tests/`` and ``solution/`` lie outside ``environment/`` and are never
    copied.

    ``image`` is an image pinned by digest to run instead of building the
    Dockerfile: the task's image with what an agent needs added to it.

    The task's ``[agent]`` network policy is kept, so the agents can reach what
    an agent under ``harbor run`` can, and only while they run. Hosts that
    name the task's own compose services, such as its grader, are dropped,
    since those services are not started. ``allowed_hosts`` adds hosts to an
    allowlist, such as the model provider of an agent that calls it from
    inside the container.

    Calling it again checks that the copy is unchanged.
    """
    from harbor.models.task.config import TaskConfig

    task = Path(task)
    manifest = tomllib.loads((task / "task.toml").read_text(encoding="utf-8"))
    table = dict(manifest.get("environment") or {})
    agent = _agent_policy(task, manifest.get("agent") or {}, list(allowed_hosts))
    for name in ("workdir", "skills_dir", "mcp_servers"):  # ARI sets the first two and replaces the submit tool
        table.pop(name, None)
    if image is not None:
        if not _PINNED.fullmatch(str(image)):
            raise ValueError("[task] image must be an image ID or digest (sha256:...)")
        table["docker_image"] = image
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
        (staged / "task.toml").write_text(TaskConfig(environment=table, agent=agent).model_dump_toml(),
                                          encoding="utf-8")
        if source.is_dir():
            shutil.copytree(source, staged / "environment", ignore=ignore)
        if not destination.exists():
            destination.parent.mkdir(parents=True, exist_ok=True)
            shutil.copytree(staged, destination)
        elif task_context_digest(destination) != task_context_digest(staged):
            raise ValueError("The Harbor task's environment changed; use a fresh output")
    return destination


def _agent_policy(task: Path, table: dict, extra: list[str]) -> dict:
    """The task's [agent] network policy without its own compose services, plus ``extra`` hosts."""
    mode, hosts = table.get("network_mode"), list(table.get("allowed_hosts") or [])
    if extra and mode != "allowlist":
        raise ValueError(f"allowed_hosts adds to the task's agent allowlist, but its [agent] network_mode is "
                         f"{mode or 'not set'}")
    if mode is None:
        return {}
    compose = task / "environment/docker-compose.yaml"
    services = set()
    if compose.is_file():
        import yaml

        services = set(((yaml.safe_load(compose.read_text(encoding="utf-8")) or {}).get("services") or {}))
    kept = [host for host in hosts if host not in services]
    policy = {"network_mode": mode}
    if mode == "allowlist":
        policy["allowed_hosts"] = list(dict.fromkeys(kept + extra))
    return policy


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
