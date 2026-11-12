"""A task says what to optimize; run settings say who works on it and for how long."""

from __future__ import annotations

import hashlib
import importlib.util
import sys
import tomllib
from collections.abc import Callable
from dataclasses import asdict, dataclass, field, replace
from pathlib import Path
from typing import Any

from .api import Limits, Metric, Objective
from .instructions import Instructions
from .ledger import StartLedger

_LIMITS = {
    "candidates": "max_candidates",
    "evaluations": "max_evaluations",
    "repeats": "selection_repeats",
    "parallel": "parallel_evaluations",
}


@dataclass(frozen=True)
class Task:
    """An optimization problem, in four parts.

    Paths are relative to the directory of the task file. ``seed`` and
    ``instructions`` default to ``seed/`` and ``instructions.md`` there, so a
    task that follows that layout names neither.

    ``objective``
        The ``Metric`` to improve, or an ``Objective`` over several.
    ``evaluator``
        ``(candidate, EvaluationContext) -> Evaluation``. Trusted code that
        measures one candidate on one case.
    ``seed``
        Directory of starting files. Each saved version of them is a candidate.
    ``instructions``
        What the researcher is told: an ``Instructions``, or Markdown text in
        its two sections. Objective says what to change and what should get
        better. Constraints lists the Rules a candidate must obey and what the
        Environment enforces regardless. ARI adds the metric, its direction and
        the starting files itself. The reviewer judges each rule. A ``Path``
        names a Markdown file to read.

    Three optional parts refine these:

    ``validity``
        ``(candidate, ValidityContext) -> Validity``. Rules that code can decide
        exactly. It runs before the reviewer.
    ``cases``
        Names of the fixed inputs every candidate is evaluated on. The default is
        one case.
    ``context``
        Read-only files for the researcher, by name under ``/context``. A
        ``Path`` to a file or directory is copied, text is written as is, and
        any other value is saved as JSON.
    """

    objective: Metric | Objective
    evaluator: Callable
    seed: Path | str = "seed"
    instructions: Instructions | str | Path = Path("instructions.md")
    validity: Callable | None = None
    cases: tuple[str, ...] = ("default",)
    context: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self):
        if isinstance(self.instructions, str):
            object.__setattr__(self, "instructions", Instructions.parse(self.instructions))
        if not isinstance(self.instructions, (Instructions, Path)):
            raise ValueError("A task needs instructions: an ari.Instructions, Markdown text, or a file path")

    def resolved(self, directory: Path) -> Task:
        """A copy with every relative path resolved against ``directory`` and its instructions read."""
        instructions = self.instructions
        if isinstance(instructions, Path):
            path = directory / instructions
            if not path.is_file():
                raise ValueError(f"No instructions at {path}. Write that file, or pass instructions=")
            instructions = Instructions.parse(path.read_text(encoding="utf-8"))
        context = {name: directory / value if isinstance(value, Path) else value
                   for name, value in self.context.items()}
        return replace(self, seed=directory / self.seed, instructions=instructions, context=context)


