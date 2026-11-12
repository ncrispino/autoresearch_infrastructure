"""Reproducible agent-and-environment forks at settled tool boundaries.

Harbor distinguishes resuming a live agent from loading a native or ATIF
trajectory. This module uses the same capability names, while keeping the
environment snapshot separate: loading a conversation does not restore files,
services, or other task state.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any, ClassVar, Literal, Mapping, Protocol, TypeVar, runtime_checkable

TrajectoryFormat = Literal["native", "atif"]
_Result = TypeVar("_Result")


def _encoded_object(value: Mapping[str, Any], label: str) -> str:
    if not isinstance(value, Mapping):
        raise ValueError(f"{label} must be a JSON object")
    try:
        encoded = json.dumps(dict(value), sort_keys=True, separators=(",", ":"), allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{label} must be a JSON object") from exc
    decoded = json.loads(encoded)
    if not isinstance(decoded, dict) or decoded != dict(value):
        raise ValueError(f"{label} must be a JSON object")
    return encoded


def _decoded_object(value: str) -> dict[str, Any]:
    return json.loads(value)


@dataclass(frozen=True)
class AgentContinuationCapabilities:
    """Harbor-compatible agent continuation capability flags."""

    resume: bool = False
    load_native_trajectory: bool = False
    load_atif_trajectory: bool = False

    def __post_init__(self) -> None:
        if any(type(value) is not bool for value in (
            self.resume, self.load_native_trajectory, self.load_atif_trajectory,
        )):
            raise ValueError("Continuation capabilities must be booleans")

    def supports_load(self, trajectory_format: TrajectoryFormat) -> bool:
        if trajectory_format == "native":
            return self.load_native_trajectory
        if trajectory_format == "atif":
            return self.load_atif_trajectory
        raise ValueError("Trajectory format must be native or atif")


@dataclass(frozen=True, init=False)
class ContinuationPoint:
    """A content-addressed agent/environment snapshot after N settled calls."""

    FORMAT_VERSION: ClassVar[int] = 1

    source_id: str
    after_tool_call: int
    trajectory_format: TrajectoryFormat
    _provenance: str
    _agent_identity: str
    _agent_state: str
    _environment_identity: str
    _environment_state: str

    def __init__(
        self,
        *,
        source_id: str,
        after_tool_call: int,
        trajectory_format: TrajectoryFormat,
        provenance: Mapping[str, Any],
        agent_identity: Mapping[str, Any],
        agent_state: Mapping[str, Any],
        environment_identity: Mapping[str, Any],
        environment_state: Mapping[str, Any],
    ) -> None:
        if not isinstance(source_id, str) or not source_id.strip():
            raise ValueError("Continuation source_id must be nonempty text")
        if type(after_tool_call) is not int or after_tool_call < 0:
            raise ValueError("after_tool_call must be a nonnegative integer")
        if not isinstance(trajectory_format, str) or trajectory_format not in {"native", "atif"}:
            raise ValueError("Trajectory format must be native or atif")
        object.__setattr__(self, "source_id", source_id)
        object.__setattr__(self, "after_tool_call", after_tool_call)
        object.__setattr__(self, "trajectory_format", trajectory_format)
        object.__setattr__(self, "_provenance", _encoded_object(provenance, "Continuation provenance"))
        object.__setattr__(self, "_agent_identity", _encoded_object(agent_identity, "Agent identity"))
        object.__setattr__(self, "_agent_state", _encoded_object(agent_state, "Agent state"))
        object.__setattr__(
            self, "_environment_identity", _encoded_object(environment_identity, "Environment identity")
        )
        object.__setattr__(
            self, "_environment_state", _encoded_object(environment_state, "Environment state")
        )

    @property
    def provenance(self) -> dict[str, Any]:
        return _decoded_object(self._provenance)

    @property
    def agent_identity(self) -> dict[str, Any]:
        return _decoded_object(self._agent_identity)

    @property
    def agent_state(self) -> dict[str, Any]:
        return _decoded_object(self._agent_state)

    @property
    def environment_identity(self) -> dict[str, Any]:
        return _decoded_object(self._environment_identity)

    @property
    def environment_state(self) -> dict[str, Any]:
        return _decoded_object(self._environment_state)

    def _content(self) -> dict[str, Any]:
        return {
            "format_version": self.FORMAT_VERSION,
            "source_id": self.source_id,
            "after_tool_call": self.after_tool_call,
            "provenance": self.provenance,
            "agent": {
                "trajectory_format": self.trajectory_format,
                "identity": self.agent_identity,
                "state": self.agent_state,
            },
            "environment": {
                "identity": self.environment_identity,
                "state": self.environment_state,
            },
        }

    @property
    def continuation_id(self) -> str:
        encoded = json.dumps(self._content(), sort_keys=True, separators=(",", ":")).encode()
        return "sha256:" + hashlib.sha256(encoded).hexdigest()

    def to_json_dict(self) -> dict[str, Any]:
        return {"continuation_id": self.continuation_id, **self._content()}

    @classmethod
    def from_json_dict(cls, value: Mapping[str, Any]) -> "ContinuationPoint":
        if not isinstance(value, Mapping) or set(value) != {
            "continuation_id", "format_version", "source_id", "after_tool_call", "provenance", "agent",
            "environment",
        }:
            raise ValueError("Invalid continuation point fields")
        if type(value["format_version"]) is not int or value["format_version"] != cls.FORMAT_VERSION:
            raise ValueError("Unsupported continuation point format")
        agent, environment = value["agent"], value["environment"]
        if not isinstance(agent, Mapping) or set(agent) != {"trajectory_format", "identity", "state"}:
            raise ValueError("Invalid agent continuation fields")
        if not isinstance(environment, Mapping) or set(environment) != {"identity", "state"}:
            raise ValueError("Invalid environment continuation fields")
        point = cls(
            source_id=value["source_id"],
            after_tool_call=value["after_tool_call"],
            trajectory_format=agent["trajectory_format"],
            provenance=value["provenance"],
            agent_identity=agent["identity"],
            agent_state=agent["state"],
            environment_identity=environment["identity"],
            environment_state=environment["state"],
        )
        if value["continuation_id"] != point.continuation_id:
            raise ValueError("Continuation point content hash does not match")
        return point


@runtime_checkable
class AgentContinuationAdapter(Protocol[_Result]):
    """Captures and loads one agent's trajectory state."""

    capabilities: AgentContinuationCapabilities

    @property
    def identity(self) -> Mapping[str, Any]: ...

    async def snapshot(self, trajectory_format: TrajectoryFormat) -> Mapping[str, Any]: ...

    async def load(
        self,
        *,
        instruction: str,
        trajectory_format: TrajectoryFormat,
        state: Mapping[str, Any],
    ) -> _Result: ...


