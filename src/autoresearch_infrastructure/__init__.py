"""Optimization experiments over saved candidates, with checked validity and measured results."""

from . import bases
from .api import Evaluation, EvaluationContext, Limits, Metric, Objective, Validity, ValidityContext
from .coding_agent import CodingAgent
from .continuations import (
    AgentContinuationAdapter,
    AgentContinuationCapabilities,
    ContinuationPoint,
    ContinuationRestoreError,
    EnvironmentContinuationAdapter,
    capture_continuation,
    load_continuation,
)
from .instructions import Instructions
from .research import EvaluationArtifact, Research
from .review import LLMValidity
from .task import Run, RunSettings, Task

__all__ = [
    "bases",
    "AgentContinuationAdapter",
    "AgentContinuationCapabilities",
    "CodingAgent",
    "ContinuationPoint",
    "ContinuationRestoreError",
    "Evaluation",
    "EvaluationArtifact",
    "Instructions",
    "EvaluationContext",
    "EnvironmentContinuationAdapter",
    "Limits",
    "LLMValidity",
    "Metric",
    "Objective",
    "Research",
    "Run",
    "RunSettings",
    "Task",
    "Validity",
    "ValidityContext",
    "capture_continuation",
    "load_continuation",
]
