"""Import an AutoResearchExam task into ARI without editing it.

    python import_task.py SOURCE_TASK DESTINATION

SOURCE_TASK is one task directory of a checkout of
https://github.com/bespokelabsai/AutoResearchExam. DESTINATION must not exist. It
receives the task, unchanged, as ``harbor-task/``, a ``task.py`` naming what the
task leaves out, and ``local.toml``, run settings without agents. The command
prints what it derived as JSON, and the reason when the task cannot be imported
this way.

Everything is read from the task's own files; nothing in the task is run:

- the deliverable from ``ARE_ARTIFACT_PATHS`` of the grader service in
  ``environment/docker-compose.yaml``;
- the grading timeout from ``ARE_GRADE_TIMEOUT_SEC`` there, since the task's
  ``[verifier] timeout_sec`` belongs to the step that reads the benchmark's
  submission ledger;
- the grader as ``environment/are-grader-ctx``, whose Dockerfile copies both
  scripts and both hidden splits into the image's ``/tests``: ``intermediate.sh``
  is the feedback script and ``test.sh`` the sealed one.

The written ``task.py`` has no rules: the tasks state none that a reviewer could
judge, so ``local.toml`` turns review off. Add rules before a run with a
reviewer. The starting deliverable comes next, from the built image:

    ari extract-seed DESTINATION/harbor-task --deliverable PATH --output DESTINATION/seed
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tomllib
from pathlib import Path

import yaml

GRADER = "environment/are-grader-ctx"
FEEDBACK, SEALED = "intermediate.sh", "test.sh"
# Every CPU task's grader drops to the agent's uid with runuser, setuid or chown.
CAPABILITIES = ["CHOWN", "SETUID", "SETGID"]

TASK_PY = '''"""AutoResearchExam's {name}, imported without editing the task.

Written by examples/task-bank/autoresearchexam/import_task.py from the task's own
files. The Harbor task in harbor-task/ is unchanged.
"""

import autoresearch_infrastructure as ari

# The task states no rules a reviewer could judge. Add them before a run with a reviewer.
RULES = []


def task(run):
    return ari.bases.harbor_deliverable(
        run,
        harbor_task="harbor-task",
        deliverable={deliverable!r},             # ARE_ARTIFACT_PATHS
        feedback="/tests/{feedback}",            # the intermediate split
        sealed="/tests/{sealed}",                # the final split
        grader="{grader}",
        capabilities={capabilities!r},
        timeout={timeout},                       # ARE_GRADE_TIMEOUT_SEC
        rules=RULES,
        memory=ari.Memory(conversation_mode="continue", conversations="all"),
    )
'''


def derive(source: Path) -> dict:
    """What task.py needs, read from the task's files, or ``problem`` saying why it cannot be imported."""
    manifest = tomllib.loads((source / "task.toml").read_text(encoding="utf-8"))
    environment = manifest.get("environment") or {}
    found = {"task": source.name, "cpus": environment.get("cpus", 1), "gpus": environment.get("gpus", 0),
             "agent_network": (manifest.get("agent") or {}).get("network_mode")}
    if found["gpus"]:
        return {**found, "problem": f"needs {found['gpus']} GPU(s)"}
    compose = yaml.safe_load((source / "environment/docker-compose.yaml").read_text(encoding="utf-8"))
    grader = ((compose or {}).get("services") or {}).get("grader") or {}
    variables = grader.get("environment") or {}
    paths = [path for path in str(variables.get("ARE_ARTIFACT_PATHS", "")).split(":") if path]
    if len(paths) != 1:
        return {**found, "problem": f"ARE_ARTIFACT_PATHS names {len(paths)} deliverables; one is supported"}
    context = source / GRADER
    missing = [name for name in (FEEDBACK, SEALED, "Dockerfile") if not (context / name).is_file()]
    if missing:
        return {**found, "problem": f"{GRADER} has no {', '.join(missing)}"}
    if "COPY . /tests" not in (context / "Dockerfile").read_text(encoding="utf-8"):
        return {**found, "problem": f"{GRADER}/Dockerfile does not copy the grader to /tests"}
    timeout = variables.get("ARE_GRADE_TIMEOUT_SEC")
    if timeout is None:
        return {**found, "problem": "the grader service sets no ARE_GRADE_TIMEOUT_SEC"}
    return {**found, "deliverable": paths[0], "timeout": int(timeout), "problem": None}


def write(source: Path, destination: Path, found: dict) -> None:
    destination.mkdir(parents=True)
    shutil.copytree(source, destination / "harbor-task", symlinks=True)
    (destination / "task.py").write_text(TASK_PY.format(
        name=source.name, deliverable=found["deliverable"], feedback=FEEDBACK, sealed=SEALED, grader=GRADER,
        capabilities=CAPABILITIES, timeout=found["timeout"]), encoding="utf-8")
    host = os.cpu_count() or 1
    fewer = (f"cpus = {host}                  # this machine has {host}; the task asks for {found['cpus']}\n"
             if found["cpus"] > host else "")
    (destination / "local.toml").write_text(
        "# No agents: grade your own edits with ari record.\nreviewer = false\n\n"
        "[limits]\ncandidates = 4\nevaluations = 4\n" + (f"\n[task]\n{fewer}" if fewer else ""), encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("source", type=Path, help="An AutoResearchExam task directory")
    parser.add_argument("destination", type=Path, help="A new directory for the ARI task")
    args = parser.parse_args(argv)
    if args.destination.exists():
        parser.error(f"{args.destination} already exists")
    found = derive(args.source)
    if found["problem"] is None:
        write(args.source, args.destination, found)
    print(json.dumps(found))
    return 0 if found["problem"] is None else 1


if __name__ == "__main__":
    sys.exit(main())
