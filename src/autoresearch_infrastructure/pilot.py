"""A harness study on Harbor benchmark tasks: screen proposals, then confirm the winner.

Researchers propose solver programs against a small *screen* panel of tasks. The
selected program and the baseline are then measured on a separate *confirmation*
panel that no researcher saw. Each panel is its own experiment; one ledger counts
the starts of both, across retries.

Run ``--check`` first, then ``--reference`` for the original task controls.
``--run`` starts paid model sessions within the plan's limits.
"""

from __future__ import annotations

import argparse
import json
import subprocess
import tomllib
from pathlib import Path

from .api import Limits, Objective, Validity
from .coding_agent import CodingAgent
from .harbor_build import ca_build_overlay
from .harbor_filesystem import native_agent_config
from .harbor_harness import HARNESS_RULES, HarborHarnessEvaluator, researcher_goal, write_researcher_context
from .harbor_trial import SOLVER_TOKENS, SUCCESS_RATE
from .instructions import Instructions, task_facts
from .ledger import StartLedger
from .persistence import digest, write_json
from .programs import starter
from .programs.package import program_manifest, validate_program
from .providers import provider_for
from .research import BASELINE, Research
from .review import LLMValidity
from .search import propose
from .search import researcher_prompt as with_protocol
from .source_tree import evidence_digest, materialize, python_tree_hash, snapshot
from .viewer import write_html_preview

FIDELITIES = ("screen", "confirmation")


def prior_new_candidates(ledger, output):
    """Count proposals already made under this ledger in other outputs.

    A fresh output must not restart the study's candidate allowance. The outputs
    are found from the ledger's researcher starts, never from candidate files.
    Baselines are not proposals.
    """
    prior_outputs = {
        Path(start["label"])
        for start in ledger.snapshot()["starts"].values()
        if start["kind"] == "researcher" and Path(start["label"]).resolve() != output.resolve()
    }
    total = 0
    for previous in prior_outputs:
        path = previous / "state.json"
        if not previous.is_absolute() or path.is_symlink():
            raise ValueError("Prior proposal accounting requires protected controller state")
        try:
            session = json.loads(path.read_text())["sessions"]["research"]
            count = session["new_proposals"]
            if type(count) is not int or count < 1 or BASELINE not in session["candidates"]:
                raise ValueError("Invalid prior proposal count or missing baseline")
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ValueError("Cannot establish prior proposal charges from controller state") from exc
        total += count - 1
    return total


def _objective(plan) -> Objective:
    return Objective(
        SUCCESS_RATE,
        tie_breakers=(SOLVER_TOKENS,) if plan.get("harness", {}).get("include_solver_tokens") else (),
        description="Improve task scores using the same model and session limits",
    )


def researcher_prompt(plan, parent_id):
    goal = Instructions.parse(
        researcher_goal(sessions=plan["max_sessions_per_trial"], advice=plan.get("research_guidance", ""))
    )
    screen = plan["screen"]
    facts = task_facts(_objective(plan), ["program.toml", "solve.py"],
                       cases=screen["cases"], repeats=screen["repeats"])
    return with_protocol(goal.render(facts), parent_id=parent_id, repeats=screen["repeats"])


class ProgramValidity(LLMValidity):
    """Checks the solver package, then asks the reviewer.

    The exact baseline is accepted without a review. ``reserved_reviews`` holds
    reviewer starts back for a later stage of the same study.
    """

    def __init__(self, criteria=None, *, harness="codex", reserved_reviews=0, ledger, **kwargs):
        super().__init__(criteria or HARNESS_RULES, ledger=ledger, **kwargs)
        if type(reserved_reviews) is not int or reserved_reviews < 0:
            raise ValueError("Reserved reviews must be a nonnegative integer")
        self.harness, self.reserved_reviews = harness, reserved_reviews

    @property
    def identity(self):
        return {**super().identity, "harness": self.harness, "reserved_reviews": self.reserved_reviews}

    def __call__(self, candidate, context):
        try:
            raw = program_manifest(candidate)
            validate_program(candidate, raw["id"], harness=self.harness)
            if "reasoning_effort" in raw.get("kwargs", {}):
                return Validity("invalid", ("Runtime reasoning effort is frozen; omit explicit overrides",))
        except (ValueError, OSError) as exc:
            return Validity("invalid", (str(exc),))
        if context.candidate_hash == self.baseline_hash:
            return Validity("valid", ("Exact controller-supplied baseline; package checks passed",))
        self.ledger.require_capacity({"reviewer": 1 + self.reserved_reviews})
        return super().__call__(candidate, context)


