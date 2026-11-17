"""Use a Harbor task's environment definition for a researcher or a reviewer.

Harbor already defines environments: ``task.toml`` holds an ``[environment]``
table (resources, network policy, environment variables, healthcheck, an
optional prebuilt ``docker_image``) and ``environment/`` holds a Dockerfile and
optionally a ``docker-compose.yaml`` with services. ARI reads only those two
parts. The Harbor task's instruction, tests and solution are ignored: ARI
supplies its own assignment and submission protocol.

An environment goes through three stages:

1. ``load`` validates the definition and starts nothing (``ari check``).
2. ``prepare`` builds the main image once, or resolves the prebuilt one, and
   records the actual image identities of the main container and every
   sidecar service (``ari prepare``, or the first start of ``ari run``).
3. Each start runs Harbor's own lifecycle on the prepared image: start the
   services, wait for health checks, set up the agent, run it, then tear
   everything down with its volumes. Nothing in the containers survives a
   start; see ``Environment.workspace`` for what ARI carries over.

ARI sets ``workdir`` and ``skills_dir`` itself and adds its isolation overlay and
mounts, so a definition may not set them or loosen them through compose.
Images must be pinned by digest, so a retagged image cannot change a study.
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import tempfile
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .persistence import digest, write_json
from .source_tree import task_context_digest

_RESERVED = {"workdir", "skills_dir"}
_UNSUPPORTED = {"tpu"}  # needs a provider other than local Docker
_RESERVED_FILES = {"isolation.json"}
_PINNED = re.compile(r"(?:[^@\s]+@)?sha256:[0-9a-f]{64}")
# Compose keys that would undo ARI's isolation or bypass its mounts.
_MAIN_LOCKED = {"user", "read_only", "volumes", "tmpfs", "cap_add", "cap_drop", "security_opt", "privileged",
                "network_mode", "pid", "ipc", "devices", "userns_mode", "image", "build", "entrypoint"}
_SERVICE_FORBIDDEN = {"privileged", "cap_add", "devices", "pid", "ipc", "userns_mode", "build"}


@dataclass(frozen=True)
class HarborEnvironment:
    """A checked Harbor environment definition, and where its preparation is recorded."""

    config: dict
    files: Path | None
    record: Path | None = None

    @property
    def identity(self) -> str:
        """The definition: its settings and every file in ``environment/``."""
        return digest({"config": self.config,
                       "files": task_context_digest(self.files) if self.files else None})

    @property
    def sidecars(self) -> dict[str, str]:
        compose = self.files / "docker-compose.yaml" if self.files else None
        if compose is None or not compose.is_file():
            return {}
        import yaml

        services = (yaml.safe_load(compose.read_text(encoding="utf-8")) or {}).get("services") or {}
        return {name: service["image"] for name, service in services.items()
                if name != "main" and (service or {}).get("image")}

    def prepared(self) -> dict | None:
        """The recorded preparation, or None before ``prepare``."""
        if self.record is None or not self.record.is_file():
            return None
        prepared = json.loads(self.record.read_text())
        if prepared.get("definition") != self.identity:
            raise ValueError(f"{self.record} records a different environment definition; use a fresh output")
        return prepared


def load(directory: Path) -> HarborEnvironment:
    """Read and check a Harbor task directory's environment. Starts nothing."""
    from harbor.models.task.config import EnvironmentConfig

    directory = Path(directory)
    manifest = directory / "task.toml"
    files = directory / "environment"
    if not manifest.is_file():
        raise ValueError(f"No Harbor task.toml at {manifest}")
    table = tomllib.loads(manifest.read_text(encoding="utf-8")).get("environment", {})
    if not isinstance(table, dict):
        raise ValueError("task.toml [environment] must be a table")
    if set(table) & _RESERVED:
        raise ValueError(f"ARI sets {sorted(_RESERVED)}; remove them from [environment]")
    if set(table) & _UNSUPPORTED:
        raise ValueError(f"ARI runs agents on Harbor's local Docker backend, which cannot provide "
                         f"{sorted(set(table) & _UNSUPPORTED)}")
    config = EnvironmentConfig.model_validate(table).model_dump(mode="json", exclude_none=True)
    if config.get("os", "linux") != "linux":
        raise ValueError("An agent environment must be a Linux container")
    if "docker_image" in table and not _PINNED.fullmatch(str(table["docker_image"])):
        raise ValueError("[environment] docker_image must be pinned by digest (sha256:...)")
    if not files.is_dir():
        if "docker_image" not in table:
            raise ValueError(f"A Harbor environment needs {files}/Dockerfile or [environment] docker_image")
        return HarborEnvironment(config, None)  # the prebuilt image is the whole environment
    if any((files / name).exists() for name in _RESERVED_FILES):
        raise ValueError(f"environment/ may not contain {sorted(_RESERVED_FILES)}; ARI writes it")
    dockerfile = files / "Dockerfile"
    if "docker_image" not in table and not dockerfile.is_file():
        raise ValueError(f"A Harbor environment needs {dockerfile} or [environment] docker_image")
    if dockerfile.is_file():
        _check_dockerfile(dockerfile.read_text(encoding="utf-8"))
    compose = files / "docker-compose.yaml"
    if compose.is_file():
        _check_compose(compose)
    task_context_digest(files)  # bounded, regular files only
    return HarborEnvironment(config, files)


