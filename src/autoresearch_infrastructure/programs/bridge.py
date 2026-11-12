"""Trusted bridge between isolated candidate Python and real harness sessions.

Only this module holds the environment executor. Candidates communicate through bounded
stdio RPC and never receive tool-service tokens, credentials or host state.
"""
from __future__ import annotations

import asyncio
import dataclasses
import hashlib
import json
import math
import os
import shutil
import tempfile
import time
import traceback
from pathlib import Path
from uuid import uuid4

from autoresearch_infrastructure.persistence import write_json

from .api import SolverInput, ToolCall, ToolObservation
from .package import program_manifest, validate_program
from .wire import MAX_MESSAGE_BYTES, Peer, RPCError

WORKER_CONTAINER_REMOVE_TIMEOUT = 30


class ProgramExecutionError(RuntimeError):
    pass


def _public_observation(raw, call_id: str) -> dict:
    if dataclasses.is_dataclass(raw):
        raw = dataclasses.asdict(raw)
    elif hasattr(raw, "model_dump"):
        raw = raw.model_dump(mode="json")
    if not isinstance(raw, dict):
        raise ProgramExecutionError("Tool executor must return a public result record")
    value = {
        "call_id": call_id, "result": raw.get("result"),
        "error": raw.get("error"), "denied": raw.get("denied", False),
    }
    if value["error"] is not None and not isinstance(value["error"], str):
        raise ProgramExecutionError("Invalid public tool error")
    if type(value["denied"]) is not bool:
        raise ProgramExecutionError("Invalid public tool denial")
    return value


