"""Scripted proposals for the packing task: no agents and no model calls."""

import argparse
from pathlib import Path

from autoresearch_infrastructure.launch import open_run
from autoresearch_infrastructure.persistence import write_json

HERE = Path(__file__).parent
PROPOSALS = (
    ["book"],                     # the unchanged baseline
    ["water", "food", "map"],     # the best selection that fits
    ["unknown"],                  # invalid: not in the catalog
    ["water", "food", "book"],    # more value, but over the weight limit
)


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True, help="A new run directory")
    args = parser.parse_args(argv)
    run = open_run(HERE, HERE / "run.toml", args.output, image=None)
    with run.research as research:
        for names in PROPOSALS:
            write_json(research.workspace / "items.json", names)
            result = research.record_candidate(
                parent_id=None if names == PROPOSALS[0] else "candidate-0001",
                total_repeats=run.settings.repeats,
                proposal={"hypothesis": "Higher value within the weight limit", "items": names},
            )
            evidence = result["evidence"]
            print(result["candidate_id"], result["status"], evidence["metrics"],
                  "feasible" if evidence.get("feasible") else "not selectable")
        summary = run.save(run.summary("completed"))
    print("Selected:", summary["selected"])
    print("Viewer:", summary["viewer"])
    return summary


if __name__ == "__main__":
    main()
