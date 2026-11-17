"""Optimization experiments over saved candidates, with checked validity and measured results."""

from . import bases, transforms
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
from .inputs import Environment, Memory, ResearcherInputs, Start, present
from .instructions import Instructions
from .research import EvaluationArtifact, Research
from .review import LLMValidity
from .task import Run, RunSettings, Task

__all__ = [
    "bases",
    "transforms",
    "AgentContinuationAdapter",
    "AgentContinuationCapabilities",
    "CodingAgent",
    "ContinuationPoint",
    "ContinuationRestoreError",
    "Environment",
    "Evaluation",
    "EvaluationArtifact",
    "Instructions",
    "EvaluationContext",
    "EnvironmentContinuationAdapter",
    "Limits",
    "Memory",
    "LLMValidity",
    "Metric",
    "Objective",
    "Research",
    "ResearcherInputs",
    "Run",
    "Start",
    "RunSettings",
    "Task",
    "Validity",
    "ValidityContext",
    "capture_continuation",
    "load_continuation",
    "present",
]
