"""The types a task is written with: metrics, objectives, limits, validity and evaluations."""

from __future__ import annotations

import hashlib
import inspect
import math
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Callable

from .persistence import digest, write_json
from .source_tree import evaluation_evidence_digest, snapshot
from .traces import atif_metadata


def code_identity(operation: Callable) -> str:
    """Hash a callback and the file that defines it, so an edited callback needs a fresh output.

    Only that one file is hashed. A callback that depends on anything else, such
    as a dataset, should be an object with an ``identity`` attribute that names it.
    """
    module = inspect.getmodule(operation if inspect.isfunction(operation) else type(operation))
    source = getattr(module, "__file__", None)
    if not source or not Path(source).is_file():
        raise ValueError("Callbacks require a source module; use a .py file, not a REPL")
    return digest(
        {
            "module": module.__name__,
            "callable": getattr(operation, "__qualname__", type(operation).__qualname__),
            "code": _function_code(operation.__code__) if inspect.isfunction(operation) else None,
            "source": hashlib.sha256(Path(source).read_bytes()).hexdigest(),
            "identity": getattr(operation, "identity", None),
        }
    )


def _function_code(code):
    """Describe a function's code without its file path, so two lambdas in one file differ."""
    return {
        "bytecode": code.co_code.hex(),
        "constants": [_code_constant(value) for value in code.co_consts],
        "names": code.co_names,
        "variables": code.co_varnames,
        "freevars": code.co_freevars,
        "line": code.co_firstlineno,
    }


def _code_constant(value):
    if inspect.iscode(value):
        return _function_code(value)
    if isinstance(value, tuple):
        return [_code_constant(item) for item in value]
    if isinstance(value, frozenset):
        return sorted(digest(_code_constant(item)) for item in value)
    return repr(value)


@dataclass(frozen=True)
class Metric:
    """A number an evaluator reports for each evaluation.

    ``direction`` says whether more (``"max"``) or less (``"min"``) is better.
    ``minimum`` and ``maximum`` are optional bounds: a candidate outside them is
    still measured but cannot be selected. ``bounds_scope="each"`` applies the
    bounds to every evaluation; ``"mean"`` applies them to the mean only.
    """

    name: str
    direction: str = "max"
    minimum: float | None = None
    maximum: float | None = None
    bounds_scope: str = "each"

    def __post_init__(self):
        if not isinstance(self.name, str) or not self.name.strip() or self.direction not in {"max", "min"}:
            raise ValueError("Metrics need a name and min/max direction")
        if self.bounds_scope not in ("each", "mean"):
            raise ValueError("Metric bounds_scope must be each or mean")
        for limit in (self.minimum, self.maximum):
            if limit is not None and (type(limit) not in (int, float) or not math.isfinite(limit)):
                raise ValueError("Metric limits must be finite numbers")
        if self.minimum is not None and self.maximum is not None and self.minimum > self.maximum:
            raise ValueError("Metric minimum exceeds maximum")


def _number(value, label):
    if type(value) not in (int, float) or not math.isfinite(value):
        raise ValueError(f"{label} must be a finite number, never a boolean")
    return float(value)


class MetricValues(Mapping):
    """Read-only measurements keyed by ``Metric``. It records which metrics a score function read."""

    def __init__(self, values: dict[Metric, float]):
        self._values = MappingProxyType(dict(values))
        self.reads: set[Metric] = set()

    def __getitem__(self, metric):
        if not isinstance(metric, Metric):
            raise TypeError("Use a registered Metric object as the key, not its name")
        if metric not in self._values:
            raise ValueError(f"Objective referenced an undeclared metric: {metric.name}")
        self.reads.add(metric)
        return self._values[metric]

    def __iter__(self):
        return iter(self._values)

    def __len__(self):
        return len(self._values)


