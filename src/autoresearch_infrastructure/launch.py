"""Open a task with its run settings, then check it, record a candidate, or run researchers.

A run directory holds::

    plan.json       the task and settings this run was opened with
    starts.json     agent starts used, when the run has agents
    context/        the researcher's read-only task material
    research/       saved candidates, verdicts and measurements
    summary.json    the outcome of the last ``run`` or ``record``
    viewer.html     the same results as a page
"""

from __future__ import annotations

import json
import shutil
from dataclasses import dataclass, replace
from pathlib import Path
from uuid import uuid4

from . import harbor_environment
from . import memory as memory_views
from .api import Validity, code_identity
from .inputs import ResearcherInputs, present
from .instructions import task_facts
from .ledger import StartLedger
from .persistence import digest, remove_tree, sha256_file, write_json
from .research import BASELINE, Research
from .search import RESEARCHER_PROTOCOL, candidate_path, propose, researcher_starts, reset_candidate
from .source_tree import python_tree_hash, snapshot, task_context_digest
from .task import Run, RunSettings, Task, load_task


@dataclass
class OpenRun:
    task: Task
    settings: RunSettings
    research: Research
    output: Path
    image: str | None
    ledger: StartLedger | None
    inputs: ResearcherInputs
    environment: harbor_environment.HarborEnvironment | None = None
    review_environment: harbor_environment.HarborEnvironment | None = None
    replay: list[dict] | None = None  # a finished run's candidates to review and evaluate again
    replay_source: Path | None = None

    @property
    def context(self) -> Path:
        return self.output / "context"

    @property
    def preloaded(self) -> Path | None:
        return self.output / "memory/preloaded" if self.task.memory.preload is not None else None

    def summary(self, status: str, **extra) -> dict:
        """The run's state for people and scripts; also saved by ``run`` and ``record``."""
        research = self.research
        summary = {
            "status": status,
            "output": str(self.output),
            "workspace": str(research.workspace),
            "review": self.review,
            "candidates": [_row(research, candidate_id) for candidate_id in research.candidates],
            "selected": research.selected,
            "budget": research.budget_summary(),
            **extra,
        }
        environments = {role: environment.prepared() or "not prepared"
                        for role, environment in (("researcher", self.environment),
                                                  ("reviewer", self.review_environment))
                        if environment is not None}
        if environments:
            summary["environments"] = environments
        if self.replay is not None:
            summary["reevaluates"] = {
                "source": str(self.replay_source),
                "candidates": [entry["id"] for entry in self.replay],
                "pending": [entry["id"] for entry in self.replay if not research.measured(entry["id"])
                            and research.candidates.get(entry["id"], {}).get("status") != "invalid"],
            }
        lineage = research.state.get("lineage")
        if lineage:
            summary["extends"] = {
                "source": str(Path(lineage["source"]).parent),  # the run directory, not its research/
                "imported_candidates": lineage["imported_candidates"],
                "source_limits": lineage["source_limits"],
            }
        if self.ledger is not None:
            starts = self.ledger.snapshot()
            costs = [start.get("cost_usd") for start in starts["starts"].values()]
            summary["starts"] = starts["counts"]
            summary["start_limits"] = starts["limits"]
            # A start with no reported cost makes the total unknown, not zero.
            summary["observed_cost_usd"] = sum(cost for cost in costs if cost is not None)
            summary["total_cost_usd"] = sum(costs) if costs and None not in costs else None
        return summary

    @property
    def reviewed(self) -> bool:
        return self.settings.reviewer is not None

    @property
    def review(self) -> str:
        """Who decides validity in this run."""
        parts = (["the task's validity function"] if self.task.validity else []) + (
            ["a reviewer"] if self.reviewed else [])
        return ", then ".join(parts) or "off"

    def save(self, summary: dict) -> dict:
        viewer = self.output / "viewer.html"
        viewer.unlink(missing_ok=True)
        from .viewer import write_html_preview

        summary["viewer"] = write_html_preview(self.research.output, viewer)["preview"]
        write_json(self.output / "summary.json", summary)
        return summary


