"""Types available to solver programs in the isolated runtime.

The starter and examples include the imports needed to use them. A copy of this
file in the research context documents the fields and function signatures.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Awaitable, Callable, Protocol


@dataclass(frozen=True)
class SolverInput:
    instruction: str
    tools: tuple[dict[str, Any], ...] = ()


@dataclass(frozen=True)
class ToolCall:
    name: str
    args: dict[str, Any]
    id: str = ""
    session_id: str = ""
    origin: str = "task"


@dataclass(frozen=True)
class ToolObservation:
    call_id: str
    result: Any = None
    error: str | None = None
    denied: bool = False


@dataclass(frozen=True)
class Allow:
    pass


@dataclass(frozen=True)
class Deny:
    reason: str


@dataclass(frozen=True)
class HarnessResult:
    text: str
    completed: bool
    session_id: str
    usage: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class SolverOutput:
    answer: str


class Runtime(Protocol):
    state: dict[str, Any]

    async def run_harness(
        self,
        *,
        input: str,
        pre_tool: Callable[[ToolCall], Awaitable[Allow | Deny]] | str | None = None,
        post_tool: Callable[[ToolCall, ToolObservation], Awaitable[ToolObservation]] | str | None = None,
        **kwargs: Any,
    ) -> HarnessResult:
        """Hooks must be async functions; post_tool returns a ToolObservation."""
        ...
