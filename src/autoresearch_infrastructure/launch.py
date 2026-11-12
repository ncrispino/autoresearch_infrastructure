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

import hashlib
import json
import shutil
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .api import Validity
from .instructions import task_facts
from .ledger import StartLedger
from .persistence import digest, write_json
from .research import BASELINE, Research
from .search import RESEARCHER_PROTOCOL, propose, researcher_prompt
from .source_tree import evidence_digest, python_tree_hash, snapshot
from .task import Run, RunSettings, Task, load_task


@dataclass
class OpenRun:
    task: Task
    settings: RunSettings
    research: Research
    output: Path
    image: str | None
    ledger: StartLedger | None
    instructions: str = ""

    @property
    def context(self) -> Path:
        return self.output / "context"

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
             progress=None) -> OpenRun:
    """Load the task and settings, freeze them in ``plan.json``, and open the experiment.

    This starts no agent and no evaluation. Reopening an output with a different
    task, settings or image is refused, so a run's budget cannot be reset.
    """
    define, task_file = load_task(task_path)
    settings = RunSettings.load(settings_path) if settings_path else RunSettings.parse({})
    if settings.researcher or settings.reviewer:
        from .harbor_filesystem import immutable_image

        if image is None:
            raise ValueError("These settings configure agents, so pass their container image with --image")
        immutable_image(image, "A run with agents")
    output = Path(output).resolve()
    opened_with = _json({
        "settings": settings.identity(),
        "image": image,
        "task_file": hashlib.sha256(task_file.read_bytes()).hexdigest(),
    })
    receipt = output / "plan.json"
    saved = json.loads(receipt.read_text()) if receipt.exists() else None
    changed = ValueError("Run plan changed; use a fresh output")
    if saved is not None and any(saved.get(key) != value for key, value in opened_with.items()):
        raise changed

    output.mkdir(parents=True, exist_ok=True)
    run = Run(settings, output, image, directory=task_file.parent, identity=opened_with)
    task = define(run)
    if not isinstance(task, Task):
        raise ValueError(f"{task_file} task(run) must return an ari.Task")
    task = task.resolved(task_file.parent)
    ledger = run.starts()
    if settings.reviewer is not None and not task.instructions.rules:
        raise ValueError("Review is on but the instructions list no rules. Add rules, or write reviewer = false")
    instructions = task.instructions.render(task_facts(
        task.objective, sorted(snapshot(Path(task.seed))), cases=len(task.cases), repeats=settings.repeats,
    ))
    staged = _stage_context(output, task.context)
    plan = _json({
        **opened_with,
        "instructions": instructions,
        "protocol": RESEARCHER_PROTOCOL.template,
        "context": evidence_digest(staged),
        "runtime": python_tree_hash(Path(__file__).parent),
    })
    if saved is not None and saved != plan:
        shutil.rmtree(staged)
        raise changed
    if (output / "context").exists():
        shutil.rmtree(staged)
    else:
        staged.rename(output / "context")
    write_json(receipt, plan)

    research = Research(
        output / "research",
        seed=task.seed,
        objective=task.objective,
        evaluator=task.evaluator,
        validity=_validity(task, settings, image, ledger),
        evaluation_config={"plan": digest(plan)},
        limits=settings.limits,
        tasks=task.cases,
        progress=progress,
    )
    return OpenRun(task, settings, research, output, image, ledger, instructions)


def _json(value):
    return json.loads(json.dumps(value))


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


def _validity(task: Task, settings: RunSettings, image, ledger):
    """The task's validity function, then the reviewer judging each rule the researcher was given."""
    if settings.reviewer is not None:
        from .review import LLMValidity

        # One check for each rule, so the verdict says which rule a candidate broke.
        rules = {f"rule_{number}": rule for number, rule in enumerate(task.instructions.rules, 1)}
        return LLMValidity(rules, agent=settings.reviewer, image=image, ledger=ledger, precheck=task.validity)
    return task.validity or _unchecked


def _unchecked(candidate, context):
    return Validity("valid", ("Review is turned off and the task has no validity function",))


def check_run(run: OpenRun) -> dict:
    """Report the opened run without starting anything."""
    return run.summary("checked")


def record_workspace(run: OpenRun) -> dict:
    """Save the current workspace as a candidate, then review and evaluate it.

    Every call saves a new candidate, including when the workspace is unchanged.
    """
    repeats = run.settings.repeats
    # The workspace was last saved as the newest candidate, so that is this one's parent.
    parent = next(reversed(run.research.candidates), None)
    result = run.research.record_candidate(total_repeats=repeats, parent_id=parent)
    measured = run.research.measured(result["candidate_id"])
    return run.save(run.summary("recorded" if measured else "not_measured", recorded=result["candidate_id"]))


def run_researchers(run: OpenRun) -> dict:
    """Measure the baseline, then let researchers propose until a budget runs out.

    Each researcher starts from the selected candidate and may submit one new
    candidate. Rerunning the command continues with whatever budget is left.
    """
    settings, research, ledger = run.settings, run.research, run.ledger
    if settings.researcher is None:
        raise ValueError("run needs a [researcher] in the settings; use record to evaluate your own edits")
    from .coding_agent import CodingAgent

    agent = CodingAgent(agent=settings.researcher, image=run.image)
    repeats = settings.repeats
    panel = len(run.task.cases) * repeats
    proposals, failure = [], None
    try:
        if research.candidates:
            research.record_candidate(BASELINE, total_repeats=repeats)
        else:
            research.record_candidate(total_repeats=repeats, proposal={"hypothesis": "Unchanged starting files"})
        if not research.measured(BASELINE):
            raise RuntimeError("The baseline is not valid and fully measured, so no researcher was started")
        while (stopped := _exhausted(run, panel)) is None:
            parent = research.selected or BASELINE
            proposals.append({"parent_id": parent, **propose(
                research, agent, researcher_prompt(run.instructions, parent_id=parent, repeats=repeats),
                parent_id=parent, repeats=repeats, ledger=ledger, context=run.context,
            )})
    except Exception as error:
        failure = f"{type(error).__name__}: {error}"
    summary = run.summary("failed" if failure else "completed", proposals=proposals)
    if failure:
        summary["error"] = failure
    else:
        summary["stopped"] = stopped
        summary["improved"] = research.selected not in (None, BASELINE)
    return run.save(summary)


def _exhausted(run: OpenRun, panel: int) -> str | None:
    """Name the budget that prevents another proposal, or None when one more fits."""
    research, ledger = run.research, run.ledger
    if ledger.remaining("researcher") < 1:
        return "researcher starts used"
    if research.proposals >= run.settings.limits.max_candidates:
        return "candidate limit reached"
    if research.available_trial_start_capacity < panel:
        return "too few evaluations left for another candidate"
    if run.reviewed and ledger.remaining("reviewer") < 1:
        return "reviewer starts used"
    return None