def _image_id(image: str, role: str) -> str:
    inspected = subprocess.run(
        ["docker", "image", "inspect", image, "--format", "{{.Id}}"],
        capture_output=True, text=True, check=True,
    ).stdout.strip()
    if not inspected.startswith("sha256:"):
        raise ValueError(f"An immutable local {role} image is required")
    return inspected


def _check_plan(plan: dict) -> list[str]:
    """Check that the plan's panels and limits agree, and return its task IDs."""
    if (
        sum(plan[f]["max_solver_trial_starts"] for f in FIDELITIES) != plan["max_solver_trial_starts"]
        or plan["max_solver_trial_starts"] * plan["max_sessions_per_trial"]
        != plan["max_harness_session_starts"]
    ):
        raise ValueError("Fidelity allocations must fit the cumulative trial/session ceilings")
    if plan.get("max_parallel_trials", 1) != 1:
        raise ValueError("This pilot owns sequential isolated task environments")
    for fidelity in FIDELITIES:
        panel = plan[fidelity]
        if panel["cases"] * panel["repeats"] * panel["candidates_including_baseline"] > (
            panel["max_solver_trial_starts"]
        ):
            raise ValueError("Reserve the complete declared fidelity allocation")
    # The plan pins both panels. They are never chosen from model scores.
    benchmark = plan["benchmark"]
    selected = benchmark["screen_task_ids"] + benchmark["confirmation_task_ids"]
    if (
        not selected
        or any(Path(n).name != n or n in {".", ".."} for n in selected)
        or len(set(selected)) != len(selected)
        or len(selected) != plan["screen"]["cases"] + plan["confirmation"]["cases"]
    ):
        raise ValueError("Pilot requires disjoint task IDs matching the declared panels")
    negative = benchmark["negative_control_task_ids"]
    if not negative or len(set(negative)) != len(negative) or not set(negative) <= set(selected):
        raise ValueError("Pin negative controls within the selected task panel")
    if plan["max_reference_trial_starts"] < len(selected) + len(negative):
        raise ValueError("Reserve all original positive and negative controls")
    return selected


def prepare(
    plan_path: Path,
    tasks_root: Path,
    output: Path,
    image: str,
    allocation_ledger: Path | None = None,
    fixed_candidate: Path | None = None,
    *,
    researcher_image: str | None = None,
    reviewer_image: str | None = None,
):
    """Validate the plan, freeze it in ``plan.json`` and open the study's ledger.

    Returns ``(plan, tasks, overlays, identity, ledger, solver_image_id)``. No
    agent or trial starts here.
    """
    plan = tomllib.loads(plan_path.read_text())
    harness = plan["harness"].get("name", "codex")
    provider_for(harness, plan["solver_provider"])
    native_agent_config(plan["researcher"], default_timeout=None, researcher=True)
    native_agent_config(plan["reviewer"], default_timeout=None)
    selected = _check_plan(plan)
    tasks = {name: tasks_root / name for name in selected}
    hashes = {name: evidence_digest(path, max_total_bytes=100_000_000) for name, path in tasks.items()}
    solver_image = _image_id(image, "solver")
    agent_images = {
        role: solver_image if configured in (None, image) else _image_id(configured, role)
        for role, configured in (("researcher", researcher_image), ("reviewer", reviewer_image))
    }
    output.mkdir(parents=True, exist_ok=True)
    identity = {
        "plan": plan,
        "task_hashes": hashes,
        "solver_image_id": solver_image,
        "agent_images": agent_images,
        "runtime": digest({
            "python": python_tree_hash(Path(__file__).parent),
            "researcher_prompt": researcher_prompt(plan, "candidate-0000"),
            "solver_usage": Path(starter.__file__).with_name("usage.md").read_text(),
        }),
    }
    if fixed_candidate is not None:
        identity["fixed_candidate"] = {"hash": digest(snapshot(fixed_candidate)), "mode": "no-proposer"}
    receipt = output / "plan.json"
    if receipt.exists() and json.loads(receipt.read_text()) != json.loads(json.dumps(identity)):
        raise ValueError("Pilot plan/runtime changed; use a fresh output")
    write_json(receipt, identity)
    overlays = {}
    for name, task in tasks.items():
        destination = output / "build-overlays" / name
        overlays[name] = (
            destination / "compose.json"
            if destination.exists()
            else ca_build_overlay(
                task,
                destination,
                Path("/etc/ssl/certs/ca-certificates.crt"),
                uv_version=plan["benchmark"].get("uv_install_version"),
            )
        )
    identity["build_overlays"] = {name: evidence_digest(path.parent) for name, path in overlays.items()}
    # The ledger outlives one output, so its identity omits what a retry may change.
    identity["allocation_identity"] = {
        key: value
        for key, value in identity.items()
        if key not in {"runtime", "build_overlays", "fixed_candidate", "agent_images"}
    }
    ledger = StartLedger(
        allocation_ledger or output / "paid-starts.json",
        limits={
            "solver_trial": plan["max_solver_trial_starts"],
            "solver_session": plan["max_harness_session_starts"],
            "researcher": plan["max_researcher_invocations"],
            "reviewer": plan["max_reviewer_invocations"],
            "reference_trial": plan["max_reference_trial_starts"],
        },
        identity=identity["allocation_identity"],
    )
    return plan, tasks, overlays, identity, ledger, solver_image