def _row(research: Research, candidate_id: str) -> dict:
    record, evidence = research.candidates[candidate_id], research.evidence(candidate_id)
    return {
        "candidate_id": candidate_id,
        "parent_id": record["parent_id"],
        "status": record["status"],
        "reasons": list(evidence.get("validity", {}).get("reasons", record.get("reasons", []))),
        "evaluation": evidence.get("evaluation", "not_started"),
        "metrics": evidence.get("metrics"),
        "score": evidence.get("score"),
        "error": evidence.get("error"),
    }


def open_run(task_path: Path, settings_path: Path | None, output: Path, image: str | None, *,
             progress=None, extends: Path | None = None, reviewer_image: str | None = None,
             reevaluates: Path | None = None) -> OpenRun:
    """Load the task and settings, freeze them in ``plan.json``, and open the experiment.

    This starts no agent, builds no image and runs no evaluation. Reopening an
    output with a different task, settings or image is refused, so a run's
    budget cannot be reset.

    ``image`` is the container of the researcher and the reviewer.
    ``reviewer_image`` gives the reviewer its own; a task's Harbor
    ``environment`` or ``review_environment`` replaces either.

    ``extends`` names a finished run directory to continue in this new output:
    its candidates, evidence and usage are verified and copied, and the
    settings' limits are cumulative totals. The source is never written.
    Reopening an extension finds its source in ``plan.json``.

    ``reevaluates`` names a finished run whose candidates this new run reviews
    and evaluates again under its own task, with the same IDs, parents and
    proposals, before any researcher starts. No evidence is reused and the
    new run's budget pays for it. The source is never written.
    """
    define, task_file = load_task(task_path)
    settings = RunSettings.load(settings_path) if settings_path else RunSettings.parse({})
    if settings.researcher or settings.reviewer:
        from .harbor_filesystem import immutable_image

        for value in (image, reviewer_image):
            if value is not None:
                immutable_image(value, "A run with agents")
    output = Path(output).resolve()
    receipt = output / "plan.json"
    saved = json.loads(receipt.read_text()) if receipt.exists() else None
    if extends is None and saved is not None and saved.get("extends"):
        extends = Path(saved["extends"]["source"])
    if reevaluates is None and saved is not None and saved.get("reevaluates"):
        reevaluates = Path(saved["reevaluates"]["source"])
    if extends is not None and reevaluates is not None:
        raise ValueError("A run either extends a finished run or re-evaluates its candidates, not both")
    source = None if extends is None else _extension_source(Path(extends), output, saved)
    replayed = None if reevaluates is None else _extension_source(Path(reevaluates), output, saved)
    reviewer_image = reviewer_image or image
    opened_with = _json({
        "settings": settings.identity(),
        "image": image,
        "reviewer_image": reviewer_image,
        "task_file": sha256_file(task_file),
        **({"extends": _lineage(source)} if source else {}),
        **({"reevaluates": _lineage(replayed)} if replayed else {}),
    })
    if saved is not None and any(saved.get(key) != value for key, value in opened_with.items()):
        raise ValueError("Run plan changed; use a fresh output")

    created = not output.exists()
    output.mkdir(parents=True, exist_ok=True)
    opening = _Opening(define, task_file, settings, output, image, reviewer_image, opened_with,
                       saved, source, progress, replayed)
    try:
        return opening.open()
    except BaseException:
        if created:
            remove_tree(output)  # a rejected open leaves no new directory behind
        raise


