"""One researcher proposal: a charged agent start that may submit one candidate."""

from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from string import Template
from uuid import uuid4

from .coding_agent import native_agent_cost
from .persistence import write_json

RESEARCHER_PROTOCOL = Template(Path(__file__).with_name("researcher-protocol.md").read_text())


def researcher_prompt(goal: str, *, parent_id: str, repeats: int, start=None) -> str:
    """The task's own instructions, followed by the file layout and how to submit.

    ``start`` is an ``inputs.Start``. Without one, or with the default memory,
    the text is the standard one: every candidate under ``/history``.
    """
    rows, evidence = "", f"Read its evidence at `/history/candidates/{parent_id}/` before editing."
    if start is not None:
        if start.visible is not None:
            shown = "no earlier candidates" if not start.visible else (
                f"only {len(start.visible)} earlier candidate(s): " + ", ".join(f"`{c}`" for c in start.visible))
            evidence = (evidence if parent_id in start.visible else
                        "Its record is not shown.") + f" In this run `/history` holds {shown}."
        if start.preloaded:
            rows += ("\n| `/history/preloaded/` | Read-only records from earlier experiments, for reference. "
                     "They are not candidates or results of this run. |")
        if start.conversations and start.conversation_mode == "files":
            rows += ("\n| `/history/conversations/` | The conversations of earlier researchers in this run, "
                     "as ATIF JSON, oldest first. |")
    return goal.rstrip() + "\n\n" + RESEARCHER_PROTOCOL.substitute(
        parent_id=parent_id, repeats=repeats, memory_rows=rows, parent_evidence=evidence,
    )


class SingleSubmissionRecorder:
    """Lets one researcher start record one new candidate from a fixed parent.

    The controller, not the researcher, chooses the parent and the repeat count.
    """

    def __init__(self, research, parent_id, repeats):
        self.research, self.parent_id, self.repeats = research, parent_id, repeats
        self.submitted = False

    def dispatch_record_candidate(self, session_id, arguments):
        if self.submitted:
            raise ValueError("This invocation already submitted its one candidate; stop")
        if not isinstance(arguments, dict) or arguments.get("candidate") != "workspace":
            raise ValueError("Submit a new workspace candidate; inspect saved history with native tools")
        if arguments.get("parent_id") not in (None, self.parent_id):
            raise ValueError("Use the controller-selected parent")
        if arguments.get("total_repeats", self.repeats) != self.repeats:
            raise ValueError("Use the repeat count the controller set")
        proposal = arguments.get("proposal")
        if not isinstance(proposal, dict) or any(
            not isinstance(proposal.get(key), str) or not proposal[key].strip()
            for key in ("hypothesis", "mechanism")
        ):
            raise ValueError("Record a nonempty hypothesis and mechanism")
        self.submitted = True
        return self.research.dispatch_record_candidate(
            session_id, {**arguments, "parent_id": self.parent_id, "total_repeats": self.repeats}
        )


def propose(research, agent, prompt: str, *, parent_id: str, repeats: int, ledger, context: Path,
            history: Path | None = None, record: dict | None = None, load_trajectory: Path | None = None,
            reset_workspace: bool = False) -> dict:
    """Start one researcher on a copy of ``parent_id`` and report what it did.

    The start is charged to ``ledger`` before the agent runs and is never
    refunded. A researcher that fails is recorded and returned, not raised, so
    the caller can spend its remaining starts. Anything it submitted before
    failing stays recorded.

    ``history`` is the directory mounted at ``/history``; the default is the
    experiment's canonical history. ``record`` describes this start's inputs. It
    is saved with the exact prompt under ``agent/inputs/<invocation>/``.
    ``load_trajectory`` is a conversation for the agent to continue.
    ``reset_workspace`` removes everything under the workspace, not only the
    candidate, before the start.
    """
    key = ledger.start("researcher", str(research.output))
    invocation = uuid4().hex
    evidence = research.root / "agent/invocations" / invocation
    inputs = research.root / "agent/inputs" / invocation
    recorder = SingleSubmissionRecorder(research, parent_id, repeats)
    status, error, settled = "failed", None, False
    try:
        if reset_workspace:
            shutil.rmtree(research.root / "workspace")
        research.reset_workspace(parent_id)
        inputs.mkdir(parents=True)
        (inputs / "prompt.md").write_text(prompt, encoding="utf-8")
        write_json(inputs / "inputs.json", {
            **(record or {}),
            "parent_id": parent_id,
            "parent_hash": research.candidates[parent_id]["source_hash"],
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "load_trajectory": _sha256(load_trajectory) if load_trajectory else None,
        })
        options = {"load_trajectory": load_trajectory} if load_trajectory else {}
        try:
            agent.run(
                prompt,
                workspace=research.root / "workspace",
                evidence=evidence,
                readonly={context: "/context", (history or research.root / "history"): "/history"},
                recorder=recorder,
                jobs_root=research.output / "harbor-jobs",
                **options,
            )
            status = "completed"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            write_json(evidence / "failure.json", {"type": type(exc).__name__, "message": str(exc)})
        settled = True
    finally:
        # An interrupted start has no final cost, so it is recorded as unknown.
        cost = native_agent_cost(evidence) if settled else None
        ledger.finish(key, status=status, cost_usd=cost)
        _index_trajectory(research, evidence)
        _log_start(research, invocation, evidence, status)
    return {"status": status, "submitted": recorder.submitted, "error": error, "evidence": str(evidence)}


def researcher_starts(research) -> list[dict]:
    """Earlier researcher starts of this experiment, oldest first."""
    path = research.root / "agent/researcher-starts.json"
    return json.loads(path.read_text())["starts"] if path.exists() else []


def _log_start(research, invocation: str, evidence: Path, status: str) -> None:
    trajectory = evidence / "trajectory.json"
    starts = researcher_starts(research)
    starts.append({
        "invocation": invocation,
        "status": status,
        "trajectory": trajectory.relative_to(research.root).as_posix() if trajectory.is_file() else None,
        "trajectory_sha256": _sha256(trajectory) if trajectory.is_file() else None,
    })
    write_json(research.root / "agent/researcher-starts.json", {"starts": starts})


def _sha256(path: Path) -> str:
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


def _index_trajectory(research, evidence: Path) -> None:
    metadata = evidence / "trajectory-metadata.json"
    if not metadata.exists():
        return
    path = research.root / "agent/trajectory-index.json"
    index = json.loads(path.read_text()) if path.exists() else {"trajectories": []}
    index["trajectories"].append(json.loads(metadata.read_text()))
    write_json(path, index)