@dataclass(frozen=True)
class Objective:
    """How candidates are ranked.

    ``score`` is a ``Metric``, or a function that computes one number from the
    mean of each metric. ``tie_breakers`` are metrics compared in order when
    scores are equal. ``metrics`` lists every metric the evaluator reports; give
    it when the evaluator reports more than ``score`` and ``tie_breakers`` name,
    or when ``score`` is a function.
    """

    score: Metric | Callable[[MetricValues], float]
    direction: str | None = None
    tie_breakers: tuple[Metric, ...] = ()
    description: str = ""
    metrics: tuple[Metric, ...] | None = None

    def __post_init__(self):
        if not isinstance(self.score, Metric) and not callable(self.score):
            raise ValueError("Objective requires a Metric or a score function")
        if callable(self.score):
            try:
                inspect.signature(self.score).bind(object())
            except (TypeError, ValueError) as exc:
                raise ValueError("A score function must accept one metric-values mapping") from exc
        direction = self.direction if self.direction is not None else (
            self.score.direction if isinstance(self.score, Metric) else "max"
        )
        if direction not in {"max", "min"}:
            raise ValueError("Objective direction must be min or max")
        object.__setattr__(self, "direction", direction)
        if (
            not isinstance(self.tie_breakers, tuple)
            or any(not isinstance(m, Metric) for m in self.tie_breakers)
            or len(set(self.tie_breakers)) != len(self.tie_breakers)
        ):
            raise ValueError("Tie-breakers must be a tuple of unique Metric objects")
        if not isinstance(self.description, str):
            raise ValueError("Objective description must be text")
        if self.metrics is not None:
            MetricSchema(self.metrics)

    @property
    def referenced_metrics(self):
        return ((self.score,) if isinstance(self.score, Metric) else ()) + self.tie_breakers

    @property
    def label(self):
        return self.description or (self.score.name if isinstance(self.score, Metric) else "Custom score")

    def identity(self):
        return {
            "description": self.label,
            "score": {"metric": self.score.name} if isinstance(self.score, Metric)
            else {"function": code_identity(self.score)},
            "direction": self.direction,
            "tie_breakers": [asdict(m) for m in self.tie_breakers],
            "aggregation": "score-of-equal-metric-means",
        }

    def assess(self, values: MetricValues, completed: bool):
        score = _number(
            values[self.score] if isinstance(self.score, Metric) else self.score(values), "Objective score"
        )
        if not values.reads:
            raise ValueError("A score function must read at least one registered metric")
        key = [score * (1 if self.direction == "max" else -1)]
        key.extend(values[m] * (1 if m.direction == "max" else -1) for m in self.tie_breakers)
        feasible = completed and all(
            (m.minimum is None or value >= m.minimum) and (m.maximum is None or value <= m.maximum)
            for m, value in values._values.items()
        )
        return score, tuple(key) if feasible else None


class MetricSchema:
    """The declared metrics. Converts between ``Metric`` keys in Python and names in saved files."""

    def __init__(self, metrics):
        self.metrics = tuple(metrics)
        if (
            not self.metrics or any(not isinstance(m, Metric) for m in self.metrics)
            or len({m.name for m in self.metrics}) != len(self.metrics)
        ):
            raise ValueError("Declare a nonempty set of Metric objects with unique names")

    def encode(self, values):
        if not isinstance(values, dict) or any(not isinstance(m, Metric) for m in values):
            raise ValueError("Evaluation measurements must use registered Metric objects as keys")
        if set(values) != set(self.metrics):
            raise ValueError("Evaluator must return exactly the declared metric definitions")
        return {m.name: _number(values[m], f"Metric {m.name}") for m in self.metrics}

    def decode(self, values):
        if not isinstance(values, dict) or set(values) != {m.name for m in self.metrics}:
            raise ValueError("Saved measurements must match exactly the declared metrics")
        return MetricValues({m: _number(values[m.name], f"Metric {m.name}") for m in self.metrics})


@dataclass(frozen=True)
class Limits:
    """The budget of one experiment output.

    ``max_candidates``
        Candidates that may be saved, including the baseline.
    ``max_evaluations``
        Evaluations that may start. A failed evaluation still counts.
    ``max_repeats``
        The most repeats one candidate may be given.
    ``selection_repeats``
        Complete repeats a candidate needs before it can be selected.
    ``parallel_evaluations``
        Evaluations of one candidate that may run at the same time.
    """

    max_evaluations: int = 20
    max_candidates: int = 10
    max_repeats: int = 3
    selection_repeats: int = 1
    parallel_evaluations: int = 1

    def __post_init__(self):
        if any(type(v) is not int or v < 1 for v in asdict(self).values()):
            raise ValueError("Limits must be positive integers")
        if self.selection_repeats > self.max_repeats or self.parallel_evaluations > 128:
            raise ValueError("Invalid repeat or parallel limit")

    # The shared evaluation lifecycle reads its limits under these names.
    exploratory_repeats = 1

    @property
    def max_solver_trial_starts(self):
        return self.max_evaluations

    @property
    def max_parallel_solver_trials(self):
        return self.parallel_evaluations


