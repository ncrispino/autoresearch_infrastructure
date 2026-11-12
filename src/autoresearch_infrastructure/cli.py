"""The ``ari`` command: open a task, run it, and view the results."""

import argparse
import json
import sys
from pathlib import Path


def main(argv=None):
    parser = argparse.ArgumentParser(prog="ari", description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    actions = {
        "check": "Open the task and settings and print the budget. Starts nothing.",
        "record": "Save the current workspace as a candidate, then review and evaluate it.",
        "run": "Measure the baseline, then start researchers until a budget runs out. May incur charges.",
    }
    for name, summary in actions.items():
        command = commands.add_parser(name, help=summary, description=summary)
        command.add_argument("task", type=Path, help="A task file, or a directory containing task.py")
        command.add_argument("--output", type=Path, required=True, help="Run directory")
        command.add_argument("--config", type=Path, help="Run settings; default is run.toml beside the task")
        command.add_argument("--image", help="Immutable image ID for the researcher and reviewer")
        command.add_argument("--json", action="store_true", help="Print one JSON summary and no progress display")
    for name, summary in {
        "view": "Export the results of a run as one HTML page.",
        "inspect": "Print the results of a run as JSON.",
    }.items():
        command = commands.add_parser(name, help=summary, description=summary)
        command.add_argument("output", type=Path, help="A run directory or an experiment output")
        if name == "view":
            command.add_argument("--html", type=Path, required=True, help="A new file to write")
        command.add_argument("--partial-evidence", type=Path,
                             help="Inventory of partial native evidence approved by the controller")
    args = parser.parse_args(argv)
    if args.command in actions:
        return _task_command(args)

    from .viewer import build_index, write_html_preview

    output = args.output / "research" if (args.output / "research/state.json").is_file() else args.output
    if args.command == "view":
        print(json.dumps(write_html_preview(output, args.html, partial_evidence=args.partial_evidence)))
    else:
        print(json.dumps(build_index(output, partial_evidence=args.partial_evidence), indent=2))
    return 0


def _task_command(args) -> int:
    from . import launch
    from .progress import TerminalProgress

    directory = args.task if args.task.is_dir() else args.task.parent
    config = args.config or (directory / "run.toml" if (directory / "run.toml").is_file() else None)
    action = {"check": launch.check_run, "record": launch.record_workspace, "run": launch.run_researchers}[args.command]
    try:
        with TerminalProgress(enabled=not args.json) as progress:
            run = launch.open_run(args.task, config, args.output, args.image, progress=progress)
            with run.research:
                summary = action(run)
    except (ValueError, RuntimeError, OSError) as error:
        print(f"ari {args.command}: {type(error).__name__}: {error}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(summary))
    else:
        _print(summary)
    return 0 if summary["status"] in {"checked", "recorded", "completed"} else 1


def _print(summary: dict) -> None:
    reason = summary.get("error") or summary.get("stopped")
    print("Status:", summary["status"] + (f" ({reason})" if reason else ""))
    for row in summary["candidates"]:
        detail = row["metrics"] if row["metrics"] else "; ".join(row["reasons"]) or row["error"] or ""
        print(f"  {row['candidate_id']}  {row['status']:<8}  {row['evaluation']:<11}  {detail}")
    print("Selected:", summary["selected"])
    budget = summary["budget"]
    for name in ("candidates", "evaluations"):
        print(f"{name.capitalize()}: {budget[name]['used']} of {budget[name]['limit']} used")
    if "starts" in summary:
        limits = summary["start_limits"]
        print("Agent starts:", ", ".join(f"{kind} {used} of {limits[kind]}" for kind, used in summary["starts"].items()))
    print("Review:", summary["review"])
    print("Workspace:", summary["workspace"])
    print("Viewer:", summary.get("viewer") or f"ari view {summary['output']} --html <new-file.html>")


if __name__ == "__main__":
    sys.exit(main())
