"""One researcher proposal: a charged agent start that may submit one candidate."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from string import Template
from uuid import uuid4

from .coding_agent import native_agent_cost
from .persistence import write_json
from .source_tree import task_context_digest

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
        if start.log:
            rows += ("\n| `/history/log.md` | A research log the controller writes from its records "
                     "before each start: every candidate shown, its idea, verdict and score. |")
        if start.summary:
            rows += "\n| `/history/summary.md` | A summary of the candidates shown, written for this run. |"
        if start.continues:
            evidence += (f" This start continues your earlier conversation in this run. Your workspace was reset"
                         f" to a copy of `{parent_id}`, which may differ from the files you last saw.")
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
            retain: bool | None = None) -> dict:
    """Start one researcher on a copy of ``parent_id`` and report what it did.

    The start is charged to ``ledger`` before the agent runs and is never
    refunded. A researcher that fails is recorded and returned, not raised, so
    the caller can spend its remaining starts. Anything it submitted before
    failing stays recorded.

    ``history`` is the directory mounted at ``/history``; the default is the
    experiment's canonical history. ``record`` describes this start's inputs. It
    is saved with the exact prompt under ``agent/inputs/<invocation>/``.
    ``load_trajectory`` is a conversation for the agent to continue.
    ``retain`` makes what survives a start explicit. ``None`` leaves files
    outside the candidate as they are. ``True`` clears the workspace, restores
    the files the previous start exported, and after this start exports the
    files left outside ``candidate/`` as a sealed copy. ``False`` clears the
    workspace and exports nothing, so a start begins with only the candidate.
    """
    key = ledger.start("researcher", str(research.output))
    invocation = uuid4().hex
    evidence = research.root / "agent/invocations" / invocation
    inputs = research.root / "agent/inputs" / invocation
    recorder = SingleSubmissionRecorder(research, parent_id, repeats)
    status, error, settled = "failed", None, False
    try:
        restored = None
        if retain is not None:
            shutil.rmtree(research.root / "workspace")
            restored = _restore(research) if retain else None
            research.workspace.mkdir(parents=True, exist_ok=True)
        research.reset_workspace(parent_id)
        inputs.mkdir(parents=True)
        (inputs / "prompt.md").write_text(prompt, encoding="utf-8")
        write_json(inputs / "inputs.json", {
            **(record or {}),
            "parent_id": parent_id,
            "parent_hash": research.candidates[parent_id]["source_hash"],
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
            "load_trajectory": _sha256(load_trajectory) if load_trajectory else None,
            "restored": restored,
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
        _log_start(research, invocation, evidence, status, _export(research, invocation) if retain else None)
    return {"status": status, "submitted": recorder.submitted, "error": error, "evidence": str(evidence)}


def researcher_starts(research) -> list[dict]:
    """Earlier researcher starts of this experiment, oldest first."""
    path = research.root / "agent/researcher-starts.json"
    return json.loads(path.read_text())["starts"] if path.exists() else []


def _log_start(research, invocation: str, evidence: Path, status: str, retained: dict | None) -> None:
    trajectory = evidence / "trajectory.json"
    starts = researcher_starts(research)
    starts.append({
        "invocation": invocation,
        "status": status,
        "trajectory": trajectory.relative_to(research.root).as_posix() if trajectory.is_file() else None,
        "trajectory_sha256": _sha256(trajectory) if trajectory.is_file() else None,
        "retained": retained,
    })
    write_json(research.root / "agent/researcher-starts.json", {"starts": starts})


def _export(research, invocation: str) -> dict:
    """Seal the files a start left outside the candidate, for the next start to restore.

    Only regular files are kept; links and special files are listed as dropped.
    A tree beyond the context limits is not retained at all, and the reason is
    recorded, so the next start begins without it rather than with a partial copy.
    """
    workspace = research.root / "workspace"
    target = research.root / "agent/retained" / invocation
    dropped = []
    target.mkdir(parents=True)
    for current, dirs, files in os.walk(workspace, followlinks=False):
        relative = Path(current).relative_to(workspace)
        if relative.parts[:1] == ("candidate",):
            dirs[:] = []
            continue
        for name in list(dirs):
            if (Path(current) / name).is_symlink():
                dropped.append((relative / name).as_posix())
                dirs.remove(name)
        for name in files:
            path = Path(current) / name
            if path.is_symlink() or not path.is_file():
                dropped.append((relative / name).as_posix())
                continue
            (target / relative).mkdir(parents=True, exist_ok=True)
            shutil.copyfile(path, target / relative / name)
    try:
        sealed = task_context_digest(target)
    except ValueError as exc:
        shutil.rmtree(target)
        return {"path": None, "sha256": None, "dropped": dropped, "error": str(exc)}
    return {"path": target.relative_to(research.root).as_posix(), "sha256": sealed, "dropped": dropped}


def _restore(research) -> dict | None:
    """Copy the last export back into the workspace after checking its seal."""
    exported = next((start["retained"] for start in reversed(researcher_starts(research))
                     if start.get("retained") and start["retained"].get("path")), None)
    if exported is None:
        return None
    source = research.root / exported["path"]
    if task_context_digest(source) != exported["sha256"]:
        raise ValueError(f"Retained workspace files changed after they were exported: {source}")
    shutil.copytree(source, research.root / "workspace", dirs_exist_ok=True)
    return {"path": exported["path"], "sha256": exported["sha256"]}


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
