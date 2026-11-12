"""Check the sum task with Harbor's reference agents: no models are called.

Oracle runs the task's reference solution and must score 1. Nop does nothing
and must score 0. Each run records the baseline and one edited candidate, and
confirms that the edit reached the container. Needs Docker.
"""

import argparse
import json
from pathlib import Path

from task import validity as task_validity

import autoresearch_infrastructure as ari
from autoresearch_infrastructure.harbor_task import HarborTaskEvaluator, task_with_image
from autoresearch_infrastructure.harbor_trial import SUCCESS_RATE
from autoresearch_infrastructure.ledger import StartLedger
from autoresearch_infrastructure.viewer import write_html_preview

HERE = Path(__file__).parent


def validity(candidate, context):
    verdict = task_validity(candidate, context)
    grader = candidate / "sum/tests/test.sh"
    # With the whole task editable, the grader must still be the original.
    if grader.exists() and grader.read_bytes() != (HERE / "harbor-task/tests/test.sh").read_bytes():
        return ari.Validity("invalid", ("The task's grader is fixed",))
    return verdict


def run(output: Path, image: str, agent: str, whole_task: bool):
    output = output.resolve()
    harbor_task = task_with_image(HERE / "harbor-task", image, output / "harbor-task")
    ledger = StartLedger(output / "starts.json", limits={"solver_trial": 2},
                         identity={"example": "sum", "agent": agent, "image": image})
    evaluator = HarborTaskEvaluator(
        tasks={"sum": harbor_task}, agent={"name": agent, "override_timeout_sec": 30}, ledger=ledger,
        editable=None if whole_task else {"sum": ["instruction.md"]},
    )
    with ari.Research(
        output / "research", seed=evaluator.seed(output / "seed"), objective=SUCCESS_RATE,
        evaluator=evaluator, validity=validity, evaluation_config={"evidence": "reference agents, no model"},
        tasks=("sum",), limits=ari.Limits(max_candidates=2, max_evaluations=2),
    ) as research:
        baseline = research.record_candidate()
        (research.workspace / "sum/instruction.md").write_text(
            "Calculate the sum of /workspace/numbers.txt. "
            "Write its integer total to /workspace/answer.txt; do not include prose.\n"
        )
        if whole_task:
            (research.workspace / "sum/environment/numbers.txt").write_text("1 4\n")
        edited = research.record_candidate(parent_id=baseline["candidate_id"])
        for result in (baseline, edited):
            if result["evidence"]["evaluation"] != "completed":
                raise RuntimeError("A trial did not finish; inspect the run directory")
            if result["evidence"]["metrics"]["success_rate"] != (1 if agent == "oracle" else 0):
                raise AssertionError(f"{agent} returned the wrong reward")
            log = next(research.candidate_files(result["candidate_id"]).parent.rglob("test-stdout.txt"))
            numbers = json.loads(log.read_text())["numbers"]
            if numbers != ("1 4" if whole_task and result is edited else "2 3"):
                raise AssertionError("The edited input did not reach the Harbor container")
            print(json.dumps({"candidate": result["candidate_id"], "status": result["status"],
                              "metrics": result["evidence"]["metrics"], "observed_numbers": numbers}))
        write_html_preview(research.output, output / "viewer.html")
    print("Harbor trial starts:", ledger.snapshot()["counts"]["solver_trial"])


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path, help="A new run directory")
    parser.add_argument("--image", required=True, help="Image ID built from harbor-task/environment")
    parser.add_argument("--agent", choices=("oracle", "nop"), default="oracle")
    parser.add_argument("--whole-task", action="store_true",
                        help="Make every task file editable and also change numbers.txt")
    args = parser.parse_args()
    run(args.output, args.image, args.agent, args.whole_task)
