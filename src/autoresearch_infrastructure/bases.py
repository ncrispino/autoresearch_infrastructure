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
    feedback: str,
    sealed: str | None = "/tests/test.sh",
    deliverable: str = "/workspace/candidate",
    seed: Path | str | None = None,
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
    feedback_artifacts: bool = False,
    cases: tuple[str, ...] | list[str] = ("default",),
    check_seed: bool = True,
    timeout: float | None = None,
) -> Task:
    """Improve the deliverable of a Harbor task, scored by that task's own grader.

    Use this for a Harbor task in which the agent writes files into one
    directory and the grader scores them. A candidate is the contents of that
    directory. A Harbor task does not say which script is safe to run on every
    submission, so the task file names it:

    ``harbor_task``
        The Harbor task directory, relative to the task file.
    ``feedback``
        The grader script whose reward the researcher sees and selection
        ranks, as a path in the grader container. ``tests/`` is mounted at
        ``/tests``.
    ``sealed``
        The grader script whose reward is held out: it is recorded privately
        for every evaluated candidate and shown to no agent. The default is
        Harbor's own ``/tests/test.sh``. Pass None for a task with no held-out
        score.
    ``deliverable``
        The container path the grader reads the candidate from. A task written
        for ARI uses the default, ``/workspace/candidate``: the researcher
        edits that same path, so the instruction is true as written here and
        under ``harbor run``. For a task from elsewhere, name its own path,
        such as ``/app/output``; the researcher then sees the candidate at that
        path as well, so the task's relative paths and tools work unchanged.
    ``seed``
        The starting deliverable, a directory relative to the task file. The
        default is ``environment/candidate`` in the Harbor task, the directory
        its Dockerfile copies to ``deliverable``, or else ``seed/`` beside the
        task file. Before the first evaluation the seed is compared with what
        the task's image holds at ``deliverable``, and a run whose seed differs
        is refused, since the researcher would start from other files than an
        agent under ``harbor run``. ``ari extract-seed`` writes what the image
        holds. ``check_seed=False`` skips the comparison, to start from files
        of your own.
    ``grader``
        Where the scripts run: by default the task's main image; or a directory
        in the Harbor task holding the grader's own Dockerfile; or an image
        pinned by digest. Name the directory even when an image of it is
        already built, and give that image as ``grader_image = "sha256:..."``
        under ``[task]`` in the run settings: the directory is then still left
        out of the agents' container.

    ``instruction.md`` is the written objective. ``rules`` and ``environment``
    add the two lists a Harbor instruction does not separate; the reviewer
    judges each rule, so a run with review on needs at least one. Pass
    ``instructions`` instead to write all three yourself.

    One reward becomes the metric ``reward``, higher is better. For anything
    else pass ``objective`` and ``rewards``, a mapping from each metric to the
    feedback reward key that supplies it.

    The task's ``[agent]`` network policy applies to them while they work,
    without hosts that name the task's own compose services. Names under
    ``[task] allowed_hosts`` in the run settings are added to an allowlist,
    such as the provider of an agent that calls its model from inside the
    container.

    The researcher and the reviewer each run in the task's main container,
    built from ``environment/`` without the task's compose file, its MCP
    servers, the grader's build directory and every path in ``private``.
    Dockerfile ``FROM`` lines must be pinned by digest. When the agents need
    something the task's image lacks, such as ``tmux``, build an image from the
    task's own with that added and name it as ``image = "sha256:..."`` under
    ``[task]`` in the run settings; the grader still uses the task's image.
    ``task_environment=False`` runs them in the ``--image`` container instead.

    A task whose ``[environment]`` asks for more CPUs than this machine has is
    refused when it opens, since a grader with time limits would score
    differently. ``cpus = N`` under ``[task]`` in the run settings gives the
    grader and the agents ``N`` instead, and is recorded with the run.

    ``timeout`` is the seconds each grader script may run. The default is the
    task's ``[verifier] timeout_sec``.

    ``cases`` names the fixed inputs every candidate is graded on. Each one is
    a separate run of the scripts, which read the case name from the
    environment variable ``ARI_CASE``.

    ``feedback_artifacts=True`` shows the researcher the files the feedback
    script writes under ``/logs/artifacts``, such as a plot. ``capabilities``,
    ``network`` and ``feedback_log`` loosen the grader container; see
    ``HarborGraderEvaluator``.
    """
    from . import harbor_deliverable as deliverables

    if (objective is None) != (rewards is None):
        raise ValueError("Pass objective and rewards together: the objective ranks the metrics that rewards supply")
    task = run.directory / harbor_task
    state = run.output / "harbor-deliverable"
    if seed is None:
        shipped = Path(harbor_task) / "environment/candidate"
        seed = shipped if (run.directory / shipped).is_dir() else "seed"
    _free_workspace(task, deliverable, workdir=task_environment)
    if type(check_seed) is not bool:
        raise ValueError("check_seed is True or False")
    evaluator = deliverables.HarborGraderEvaluator(
        task=task, deliverable=deliverable, feedback=feedback, sealed=sealed, grader=grader, state=state,
        rewards=rewards, capabilities=capabilities, network=network, feedback_log=feedback_log,
        feedback_artifacts=feedback_artifacts, seed=run.directory / seed if check_seed else None,
        task_image=run.settings.task.get("image"), grader_image=run.settings.task.get("grader_image"),
        timeout=timeout, cpus=run.settings.task.get("cpus"),
    )
    elsewhere = deliverable.rstrip("/") != _CANDIDATE
    harbor = None
    if task_environment and (run.settings.researcher is not None or run.settings.reviewer is not None):
        hidden = list(private) + ([grader] if evaluator.context is not None and grader is not None else [])
        harbor = deliverables.researcher_environment(task, state / "researcher-environment", private=hidden,
                                                     image=run.settings.task.get("image"),
                                                     allowed_hosts=run.settings.task.get("allowed_hosts", ()),
                                                     cpus=run.settings.task.get("cpus"))
    if instructions is None:
        source = task / "instruction.md"
        if not source.is_file() or not source.read_text(encoding="utf-8").strip():
            raise ValueError(f"No instruction at {source}. Write it, or pass instructions=")
        instructions = Instructions(
            objective=source.read_text(encoding="utf-8").strip(),
            rules=tuple(rules),
            environment=(
                *((f"`{deliverable}`, the deliverable directory the text above names, is also mounted at "
                   "`/workspace/candidate/`. The two paths are one directory, not two copies. Its contents "
                   f"are what is graded, and the grader reads them at `{deliverable}`.",)
                  if elsewhere else ()),
                "Submitting is described under the file layout below. It replaces any submission tool, "
                "time limit or submission count the text above names.",
                "The grader runs in a separate container that you cannot reach. Its score is returned to you "
                "after each submission.",
                *(_network_note(harbor) if harbor is not None else ()),
                *(("You work in the task's main container, so its other paths are as the text above says. "
                   + (f"Only `{deliverable}`, `/workspace` and `/tmp` are writable." if elsewhere
                      else "Only `/workspace` and `/tmp` are writable."),) if harbor is not None else ()),
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
        cases=tuple(cases),
        environment=Environment(harbor=harbor if run.settings.researcher is not None else None,
                                candidate_at=deliverable if elsewhere else None),
        review_environment=harbor if run.settings.reviewer is not None else None,
        memory=memory or Memory(),
    )


_CANDIDATE = "/workspace/candidate"


def _network_note(harbor: Path) -> tuple[str, ...]:
    """What the agents' network reaches while they work, when the task restricts it."""
    from .harbor_environment import load

    policy = load(harbor).agent_network
    if policy is None or policy["network_mode"] == "public":
        return ()
    if policy["network_mode"] == "no-network":
        return ("While you work, this container has no network.",)
    hosts = ", ".join(f"`{host}`" for host in policy["allowed_hosts"]) or "nothing"
    return (f"While you work, this container's network reaches only: {hosts}.",)


def _free_workspace(task: Path, deliverable: str, *, workdir: bool) -> None:
    """Refuse a Harbor task that keeps its own files where ARI mounts the researcher's workspace.

    The deliverable is checked for every task. ``workdir`` also checks the
    task's working directory, which matters only when agents run in its image.
    """
    import tomllib

    manifest = task / "task.toml"
    declared = (tomllib.loads(manifest.read_text(encoding="utf-8")).get("environment") or {}).get(
        "workdir", "") if workdir and manifest.is_file() else ""
    for label, path in (("[environment] workdir", declared), ("deliverable", deliverable)):
        path = str(path).rstrip("/")
        if path != _CANDIDATE and (path == "/workspace" or path.startswith("/workspace/")):
            raise ValueError(
                f"The Harbor task's {label} is {path}, but ARI mounts the researcher's workspace over "
                "/workspace, which would hide the task's files there. Use /workspace/candidate for the "
                "deliverable and another directory for everything else")


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
