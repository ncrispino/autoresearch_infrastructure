"""Codex adapter: native settings, capabilities, launch and evidence contract."""

from __future__ import annotations

from pathlib import PurePosixPath

from . import base as native_io
from .base import (
    HarnessAdapter,
    _process_command,
    _quote,
    _read_events,
    _SessionFiles,
    _stop_process,
)

CODEX_DISABLED_FEATURES = (
    "shell_tool",
    "unified_exec",
    "multi_agent",
    "multi_agent_v2",
    "apps",
    "browser_use",
    "browser_use_external",
    "browser_use_full_cdp_access",
    "computer_use",
    "image_generation",
    "view_image",
    "code_mode",
    "code_mode_only",
    "sleep_tool",
    "tool_suggest",
    "workspace_dependencies",
    "skill_mcp_dependency_install",
    "goals",
)


class CodexAdapter(HarnessAdapter):
    @staticmethod
    def name():
        return "codex"

    setting_specs = {
        "reasoning_effort": {
            "type": "enum",
            "default": None,
            "values": ["none", "minimal", "low", "medium", "high", "xhigh", "max"],
            "description": "Codex model reasoning-effort request. Extra deliberation may help "
            "resolve long authorization chains at additional latency and cost, "
            "but is not enforcement. Availability also depends on the frozen "
            "model; identity and ceilings do not change.",
            "example": "high",
        },
        "web_search": {
            "type": "enum",
            "default": "disabled",
            "values": ["disabled"],
            "description": "Native Codex web search remains disabled in mediated task-tool "
            "sessions. This avoids an unmediated observation surface; use an "
            "explicitly registered task tool when the experiment permits retrieval. "
            "Cached/live values are not exposed.",
            "example": "disabled",
            "pass_to_agent": False,
        },
    }
    capability_specs = {
        "skill_files": {
            "tools": ["exec_command"],
            "scope": "Literal cat/ls on selected skill roots only. Editable pre-tool denial and "
            "post-tool observation; native result transformation is unsupported, "
            "including Code Mode.",
        },
        "native_subagents": {
            "tools": ["spawn_agent", "send_input", "wait", "close_agent"],
            "supported": False,
            "reason": "Native delegation is disabled until child isolation, fixed-model "
            "accounting and budgets are verified.",
        },
    }
    cli_version = "0.159.2"
    cli_command = "codex"
    finalizer_count = 2
    requires_program = False
    native_tools = {
        "Bash": {"kind": "command", "argument": "command"},
        "exec_command": {"kind": "command", "argument": "cmd"},
    }
    skill_instructions = (
        "Use native exec_command with exactly `cat -- ABSOLUTE_FILE` or "
        "`ls -- ABSOLUTE_DIRECTORY`. No shell expansion, scripts, pipes or other commands are permitted."
    )

    @classmethod
    def version_label(cls, version):
        return f"codex-cli {version}"

    @classmethod
    def skill_roots(cls, root, logs, count):
        return [str(root / "codex-home" / "skills" / f"skill-{i}") for i in range(count)]

    @classmethod
    def task_tool_names(cls, names):
        return [spelling for name in names for spelling in ("mcp__task__" + name, "mcp__task." + name)]


    @classmethod
    def read_output(cls, directory):
        events = _read_events(directory / "codex.txt")
        if any(
            isinstance(x.get("item"), dict)
            and "Code Mode is unavailable" in str(x["item"].get("message", ""))
            for x in events
        ):
            raise RuntimeError(
                "Codex model requires unavailable Code Mode; direct task tools were not usable"
            )
        if any(x.get("type") == "turn.failed" for x in events):
            raise RuntimeError("Codex session failed")
        if not any(x.get("type") == "turn.completed" for x in events):
            raise RuntimeError("Codex session has no completed turn")
        answers = [
            x["item"].get("text", "")
            for x in events
            if x.get("type") == "item.completed"
            and isinstance(x.get("item"), dict)
            and x["item"].get("type") == "agent_message"
        ]
        if not answers or not isinstance(answers[-1], str):
            raise RuntimeError("Codex session has no final answer")
        return answers[-1]

    @classmethod
    async def create_session(
        cls,
        runner,
        *,
        settings,
        remote,
        remote_logs,
        local,
        skills_dir,
        mcp,
        prompt,
        roots,
        gate_command,
        hook_timeout,
    ):
        await runner._upload_text(prompt, str(remote / "system.txt"))
        hooks = [
            {"matcher": "*", "hooks": [{"type": "command", "command": gate_command, "timeout": hook_timeout}]}
        ]
        config = {
            "model_instructions_file": str(remote / "system.txt"),
            "web_search": "disabled",
            "approval_policy": "never",
            "features": {**{feature: False for feature in CODEX_DISABLED_FEATURES}, "code_mode_host": True},
            "hooks": {"PreToolUse": hooks, "PostToolUse": hooks},
        }
        if roots:
            config["features"].update({"shell_tool": True, "unified_exec": True, "unified_exec_tty": False})
        env = {"OPENAI_API_KEY": runner.provider.key(), "OPENAI_BASE_URL": runner.provider.base_url}
        config.update(
            {
                "model_provider": "autoresearch",
                "model_providers": {
                    "autoresearch": {
                        "name": runner.provider.name,
                        "base_url": runner.provider.base_url,
                        "env_key": "OPENAI_API_KEY",
                        "wire_api": "responses",
                        "supports_websockets": False,
                        "request_max_retries": 0,
                        "stream_max_retries": 0,
                    }
                },
            }
        )
        if runner.provider.name == "openrouter":
            env["ARI_ROUTED_MODEL"] = runner.model
        return _session_type()(
            logs_dir=local,
            model_name=runner.model,
            environment_logs_dir=remote_logs,
            remote_root=remote,
            run_env=env,
            extra_env=env,
            skills_dir=skills_dir,
            version=runner.version,
            config=config,
            mcp_servers=[mcp],
            dangerously_bypass_hook_trust=True,
            **cls.native_options(settings),
        )