@dataclass
class _Opening:
    define: object
    task_file: Path
    settings: RunSettings
    output: Path
    image: str | None
    reviewer_image: str | None
    opened_with: dict
    saved: dict | None
    source: Path | None
    progress: object
    replayed: Path | None = None

    def open(self) -> OpenRun:
        settings, output, source = self.settings, self.output, self.source
        run = Run(settings, output, self.image, directory=self.task_file.parent, identity=self.opened_with,
                  continues=source / "starts.json" if source else None)
        task = self.define(run)
        if not isinstance(task, Task):
            raise ValueError(f"{self.task_file} task(run) must return an ari.Task")
        task = task.resolved(self.task_file.parent)
        environments = {
            "environment": harbor_environment.load(task.environment.harbor) if task.environment.harbor else None,
            "review-environment": (harbor_environment.load(task.review_environment)
                                   if task.review_environment is not None else None),
        }
        if (settings.researcher is not None and environments["environment"] is None and self.image is None) or (
            settings.reviewer is not None and environments["review-environment"] is None
            and self.reviewer_image is None
        ):
            raise ValueError("These settings configure agents, so pass their container image with --image")
        original = ResearcherInputs.resolve(task.instructions, task_facts(
            task.objective, sorted(snapshot(Path(task.seed))), cases=len(task.cases), repeats=settings.repeats,
        ), environment=task.environment, memory=task.memory)
        inputs = _transform(original, task.transforms)
        if settings.reviewer is not None and not inputs.rules:
            raise ValueError("Review is on but the instructions list no rules. Add rules, or write reviewer = false")
        replay = _replay_plan(self.replayed, Path(task.seed), settings) if self.replayed else None
        ledger = run.starts()
        instructions = inputs.specification()

        # Everything an agent may read is copied into the run, sealed by digest
        # in the plan, and checked again whenever the run is reopened.
        staged = {"context": _stage_context(output, task.context)}
        try:
            if task.memory.preload is not None:
                staged["memory/preloaded"] = _stage_tree(output, task.memory.preload)
            for name, environment in environments.items():
                if environment is not None:
                    staged[name] = harbor_environment.stage(environment, output / f".stage-{uuid4().hex}")
            digests = {name: task_context_digest(path) for name, path in staged.items()}
        except BaseException:
            _discard(staged)
            raise

        def environment_identity(name):
            environment = environments[name]
            if environment is None:
                return None
            return digest({"config": environment.config, "files": digests[name],
                           **({"agent_network": environment.agent_network} if environment.agent_network else {})})

        plan = _json({
            **self.opened_with,
            "instructions": instructions,
            "protocol": RESEARCHER_PROTOCOL.template,
            "context": digests["context"],
            "runtime": python_tree_hash(Path(__file__).parent),
            # The researcher's inputs beyond the instructions above and the context.
            "inputs": {
                "memory": task.memory.identity(digests.get("memory/preloaded"),
                                               summarize=_identity(task.memory.summarize)),
                "environment": {"harbor": environment_identity("environment"),
                                "workspace": task.environment.workspace,
                                "candidate_at": task.environment.candidate_at},
                "presenter": _identity(task.presenter) or "default",
                "transforms": [code_identity(transform) for transform in task.transforms],
                "original_instructions": original.specification() if task.transforms else None,
                "rule_names": list(inputs.named_rules),
                "environment_names": list(inputs.named_environment),
                "instruction_split": task.instruction_split,
                "researcher_timeout": task.researcher_timeout,
            },
            "review_environment": environment_identity("review-environment"),
            # What the evaluator says it is, when it says so plainly, such as a Harbor grader's
            # scripts and capabilities. Its code identity is in the experiment's contract either way.
            "evaluator": _evaluator_identity(task.evaluator),
        })
        if self.saved is not None and self.saved != plan:
            _discard(staged)
            raise ValueError("Run plan changed; use a fresh output")
        if source is not None:
            before = _evaluation_plan(json.loads((source / "plan.json").read_text()))
            differ = sorted(key for key, value in _evaluation_plan(plan).items() if before.get(key) != value)
            if differ:
                _discard(staged)
                raise ValueError("Saved evidence cannot be reused: the evaluation contract changed in "
                                 + ", ".join(differ))
        _place(output, staged, digests)
        write_json(output / "plan.json", plan)
        prepared = {
            name: None if environment is None else harbor_environment.HarborEnvironment(
                environment.config, output / name, output / "prepared" / f"{name}.json",
                agent_network=environment.agent_network)
            for name, environment in environments.items()
        }

        research = Research(
            output / "research",
            seed=task.seed,
            objective=task.objective,
            evaluator=task.evaluator,
            validity=_validity(task, inputs, settings, self.reviewer_image, prepared["review-environment"],
                               ledger, instructions),
            evaluation_config={"plan": digest(_evaluation_plan(plan))},
            limits=settings.limits,
            tasks=task.cases,
            progress=self.progress,
            extends=source / "research" if source else None,
        )
        return OpenRun(task, settings, research, output, self.image, ledger, inputs,
                       prepared["environment"], prepared["review-environment"], replay, self.replayed)


