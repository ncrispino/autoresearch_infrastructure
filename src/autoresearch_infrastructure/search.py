"""One researcher proposal: a charged agent start that may submit one candidate."""

from __future__ import annotations

import json
from pathlib import Path
from string import Template
from uuid import uuid4

from .coding_agent import native_agent_cost
from .persistence import write_json

RESEARCHER_PROTOCOL = Template(Path(__file__).with_name("researcher-protocol.md").read_text())


def researcher_prompt(goal: str, *, parent_id: str, repeats: int) -> str:
    """The task's own instructions, followed by the file layout and how to submit."""
    return goal.rstrip() + "\n\n" + RESEARCHER_PROTOCOL.substitute(parent_id=parent_id, repeats=repeats)


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


def propose(research, agent, prompt: str, *, parent_id: str, repeats: int, ledger, context: Path) -> dict:
    """Start one researcher on a copy of ``parent_id`` and report what it did.

    The start is charged to ``ledger`` before the agent runs and is never
    refunded. A researcher that fails is recorded and returned, not raised, so
    the caller can spend its remaining starts. Anything it submitted before
    failing stays recorded.
    """
    key = ledger.start("researcher", str(research.output))
    evidence = research.root / "agent/invocations" / uuid4().hex
    recorder = SingleSubmissionRecorder(research, parent_id, repeats)
    status, error, settled = "failed", None, False
    try:
        research.reset_workspace(parent_id)
        try:
            agent.run(
                prompt,
                workspace=research.root / "workspace",
                evidence=evidence,
                readonly={context: "/context", research.root / "history": "/history"},
                recorder=recorder,
                jobs_root=research.output / "harbor-jobs",
            )
            status = "completed"
        except Exception as exc:
            error = f"{type(exc).__name__}: {exc}"
            write_json(evidence / "failure.json", {"type": type(exc).__name__, "message": str(exc)})
        settled = True
    finally:
        # An interrupted start has no final cost, so it is recorded as unknown.
        ledger.finish(key, status=status, cost_usd=native_agent_cost(evidence) if settled else None)
        _index_trajectory(research, evidence)
    return {"status": status, "submitted": recorder.submitted, "error": error, "evidence": str(evidence)}


def _index_trajectory(research, evidence: Path) -> None:
    metadata = evidence / "trajectory-metadata.json"
    if not metadata.exists():
        return
    path = research.root / "agent/trajectory-index.json"
    index = json.loads(path.read_text()) if path.exists() else {"trajectories": []}
    index["trajectories"].append(json.loads(metadata.read_text()))
    write_json(path, index)
