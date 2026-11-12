"""The Harbor agent that runs a candidate solver program against a Harbor task."""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import shlex
from pathlib import Path

from harbor.agents.base import BaseAgent
from harbor.agents.capabilities import AgentCapabilities

from . import sandbox_exec
from .harnesses.sessions import HarnessSessionRunner
from .ledger import StartLedger
from .persistence import write_bytes, write_json
from .programs.bridge import ProgramBridge
from .providers import Provider, provider_for


class HarborTaskTools:
    """The one tool a solver gets: run a shell command inside the Harbor task's container."""

    tools = [
        {
            "name": "exec",
            "description": "Run a shell command in the task environment to read files, edit code or check results. timeout_seconds is in seconds: 1 to 30, default 30.",
            "parameters": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "minLength": 1},
                    "timeout_seconds": {
                        "type": "integer", "minimum": 1, "maximum": 30, "default": 30,
                        "description": "Maximum command runtime in seconds, from 1 to 30. Omit for 30 seconds. Do not use milliseconds.",
                    },
                },
                "required": ["command"],
                "additionalProperties": False,
            },
            "annotations": {"readOnlyHint": False, "destructiveHint": True, "openWorldHint": True},
        }
    ]

    def __init__(self, environment, *, max_seconds=30, max_output_bytes=200_000):
        self.environment = environment
        self.max_seconds = max_seconds
        self.max_output_bytes = max_output_bytes
        self.script = Path(sandbox_exec.__file__).read_text()

    async def __call__(self, call):
        if call.get("name") != "exec":
            raise ValueError("Unknown benchmark capability")
        args = call.get("args", {})
        if not isinstance(args, dict) or set(args) - {"command", "timeout_seconds"}:
            raise ValueError("Unsupported benchmark command arguments")
        command = args.get("command")
        timeout = args.get("timeout_seconds", self.max_seconds)
        if not isinstance(command, str) or not command.strip() or len(command) > 100_000:
            raise ValueError("Command must be bounded nonempty text")
        if type(timeout) is not int or not 1 <= timeout <= self.max_seconds:
            raise ValueError(
                f"timeout_seconds must be an integer between 1 and {self.max_seconds} seconds. "
                f"Omit it to use {self.max_seconds} seconds."
            )
        payload = base64.b64encode(
            json.dumps(
                {"command": command, "timeout_seconds": timeout, "max_output_bytes": self.max_output_bytes}
            ).encode()
        ).decode()
        result = await asyncio.wait_for(
            self.environment.exec(
                command="python -c " + shlex.quote(self.script) + " " + shlex.quote(payload),
                timeout_sec=timeout + 5,
            ),
            timeout=timeout + 10,
        )
        if result.return_code:
            raise RuntimeError("Benchmark command supervisor failed")
        if len((result.stdout or "").encode()) > self.max_output_bytes * 6 + 2000:
            raise ValueError("Benchmark command response exceeded its envelope")
        value = json.loads(result.stdout)
        return {"result": value, "error": value.get("failure"), "denied": False}