class ProgramBridge:
    def __init__(
        self,
        package: Path,
        program_id: str,
        harness: str,
        model: str,
        task: SolverInput | dict,
        run_session,
        execute_tool,
        evidence_dir: Path,
        timeout_seconds: float,
        max_sessions: int = 32,
        max_depth: int = 2,
        hook_timeout_seconds: float = 30,
        max_tool_calls: int = 512,
        manifest_name: str = "program.toml",
        evidence_name: str = "program-runtime.json",
    ):
        validate_program(package, program_id, harness=harness, manifest_name=manifest_name)
        if isinstance(task, dict):
            if set(task) != {"instruction", "tools"}:
                raise ValueError("Only public instruction and tool schemas are program inputs")
            task = SolverInput(task["instruction"], tuple(task["tools"]))
        if not isinstance(task, SolverInput) or not isinstance(task.instruction, str):
            raise ValueError("Program requires public SolverInput")
        if (
            type(timeout_seconds) not in (int, float) or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0 or type(max_sessions) is not int or not 1 <= max_sessions <= 128
            or type(max_depth) is not int or not 0 <= max_depth <= 4
            or type(max_tool_calls) is not int or not 1 <= max_tool_calls <= 10_000
            or type(hook_timeout_seconds) not in (int, float)
            or not math.isfinite(hook_timeout_seconds) or hook_timeout_seconds <= 0
        ):
            raise ValueError("Invalid frozen program runtime limits")
        self.package, self.harness, self.model = package, harness, model
        if Path(evidence_name).name != evidence_name:
            raise ValueError("Evidence name must be a filename")
        self.evidence_name = evidence_name
        self.manifest = program_manifest(package, program_id, manifest_name=manifest_name)
        self.task, self.run_session, self.execute_tool = task, run_session, execute_tool
        self.evidence_dir = evidence_dir
        self.timeout_seconds = float(timeout_seconds)
        self.hook_timeout_seconds = min(float(hook_timeout_seconds), self.timeout_seconds)
        self.max_sessions, self.max_depth, self.max_tool_calls = max_sessions, max_depth, max_tool_calls
        self.sessions: list[dict] = []
        self.by_session: dict[str, dict] = {}
        self.events: list[dict] = []
        self.tool_calls = 0
        self.native_pending: dict[tuple[str, str], dict] = {}
        self.native_seen: set[tuple[str, str]] = set()
        self.peer: Peer | None = None
        self.deadline = 0.0
        self.action_lock = asyncio.Lock()
        self.failed: str | None = None
        self.container_name: str | None = None
        self.process = None
        self.stderr_bytes = 0
        self.stderr_truncated = False
        self.worker_cleanup_error: str | None = None

    def _remaining(self) -> float:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ProgramExecutionError("Program trial deadline exceeded")
        if self.failed:
            raise ProgramExecutionError(self.failed)
        return remaining

    def _save(self) -> None:
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        write_json(self.evidence_dir / self.evidence_name, {
            "harness": self.harness, "model": self.model,
            "sessions": self.sessions, "events": self.events,
            "sessions_started": len(self.sessions), "tool_calls": self.tool_calls,
            "failure": self.failed,
            "worker_stderr_bytes": self.stderr_bytes,
            "worker_stderr_truncated": self.stderr_truncated,
            "worker_cleanup_error": self.worker_cleanup_error,
            "usage": self._usage(),
        })

    async def _dispatch(self, method: str, params: dict):
        if method != "run_harness" or set(params) != {"input", "kwargs", "hooks", "parent_id"}:
            raise ProgramExecutionError("Unsupported program runtime operation")
        self._remaining()
        if not isinstance(params["input"], str) or not params["input"].strip():
            raise ProgramExecutionError("Harness input must be nonempty text")
        hooks = params["hooks"]
        if (
            not isinstance(hooks, dict) or set(hooks) - {"pre_tool", "post_tool"}
            or not all(isinstance(value, str) and 0 < len(value) <= 100 for value in hooks.values())
        ):
            raise ProgramExecutionError("Invalid hook references")
        from autoresearch_infrastructure.harnesses.settings import validate_kwargs

        settings = validate_kwargs(self.harness, params["kwargs"], package_root=self.package)
        if any(settings.get(key) is not None for key in ("pre_tool", "post_tool")):
            raise ProgramExecutionError("Callbacks must use worker-registered references")
        if len(self.sessions) >= self.max_sessions:
            raise ProgramExecutionError("Program harness-session ceiling exhausted")
        parent_id = params["parent_id"]
        if parent_id is not None:
            parent = self.by_session.get(parent_id)
            if parent is None or parent["status"] != "running":
                raise ProgramExecutionError("Unknown or inactive parent harness session")
            policy = parent["settings"].get("subagents", {})
            depth = parent["depth"] + 1
            if not policy.get("enabled", False) or depth > policy.get("max_depth", 0):
                raise ProgramExecutionError("Parent session does not permit this helper depth")
            if depth > self.max_depth:
                raise ProgramExecutionError("Frozen helper depth exhausted")
            settings.setdefault("tools", {"allow": []})
        else:
            depth = 0
        tool_names = {tool["name"] for tool in self.task.tools}
        allowed = settings.get("tools", {"allow": sorted(tool_names)})["allow"]
        if not set(allowed) <= tool_names:
            raise ProgramExecutionError("Session requests tools outside the frozen task")
        if parent_id is not None and allowed:
            # First implementation deliberately avoids recursive effect graphs.
            raise ProgramExecutionError("Nested helper sessions must be tool-free")
        # Admission is synchronous through record allocation: startup and teardown
        # occupy a slot, and no queue can deadlock a root awaiting its helper.
        active = [item for item in self.sessions if item["status"] == "running"]
        if parent_id is None and any(item["parent_id"] is None for item in active):
            raise ProgramExecutionError("Concurrent root harness sessions are unsupported")
        if len(active) >= 1 + self.max_depth:
            raise ProgramExecutionError("Active harness-session capacity exhausted")
        settings["tools"] = {"allow": list(allowed)}
        settings["timeout_seconds"] = min(settings.get("timeout_seconds", self._remaining()), self._remaining())
        session_id = f"session-{len(self.sessions) + 1:04d}"
        record = {
            "session_id": session_id, "parent_id": parent_id, "depth": depth,
            "harness": self.harness, "model": self.model,
            "input": params["input"], "settings": settings, "hooks": hooks,
            "status": "running", "usage": {},
        }
        self.sessions.append(record)
        self.by_session[session_id] = record
        self._save()
        async def tool_handler(raw):
            # Complete check/effect/result sequences serialize across the trial.
            # Nested helpers are tool-free and cannot reenter this lock.
            async with self.action_lock:
                return await self._tool(record, raw)

        native_lock = asyncio.Lock()

        async def native_handler(raw):
            # A parent policy may await a helper loading its own selected skill.
            # Those read-only calls cannot reacquire the parent's effect lock.
            async with native_lock:
                if parent_id is not None:
                    return await self._native_tool(record, raw)
                async with self.action_lock:
                    return await self._native_tool(record, raw)

        try:
            native = asyncio.create_task(self.run_session(
                session_id=session_id, input=params["input"], settings=settings,
                tool_handler=tool_handler, parent_id=parent_id, native_handler=native_handler,
            ))
            try:
                result = await asyncio.wait_for(asyncio.shield(native), timeout=settings["timeout_seconds"])
            except BaseException:
                native.cancel()
                escalated = False
                # Keep the record/slot until native teardown ends. Repeated RPC
                # cancellation must not abandon wait_for's inner native task.
                while not native.done():
                    try:
                        await asyncio.shield(native)
                    except asyncio.CancelledError:
                        if not native.done() and not escalated:
                            # Interrupt slow stop/download once; further cancels
                            # must leave the adapter's final container removal alone.
                            native.cancel()
                            escalated = True
                        continue
                    except BaseException:
                        break
                if not native.cancelled() and native.exception() is not None:
                    raise native.exception()
                raise
            if (
                not isinstance(result, dict) or not isinstance(result.get("text"), str)
                or type(result.get("completed")) is not bool
                or not isinstance(result.get("usage", {}), dict)
            ):
                raise ProgramExecutionError("Harness returned a malformed result")
            record["status"] = "completed" if result["completed"] else "incomplete"
            record["usage"] = result.get("usage", {})
            record["text"] = result["text"]
            if "trajectory" in result:
                record["trajectory"] = result["trajectory"]
            if any(key[0] == session_id for key in self.native_pending):
                raise ProgramExecutionError("Native tool has no matching completion hook")
            self._remaining()
            self._save()
            return {
                "text": result["text"], "completed": result["completed"],
                "session_id": session_id, "usage": record["usage"],
            }
        except BaseException as exc:
            # Only the trusted executor's typed failure may contribute partial
            # usage/evidence. It cannot override status or admit a failed run.
            if hasattr(exc, "session_result"):
                from autoresearch_infrastructure.harnesses.sessions import HarnessSessionError

                if isinstance(exc, HarnessSessionError):
                    self._retain_partial(record, exc.session_result)
                    if getattr(exc, "cleanup_failure", None) == "session_container_cleanup_failed":
                        record["cleanup_failure"] = "session_container_cleanup_failed"
            record["status"] = "interrupted" if isinstance(exc, asyncio.CancelledError) else "failed"
            record["error"] = str(exc)[:2000] if isinstance(exc, ProgramExecutionError) else type(exc).__name__
            # Retain diagnostic locations without exception text, command arguments,
            # or locals, which may contain transport credentials.
            record["error_context"] = {
                "errno": exc.errno if isinstance(exc, OSError) else None,
                "frames": [
                    {"file": Path(frame.filename).name, "function": frame.name, "line": frame.lineno}
                    for frame in traceback.extract_tb(exc.__traceback__)[-12:]
                ],
            }
            self.failed = f"Harness session {session_id} failed: {type(exc).__name__}"
            self._save()
            if isinstance(exc, (asyncio.CancelledError, ProgramExecutionError)):
                raise
            raise ProgramExecutionError(self.failed) from exc

    def _retain_partial(self, record: dict, partial) -> None:
        if not isinstance(partial, dict) or partial.get("completed") is not False:
            return
        usage = partial.get("usage")
        if isinstance(usage, dict):
            try:
                raw = json.dumps(usage, allow_nan=False).encode("utf-8")
                if len(raw) <= 100_000:
                    record["usage"] = json.loads(raw)
            except (TypeError, ValueError):
                pass
        text = partial.get("text")
        if isinstance(text, str):
            record["text"] = text[:100_000]
        descriptor = partial.get("trajectory")
        if not isinstance(descriptor, dict):
            return
        expected = self.evidence_dir.parent / "sessions" / record["session_id"] / "trajectory.json"
        if (
            descriptor.get("session_id") != record["session_id"]
            or descriptor.get("parent_id") != record["parent_id"]
            or descriptor.get("format") != "ATIF"
            or not isinstance(descriptor.get("path"), str)
            or Path(descriptor["path"]).absolute() != expected.absolute()
        ):
            return
        try:
            current = expected.absolute()
            root = self.evidence_dir.parent.absolute()
            while current != root:
                if current.is_symlink():
                    return
                current = current.parent
            if (not expected.is_file() or expected.stat().st_nlink != 1
                    or expected.stat().st_size > 50_000_000):
                return
            data = expected.read_bytes()
            if descriptor.get("sha256") != hashlib.sha256(data).hexdigest():
                return
            document = json.loads(data)
            if document.get("agent", {}).get("name") != self.harness:
                return
        except (OSError, ValueError, AttributeError):
            return
        record["trajectory"] = {
            key: descriptor[key] for key in ("path", "sha256", "session_id", "parent_id", "format")
        }

    async def _hook(self, record: dict, kind: str, call: dict, observation=None):
        hook_id = record["hooks"].get(kind)
        if hook_id is None:
            return None
        if self.peer is None:
            raise ProgramExecutionError("Program worker is unavailable")
        limit = min(
            record["settings"].get("hook_timeout_seconds", self.hook_timeout_seconds),
            self.hook_timeout_seconds, self._remaining(),
        )
        try:
            # The action lock serializes callbacks across the complete pipeline.
            # Task-tool-free helpers can run while this callback awaits inference.
            return await asyncio.wait_for(self.peer.request("hook", {
                "hook_id": hook_id, "kind": kind,
                "call": call, "observation": observation,
                "state_scope": record["settings"].get("state_scope", "session"),
            }), timeout=limit)
        except BaseException as exc:
            self.failed = f"Program {kind} failed: {type(exc).__name__}"
            self._save()
            raise ProgramExecutionError(self.failed) from exc

    async def _tool(self, record: dict, raw: dict) -> dict:
        self._remaining()
        if record["status"] != "running":
            raise ProgramExecutionError("Harness session is no longer active")
        if (
            not isinstance(raw, dict) or set(raw) - {"name", "args", "id", "session_id"}
            or not isinstance(raw.get("name"), str) or not isinstance(raw.get("args", {}), dict)
        ):
            raise ProgramExecutionError("Malformed public tool call")
        if raw["name"] not in record["settings"]["tools"]["allow"]:
            raise ProgramExecutionError("Tool is outside the session allowlist")
        if self.tool_calls >= self.max_tool_calls:
            raise ProgramExecutionError("Program tool-call ceiling exhausted")
        self.tool_calls += 1
        call = dataclasses.asdict(ToolCall(
            name=raw["name"], args=raw.get("args", {}),
            id=f"call-{self.tool_calls:05d}", session_id=record["session_id"],
        ))
        event = {"call": call, "status": "pending"}
        self.events.append(event)
        self._save()
        denied = await self._before_tool(record, event)
        if denied is not None:
            return denied
        # The action lock also protects authoritative effects. No other path
        # executes effects, and helpers cannot reenter this task-tool pipeline.
        self._remaining()
        observation = _public_observation(await self.execute_tool(call), call["id"])
        event["raw"] = observation
        self._save()
        return await self._after_tool(record, event, observation)

    async def _before_tool(self, record, event):
        call = event["call"]
        decision = await self._hook(record, "pre_tool", call)
        if decision is not None:
            if (
                not isinstance(decision, dict) or set(decision) != {"allowed", "reason"}
                or type(decision["allowed"]) is not bool
                or not isinstance(decision["reason"], str) or len(decision["reason"]) > 2000
            ):
                self.failed = "Malformed pre-tool decision"
                self._save()
                raise ProgramExecutionError(self.failed)
            event["decision"] = decision
            if not decision["allowed"]:
                result = dataclasses.asdict(ToolObservation(
                    call_id=call["id"], error=decision["reason"], denied=True,
                ))
                event.update(status="denied", raw=result, visible=result)
                self._save()
                return result
        return None

    async def _after_tool(self, record, event, observation):
        call = event["call"]
        transformed = await self._hook(record, "post_tool", call, observation)
        if transformed is None:
            transformed = observation
        if (
            not isinstance(transformed, dict)
            or set(transformed) != {"call_id", "result", "error", "denied"}
            or transformed["call_id"] != call["id"]
            or type(transformed["denied"]) is not bool
            or transformed["denied"] != observation["denied"]
            or transformed["error"] != observation["error"]
        ):
            self.failed = "Post-tool hook changed protected result identity or status"
            self._save()
            raise ProgramExecutionError(self.failed)
        event.update(status="completed", visible=transformed)
        self._save()
        return transformed

    async def _native_tool(self, record, raw):
        from autoresearch_infrastructure.harnesses.native_hooks import (
            native_request,
            validate_native_transform,
        )

        try:
            self._remaining()
            raw = native_request(raw, self.harness)
            if record["status"] != "running" or not record["settings"].get("skills"):
                raise ValueError("Session has no permitted native skill tools")
            key = (record["session_id"], raw["id"])
            if raw["stage"] == "pre":
                if key in self.native_seen or self.tool_calls >= self.max_tool_calls:
                    raise ValueError("Repeated native call or exhausted tool-call ceiling")
                self.native_seen.add(key)
                self.tool_calls += 1
                call = dataclasses.asdict(ToolCall(
                    name=raw["name"], args=raw["args"], id=f"call-{self.tool_calls:05d}",
                    session_id=record["session_id"], origin="native",
                ))
                event = {"call": call, "native_id": raw["id"], "status": "pending"}
                self.events.append(event)
                self._save()
                denied = await self._before_tool(record, event)
                if denied is not None:
                    return {"allowed": False, "reason": denied["error"]}
                self.native_pending[key] = event
                return {"allowed": True, "reason": ""}
            event = self.native_pending.pop(key, None)
            if event is None or event["call"]["name"] != raw["name"] or event["call"]["args"] != raw["args"]:
                raise ValueError("Unmatched native completion hook")
            observation = dataclasses.asdict(ToolObservation(
                call_id=event["call"]["id"], result=raw.get("result"), error=raw.get("error"),
            ))
            event["raw"] = observation
            self._save()
            transformed = await self._after_tool(record, event, observation)
            try:
                validate_native_transform(self.harness, observation["result"], transformed["result"])
            except ValueError:
                event.update(status="failed", requested=transformed, failure="unsupported_native_transformation")
                event.pop("visible", None)
                raise
            return {"result": transformed["result"]}
        except Exception as exc:
            self.failed = "Native program hook failed: " + type(exc).__name__
            self._save()
            raise ProgramExecutionError(self.failed) from exc

    def _worker_bundle(self, root: Path) -> Path:
        destination = root / "runtime"
        package = destination / "autoresearch_infrastructure" / "programs"
        package.mkdir(parents=True)
        (destination / "autoresearch_infrastructure" / "__init__.py").write_text("")
        for name in ("__init__.py", "api.py", "parsing.py", "wire.py", "worker.py"):
            shutil.copyfile(Path(__file__).with_name(name), package / name)
        return destination

    async def run(self, image: str) -> dict:
        """Execute candidate code only inside a network-disabled disposable container."""
        if not isinstance(image, str) or not image or image.startswith("-"):
            raise ValueError("Program execution requires a frozen container image")
        self.container_name = f"ari-program-{uuid4().hex}"
        with tempfile.TemporaryDirectory(prefix="defense-runtime-") as temporary:
            bundle = self._worker_bundle(Path(temporary))
            command = [
                # Explicit cleanup below owns removal. Docker's --rm can race
                # its verification while an exited worker is being destroyed.
                "docker", "run", "--interactive", "--init", "--pull=never",
                "--name", self.container_name, "--network=none", "--read-only",
                "--cap-drop=ALL", "--security-opt=no-new-privileges",
                "--pids-limit=128", "--memory=512m", "--cpus=1",
                "--user", f"{os.getuid()}:{os.getgid()}",
                "--tmpfs=/tmp:rw,noexec,nosuid,size=64m",
                "--mount", f"type=bind,src={self.package.resolve()},dst=/candidate,readonly",
                "--mount", f"type=bind,src={bundle.resolve()},dst=/opt/runtime,readonly",
                "--workdir=/candidate", "--env=PYTHONPATH=/opt/runtime",
                "--env=PYTHONDONTWRITEBYTECODE=1",
                image, "python", "-m", "autoresearch_infrastructure.programs.worker",
            ]
            try:
                return await self._run_process(command, "/candidate")
            finally:
                if not await self._cleanup_worker():
                    self.failed = self.failed or "Program worker cleanup failed"
                    self._save()
                    raise ProgramExecutionError("Program worker cleanup failed")

    async def _cleanup_worker(self) -> bool:
        """Verify teardown without persisting Docker messages or exception text."""
        try:
            cleanup = await asyncio.create_subprocess_exec(
                "docker", "rm", "--force", self.container_name,
                stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                await asyncio.wait_for(
                    cleanup.wait(), timeout=WORKER_CONTAINER_REMOVE_TIMEOUT,
                )
            except TimeoutError:
                cleanup.kill()
                await cleanup.wait()
                self.worker_cleanup_error = "worker_container_remove_timeout"
                return False
            if cleanup.returncode == 0:
                return True
            # --rm may already have removed the worker when its client exited.
            # A successful listing distinguishes absence from an unreachable daemon.
            probe = await asyncio.create_subprocess_exec(
                "docker", "container", "ls", "--all", "--filter",
                "name=" + self.container_name, "--format", "{{.ID}}",
                stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL,
            )
            try:
                output, _ = await asyncio.wait_for(probe.communicate(), timeout=10)
            except TimeoutError:
                probe.kill()
                await probe.wait()
                self.worker_cleanup_error = "worker_container_verification_timeout"
                return False
            if probe.returncode == 0 and not output.strip():
                return True
            self.worker_cleanup_error = "worker_container_remove_failed"
        except OSError:
            self.worker_cleanup_error = "worker_container_cleanup_unavailable"
        return False

    async def _run_process(self, command: list[str], worker_package: str) -> dict:
        """Transport implementation; tests inject a fixture subprocess, never a live plan."""
        self.deadline = time.monotonic() + self.timeout_seconds
        self.evidence_dir.mkdir(parents=True, exist_ok=True)
        process = await asyncio.create_subprocess_exec(
            *command, stdin=asyncio.subprocess.PIPE, stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE, limit=MAX_MESSAGE_BYTES + 1,
        )
        self.process = process

        async def write(raw):
            process.stdin.write(raw)
            await process.stdin.drain()

        async def read():
            return await process.stdout.readline()

        async def stderr():
            count = 0
            with (self.evidence_dir / "worker-stderr.log").open("wb") as stream:
                while raw := await process.stderr.read(65536):
                    self.stderr_bytes += len(raw)
                    self.stderr_truncated = self.stderr_bytes > 2_000_000
                    keep = raw[:max(0, 2_000_000 - count)]
                    stream.write(keep)
                    count += len(keep)

        self.peer = Peer(read, write, self._dispatch, prefix="host")
        reader = asyncio.create_task(self.peer.run())
        errors = asyncio.create_task(stderr())
        try:
            result = await asyncio.wait_for(self.peer.request("start", {
                "package": worker_package, "entrypoint": self.manifest["entrypoint"],
                "task": dataclasses.asdict(self.task), "kwargs": self.manifest.get("kwargs", {}),
            }), timeout=self.timeout_seconds)
            if self.failed:
                raise ProgramExecutionError(self.failed)
            if not isinstance(result, dict) or set(result) != {"answer"} or not isinstance(result["answer"], str):
                raise ProgramExecutionError("Program returned a malformed solver output")
            if any(record["status"] != "completed" for record in self.sessions):
                raise ProgramExecutionError("Program returned with incomplete harness evidence")
            return {"answer": result["answer"], "sessions": self.sessions,
                    "events": self.events, "usage": self._usage()}
        except BaseException as exc:
            self.failed = self.failed or f"Candidate program failed: {type(exc).__name__}"
            raise ProgramExecutionError(str(exc)) from exc
        finally:
            # Stop candidate computation immediately, before waiting for native-session
            # cancellation/download cleanup. Closing RPC also prevents any new calls.
            self.peer.closed = True
            if process.returncode is None:
                process.kill()
            await process.wait()
            cancellation_failed = False
            try:
                from autoresearch_infrastructure.harnesses.sessions import SESSION_CANCELLATION_TIMEOUT

                await self.peer.close(cleanup_timeout=SESSION_CANCELLATION_TIMEOUT)
            except RPCError:
                cancellation_failed = True
                self.failed = self.failed or "Harness session teardown unconfirmed"
            finally:
                reader.cancel()
                await asyncio.gather(reader, return_exceptions=True)
                await errors
                for record in self.sessions:
                    if record["status"] == "running":
                        record["status"] = "interrupted"
                        if cancellation_failed:
                            record["cleanup_failure"] = "session_teardown_unconfirmed"
                self._save()
            if cancellation_failed:
                raise ProgramExecutionError("Harness session teardown unconfirmed")

    def _usage(self) -> dict:
        costs = []
        for session in self.sessions:
            usage = session.get("usage", {})
            price = usage.get("cost_usd", usage.get("total_cost_usd"))
            if type(price) in (int, float) and math.isfinite(price) and price >= 0:
                costs.append(price)
        totals = {}
        coverage = {}
        for name in ("n_input_tokens", "n_output_tokens", "n_cache_tokens"):
            values = [session.get("usage", {}).get(name) for session in self.sessions]
            observed = [value for value in values if type(value) is int and value >= 0]
            totals[name] = sum(observed) if observed and len(observed) == len(self.sessions) else None
            totals["observed_" + name] = sum(observed) if observed else None
            coverage[name] = len(observed)
        # A subtotal cannot stand in for a complete cost or efficiency measurement.
        # Keep the known part for accounting without concealing missing telemetry.
        return {"sessions_started": len(self.sessions), "sessions_with_cost": len(costs),
                "cost_usd": sum(costs) if costs and len(costs) == len(self.sessions) else None,
                "observed_cost_usd": sum(costs) if costs else None, "tool_calls": self.tool_calls,
                **totals, "sessions_with_token_usage": coverage}
