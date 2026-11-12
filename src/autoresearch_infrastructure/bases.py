"""Tasks to build on.

Each function returns a complete ``ari.Task`` for a common kind of problem. A
task file calls one with its own Harbor tasks, starting files and instructions:

    def task(run):
        return ari.bases.harbor_task_files(
            run, harbor_tasks={"sum": "harbor-task"}, editable=["instruction.md"],
        )

Paths are relative to the task file. Both read the solver from ``[task.solver]`` in the run settings and count one
``solver_trial`` start for each evaluation.
"""

from __future__ import annotations

from pathlib import Path

from .api import Metric, Objective, Validity
from .task import Run, Task


def _harbor_tasks(run: Run, harbor_tasks: dict[str, Path]) -> dict[str, Path]:
    """The Harbor tasks to evaluate on, pinned to ``[task] image`` when the settings give one."""
    from .harbor_task import task_with_image

    image = run.settings.task.get("image")
    if image is None:
        return {name: run.directory / path for name, path in harbor_tasks.items()}
    return {
        name: task_with_image(run.directory / path, image, run.output / "harbor-tasks" / name)
        for name, path in harbor_tasks.items()
    }


def _solver(run: Run, example: str) -> dict:
    solver = run.settings.task.get("solver")
    if not isinstance(solver, dict):
        raise ValueError(f"Add a [task.solver] table to the run settings, for example: {example}")
    return solver


def harbor_task_files(
    run: Run,
    *,
    harbor_tasks: dict[str, Path | str],
    instructions: str | Path = Path("instructions.md"),
    editable: tuple[str, ...] | list[str] | None = ("instruction.md",),
    validity=None,
    objective: Metric | Objective | None = None,
    rewards: dict[Metric, str] | None = None,
) -> Task:
    """Improve the files of Harbor tasks while the agent that solves them stays fixed.

    ``harbor_tasks`` maps a case name to a Harbor task directory, relative to the
    task file. ``instructions`` defaults to ``instructions.md`` there. ``editable``
    lists the files of each Harbor task that a candidate may change; ``None``
    makes the whole Harbor task editable, including its grader. The solver is the
    Harbor agent configuration in ``[task.solver]``.

    By default the objective is the success rate from each Harbor task's own
    grader. For other rewards, pass ``objective`` and ``rewards``, a mapping from
    each metric to the Harbor reward key that supplies it.
    """
    from .harbor_task import HarborTaskEvaluator
    from .harbor_trial import SUCCESS_RATE

    if (objective is None) != (rewards is None):
        raise ValueError("Pass objective and rewards together: the objective ranks the metrics that rewards supply")
    solver = _solver(run, 'name = "terminus-2", model_name = "..."')
    tasks = _harbor_tasks(run, harbor_tasks)
    evaluator = HarborTaskEvaluator(
        tasks=tasks,
        agent=solver,
        ledger=run.starts(solver_trial=run.settings.limits.max_evaluations),
        editable=None if editable is None else {name: list(editable) for name in tasks},
        metrics=rewards,
    )
    return Task(
        seed=evaluator.seed(run.output / "seed"),
        instructions=instructions,
        objective=objective or SUCCESS_RATE,
        evaluator=evaluator,
        validity=validity,
        cases=tuple(tasks),
    )


def harbor_harness(
    run: Run,
    *,
    harbor_tasks: dict[str, Path | str],
    seed: Path | str = "seed",
    advice: str = "",
    validity=None,
) -> Task:
    """Improve the program that drives a solver agent, on fixed Harbor tasks.

    ``harbor_tasks`` and ``seed`` are relative to the task file. ``seed``
    defaults to ``seed/`` there and is the starting solver program:
    ``program.toml``, ``solve.py`` and
    any prompts, skills or hooks. ``[task.solver]`` names the coding-agent CLI
    and its model: ``harness``, ``provider`` and ``model``, and optionally
    ``timeout_seconds`` and ``max_sessions`` for each trial.

    The instructions are the package's standard ones for a harness researcher,
    followed by ``advice``. Every candidate must be a loadable solver program that
    keeps the seed's ID; ``validity`` adds your own function after that.
    """
    from .harbor_harness import (
        HARNESS_RULES,
        HarborHarnessEvaluator,
        researcher_goal,
        write_researcher_context,
    )
    from .harbor_trial import SUCCESS_RATE
    from .programs.package import program_manifest
    from .providers import provider_for

    solver = _solver(run, 'harness = "codex", provider = "openrouter", model = "..."')
    unknown = set(solver) - {"harness", "provider", "model", "timeout_seconds", "max_sessions"}
    if unknown or not {"harness", "provider", "model"} <= set(solver):
        raise ValueError("[task.solver] needs harness, provider and model; it also accepts "
                         "timeout_seconds and max_sessions")
    sessions = solver.get("max_sessions", 1)
    evaluations = run.settings.limits.max_evaluations
    evaluator = HarborHarnessEvaluator(
        tasks=_harbor_tasks(run, harbor_tasks),
        solver_image=run.image,
        model=solver["model"],
        harness=solver["harness"],
        provider=provider_for(solver["harness"], solver["provider"]),
        ledger=run.starts(solver_trial=evaluations, solver_session=evaluations * sessions),
        timeout_seconds=solver.get("timeout_seconds", 120),
        max_harness_sessions=sessions,
    )
    reference = write_researcher_context(run.output / "harness-reference", solver["harness"], HARNESS_RULES)
    return Task(
        seed=run.directory / seed,
        instructions=researcher_goal(sessions=sessions, advice=advice),
        objective=Objective(SUCCESS_RATE, description="Complete the Harbor tasks more reliably"),
        evaluator=evaluator,
        validity=_ProgramValidity(program_manifest(run.directory / seed)["id"], validity),
        cases=tuple(harbor_tasks),
        context={path.name: path for path in sorted(reference.iterdir())},
    )


class _ProgramValidity:
    """Requires a loadable solver program with a fixed ID, then applies the task's own validity function."""

    def __init__(self, program_id: str, then=None):
        self.program_id, self.then = program_id, then

    @property
    def identity(self):
        from .api import code_identity

        return {"program_id": self.program_id, "then": code_identity(self.then) if self.then else None}

    def __call__(self, candidate, context):
        from .programs.package import program_manifest

        try:
            program_manifest(candidate, self.program_id)
        except (ValueError, OSError) as exc:
            return Validity("invalid", (str(exc),))
        return self.then(candidate, context) if self.then else Validity()