def _session_type():
    from harbor.agents.installed.codex import Codex

    class NativeSession(_SessionFiles, Codex):
        def __init__(self, *args, remote_root: PurePosixPath, run_env: dict, **kwargs):
            self.remote_root = remote_root
            self.run_env = run_env
            self.command_timeout = 120
            self._REMOTE_CODEX_HOME = remote_root / "codex-home"
            self._REMOTE_CODEX_SECRETS_DIR = remote_root / "secrets"
            super().__init__(*args, **kwargs)

        async def exec_as_agent(self, environment, command, env=None, cwd=None, timeout_sec=None):
            if getattr(self, "_cleanup_mode", False):
                timeout_sec = min(timeout_sec or native_io.NATIVE_STOP_TIMEOUT, native_io.NATIVE_STOP_TIMEOUT)
            if "codex exec " in command:
                command = command.replace("--enable unified_exec ", "")
                command = command.replace("codex exec ", "codex exec --strict-config ", 1)
                # Harbor strips the OpenRouter publisher; retain the exact configured ID.
                if self.run_env.get("ARI_ROUTED_MODEL"):
                    old = "--model " + self.model_name.split("/")[-1] + " "
                    command = command.replace(old, "--model " + _quote(self.model_name) + " ", 1)
                command = _process_command(command, self.remote_root, self.command_timeout)
            merged = {**(env or {}), **self.run_env, "HOME": str(self.remote_root / "home")}
            merged.pop("ARI_ROUTED_MODEL", None)
            try:
                return await super().exec_as_agent(
                    environment,
                    command,
                    env=merged,
                    cwd=str(self.remote_root / "work"),
                    timeout_sec=timeout_sec,
                )
            except BaseException:
                self._cleanup_mode = True
                if "/session_process.py " in command:
                    await _stop_process(environment, self.remote_root)
                raise

        def _build_effective_config(self, openai_base_url=None):
            config = super()._build_effective_config(openai_base_url)
            # Advertise the complete frozen task surface in both direct and V8 code
            # modes; model metadata must not defer it behind a native search tool.
            config["mcp_servers"]["task"]["omit_tools_from"] = ["deferred"]
            return config

        def _build_register_skills_command(self):
            if not self.skills_dir:
                return None
            return f'mkdir -p "$CODEX_HOME/skills" && cp -R {_quote(self.skills_dir)}/. "$CODEX_HOME/skills/"'

    return NativeSession