def stage(environment: HarborEnvironment, target: Path) -> Path:
    """Copy the definition's files to ``target``, so later edits to the task cannot change a run."""
    if environment.files is None:
        target.mkdir(parents=True)
    else:
        shutil.copytree(environment.files, target)
    return target


def prepare(environment: HarborEnvironment) -> dict:
    """Build or resolve the images once and record their identities.

    The main image is built from the Dockerfile with ``docker build`` and
    identified by the image ID Docker reports, or is the pinned prebuilt image.
    Sidecars are pinned images, so their references are their identities. A
    run reuses the recorded image; if it is gone, preparing again would build a
    possibly different image, so that is refused.
    """
    prepared = environment.prepared()
    if prepared is not None:
        if not _image_present(prepared["main"]):
            raise ValueError(f"The prepared image {prepared['main']} is no longer available; "
                             "load it again (docker load) or use a fresh output")
        return prepared
    dockerfile = environment.files / "Dockerfile" if environment.files else None
    if dockerfile is not None and dockerfile.is_file() and "docker_image" not in environment.config:
        with tempfile.TemporaryDirectory(prefix="ari-build-") as scratch:
            iid = Path(scratch) / "image-id"
            _docker(["build", "--quiet", "--iidfile", str(iid), str(environment.files)])
            main = iid.read_text().strip()
        if not _PINNED.fullmatch(main):
            raise RuntimeError(f"docker build reported no image ID: {main!r}")
        built = True
    else:
        main, built = environment.config["docker_image"], False
    prepared = {"definition": environment.identity, "main": main, "built": built,
                "sidecars": environment.sidecars}
    if environment.record is not None:
        write_json(environment.record, prepared)
    return prepared


def runtime(environment: HarborEnvironment, target: Path) -> dict:
    """Write the files a start's Harbor task needs and return its ``[environment]`` settings."""
    prepared = prepare(environment)
    if environment.files is not None:
        shutil.copytree(environment.files, target, ignore=shutil.ignore_patterns("Dockerfile"))
    else:
        target.mkdir(parents=True)
    return {"cpus": 1, "memory_mb": 4096, **environment.config, "docker_image": prepared["main"]}


def _docker(args: list[str]) -> str:
    result = subprocess.run(["docker", *args], capture_output=True, text=True)
    if result.returncode != 0:
        raise RuntimeError(f"docker {args[0]} failed: {result.stderr.strip()[-2000:]}")
    return result.stdout


def _image_present(image: str) -> bool:
    try:
        _docker(["image", "inspect", "--format", "{{.Id}}", image])
    except RuntimeError:
        return False
    return True


def _check_dockerfile(text: str) -> None:
    stages = set()
    for line in text.splitlines():
        words = line.split()
        if not words or words[0].upper() != "FROM":
            continue
        args = [word for word in words[1:] if not word.startswith("--")]
        if not args:
            raise ValueError("A Dockerfile FROM line names no image")
        image = args[0]
        if image.lower() not in stages and image != "scratch" and not _PINNED.fullmatch(image):
            raise ValueError(f"Dockerfile FROM {image} must be pinned by digest (image@sha256:...)")
        if len(args) >= 3 and args[1].upper() == "AS":
            stages.add(args[2].lower())


def _check_compose(path: Path) -> None:
    """Reject compose settings that would loosen ARI's isolation or hide an unrecorded image."""
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for name, service in (data.get("services") or {}).items():
        service = service or {}
        if name == "main":
            locked = sorted(set(service) & _MAIN_LOCKED)
            if locked:
                raise ValueError(f"Compose may not set {locked} on the main service; ARI controls them")
            continue
        forbidden = sorted(set(service) & _SERVICE_FORBIDDEN)
        if forbidden:
            raise ValueError(f"Compose service {name} may not set {forbidden}")
        if service.get("network_mode") == "host":
            raise ValueError(f"Compose service {name} may not use the host network")
        image = service.get("image")
        if image is None or not _PINNED.fullmatch(str(image)):
            raise ValueError(f"Compose service {name} image must be pinned by digest")
        for volume in service.get("volumes") or []:
            source = volume.get("source") if isinstance(volume, dict) else str(volume).split(":")[0]
            kind = volume.get("type") if isinstance(volume, dict) else None
            if kind == "bind" or str(source).startswith(("/", ".", "~")):
                raise ValueError(f"Compose service {name} may not bind host paths; use named volumes")