def _require_capacity(plan, ledger, new_candidates: int, *, fixed: bool) -> None:
    """Before the first start, require room in the ledger for the whole planned study."""
    screen, confirmation = plan["screen"], plan["confirmation"]
    confirm_trials = confirmation["cases"] * confirmation["repeats"] * 2
    if fixed:
        trials = screen["cases"] * screen["repeats"] * 2 + confirm_trials
        agents = {"reviewer": 2}
    else:
        trials = screen["cases"] * screen["repeats"] * (1 + new_candidates) + confirm_trials
        agents = {"researcher": new_candidates, "reviewer": new_candidates + (1 if new_candidates else 0)}
    ledger.require_capacity({
        "solver_trial": trials,
        "solver_session": trials * plan["max_sessions_per_trial"],
        **agents,
    })


def _require_controls(plan, tasks, identity, output) -> None:
    """Paid sessions need passing original controls for these exact task and build bytes."""
    from .canary import verified_control

    references = json.loads((output / "references.json").read_text())
    if references.get("all_passed") is not True or any(
        references.get(key) != identity[key] for key in ("task_hashes", "build_overlays")
    ):
        raise ValueError("Run original controls for these exact task/build bytes before paid use")
    controls = {
        f"{agent}-{name}": verified_control(output, agent, name, identity)
        for agent in ("oracle", "nop")
        for name in tasks
        if agent == "oracle" or name in plan["benchmark"]["negative_control_task_ids"]
    }
    if references.get("controls") != controls:
        raise ValueError("Reference receipt differs from original control evidence")


def _record(research, candidate_id: str, repeats: int, **new):
    """Measure a saved candidate's missing repeats, or record the workspace as that candidate."""
    if candidate_id in research.candidates:
        return research.record_candidate(candidate_id, total_repeats=repeats)
    return research.record_candidate(total_repeats=repeats, **new)


