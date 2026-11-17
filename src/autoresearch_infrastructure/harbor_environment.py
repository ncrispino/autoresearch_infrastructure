"""Use a Harbor task's environment definition as the researcher's container.

Harbor already defines environments: ``task.toml`` holds an ``[environment]``
table (resources, network policy, environment variables, healthcheck, an
optional prebuilt ``docker_image``) and ``environment/`` holds a Dockerfile and
optionally a ``docker-compose.yaml`` with services. ARI reads only those two
parts. The Harbor task's instruction, tests and solution are ignored: ARI
supplies its own assignment and submission protocol.

ARI sets ``workdir`` and ``skills_dir`` itself and adds its isolation overlay,
so a definition may not set them. Images must be pinned by digest, in
``docker_image``, in every Dockerfile ``FROM`` and in every compose
``image:``, so a retagged image cannot change a study. Steps that download
during a build are not frozen by this; the definition's hash is recorded, not
the built image.
"""

from __future__ import annotations

import re
import shutil
import tomllib
from dataclasses import dataclass
from pathlib import Path

from .persistence import digest
from .source_tree import task_context_digest

_RESERVED = {"workdir", "skills_dir"}
_RESERVED_FILES = {"isolation.json"}
_PINNED = re.compile(r"(?:[^@\s]+@)?sha256:[0-9a-f]{64}")


@dataclass(frozen=True)
class HarborEnvironment:
    """A checked Harbor environment: its ``[environment]`` table and its files."""

    config: dict
    files: Path | None

    @property
    def identity(self) -> str:
        return digest({"config": self.config,
                       "files": task_context_digest(self.files) if self.files else None})


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
        raise ValueError(f"ARI sets {sorted(_RESERVED)} for the researcher; remove them from [environment]")
    config = EnvironmentConfig.model_validate(table).model_dump(mode="json", exclude_none=True)
    if config.get("os", "linux") != "linux":
        raise ValueError("The researcher environment must be a Linux container")
    if not files.is_dir():
        if "docker_image" not in table:
            raise ValueError(f"A Harbor environment needs {files}/Dockerfile or [environment] docker_image")
        return HarborEnvironment(config, None)  # the prebuilt image is the whole environment
    if any((files / name).exists() for name in _RESERVED_FILES):
        raise ValueError(f"environment/ may not contain {sorted(_RESERVED_FILES)}; ARI writes it")
    if "docker_image" in table and not _PINNED.fullmatch(str(table["docker_image"])):
        raise ValueError("[environment] docker_image must be pinned by digest (sha256:...)")
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


def stage(environment: HarborEnvironment, target: Path) -> HarborEnvironment:
    """Copy the definition into the run directory, so later edits to the task cannot change it."""
    if environment.files is None:
        target.mkdir(parents=True)
    else:
        shutil.copytree(environment.files, target)
    return HarborEnvironment(environment.config, target)


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
    import yaml

    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    for name, service in (data.get("services") or {}).items():
        image = (service or {}).get("image")
        if image is not None and not _PINNED.fullmatch(str(image)):
            raise ValueError(f"Compose service {name} image must be pinned by digest")