@dataclass(frozen=True)
class Validity:
    """Whether a candidate follows the task's rules, and why. The default is valid."""

    verdict: str = "valid"
    reasons: tuple[str, ...] = ("Project checks passed",)

    def __post_init__(self):
        if (
            self.verdict not in {"valid", "invalid"}
            or not self.reasons
            or any(not isinstance(r, str) or not r.strip() for r in self.reasons)
        ):
            raise ValueError("Validity needs a supported verdict and nonempty reasons")


@dataclass(frozen=True)
class ValidityContext:
    """What a validity check is told: the candidate and setup hashes, and a directory for its files."""

    candidate_hash: str
    contract_hash: str
    directory: Path


@dataclass(frozen=True)
class EvaluationContext:
    """What an evaluator is told about one evaluation.

    ``task`` is the case name. ``artifacts`` is a new directory for evidence that
    researchers may inspect, such as plots or logs. ``private_artifacts`` is a
    separate trusted directory that is sealed with the result but never mounted
    in researcher history. Direct callers may omit it for compatibility.
    """

    task: str
    condition: str
    repeat: int
    candidate_hash: str
    contract_hash: str
    artifacts: Path
    private_artifacts: Path | None = None


def _private_artifacts_path(result_path: Path) -> Path:
    """Map one controller-owned public result path to its private evidence peer."""
    result_path = Path(result_path)
    try:
        session_root = next(
            parent for parent in result_path.parents
            if parent.parent.name == "sessions"
        )
        relative = result_path.parent.relative_to(session_root / "history")
    except (StopIteration, ValueError) as exc:
        raise ValueError("Evaluation result is outside session history") from exc
    return session_root / "private" / relative / "artifacts"


@dataclass(frozen=True)
class Evaluation:
    """What an evaluator returns: one value for every declared metric.

    ``completed`` means the measurement finished. A finished attempt that scored
    badly is still completed. ``trajectory`` may name an ATIF file inside the
    artifacts directory.
    """

    metrics: dict[Metric, float]
    completed: bool = True
    trajectory: Path | None = None
    metadata: dict = field(default_factory=dict)


class FunctionEvaluator:
    """Calls the task's evaluator for one evaluation and saves its result with a hash of its artifacts."""

    def __init__(self, function: Callable, schema: MetricSchema):
        self.function = function
        self.schema = schema

    def __call__(self, candidate, task, condition, repeat, contract_hash, result_path):
        source_hash = digest(snapshot(candidate))
        artifacts = result_path.parent / "artifacts"
        artifacts.mkdir(exist_ok=False)
        private_artifacts = _private_artifacts_path(result_path)
        private_artifacts.mkdir(parents=True, exist_ok=False)
        context = EvaluationContext(
            task, condition, repeat, source_hash, contract_hash,
            artifacts, private_artifacts,
        )
        value = self.function(candidate, context)
        if not isinstance(value, Evaluation) or type(value.completed) is not bool:
            raise ValueError("Evaluator must return Evaluation with boolean completed")
        metrics = self.schema.encode(value.metrics)
        trajectory = None
        if value.trajectory is not None:
            # A trajectory outside the artifacts directory is rejected here.
            local = value.trajectory.relative_to(artifacts)
            trajectory = atif_metadata(value.trajectory)
            session_root = next(p for p in candidate.parents if p.parent.name == "sessions")
            trajectory["path"] = "/" + (artifacts / local).relative_to(session_root).as_posix()
        result = {
            "metrics": metrics,
            "completed": value.completed,
            "candidate_hash": source_hash,
            "metadata": value.metadata,
            "artifacts_sha256": evaluation_evidence_digest(artifacts, cells=1),
            "private_artifacts_sha256": evaluation_evidence_digest(
                private_artifacts, cells=1),
        }
        wrapper = {
            "task_id": task,
            "condition": condition,
            "repeat": repeat,
            "contract_hash": contract_hash,
            "result": result,
        }
        if trajectory is not None:
            result["trajectory"] = trajectory
            write_json(result_path.with_name("trajectory.json"), {"solver_trajectory": trajectory})
            wrapper["solver_trajectory"] = trajectory
        write_json(result_path, wrapper)
        return result
