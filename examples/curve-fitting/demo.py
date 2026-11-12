"""Scripted proposals for the curve-fitting task: no agents and no model calls.

Records the zero baseline, three least-squares fits, and one out-of-range
proposal that the validity check rejects.
"""

import argparse
import json
from pathlib import Path

from autoresearch_infrastructure.launch import open_run
from autoresearch_infrastructure.persistence import write_json

HERE = Path(__file__).parent


def least_squares(observations, degree):
    """Coefficients of the best-fitting polynomial, by solving the normal equations."""
    size = degree + 1
    rows = [[sum(x ** (i + j) for x, _ in observations) for j in range(size)]
            + [sum(y * x ** i for x, y in observations)] for i in range(size)]
    for column in range(size):
        pivot = max(range(column, size), key=lambda row: abs(rows[row][column]))
        rows[column], rows[pivot] = rows[pivot], rows[column]
        divisor = rows[column][column]
        rows[column] = [value / divisor for value in rows[column]]
        for row in range(size):
            if row != column:
                factor = rows[row][column]
                rows[row] = [value - factor * base for value, base in zip(rows[row], rows[column])]
    return [row[-1] for row in rows]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="A new run directory")
    parser.add_argument("--json", action="store_true", help="Print one JSON summary")
    args = parser.parse_args(argv)
    if args.output.exists():
        parser.error("the demo needs a new output directory")
    run = open_run(HERE, HERE / "local.toml", args.output, image=None)
    # Read the observations where a researcher would: the run's read-only context.
    observations = json.loads((run.context / "task.json").read_text())["observations"]
    proposals = (
        ([0.0], "Baseline: predict zero"),
        (least_squares(observations, 0), "Fit a constant to the observations"),
        (least_squares(observations, 1), "Allow a slope: fit a straight line"),
        (least_squares(observations, 2), "Allow curvature: fit a quadratic"),
        ([1000.0], "Forbidden proposal: a coefficient outside the allowed range"),
    )
    with run.research as research:
        parent = None
        for coefficients, hypothesis in proposals:
            write_json(research.workspace / "coefficients.json", {"coefficients": coefficients})
            result = research.record_candidate(parent_id=parent, proposal={"hypothesis": hypothesis})
            if result["status"] == "valid":
                parent = result["candidate_id"]
        summary = run.save(run.summary("completed"))
    if args.json:
        print(json.dumps(summary))
    else:
        for row in summary["candidates"]:
            metrics = {name: round(value, 2) for name, value in (row["metrics"] or {}).items()}
            print(row["candidate_id"], row["status"], row["evaluation"], metrics or row["reasons"])
        print("Selected:", summary["selected"])
        print("Viewer:", summary["viewer"])
    return summary


if __name__ == "__main__":
    main()