def run(
    plan_path: Path,
    tasks_root: Path,
    output: Path,
    image: str,
    *,
    check=False,
    allocation_ledger=None,
    fixed_candidate=None,
    researcher_image=None,
    reviewer_image=None,
):
    plan, tasks, overlays, identity, ledger, image_id = prepare(
        plan_path, tasks_root, output, image, allocation_ledger, fixed_candidate,
        researcher_image=researcher_image, reviewer_image=reviewer_image,
    )
    fixed_payload = snapshot(fixed_candidate) if fixed_candidate is not None else None
    if fixed_payload is not None and digest(fixed_payload) != identity["fixed_candidate"]["hash"]:
        raise ValueError("Frozen candidate changed after preflight")
    new_candidates = max(0, plan["max_new_candidates"] - prior_new_candidates(ledger, output / "screen"))
    if not (output / "screen/state.json").exists():
        _require_capacity(plan, ledger, new_candidates, fixed=fixed_payload is not None)
    if check:
        print(json.dumps({
            "status": "ready_for_reference_checks",
            "plan": str(output / "plan.json"),
            "ledger": ledger.snapshot()["counts"],
            "launches_paid_models": False,
        }))
        return
    if plan.get("runnable") is not True:
        raise ValueError("Selected plan is blocked/non-runnable; inspect its status")
    _require_controls(plan, tasks, identity, output)

    harness = plan["harness"].get("name", "codex")
    token_tie_breaker = plan["harness"].get("include_solver_tokens", False)
    solver_provider = provider_for(harness, plan["solver_provider"])
    solver_provider.key()
    seed = output / "seed"
    if not seed.exists():
        materialize(seed, {
            "program.toml": 'id="benchmark-harness"\ndescription="Shared task solver"\nentrypoint="solve.py:solve"\n',
            "solve.py": Path(starter.__file__).read_text(),
        })
    seed_hash = digest(snapshot(seed))
    rules = plan.get("validity")
    context = write_researcher_context(output / "research-context", harness, rules or HARNESS_RULES)

    def experiment(fidelity):
        """The experiment for one panel. Both panels share the seed, objective and ledger."""
        panel, names = plan[fidelity], plan["benchmark"][fidelity + "_task_ids"]
        evaluator = HarborHarnessEvaluator(
            tasks={n: tasks[n] for n in names},
            solver_image=image_id,
            model=plan["solver_model"],
            harness=harness,
            provider=solver_provider,
            ledger=ledger,
            timeout_seconds=plan["solver_timeout_seconds"],
            max_harness_sessions=plan["max_sessions_per_trial"],
            include_solver_tokens=token_tie_breaker,
            compose_overlays={n: overlays[n] for n in names},
        )
        reviewer = ProgramValidity(
            rules,
            agent=plan["reviewer"],
            image=identity["agent_images"]["reviewer"],
            ledger=ledger,
            harness=harness,
            # Screening leaves one review for the confirmed winner.
            reserved_reviews=1 if fidelity == "screen" else 0,
            context={
                "editable_scope": "Only generic program, prompts, skills and hooks under /review/candidate",
                "protected_by_controller": "Original benchmark definitions and verifier, references, model/provider, inference settings, permissions and budgets",
                "benchmark_definition_hashes": digest(identity["task_hashes"]),
                "build_overlay_hashes": digest(identity["build_overlays"]),
                "enforcement": "Task source bytes are checked before each isolated trial; original grading runs outside candidate program control",
                "baseline_hash": seed_hash,
            },
        )
        return Research(
            output / fidelity,
            seed=seed,
            objective=_objective(plan),
            evaluator=evaluator,
            validity=reviewer,
            evaluation_config={"fidelity": fidelity, "pilot": identity},
            limits=Limits(
                max_evaluations=panel["max_solver_trial_starts"],
                max_candidates=2 if fixed_payload is not None else panel["candidates_including_baseline"],
                max_repeats=panel["repeats"],
                selection_repeats=panel["repeats"],
            ),
            tasks=tuple(names),
            conditions=(fidelity,),
        )

    with experiment("screen") as research:
        repeats = plan["screen"]["repeats"]
        baseline = _record(research, BASELINE, repeats, proposal={"hypothesis": "Unchanged one-session baseline"})
        if baseline["evidence"]["evaluation"] != "completed":
            raise RuntimeError("Baseline panel is incomplete; do not start paid search")
        if fixed_payload is not None:
            if "candidate-0002" not in research.candidates:
                research.reset_workspace(fixed_payload)
            _record(research, "candidate-0002", repeats, parent_id=BASELINE, proposal={
                "hypothesis": "Evaluate the preregistered frozen program with a fresh baseline",
                "mechanism": "No proposer invocation; exact supplied source bytes",
                "source_hash": digest(fixed_payload),
            })
        else:
            agent = CodingAgent(agent=plan["researcher"], image=identity["agent_images"]["researcher"])
            # One review is held back for confirmation, and each researcher start
            # can record at most one candidate.
            target = 1 + min(new_candidates, plan["max_reviewer_invocations"] - 1)
            while ledger.remaining("researcher") > 0 and research.proposals < target:
                parent_id = research.selected or BASELINE
                propose(
                    research, agent, researcher_prompt(plan, parent_id),
                    parent_id=parent_id, repeats=repeats, ledger=ledger, context=context,
                )
        if not research.selected:
            raise RuntimeError("No completed screening candidate to confirm")
        winner = snapshot(research.candidate_files(research.selected))
        promotion = {
            "source_hash": digest(winner),
            "screen_contract": research.contract_hash,
            "screen_candidate": research.selected,
        }
        write_json(output / "promotion.json", promotion)

    # Confirmation measures the baseline again on its own panel, then the winner
    # by its exact bytes. If the baseline won the screen, it is confirmed alone.
    with experiment("confirmation") as confirmation:
        repeats = plan["confirmation"]["repeats"]
        _record(confirmation, BASELINE, repeats, proposal={"hypothesis": "Unchanged confirmation baseline"})
        if digest(winner) != seed_hash:
            if "candidate-0002" not in confirmation.candidates:
                confirmation.reset_workspace(winner)
            _record(confirmation, "candidate-0002", repeats, parent_id=BASELINE, proposal=promotion)
        complete = all(
            confirmation.evidence(candidate_id).get("evaluation") == "completed"
            for candidate_id in confirmation.candidates
        )
        write_json(output / "summary.json", {
            "status": "completed" if complete else "incomplete",
            "promotion": promotion,
            "fixed_candidate": identity.get("fixed_candidate"),
            "confirmation": confirmation.state,
            "paid_starts": ledger.snapshot(),
        })
        if not complete:
            raise RuntimeError("Confirmation is incomplete; evidence and paid starts retained")
    for fidelity in FIDELITIES:
        target = output / (fidelity + ".html")
        if not target.exists():
            write_html_preview(output / fidelity, target)
    print(json.dumps({"status": "completed", "summary": str(output / "summary.json")}))