def _transform(inputs: ResearcherInputs, transforms) -> ResearcherInputs:
    """Apply a task's input transformations in order. They change content, never policy."""
    for transform in transforms:
        changed = transform(inputs)
        if not isinstance(changed, ResearcherInputs):
            raise ValueError("An input transformation must return ResearcherInputs")
        if changed.memory != inputs.memory or changed.environment != inputs.environment:
            raise ValueError("Input transformations change what the researcher is told, not memory or environment")
        inputs = changed
    return inputs


def _identity(function) -> str | None:
    return None if function is None else code_identity(function)


def _place(output: Path, staged: dict, digests: dict) -> None:
    """Move staged copies into the run, or check that the copies already there are unchanged."""
    changed = ValueError("Run plan changed; use a fresh output")
    for name, path in staged.items():
        final = output / name
        if final.exists() or final.is_symlink():
            try:
                same = task_context_digest(final) == digests[name]
            except ValueError as exc:
                _discard(staged)
                raise changed from exc
            if not same:
                _discard(staged)
                raise changed
            remove_tree(path)
        else:
            final.parent.mkdir(parents=True, exist_ok=True)
            path.rename(final)


def _evaluator_identity(evaluator) -> dict | None:
    identity = getattr(evaluator, "identity", None)
    return identity if isinstance(identity, dict) else None


def _evaluation_plan(plan: dict) -> dict:
    """The parts of a plan that saved validity and evaluation evidence depend on.

    Allocation and the researcher's own settings and inputs are left out, so an
    extension may raise limits or change how researchers work without
    invalidating evidence. Repeats are in the experiment's contract.
    """
    settings = plan["settings"]
    return {
        "task_file": plan["task_file"],
        "image": plan["image"],  # a task may read run.image, as the harness base does for its solver
        "reviewer_image": plan.get("reviewer_image", plan["image"]),
        "review_environment": plan.get("review_environment"),
        "reviewer": settings["reviewer"],
        "task": settings["task"],
        "instructions": plan["instructions"],
        "context": plan["context"],
        "runtime": plan["runtime"],
    }


def _extension_source(source: Path, output: Path, saved) -> Path:
    source = source.resolve()
    if source == output or output.is_relative_to(source) or source.is_relative_to(output):
        raise ValueError("A run made from another run needs its own output, outside the source run")
    if not (source / "plan.json").is_file() or not (source / "research/state.json").is_file():
        raise ValueError(f"No finished run at {source}")
    if saved is None and output.exists() and any(output.iterdir()):
        raise ValueError("A run made from another run needs a fresh output")
    return source


def _lineage(source: Path) -> dict:
    """The source files a run is bound to; any later change to them is refused."""
    files = {"plan": "plan.json", "starts": "starts.json", "state": "research/state.json"}
    return {"source": str(source), **{
        name: sha256_file(source / path) if (source / path).is_file() else None for name, path in files.items()}}


def _replay_plan(source: Path, seed: Path, settings: RunSettings) -> list[dict]:
    """Verified candidates of a finished run, in order, for a new run to review and evaluate again."""
    state = json.loads((source / "research/state.json").read_text())
    candidates = state["sessions"]["research"]["candidates"]
    root = source / "research/sessions/research/history/candidates"
    replay = []
    for candidate_id, record in candidates.items():
        files = snapshot(root / candidate_id / "snapshot")
        if digest(files) != record["source_hash"]:
            raise ValueError(f"The source's {candidate_id} does not match its saved hash; it was edited")
        replay.append({"id": candidate_id, "parent_id": record["parent_id"], "proposal": record["proposal"],
                       "files": files})
    if not replay or replay[0]["id"] != BASELINE or digest(replay[0]["files"]) != digest(snapshot(seed)):
        raise ValueError("Re-evaluation needs the source's baseline to be this task's starting files")
    cells = settings.repeats * len(state["sessions"]["research"]["spec"]["affected_tasks"])
    if settings.limits.max_candidates < len(replay) or settings.limits.max_evaluations < len(replay) * cells:
        raise ValueError(f"Re-evaluating {len(replay)} candidates needs at least {len(replay)} candidates and "
                         f"{len(replay) * cells} evaluations in the limits")
    return replay