@dataclass(frozen=True)
class RunSettings:
    """Everything about a run that is not the task, read from one TOML file.

    ``[limits]``
        ``candidates`` and ``evaluations`` this run may use, ``repeats`` of each
        case per candidate, and ``parallel`` evaluations at a time.
    ``[researcher]``
        The Harbor agent that proposes candidates. Leave it out to propose them
        yourself.
    ``[reviewer]``
        The Harbor agent that judges each candidate against the instructions.
        It defaults to the researcher's agent. Write ``reviewer = false`` to turn
        review off.
    ``[task]``
        Values the task reads, such as the solver it evaluates.

    Agent starts follow from the limits: one reviewer start for each candidate,
    and one researcher start for each candidate after the baseline.
    """

    limits: Limits = Limits()
    researcher: dict | None = None
    reviewer: dict | None = None
    task: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: Path) -> RunSettings:
        return cls.parse(tomllib.loads(Path(path).read_text(encoding="utf-8")))

    @classmethod
    def parse(cls, data: dict) -> RunSettings:
        tables = {"limits", "researcher", "reviewer", "task"}
        if set(data) - tables:
            raise ValueError(f"Run settings accept only {sorted(tables)}; found {sorted(set(data) - tables)}")
        limits = data.get("limits", {})
        if not isinstance(limits, dict) or set(limits) - set(_LIMITS):
            raise ValueError(f"[limits] accepts only {sorted(_LIMITS)}")
        fields = {_LIMITS[name]: value for name, value in limits.items()}
        if "selection_repeats" in fields:
            fields["max_repeats"] = fields["selection_repeats"]
        researcher = _agent(data["researcher"], "researcher") if "researcher" in data else None
        reviewer = data.get("reviewer", researcher)
        if reviewer is False:
            reviewer = None
        elif reviewer is None:
            raise ValueError(
                "No reviewer is configured. Add a [reviewer] table, or write reviewer = false "
                "to rely on the task's check alone"
            )
        elif reviewer is not researcher:
            reviewer = _agent(reviewer, "reviewer")
        return cls(Limits(**fields), researcher, reviewer, dict(data.get("task", {})))

    @property
    def repeats(self) -> int:
        return self.limits.selection_repeats

    @property
    def starts(self) -> dict[str, int]:
        """The most agent starts this run may make, by kind. A failed start still counts."""
        starts = {}
        if self.researcher:
            starts["researcher"] = self.limits.max_candidates - 1
        if self.reviewer:
            starts["reviewer"] = self.limits.max_candidates
        return starts

    def identity(self) -> dict:
        return asdict(self)


def _agent(table, role: str) -> dict:
    """Check a Harbor agent table now, so a mistake costs no start, and fill its timeout."""
    from .harbor_filesystem import native_agent_config

    if not isinstance(table, dict):
        raise ValueError(f"[{role}] must be a table of Harbor agent settings")
    researcher = role == "researcher"
    agent = native_agent_config(table, default_timeout=600 if researcher else 240, researcher=researcher)
    return agent.model_dump(mode="json")


class Run:
    """What a task may read while it is being defined for one run.

    ``settings`` are the run settings and ``output`` is the run directory; a
    task may prepare files beneath it. ``image`` is the container image of the
    researcher and reviewer. ``directory`` is where the task file lives.
    """

    def __init__(self, settings: RunSettings, output: Path, image: str | None = None, *,
                 directory: Path | None = None, identity=None):
        self.settings, self.output, self.image = settings, Path(output), image
        self.directory = Path(directory) if directory is not None else Path.cwd()
        self._identity = identity if identity is not None else settings.identity()
        self._ledger: StartLedger | None = None

    def starts(self, **more: int) -> StartLedger | None:
        """The ledger that counts this run's starts.

        A task whose evaluator starts agents names those kinds and their limits
        here, once, such as ``run.starts(solver_trial=20)``. Returns None when
        the run has nothing to count.
        """
        if self._ledger is None:
            limits = {**self.settings.starts, **more}
            if limits:
                self._ledger = StartLedger(self.output / "starts.json", limits=limits, identity=self._identity)
        elif more:
            raise ValueError("Name a task's start kinds in one call to run.starts(...)")
        return self._ledger


def load_task(path: Path) -> tuple[Callable[[Run], Task], Path]:
    """Import a task file and return its ``task(run)`` function and the file's path.

    ``path`` is the file, or a directory containing ``task.py``. Its directory is
    added to the import path so that the task can import neighbouring modules.
    """
    path = Path(path).resolve()
    if path.is_dir():
        path = path / "task.py"
    if not path.is_file():
        raise ValueError(f"No task file at {path}")
    name = "ari_task_" + hashlib.sha256(str(path).encode()).hexdigest()[:12]
    spec = importlib.util.spec_from_file_location(name, path)
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    sys.path.insert(0, str(path.parent))
    spec.loader.exec_module(module)
    define = getattr(module, "task", None)
    if not callable(define):
        raise ValueError(f"{path} must define task(run) returning an ari.Task")
    return define, path
