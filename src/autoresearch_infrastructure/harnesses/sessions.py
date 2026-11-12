"""Fresh real Codex/Claude Code sessions, each in its own container.

Harbor owns the outer trial, installed-agent adapters and original ATIF conversion.
This layer creates separate session containers, exposes registered MCP tools and
permits only explicitly scoped native capabilities. It never imports candidate Python.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import re
import tempfile
from pathlib import Path, PurePosixPath

from harbor.models.agent.context import AgentContext
from harbor.models.task.config import MCPServerConfig
from harbor.models.trajectories import Trajectory
from harbor.utils.trajectory_validator import TrajectoryValidator

from autoresearch_infrastructure.persistence import write_json
from autoresearch_infrastructure.source_tree import materialize, snapshot

from .adapters import ADAPTERS, get_adapter
from .adapters.base import MAX_LOG_BYTES, NATIVE_STOP_TIMEOUT, _quote, _stop_process, _upload_session_text
from .container import CONTAINER_REMOVE_TIMEOUT, SessionContainer, _join_owned_task
from .settings import validate_kwargs

BASE_SYSTEM = "Complete the user's task using the supplied tools and explicitly granted capabilities."

EVIDENCE_DOWNLOAD_TIMEOUT = 30
# Shutdown keeps inference cancellation, verified evidence and container removal
# under one owner. This is distinct from the inference deadline; local validation
# and scheduling add no further subprocess grace. Stop phases: agent process group,
# two pinned Harbor Codex finalizers (Claude has one), then runner process group.
# MCP uses poll_interval=.5 + join5.
SESSION_CANCELLATION_TIMEOUT = (
    (2 + max(adapter.finalizer_count for adapter in ADAPTERS.values())) * NATIVE_STOP_TIMEOUT + EVIDENCE_DOWNLOAD_TIMEOUT + CONTAINER_REMOVE_TIMEOUT + 5.5
)


class HarnessSessionError(RuntimeError):
    """A failed real session with any verified partial evidence retained."""

    def __init__(self, message, session_result, *, cleanup_failure=None):
        super().__init__(message)
        self.session_result = session_result
        self.cleanup_failure = cleanup_failure


def system_text(settings: dict, baseline: str = BASE_SYSTEM) -> str:
    text = settings.get("system")
    if text is None:
        return baseline
    if settings.get("system_mode", "extend") == "replace":
        return text
    return baseline + "\n\n" + text


def _validate_download(root: Path) -> None:
    count = 0
    size = 0
    if root.is_symlink() or not root.is_dir():
        raise RuntimeError("Session evidence is not a regular directory")
    for path in root.rglob("*"):
        if path.is_symlink() or (not path.is_dir() and not path.is_file()):
            raise RuntimeError("Unsafe session evidence path")
        if path.is_file():
            if path.stat().st_nlink != 1:
                raise RuntimeError("Hardlinked session evidence")
            count += 1
            size += path.stat().st_size
            if count > 3000 or size > MAX_LOG_BYTES:
                raise RuntimeError("Session evidence exceeds limits")


class HarnessSessionRunner:
    """A fixed harness/model session callback for ProgramBridge."""

    def __init__(self, *, harness, model, logs_root: Path, package_root: Path,
                 tools: list[dict], provider, version: str | None = None,
                 environment=None, image: str | None = None, instruction_baseline: str = BASE_SYSTEM):
        self.instruction_baseline = instruction_baseline
        self.adapter = get_adapter(harness)
        self.harness = harness
        self.model = model
        if (environment is None) == (image is None):
            raise ValueError("Select an isolated session image or an injected fixture environment")
        self.environment = environment
        self.image = image
        self.logs_root = Path(logs_root)
        self.package_root = Path(package_root)
        self.tools = tools
        self.provider = provider
        self.version = version or self.adapter.cli_version
        self._sessions: set[str] = set()

    async def _upload_text(self, text: str, remote: str) -> None:
        await _upload_session_text(self.environment, text, remote)

    async def _download_logs(self, remote: PurePosixPath, local: Path) -> None:
        # Docker's archive API cannot see tmpfs contents on some engines. Use the
        # same exec namespace as the CLI rather than retrying daemon-side cp.
        script = """