def _replay(run: OpenRun) -> None:
    """Record the source's candidates again, in order, so each is reviewed and evaluated under this task."""
    research, repeats = run.research, run.settings.repeats
    for entry in run.replay or ():
        if entry["id"] in research.candidates:
            if not research.measured(entry["id"]) and research.candidates[entry["id"]]["status"] != "invalid":
                research.record_candidate(entry["id"], total_repeats=repeats)
            continue
        research.reset_workspace(entry["files"])
        proposal = {**(entry["proposal"] or {}), "reevaluated_from": entry["id"]}
        result = research.record_candidate(total_repeats=repeats, parent_id=entry["parent_id"], proposal=proposal)
        if result["candidate_id"] != entry["id"]:
            raise RuntimeError(f"Re-evaluated {entry['id']} was saved as {result['candidate_id']}")


def _json(value):
    return json.loads(json.dumps(value))


def _stage_tree(output: Path, source: Path) -> Path:
    if not Path(source).is_dir() or Path(source).is_symlink():
        raise ValueError(f"Memory preload must be a directory: {source}")
    staged = output / f".stage-{uuid4().hex}"
    shutil.copytree(source, staged, symlinks=True)
    return staged


def _discard(staged: dict) -> None:
    for path in staged.values():
        if path.exists():
            remove_tree(path)


def _stage_context(output: Path, files: dict) -> Path:
    staged = output / f".context-{uuid4().hex}"
    staged.mkdir()
    for name, value in files.items():
        target = staged / name
        if Path(name).name != name or name in {".", ".."}:
            raise ValueError(f"Context entries are plain file names, not paths: {name}")
        if isinstance(value, Path) and value.is_dir():
            shutil.copytree(value, target)
        elif isinstance(value, Path):
            shutil.copyfile(value, target)
        elif isinstance(value, str):
            target.write_text(value, encoding="utf-8")
        else:
            write_json(target, value)
    return staged


def _validity(task: Task, inputs: ResearcherInputs, settings: RunSettings, image, environment, ledger,
              instructions: str):
    """The task's validity function, then the reviewer judging each rule the researcher was given.

    The reviewer also receives ``instructions``, the task text the researcher
    was given, so that it can tell what a rule refers to. It gets nothing else:
    no context files, no memory, no evaluator data and no submission steps. It
    runs in its own image or Harbor environment, never the researcher's.
    """
    if settings.reviewer is not None:
        from .review import TASK_SPECIFICATION, LLMValidity

        # One check for each rule, so the verdict says which rule a candidate broke.
        rules = inputs.named_rules
        return LLMValidity(rules, agent=settings.reviewer, image=None if environment else image,
                           environment=environment, ledger=ledger,
                           context={TASK_SPECIFICATION: instructions}, precheck=task.validity)
    return task.validity or _unchecked


def _unchecked(candidate, context):
    return Validity("valid", ("Review is turned off and the task has no validity function",))


def check_run(run: OpenRun) -> dict:
    """Report the opened run without starting, building or pulling anything."""
    return run.summary("checked")


def prepare_run(run: OpenRun) -> dict:
    """Build or resolve the run's Harbor environments and record their image identities.

    Starts no agent and no evaluation. ``run`` and ``record`` do this first
    themselves; calling it separately makes a build failure cost nothing.
    """
    _prepare(run)
    return run.save(run.summary("prepared"))


def _prepare(run: OpenRun) -> None:
    for environment in (run.environment, run.review_environment):
        if environment is not None:
            harbor_environment.prepare(environment)
    # An evaluator that needs images or checks of its own, such as a Harbor grader, does them now,
    # so a failure costs no candidate or start.
    prepare = getattr(run.task.evaluator, "prepare", None)
    if callable(prepare):
        prepare()


def record_workspace(run: OpenRun) -> dict:
    """Save the current workspace as a candidate, then review and evaluate it.

    Every call saves a new candidate, including when the workspace is unchanged.
    """
    repeats = run.settings.repeats
    _prepare(run)
    _replay(run)
    # The workspace was last saved as the newest candidate, so that is this one's parent.
    parent = next(reversed(run.research.candidates), None)
    result = run.research.record_candidate(total_repeats=repeats, parent_id=parent)
    measured = run.research.measured(result["candidate_id"])
    return run.save(run.summary("recorded" if measured else "not_measured", recorded=result["candidate_id"]))


