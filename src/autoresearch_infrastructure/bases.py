"""Tasks to build on.

Each function returns a complete ``ari.Task`` for a common kind of problem. A
task file calls one with its own Harbor tasks, starting files and instructions:

    def task(run):
        return ari.bases.harbor_task_files(
            run, harbor_tasks={"sum": "harbor-task"}, editable=["instruction.md"],
        )

Paths are relative to the task file. ``harbor_deliverable`` grades the
candidate itself with the Harbor task's grader and starts no solver. The other
two read the solver from ``[task.solver]`` in the run settings and count one
``solver_trial`` start for each evaluation.
"""

from __future__ import annotations

from pathlib import Path

from .api import Metric, Objective, Validity
from .inputs import Environment, Memory
from .instructions import Instructions
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


def harbor_deliverable(
    run: Run,
    *,
    harbor_task: Path | str,
    deliverable: str,
    feedback: str,
    sealed: str | None = "/tests/test.sh",
    seed: Path | str = "seed",
    grader: str | None = None,
    rules: tuple[str, ...] | list[str] = (),
    environment: tuple[str, ...] | list[str] = (),
    instructions: Instructions | str | Path | None = None,
    objective: Metric | Objective | None = None,
    rewards: dict[Metric, str] | None = None,
    validity=None,
    task_environment: bool = True,
    private: tuple[str, ...] | list[str] = (),
    memory: Memory | None = None,
    capabilities: tuple[str, ...] | list[str] = (),
    network: bool = False,
    feedback_log: bool = False,
) -> Task:
    """Improve the deliverable of a Harbor task, scored by that task's own grader.

    Use this for a Harbor task in which the agent writes files into one
    directory and the grader scores them. A Harbor task says neither which
    directory is the deliverable nor which script is safe to run on every
    submission, so the task file names both:

    ``harbor_task``
        The Harbor task directory, relative to the task file.
    ``deliverable``
        The container path the grader reads, such as ``/app/output``. A
        candidate is the contents of that directory.
    ``feedback``
        The grader script whose reward the researcher sees and selection
        ranks, as a path in the grader container. ``tests/`` is mounted at
        ``/tests``.
    ``sealed``
        The grader script whose reward is held out: it is recorded privately
        for every evaluated candidate and shown to no agent. The default is
        Harbor's own ``/tests/test.sh``. Pass None for a task with no held-out
        score.
    ``seed``
        The starting deliverable, a directory relative to the task file.
    ``grader``
        Where the scripts run: by default the task's main image; or a directory
        in the Harbor task holding the grader's own Dockerfile; or an image
        pinned by digest.

    ``instruction.md`` is the written objective. ``rules`` and ``environment``
    add the two lists a Harbor instruction does not separate; the reviewer
    judges each rule, so a run with review on needs at least one. Pass
    ``instructions`` instead to write all three yourself.

    One reward becomes the metric ``reward``, higher is better. For anything
    else pass ``objective`` and ``rewards``, a mapping from each metric to the
    feedback reward key that supplies it.

    The researcher and the reviewer each run in the task's main container,
    built from ``environment/`` without the task's compose file, its MCP
    servers, the grader's build directory and every path in ``private``.
    Dockerfile ``FROM`` lines must be pinned by digest.
    ``task_environment=False`` runs them in the ``--image`` container instead.

    ``capabilities``, ``network`` and ``feedback_log`` loosen the grader
    container; see ``HarborGraderEvaluator``.
    """
    from . import harbor_deliverable as deliverables

    if (objective is None) != (rewards is None):
        raise ValueError("Pass objective and rewards together: the objective ranks the metrics that rewards supply")
    task = run.directory / harbor_task
    state = run.output / "harbor-deliverable"
    evaluator = deliverables.HarborGraderEvaluator(
        task=task, deliverable=deliverable, feedback=feedback, sealed=sealed, grader=grader, state=state,
        rewards=rewards, capabilities=capabilities, network=network, feedback_log=feedback_log,
    )
    harbor = None
    if task_environment and (run.settings.researcher is not None or run.settings.reviewer is not None):
        hidden = list(private) + ([grader] if evaluator.context is not None and grader is not None else [])
        harbor = deliverables.researcher_environment(task, state / "researcher-environment", private=hidden)
    if instructions is None:
        source = task / "instruction.md"
        if not source.is_file() or not source.read_text(encoding="utf-8").strip():
            raise ValueError(f"No instruction at {source}. Write it, or pass instructions=")
        instructions = Instructions(
            objective=source.read_text(encoding="utf-8").strip(),
            rules=tuple(rules),
            environment=(
                f"Where the text above names `{deliverable}`, the deliverable directory, use "
                "`/workspace/candidate/` instead: that directory is what is graded.",
                "Submitting is described under the file layout below. It replaces any submission tool, "
                "time limit or submission count the text above names.",
                "The grader runs in a separate container that you cannot reach. Its score is returned to you "
                "after each submission.",
                *(("You work in the task's main container, so its other paths are as the text above says. "
                   "Only `/workspace` and `/tmp` are writable.",) if harbor is not None else ()),
                *environment,
            ),
        )
    elif rules or environment:
        raise ValueError("Pass either instructions, or rules and environment to add to instruction.md")
    return Task(
        seed=seed,
        instructions=instructions,
        objective=objective or deliverables.REWARD,
        evaluator=evaluator,
        validity=validity,
        environment=Environment(harbor=harbor if run.settings.researcher is not None else None),
        review_environment=harbor if run.settings.reviewer is not None else None,
        memory=memory or Memory(),
    )


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