class HarnessProgramAgent(BaseAgent):
    """Runs a solver program's sessions for one Harbor trial.

    The solver CLI runs in its own session containers. Its tool calls are
    executed in the Harbor task's container. Harbor still owns the task and
    runs its grader.
    """

    capabilities = AgentCapabilities(atif=True)

    def __init__(
        self,
        *args,
        program_path,
        program_id,
        solver_image,
        harness="codex",
        provider_name="openrouter",
        provider_base_url=None,
        provider_key_variable=None,
        timeout_seconds=120,
        max_harness_sessions=2,
        max_harness_depth=1,
        hook_timeout_seconds=30,
        ledger_path=None,
        ledger_limits=None,
        ledger_identity=None,
        **kwargs,
    ):
        super().__init__(*args, **kwargs)
        self.program_path = Path(program_path)
        self.program_id = program_id
        self.solver_image = solver_image
        self.harness = harness
        defaults = provider_for(harness, provider_name)
        self.provider = Provider(
            provider_name, provider_base_url or defaults.base_url,
            provider_key_variable or defaults.key_variable,
        )
        self.timeout_seconds = timeout_seconds
        self.max_harness_sessions = max_harness_sessions
        self.max_harness_depth = max_harness_depth
        self.hook_timeout_seconds = hook_timeout_seconds
        self.ledger = (
            StartLedger(Path(ledger_path), limits=ledger_limits, identity=ledger_identity)
            if ledger_path
            else None
        )

    @staticmethod
    def name():
        return "ari-harness-program"

    def version(self):
        return "1"

    async def setup(self, environment):
        # Nothing is installed in the task's container: the solver CLI and the
        # candidate program run in separate session containers.
        pass

    async def run(self, instruction, environment, context):
        self.logs_dir.mkdir(parents=True, exist_ok=True)
        sessions = HarnessSessionRunner(
            harness=self.harness,
            model=self.provider.model(self.model_name),
            image=self.solver_image,
            logs_root=self.logs_dir / "sessions",
            package_root=self.program_path,
            tools=HarborTaskTools.tools,
            provider=self.provider,
        )

        async def run_session(**kwargs):
            key = (
                self.ledger.start("solver_session", f"{self.logs_dir}:{kwargs['session_id']}")
                if self.ledger
                else None
            )
            try:
                result = await sessions.run_session(**kwargs)
            except BaseException:
                if key:
                    self.ledger.finish(key, status="failed")
                raise
            if key:
                self.ledger.finish(key, status="completed", cost_usd=result.get("usage", {}).get("cost_usd"))
            return result

        bridge = ProgramBridge(
            package=self.program_path,
            program_id=self.program_id,
            harness=self.harness,
            model=self.provider.model(self.model_name),
            task={"instruction": instruction, "tools": HarborTaskTools.tools},
            run_session=run_session,
            execute_tool=HarborTaskTools(environment),
            evidence_dir=self.logs_dir / "program",
            timeout_seconds=self.timeout_seconds,
            max_sessions=self.max_harness_sessions,
            max_depth=self.max_harness_depth,
            hook_timeout_seconds=self.hook_timeout_seconds,
        )
        result = await bridge.run(self.solver_image)
        write_json(self.logs_dir / "program-result.json", result)
        roots = [
            s
            for s in result["sessions"]
            if s.get("parent_id") is None and s.get("status") == "completed" and s.get("trajectory")
        ]
        if not roots:
            raise RuntimeError("Program returned without a completed root solver session")
        selected = roots[-1]
        source = self.logs_dir / "sessions" / selected["session_id"] / "trajectory.json"
        metadata = selected["trajectory"]
        if Path(metadata["path"]).absolute() != source.absolute() or source.is_symlink():
            raise ValueError("Primary session trajectory path differs from its identity")
        raw = source.read_bytes()
        if hashlib.sha256(raw).hexdigest() != metadata["sha256"]:
            raise ValueError("Primary session trajectory changed")
        from .traces import atif_metadata

        atif_metadata(source, harbor_validation=True)
        document = json.loads(raw)
        if document["agent"]["name"] != self.harness:
            raise ValueError("Primary session harness identity changed")
        # Copy any media the trajectory refers to, so Harbor's copy has no broken links.
        from .traces import atif_media_paths

        for relative in atif_media_paths(document):
            target = self.logs_dir / relative
            media = source.parent / relative
            if (
                target.exists()
                or media.is_symlink()
                or source.parent.resolve() not in media.resolve().parents
            ):
                raise ValueError("Unsafe or colliding primary trajectory media")
            write_bytes(target, media.read_bytes())
        write_bytes(self.logs_dir / "trajectory.json", raw)
        usage = result.get("usage", {})
        for field in ("n_input_tokens", "n_output_tokens", "n_cache_tokens", "cost_usd"):
            setattr(context, field, usage.get(field))
