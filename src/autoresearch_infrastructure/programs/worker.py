"""Container-only program entrypoint. Never import this to load a host candidate."""
from __future__ import annotations

import asyncio
import contextvars
import dataclasses
import importlib.util
import inspect
import sys
from pathlib import Path

from .api import Allow, Deny, HarnessResult, SolverInput, SolverOutput, ToolCall, ToolObservation
from .wire import MAX_MESSAGE_BYTES, Peer, RPCError

_PARENT = contextvars.ContextVar("defense_parent_session", default=None)
_STATE_SCOPE = contextvars.ContextVar("defense_state_scope", default="trial")


class WorkerRuntime:
    def __init__(self, peer: Peer, defaults: dict, package: Path):
        self.peer = peer
        self.defaults = defaults
        self.package = package
        self.modules: dict[str, object] = {}
        self.hooks: dict[str, object] = {}
        self._trial_state: dict = {}
        self._session_states: dict[str, dict] = {}

    @property
    def state(self) -> dict:
        session_id = _PARENT.get()
        if session_id is not None and _STATE_SCOPE.get() == "session":
            return self._session_states.setdefault(session_id, {})
        return self._trial_state

    async def run_harness(self, *, input: str, **kwargs) -> HarnessResult:
        if not isinstance(input, str) or not input.strip():
            raise ValueError("Harness input must be nonempty text")
        merged = {**self.defaults, **kwargs}
        hooks = {}
        for name in ("pre_tool", "post_tool"):
            callback = merged.pop(name, None)
            if isinstance(callback, str):
                filename, function = callback.split(":")
                if filename not in self.modules:
                    spec = importlib.util.spec_from_file_location(
                        f"_defense_callback_{len(self.modules)}", self.package / filename,
                    )
                    if spec is None or spec.loader is None:
                        raise ValueError("Cannot load static program callback")
                    module = importlib.util.module_from_spec(spec)
                    sys.modules[spec.name] = module
                    spec.loader.exec_module(module)
                    self.modules[filename] = module
                callback = getattr(self.modules[filename], function)
            if callback is not None:
                if not callable(callback) or not inspect.iscoroutinefunction(callback):
                    raise ValueError(f"{name} must be an async callable")
                hook_id = f"hook-{len(self.hooks) + 1}"
                self.hooks[hook_id] = callback
                hooks[name] = hook_id
        value = await self.peer.request("run_harness", {
            "input": input, "kwargs": merged, "hooks": hooks, "parent_id": _PARENT.get(),
        })
        if not isinstance(value, dict) or set(value) != {"text", "completed", "session_id", "usage"}:
            raise RPCError("Invalid harness result")
        return HarnessResult(**value)

    async def hook(self, params: dict):
        if set(params) != {"hook_id", "kind", "call", "observation", "state_scope"}:
            raise ValueError("Invalid hook request")
        callback = self.hooks.get(params["hook_id"])
        if callback is None:
            raise ValueError("Unknown hook")
        call = ToolCall(**params["call"])
        token = _PARENT.set(call.session_id)
        state_token = _STATE_SCOPE.set(params["state_scope"])
        try:
            if params["kind"] == "pre_tool":
                decision = await callback(call)
                if isinstance(decision, Allow):
                    return {"allowed": True, "reason": ""}
                if isinstance(decision, Deny) and isinstance(decision.reason, str):
                    return {"allowed": False, "reason": decision.reason}
                raise ValueError("Pre-tool hook must return Allow or Deny")
            if params["kind"] == "post_tool":
                observation = ToolObservation(**params["observation"])
                result = await callback(call, observation)
                if not isinstance(result, ToolObservation):
                    raise ValueError("Post-tool hook must return ToolObservation")
                return dataclasses.asdict(result)
            raise ValueError("Unknown hook operation")
        finally:
            _PARENT.reset(token)
            _STATE_SCOPE.reset(state_token)


async def main() -> None:
    protocol_output = sys.stdout.buffer
    # Candidate print/log calls must not corrupt protocol frames.
    sys.stdout = sys.stderr
    runtime = None
    started = False

    async def read():
        return await asyncio.to_thread(sys.stdin.buffer.readline, MAX_MESSAGE_BYTES + 1)

    async def write(raw):
        protocol_output.write(raw)
        protocol_output.flush()

    async def handle(method, params):
        nonlocal runtime, started
        if method == "hook":
            if runtime is None:
                raise ValueError("Program not initialized")
            return await runtime.hook(params)
        if method != "start" or started:
            raise ValueError("Unknown or duplicate program method")
        started = True
        if set(params) != {"package", "entrypoint", "task", "kwargs"}:
            raise ValueError("Invalid program start request")
        package = Path(params["package"])
        filename, function_name = params["entrypoint"].split(":")
        sys.path.insert(0, str(package))
        spec = importlib.util.spec_from_file_location("_candidate_defense", package / filename)
        if spec is None or spec.loader is None:
            raise ValueError("Cannot load program entrypoint")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        function = getattr(module, function_name)
        if not inspect.iscoroutinefunction(function):
            raise ValueError("Program entrypoint must be async")
        runtime = WorkerRuntime(peer, params["kwargs"], package)
        task = SolverInput(
            instruction=params["task"]["instruction"],
            tools=tuple(params["task"]["tools"]),
        )
        result = await function(task, runtime)
        if not isinstance(result, SolverOutput) or not isinstance(result.answer, str):
            raise ValueError("Program must return SolverOutput(answer=str)")
        return {"answer": result.answer}

    peer = Peer(read, write, handle, prefix="worker")
    await peer.run()


if __name__ == "__main__":
    asyncio.run(main())
