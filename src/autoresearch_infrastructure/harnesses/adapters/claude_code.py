"""ClaudeCode adapter: native settings, capabilities, launch and evidence contract."""

from __future__ import annotations

import json
from copy import deepcopy
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


class ClaudeCodeAdapter(HarnessAdapter):
    @staticmethod
    def name():
        return "claude-code"

    setting_specs = {
        "reasoning_effort": {
            "type": "enum",
            "default": None,
            "values": ["low", "medium", "high", "xhigh", "max"],
            "description": "Claude Code effort request. It can alter deliberation and cost "
            "when reviewing ambiguous authorization, but is not equivalent "
            "numerically to Codex effort and does not enforce a decision. Only "
            "the selected model is used.",
            "example": "high",
        },
        "max_turns": {
            "type": "positive integer",
            "default": None,
            "description": "Claude Code's native turn ceiling. A low cap can contain a reviewing "
            "helper or an unproductive tool loop, but can truncate legitimate "
            "recovery. It is not a portable token/time budget. Exhaustion is reported "
            "as incomplete, not success.",
            "example": 4,
            "minimum": 1,
            "maximum": 1000,
        },
    }
    capability_specs = {
        "skill_files": {
            "tools": ["Read"],
            "scope": "Selected skill roots and reference files only. Editable pre/post hooks; text "
            "Read file.content may be replaced while output structure stays fixed.",
        },
        "native_subagents": {
            "tools": ["Agent"],
            "supported": False,
            "reason": "Native delegation is disabled until child isolation, fixed-model "
            "accounting and budgets are verified.",
        },
    }
    cli_version = "2.1.309"
    cli_command = "claude"
    finalizer_count = 1
    native_tools = {"Read": {"kind": "file", "argument": "file_path"}}
    native_replacement_field = "updatedToolOutput"
    native_failure_mode = "stop"
    skill_instructions = "Use the native Read tool with an absolute file_path."

    @classmethod
    def version_label(cls, version):
        return f"{version} (Claude Code)"

    @classmethod
    def skill_roots(cls, root, logs, count):
        return [str(logs / "sessions" / "skills" / f"skill-{i}") for i in range(count)]

    @classmethod
    def task_tool_names(cls, names):
        return ["mcp__task__" + name for name in names]


    @classmethod
    def read_output(cls, directory):
        events = _read_events(directory / "claude-code.txt")
        final = [x for x in events if x.get("type") == "result"]
        if len(final) != 1 or final[0].get("is_error") or final[0].get("subtype") != "success":
            raise RuntimeError("Claude Code session has no successful completed result")
        text = final[0].get("result")
        if not isinstance(text, str):
            raise RuntimeError("Claude Code result is not text")
        return text

    @classmethod
    def validate_native_transform(cls, original, transformed):
        if original == transformed:
            return
        if (
            not isinstance(original, dict)
            or original.get("type") != "text"
            or not isinstance(original.get("file"), dict)
            or not isinstance(transformed, dict)
            or not isinstance(transformed.get("file"), dict)
            or not isinstance(transformed["file"].get("content"), str)
        ):
            raise ValueError("Only native Claude text Read content can be transformed")
        expected = deepcopy(original)
        expected["file"]["content"] = transformed["file"]["content"]
        if transformed != expected:
            raise ValueError("Native Read transformation changed protected output structure")

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
        hooks = [
            {"matcher": "*", "hooks": [{"type": "command", "command": gate_command, "timeout": hook_timeout}]}
        ]
        native_config = {"hooks": {"PreToolUse": hooks, "PostToolUse": hooks, "PostToolUseFailure": hooks}}
        mcp_config = {"mcpServers": {"task": {"type": "stdio", "command": mcp.command, "args": mcp.args}}}
        await runner._upload_text(json.dumps(mcp_config), str(remote / "mcp.json"))
        env = {"ANTHROPIC_BASE_URL": runner.provider.base_url}
        key_name = "ANTHROPIC_AUTH_TOKEN" if runner.provider.name == "openrouter" else "ANTHROPIC_API_KEY"
        env[key_name] = runner.provider.key()
        skill_tools = cls.capability_specs["skill_files"]["tools"] if roots else []
        return _session_type()(
            logs_dir=local,
            model_name=runner.model,
            environment_logs_dir=remote_logs,
            remote_root=remote,
            run_env=env,
            extra_env=env,
            skills_dir=skills_dir,
            mcp_path=str(remote / "mcp.json"),
            system_prompt=prompt,
            native_tools=",".join(skill_tools),
            version=runner.version,
            config=native_config,
            mcp_servers=[mcp],
            permission_mode="bypassPermissions",
            **cls.native_options(settings),
        )


def _session_type():
    from harbor.agents.installed.claude_code import ClaudeCode

    class NativeSession(_SessionFiles, ClaudeCode):
        def __init__(
            self,
            *args,
            remote_root: PurePosixPath,
            run_env: dict,
            mcp_path: str,
            system_prompt: str,
            native_tools: str = "",
            **kwargs,
        ):
            self.remote_root = remote_root
            self.run_env = run_env
            self.mcp_path = mcp_path
            self.system_prompt = system_prompt
            self.native_tools = native_tools
            self.command_timeout = 120
            self._REMOTE_SETTINGS_DIR = remote_root / "settings"
            self._REMOTE_SETTINGS_PATH = self._REMOTE_SETTINGS_DIR / "settings.json"
            super().__init__(*args, **kwargs)

        async def exec_as_agent(self, environment, command, env=None, cwd=None, timeout_sec=None):
            if getattr(self, "_cleanup_mode", False):
                timeout_sec = min(timeout_sec or native_io.NATIVE_STOP_TIMEOUT, native_io.NATIVE_STOP_TIMEOUT)
            if "claude --verbose " in command:
                extra = (
                    f"claude --tools {_quote(self.native_tools)} --strict-mcp-config --setting-sources '' "
                    f"--system-prompt {_quote(self.system_prompt)} "
                    f"--mcp-config {_quote(self.mcp_path)} --verbose "
                )
                command = command.replace("claude --verbose ", extra, 1)
                command = _process_command(command, self.remote_root, self.command_timeout)
            merged = {**(env or {}), **self.run_env, "HOME": str(self.remote_root / "home")}
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

    return NativeSession