def reference(plan_path, tasks_root, output, image, allocation_ledger=None, fixed_candidate=None,
              *, researcher_image=None, reviewer_image=None):
    from .canary import controls

    plan, tasks, overlays, identity, ledger, _ = prepare(
        plan_path, tasks_root, output, image, allocation_ledger, fixed_candidate,
        researcher_image=researcher_image, reviewer_image=reviewer_image,
    )
    result = controls(
        tasks, overlays, identity, ledger, output, plan["benchmark"]["negative_control_task_ids"]
    )
    print(json.dumps({"status": "all_references_passed", "controls": len(result), "paid_calls": 0}))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--plan", type=Path, required=True)
    parser.add_argument("--tasks-root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--image", default="ari-solver:local", help="Solver image")
    parser.add_argument("--researcher-image", help="Researcher image; defaults to the solver image")
    parser.add_argument("--reviewer-image", help="Reviewer image; defaults to the solver image")
    parser.add_argument("--allocation-ledger", type=Path,
                        help="An existing ledger file, to keep one budget across several outputs")
    parser.add_argument("--fixed-candidate", type=Path,
                        help="Evaluate this exact solver program against a fresh baseline, with no researcher")
    choice = parser.add_mutually_exclusive_group(required=True)
    choice.add_argument("--check", action="store_true", help="Validate the plan and budget; start nothing")
    choice.add_argument("--reference", action="store_true", help="Run the original task controls; no models")
    choice.add_argument("--run", action="store_true", help="Run the study; starts paid model sessions")
    args = parser.parse_args()
    study = (args.plan, args.tasks_root, args.output, args.image, args.allocation_ledger, args.fixed_candidate)
    images = {"researcher_image": args.researcher_image, "reviewer_image": args.reviewer_image}
    ledger = prepare(*study, **images)[4]
    # Hold the ledger for the whole command, so no other controller can spend from it.
    with ledger.allocation_owner():
        if args.reference:
            reference(*study, **images)
        else:
            run(args.plan, args.tasks_root, args.output, args.image, check=args.check,
                allocation_ledger=args.allocation_ledger, fixed_candidate=args.fixed_candidate, **images)


if __name__ == "__main__":
    main()