import base64,json,os,stat,sys
from pathlib import Path
root=Path(sys.argv[1]); limit=int(sys.argv[2]); files=[]; size=0
if root.is_symlink() or not root.is_dir():
    raise RuntimeError('Missing regular session log directory')
for directory,dirs,names in os.walk(root,followlinks=False):
    for name in dirs+names:
        path=Path(directory)/name; meta=path.lstat()
        if stat.S_ISLNK(meta.st_mode) or not (stat.S_ISDIR(meta.st_mode) or stat.S_ISREG(meta.st_mode)):
            raise RuntimeError('Unsafe session evidence path')
        if stat.S_ISREG(meta.st_mode):
            if meta.st_nlink!=1:
                raise RuntimeError('Hardlinked session evidence')
            size+=meta.st_size
            if len(files)>=3000 or size>limit:
                raise RuntimeError('Session evidence exceeds limits')
            data=path.read_bytes()
            if len(data)!=meta.st_size:
                raise RuntimeError('Session evidence changed during download')
            files.append([path.relative_to(root).as_posix(),base64.b64encode(data).decode('ascii')])
print(json.dumps(files,separators=(',',':')))
"""
        result = await self.environment.exec(
            command="python -c " + _quote(script) + " " + _quote(str(remote)) + " " + str(MAX_LOG_BYTES),
            timeout_sec=EVIDENCE_DOWNLOAD_TIMEOUT,
        )
        if result.return_code:
            raise RuntimeError("Could not collect native session evidence through environment exec")
        encoded = result.stdout or ""
        if len(encoded) > MAX_LOG_BYTES * 4 // 3 + 2_000_000:
            raise RuntimeError("Session evidence transport exceeds limits")
        rows = json.loads(encoded)
        if not isinstance(rows, list) or len(rows) > 3000:
            raise RuntimeError("Invalid session evidence transport")
        pending = []
        paths = set()
        size = 0
        for row in rows:
            if not isinstance(row, list) or len(row) != 2 or not all(isinstance(x, str) for x in row):
                raise RuntimeError("Invalid session evidence entry")
            name, payload = row
            relative = PurePosixPath(name)
            if (not name or relative.is_absolute() or ".." in relative.parts
                    or relative.as_posix() != name or "\\" in name or "\x00" in name
                    or name in paths):
                raise RuntimeError("Unsafe session evidence destination")
            paths.add(name)
            destination = local.joinpath(*relative.parts)
            # Metadata lives beside downloaded logs and must never be overwritten.
            if destination.exists() or any(parent.is_symlink() for parent in destination.parents):
                raise RuntimeError("Session evidence collides with trusted metadata")
            data = base64.b64decode(payload, validate=True)
            size += len(data)
            if size > MAX_LOG_BYTES:
                raise RuntimeError("Session evidence exceeds limits")
            pending.append((destination, data))
        if any(a in b.parents for a, _ in pending for b, _ in pending):
            raise RuntimeError("Conflicting session evidence destinations")
        for destination, data in pending:
            destination.parent.mkdir(parents=True, exist_ok=True)
            destination.write_bytes(data)

    async def _prepare_skills(self, settings: dict, root: PurePosixPath) -> str | None:
        if not settings.get("skills"):
            return None
        remote = root / "skills"
        await self.environment.exec(command=f"mkdir -p {_quote(str(remote))}")
        for index, relative in enumerate(settings["skills"]):
            with tempfile.TemporaryDirectory(prefix="session-skills-") as directory:
                local = Path(directory) / "skill"
                materialize(local, snapshot(self.package_root / relative))
                local.chmod(0o755)
                for path in local.rglob("*"):
                    path.chmod(0o755 if path.is_dir() else 0o644)
                await self.environment.upload_dir(local, str(remote / f"skill-{index}"))
        return str(remote)

    async def run_session(self, *, session_id: str, input: str, settings: dict,
                          tool_handler, parent_id: str | None = None, native_handler=None) -> dict:
        if self.image is not None:
            if (not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", session_id)
                    or session_id in self._sessions):
                raise ValueError("Harness sessions require fresh safe identities")
            validate_kwargs(self.harness, settings, self.package_root)
            self._sessions.add(session_id)
            # Never replace shared mutable self.environment: parent and child can
            # run concurrently while the parent is waiting for its tool result.
            result, primary_error = None, None
            try:
                async with SessionContainer(self.image) as environment:
                    scoped = HarnessSessionRunner(
                        harness=self.harness, model=self.model, environment=environment,
                        logs_root=self.logs_root, package_root=self.package_root, tools=self.tools,
                        provider=self.provider, version=self.version, instruction_baseline=self.instruction_baseline,
                    )
                    try:
                        result = await scoped.run_session(
                            session_id=session_id, input=input, settings=settings,
                            tool_handler=tool_handler, parent_id=parent_id, native_handler=native_handler,
                        )
                    except BaseException as exc:
                        primary_error = exc
                        raise
                return result
            except BaseException as exc:
                if isinstance(exc, asyncio.CancelledError):
                    if isinstance(primary_error, HarnessSessionError):
                        raise primary_error from exc
                    if result is not None:
                        raise HarnessSessionError(
                            "Native session interrupted after inference completed",
                            {**result, "completed": False},
                        ) from exc
                    raise
                if exc is primary_error or (primary_error is None and result is None):
                    raise
                reason = "session_container_cleanup_failed"
                local = self.logs_root / session_id
                try:
                    write_json(local / "cleanup-failure.json", {"reason": reason})
                except OSError:
                    pass  # Preserve native evidence even if the host cannot write this note.
                if isinstance(primary_error, HarnessSessionError):
                    primary_error.cleanup_failure = reason
                    raise primary_error from exc
                if primary_error is not None:
                    raise primary_error from exc
                # Native inference completed, but the owned container did not
                # clean up. Keep its evidence while failing the overall session.
                raise HarnessSessionError(
                    "Native session completed but container cleanup failed",
                    {**result, "completed": False}, cleanup_failure=reason,
                ) from exc
        from ..mcp_service import MCP_PROXY_PATH, session_mcp_service

        if not isinstance(session_id, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,100}", session_id):
            raise ValueError("Unsafe harness session identity")
        if session_id in self._sessions:
            raise ValueError("Harness sessions are fresh and cannot be reused")
        self._sessions.add(session_id)
        settings = validate_kwargs(self.harness, settings, self.package_root)
        allowed = settings.get("tools", {}).get("allow")
        available = {tool["name"] for tool in self.tools}
        if allowed is None:
            allowed = sorted(available) if parent_id is None else []
        if not set(allowed) <= available:
            raise ValueError("Session requests tools outside the frozen trial")
        selected = [tool for tool in self.tools if tool["name"] in allowed]
        from .capabilities import capabilities
        settings = {**settings, "tools": {"allow": allowed}}
        local = self.logs_root / session_id
        local.mkdir(parents=True, exist_ok=False)
        remote = PurePosixPath("/tmp/ari-sessions") / session_id
        remote_logs = remote / "logs"
        timeout = settings.get("timeout_seconds", 120)
        write_json(local / "session.json", {
            "session_id": session_id, "parent_id": parent_id, "harness": self.harness,
            "model": self.model, "settings": settings, "effective_tools": [t["name"] for t in selected],
            "capabilities": capabilities(self.harness, settings),
            "isolation": {"kind": "docker-container" if isinstance(self.environment, SessionContainer)
                          else "fixture", "container": getattr(self.environment, "name", None),
                          "image": getattr(self.environment, "image", None)},
        })
        prepared = await self.environment.exec(command=(
            "mkdir -p " + " ".join(_quote(str(remote / x)) for x in ("home", "work", "logs", "settings"))
        ))
        if prepared.return_code:
            raise RuntimeError("Could not initialize isolated harness session directories")
        check = await self.environment.exec(command=(
            self.adapter.cli_command + " --version"
        ))
        if check.return_code or not self.adapter.matches_version(check.stdout, self.version):
            raise RuntimeError(f"The session image must contain {self.harness} {self.version}")
        skills_dir = await self._prepare_skills(settings, remote)
        await self._upload_text(MCP_PROXY_PATH.read_text(), str(remote / "task_mcp.py"))
        await self._upload_text(Path(__file__).with_name("session_process.py").read_text(),
                                str(remote / "session_process.py"))
        context = AgentContext()
        failed = None
        native_pending = {}
        native_seen = set()
        native_failed = False

        async def native_dispatch(raw):
            nonlocal native_failed
            from .native_hooks import native_request, validate_native_transform
            try:
                raw = native_request(raw, self.harness)
                if not settings.get("skills"):
                    raise ValueError("Native skill capability is absent")
                key = raw["id"]
                if raw["stage"] == "pre":
                    if key in native_seen or len(native_seen) >= 512:
                        raise ValueError("Repeated or excessive native hook calls")
                    native_seen.add(key)
                    native_pending[key] = raw
                else:
                    pending = native_pending.pop(key, None)
                    if pending is None or pending["name"] != raw["name"] or pending["args"] != raw["args"]:
                        raise ValueError("Unmatched native hook completion")
                result = await native_handler(raw) if native_handler is not None else (
                    {"allowed": True, "reason": ""} if raw["stage"] == "pre" else {"result": raw.get("result")}
                )
                if raw["stage"] == "pre" and not result["allowed"]:
                    native_pending.pop(key)
                elif raw["stage"] == "post":
                    validate_native_transform(self.harness, raw["result"], result["result"])
                return result
            except BaseException:
                native_failed = True
                raise

        with session_mcp_service(selected, tool_handler, asyncio.get_running_loop(),
                                 timeout_seconds=timeout, native_handler=native_dispatch) as (url, token):
            # The narrow task-service token is session-local and never enters evidence.
            args = [f"ARI_TOOL_URL={url}", f"ARI_TOOL_TOKEN={token}",
                    "python", str(remote / "task_mcp.py")]
            mcp = MCPServerConfig(name="task", transport="stdio", command="env", args=args)
            agent = await self._agent(settings, remote, remote_logs, local, skills_dir, mcp)
            agent.command_timeout = timeout
            agent_task = asyncio.create_task(agent.run(input, self.environment, context))
            try:
                await asyncio.wait_for(asyncio.shield(agent_task), timeout=timeout)
            except BaseException as exc:
                failed = exc
                agent_task.cancel()
                try:
                    await _join_owned_task(agent_task)
                except BaseException:
                    pass  # The original failure owns status; cancellation cleanup is joined.
            finally:
                async def collect_evidence():
                    if failed is not None:
                        await self._stop(remote)
                    await self._download_logs(remote_logs, local)
                    _validate_download(local)
                    agent.populate_context_post_run(context)

                evidence = asyncio.create_task(collect_evidence())
                try:
                    _, interrupted = await _join_owned_task(evidence)
                    if interrupted and failed is None:
                        failed = asyncio.CancelledError()
                except BaseException as exc:
                    if failed is None:
                        failed = exc
        if native_failed or native_pending or (local / "native-hook-failure.json").exists():
            failed = failed or RuntimeError("Native program hook did not complete safely")
        if failed is None:
            try:
                text = self.adapter.read_output(local)
                trajectory_path = local / "trajectory.json"
                validator = TrajectoryValidator()
                if not validator.validate(trajectory_path):
                    raise RuntimeError("Harness did not produce valid ATIF")
                data = trajectory_path.read_bytes()
                raw = json.loads(data)
                if raw.get("agent", {}).get("name") != self.harness:
                    raise RuntimeError("ATIF harness identity differs from the frozen session")
                trajectory = Trajectory.model_validate(raw)
            except Exception as exc:
                failed = exc
        if failed is not None:
            write_json(local / "failure.json", {"type": type(failed).__name__,
                                                "message": "Harness session did not complete"})
            partial = {"text": "", "completed": False,
                       "usage": context.model_dump(exclude_none=True), "trajectory": None}
            candidate_atif = local / "trajectory.json"
            if candidate_atif.is_file() and not candidate_atif.is_symlink():
                raw_bytes = candidate_atif.read_bytes()
                validator = TrajectoryValidator()
                if len(raw_bytes) <= MAX_LOG_BYTES and validator.validate(candidate_atif):
                    document = json.loads(raw_bytes)
                    if document.get("agent", {}).get("name") == self.harness:
                        partial["trajectory"] = {
                            "path": str(candidate_atif), "sha256": hashlib.sha256(raw_bytes).hexdigest(),
                            "format": "ATIF", "session_id": session_id, "parent_id": parent_id,
                        }
            write_json(local / "result.json", partial)
            raise HarnessSessionError(
                f"{self.harness} session did not complete: {type(failed).__name__}", partial,
            ) from failed
        usage = context.model_dump(exclude_none=True)
        if trajectory.final_metrics:
            usage["final_metrics"] = trajectory.final_metrics.model_dump(exclude_none=True)
        result = {"text": text, "completed": True, "usage": usage, "trajectory": {
            "path": str(trajectory_path), "sha256": hashlib.sha256(data).hexdigest(), "format": "ATIF",
            "session_id": session_id, "parent_id": parent_id,
        }}
        write_json(local / "result.json", result)
        return result

    async def _stop(self, remote: PurePosixPath) -> None:
        await _stop_process(self.environment, remote)

    async def _agent(self, settings, remote, remote_logs, local, skills_dir, mcp):
        prompt = system_text(settings, self.instruction_baseline)
        roots = self.adapter.skill_roots(remote, remote_logs, len(settings.get("skills", [])))
        if roots:
            prompt += "\n\n" + self.adapter.skill_access(roots)
        from autoresearch_infrastructure.programs import parsing
        await self._upload_text(Path(parsing.__file__).read_text(), str(remote / "program_parsing.py"))
        service = dict(arg.split("=", 1) for arg in mcp.args if arg.startswith("ARI_TOOL_"))
        native_timeout = min(settings.get("timeout_seconds", 120), settings.get("hook_timeout_seconds", 30)) + 5
        await self._upload_text(json.dumps({
            "url": service["ARI_TOOL_URL"], "token": service["ARI_TOOL_TOKEN"],
            "timeout": native_timeout, "native_tools": self.adapter.native_tools,
            "replacement_field": self.adapter.native_replacement_field,
        }), str(remote / "native-hooks.json"))
        await self._upload_text(Path(__file__).with_name("native_gate.py").read_text(), str(remote / "native_gate.py"))
        allowed = settings.get("tools", {}).get("allow", [tool["name"] for tool in self.tools])
        gate_command = ("python " + _quote(str(remote / "native_gate.py")) + " "
                        + _quote(json.dumps(self.adapter.task_tool_names(allowed))) + " " + _quote(json.dumps(roots))
                        + " " + _quote(str(remote / "native-hooks.json")) + " " + _quote(self.adapter.native_failure_mode))
        return await self.adapter.create_session(
            self, settings=settings, remote=remote, remote_logs=remote_logs, local=local,
            skills_dir=skills_dir, mcp=mcp, prompt=prompt, roots=roots,
            gate_command=gate_command, hook_timeout=native_timeout + 1,
        )