@runtime_checkable
class EnvironmentContinuationAdapter(Protocol):
    """Captures and restores task state independently of the agent."""

    @property
    def identity(self) -> Mapping[str, Any]: ...

    async def snapshot(self) -> Mapping[str, Any]: ...

    async def restore(self, state: Mapping[str, Any]) -> None: ...


class ContinuationRestoreError(RuntimeError):
    """Restoring one side of a continuation point failed."""

    def __init__(self, stage: Literal["environment", "agent"], *, environment_restored: bool):
        super().__init__(f"Continuation {stage} restore failed")
        self.stage = stage
        self.environment_restored = environment_restored


def _adapter_identity(adapter: object, label: str) -> dict[str, Any]:
    try:
        identity = getattr(adapter, "identity")
    except (AttributeError, TypeError) as exc:
        raise ValueError(f"{label} adapter has no identity") from exc
    return _decoded_object(_encoded_object(identity, f"{label} identity"))


def _require_load(agent: AgentContinuationAdapter[Any], trajectory_format: TrajectoryFormat) -> None:
    capabilities = getattr(agent, "capabilities", None)
    if not isinstance(capabilities, AgentContinuationCapabilities):
        raise ValueError("Agent adapter must declare continuation capabilities")
    if not capabilities.supports_load(trajectory_format):
        name = "load_native_trajectory" if trajectory_format == "native" else "load_atif_trajectory"
        raise ValueError(f"Agent adapter does not support {name}")


async def capture_continuation(
    *,
    source_id: str,
    after_tool_call: int,
    trajectory_format: TrajectoryFormat,
    provenance: Mapping[str, Any],
    agent: AgentContinuationAdapter[Any],
    environment: EnvironmentContinuationAdapter,
) -> ContinuationPoint:
    """Capture a fork after all effects and observations for one tool call settle.

    The caller owns quiescence: no tool execution may overlap this function.
    Capture is deliberately sequential so an adapter can make that boundary
    durable before the next snapshot is requested.
    """

    _require_load(agent, trajectory_format)
    agent_identity = _adapter_identity(agent, "Agent")
    environment_identity = _adapter_identity(environment, "Environment")
    environment_state = await environment.snapshot()
    agent_state = await agent.snapshot(trajectory_format)
    return ContinuationPoint(
        source_id=source_id,
        after_tool_call=after_tool_call,
        trajectory_format=trajectory_format,
        provenance=provenance,
        agent_identity=agent_identity,
        agent_state=agent_state,
        environment_identity=environment_identity,
        environment_state=environment_state,
    )


async def load_continuation(
    point: ContinuationPoint,
    *,
    instruction: str,
    agent: AgentContinuationAdapter[_Result],
    environment: EnvironmentContinuationAdapter,
) -> _Result:
    """Restore task state, then load and resume the agent from ``point``.

    Identity and capability checks happen before either adapter mutates state.
    Environment restoration is not transactional; an agent-stage failure is
    reported with ``environment_restored=True`` so callers retain honest evidence.
    """

    if not isinstance(point, ContinuationPoint):
        raise ValueError("load_continuation requires a ContinuationPoint")
    if not isinstance(instruction, str) or not instruction.strip():
        raise ValueError("Continuation instruction must be nonempty text")
    _require_load(agent, point.trajectory_format)
    if _adapter_identity(agent, "Agent") != point.agent_identity:
        raise ValueError("Agent continuation identity changed")
    if _adapter_identity(environment, "Environment") != point.environment_identity:
        raise ValueError("Environment continuation identity changed")
    try:
        await environment.restore(point.environment_state)
    except Exception as exc:
        raise ContinuationRestoreError("environment", environment_restored=False) from exc
    try:
        return await agent.load(
            instruction=instruction,
            trajectory_format=point.trajectory_format,
            state=point.agent_state,
        )
    except Exception as exc:
        raise ContinuationRestoreError("agent", environment_restored=True) from exc
