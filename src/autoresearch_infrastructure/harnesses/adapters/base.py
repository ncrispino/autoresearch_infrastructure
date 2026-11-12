"""Common adapter contract and bounded native-session IO helpers."""

from __future__ import annotations

import asyncio
import json
import shlex
import tempfile
from abc import ABC, abstractmethod
from pathlib import Path, PurePosixPath

MAX_LOG_BYTES = 50_000_000
NATIVE_STOP_TIMEOUT = 10


def _quote(value: str) -> str:
    return shlex.quote(value)


def _process_command(command: str, root: PurePosixPath, timeout: float) -> str:
    return (
        "python "
        + _quote(str(root / "session_process.py"))
        + " "
        + str(timeout)
        + " "
        + _quote(str(root / "process.pid"))
        + " "
        + _quote(command)
    )


def _read_events(path: Path) -> list[dict]:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_LOG_BYTES:
        raise RuntimeError("Harness did not produce a bounded event log")
    events = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            value = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(value, dict):
            events.append(value)
    return events


async def _stop_process(environment, remote: PurePosixPath) -> None:
    command = (
        "python "
        + _quote(str(remote / "session_process.py"))
        + " stop "
        + _quote(str(remote / "process.pid"))
    )
    try:
        await asyncio.wait_for(environment.exec(command=command), timeout=NATIVE_STOP_TIMEOUT)
    except Exception:
        pass  # Container teardown remains the final containment boundary.


async def _upload_session_text(environment, text: str, remote: str, filename: str = "content") -> None:
    with tempfile.TemporaryDirectory(prefix="session-config-") as directory:
        local = Path(directory) / filename
        local.write_text(text, encoding="utf-8")
        local.chmod(0o644)
        await environment.upload_file(local, remote)


class _SessionFiles:
    def _redact_command(self, command):
        value = super()._redact_command(command)
        for server in self.mcp_servers:
            for argument in server.args:
                if argument.startswith("ARI_TOOL_TOKEN="):
                    value = value.replace(argument.split("=", 1)[1], "<session-token>")
        return value

    async def _upload_config_text(self, environment, *, content, remote_path, filename):
        await _upload_session_text(environment, content, remote_path, filename)


class HarnessAdapter(ABC):
    """Trusted implementation selected by one explicit registry, never by candidates."""

    setting_specs: dict = {}
    capability_specs: dict = {}
    native_tools: dict = {}
    cli_version: str
    cli_command: str
    finalizer_count: int
    requires_program = True
    native_replacement_field: str | None = None
    native_failure_mode = "block"

    @staticmethod
    @abstractmethod
    def name() -> str: ...

    @classmethod
    @abstractmethod
    def version_label(cls, version: str) -> str: ...

    @classmethod
    def matches_version(cls, stdout, version):
        if not isinstance(stdout, str):
            return False
        lines = stdout.strip().splitlines()
        return (
            bool(lines)
            and lines[-1] == cls.version_label(version)
            and all(line.startswith("WARNING: ") for line in lines[:-1])
        )

    @classmethod
    @abstractmethod
    def read_output(cls, directory: Path) -> str: ...

    @classmethod
    @abstractmethod
    def skill_roots(cls, root, logs, count): ...

    @classmethod
    @abstractmethod
    def task_tool_names(cls, names): ...


    @classmethod
    @abstractmethod
    async def create_session(cls, runner, **kwargs): ...

    @classmethod
    def validate_native_transform(cls, original, transformed):
        if original != transformed:
            raise ValueError("Native result transformation is unsupported for this adapter")

    @classmethod
    def skill_access(cls, roots):
        return (
            "Selected skill files are readable through the native harness tool. "
            + cls.skill_instructions
            + " Only these skill roots and their reference files are granted: "
            + ", ".join(roots)
            + ". Start with SKILL.md in each selected root. "
            "This grants no other filesystem, execution or network access."
        )

    @classmethod
    def native_options(cls, settings):
        return {
            name: settings[name]
            for name, spec in cls.setting_specs.items()
            if name in settings and spec.get("pass_to_agent", True)
        }