def run_researchers(run: OpenRun) -> dict:
    """Measure the baseline, then let researchers propose until a budget runs out.

    Each researcher starts from the selected candidate and may submit one new
    candidate, or several in one session when the task's memory says so. Rerunning the command continues with whatever budget is left.
    """
    settings, research, ledger = run.settings, run.research, run.ledger
    if settings.researcher is None:
        raise ValueError("run needs a [researcher] in the settings; use record to evaluate your own edits")
    from .coding_agent import CodingAgent

    where = {"image": run.image} if run.environment is None else {"environment": run.environment}
    if run.task.environment.candidate_at is not None:
        where["candidate_at"] = run.task.environment.candidate_at
    # The task's own limit applies unless the settings give one.
    researcher = {**settings.researcher, "override_timeout_sec": settings.researcher.get("override_timeout_sec")
                  or run.task.researcher_timeout or 600}
    agent = CodingAgent(agent=researcher, **where)
    inputs = run.inputs
    present_start = run.task.presenter or present
    repeats = settings.repeats
    panel = len(run.task.cases) * repeats
    proposals, failure = [], None
    try:
        _prepare(run)
        _replay(run)
        if research.candidates:
            research.record_candidate(BASELINE, total_repeats=repeats)
        else:
            research.record_candidate(total_repeats=repeats, proposal={"hypothesis": "Unchanged starting files"})
        if not research.measured(BASELINE):
            raise RuntimeError("The baseline is not valid and fully measured, so no researcher was started")
        while (stopped := _exhausted(run, panel)) is None:
            parent = research.selected or BASELINE
            number = len(researcher_starts(research)) + 1

            def view(parent_id):
                return memory_views.prepare(
                    research, inputs.memory, number=number, parent_id=parent_id, repeats=repeats,
                    preloaded=run.preloaded, view=research.root / "agent/view",
                )

            def advance():
                """After a submission in a continuing session: the next parent, or why there is none."""
                blocked = _exhausted(run, panel, starting=False)
                if blocked is not None:
                    raise StopIteration(blocked)
                following = research.selected or BASELINE
                reset_candidate(research, following)
                view(following)  # refreshes a filtered /history in place
                return following

            start, history, manifest = view(parent)
            start = replace(start, context=any(run.context.iterdir()),
                            candidate_at=run.task.environment.candidate_at)
            try:
                prompt = present_start(inputs, start)
                if not isinstance(prompt, str) or not prompt.strip():
                    raise ValueError("A presenter must return the prompt text")
                proposals.append({"parent_id": parent, **propose(
                    research, agent, prompt, parent_id=parent, repeats=repeats, ledger=ledger,
                    context=run.context, history=history,
                    retain=inputs.environment.workspace == "keep",
                    submissions=start.submissions, advance=advance, candidate=candidate_path(start),
                    record={**manifest, "presenter": _identity(run.task.presenter) or "default",
                            "environment": run.environment.prepared() if run.environment else None},
                )})
            finally:
                memory_views.discard(history)
    except Exception as error:
        failure = f"{type(error).__name__}: {error}"
    summary = run.summary("failed" if failure else "completed", proposals=proposals)
    if failure:
        summary["error"] = failure
    else:
        summary["stopped"] = stopped
        summary["improved"] = research.selected not in (None, BASELINE)
    return run.save(summary)


def _exhausted(run: OpenRun, panel: int, starting: bool = True) -> str | None:
    """Name the budget that prevents another proposal, or None when one more fits.

    ``starting=False`` asks about another proposal within a session that is
    already running, which needs no further researcher start.
    """
    research, ledger = run.research, run.ledger
    if starting and ledger.remaining("researcher") < 1:
        return "researcher starts used"
    if research.proposals >= run.settings.limits.max_candidates:
        return "candidate limit reached"
    if research.available_trial_start_capacity < panel:
        return "too few evaluations left for another candidate"
    if run.reviewed and ledger.remaining("reviewer") < 1:
        return "reviewer starts used"
    return None
