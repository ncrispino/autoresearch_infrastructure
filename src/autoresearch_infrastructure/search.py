"""One researcher start: a charged agent session that submits one candidate, or several in a row."""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from string import Template
from uuid import uuid4

from .coding_agent import native_agent_cost
from .persistence import sha256_file, write_json
from .source_tree import snapshot, task_context_digest

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
        if start.conversations:
            rows += ("\n| `/history/conversations/` | The conversations of earlier researchers in this run, "
                     "as ATIF JSON, oldest first. |")
    submissions = 1 if start is None else start.submissions
    return goal.rstrip() + "\n\n" + RESEARCHER_PROTOCOL.substitute(
        parent_id=parent_id, repeats=repeats, memory_rows=rows, parent_evidence=evidence,
        after_recording=_ONE_SUBMISSION if submissions == 1 else _SEVERAL_SUBMISSIONS.format(
            limit="as many candidates as the run's budget allows" if submissions is None
            else f"up to {submissions} candidates"),
    )


_ONE_SUBMISSION = """\
- A reply containing `candidate_id` means your candidate was recorded. Stop
  there, even if it is invalid, its evaluation failed, or its score is poor.
  This attempt allows one submission."""
_SEVERAL_SUBMISSIONS = """\
- A reply containing `candidate_id` means your candidate was recorded, even if
  it is invalid, its evaluation failed, or its score is poor. In this session
  you may record {limit}, one at a time.
- Read `next` in each reply. When it names a `parent_id`, `/workspace/candidate/`
  now holds a fresh copy of that candidate, which is the best one so far and
  may not be the one you just submitted. `/history` has been updated. Read the
  new evidence, edit the files and submit again in the same way.
- When `next` is null, stop: this session can record no more candidates."""


class SubmissionRecorder:
    """Lets one researcher start record candidates, each from the parent the controller chose.

    The controller, not the researcher, chooses the parent and the repeat count.
    By default a start records one candidate. With ``limit`` above one, or
    ``None`` for no limit of its own, the same session may record more:
    after each recording, ``advance()`` either returns the next parent, having
    put a fresh copy of it in the workspace, or raises ``StopIteration`` with
    the reason no further candidate fits. The reply's ``next`` tells the
    researcher which.
    """

    def __init__(self, research, parent_id, repeats, *, limit: int | None = 1, advance=None):
        self.research, self.parent_id, self.repeats = research, parent_id, repeats
        self.limit, self.advance = limit, advance
        self.attempts, self.closed, self.submissions = 0, None, []

    def dispatch_record_candidate(self, session_id, arguments):
        if self.limit == 1 and self.attempts:
            raise ValueError("This invocation already submitted its one candidate; stop")
        if self.closed:
            raise ValueError(f"This session can record no more candidates ({self.closed}); stop")
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
        self.attempts += 1
        result = self.research.dispatch_record_candidate(
            session_id, {**arguments, "parent_id": self.parent_id, "total_repeats": self.repeats}
        )
        entry = {"candidate_id": result.get("candidate_id"), "parent_id": self.parent_id, "next_parent_id": None}
        self.submissions.append(entry)
        if self.limit == 1:
            return result
        if self.limit is not None and self.attempts >= self.limit:
            self.closed = "this session's candidates are used"
        else:
            try:
                self.parent_id = entry["next_parent_id"] = self.advance()
            except StopIteration as stop:
                self.closed = str(stop.value)
        if self.closed:
            return {**result, "next": None, "stopped": self.closed}
        return {**result, "next": {
            "parent_id": self.parent_id,
            "remaining": None if self.limit is None else self.limit - self.attempts,
            "note": f"/workspace/candidate/ now holds a fresh copy of {self.parent_id}. Edit it and submit again.",
        }}


def reset_candidate(research, parent_id: str) -> None:
    """Replace the candidate files in place, so a running agent's working directory stays valid."""
    payload = snapshot(research.candidate_files(parent_id))
    for child in research.workspace.iterdir():
        if child.is_dir() and not child.is_symlink():
            shutil.rmtree(child)
        else:
            child.unlink()
    for name, text in payload.items():
        path = research.workspace / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(text, encoding="utf-8")


def propose(research, agent, prompt: str, *, parent_id: str, repeats: int, ledger, context: Path,
            history: Path | None = None, record: dict | None = None, retain: bool | None = None,
            submissions: int | None = 1, advance=None) -> dict:
    """Start one researcher on a copy of ``parent_id`` and report what it did.

    The start is charged to ``ledger`` before the agent runs and is never
    refunded. A researcher that fails is recorded and returned, not raised, so
    the caller can spend its remaining starts. Anything it submitted before
    failing stays recorded.

    ``history`` is the directory mounted at ``/history``; the default is the
    experiment's canonical history. ``record`` describes this start's inputs. It
    is saved with the exact prompt under ``agent/inputs/<invocation>/``.
    ``submissions`` is how many candidates this one session may record: one
    by default, a larger number, or ``None`` for no limit of its own. Beyond
    one, ``advance()`` is called after each recording; see ``SubmissionRecorder``.
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
    recorder = SubmissionRecorder(research, parent_id, repeats, limit=submissions, advance=advance)
    status, error, settled = "failed", None, False
    described = {
        **(record or {}),
        "parent_id": parent_id,
        "parent_hash": research.candidates[parent_id]["source_hash"],
        "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest(),
    }
    try:
        restored = None
        if retain is not None:
            shutil.rmtree(research.root / "workspace")
            restored = _restore(research) if retain else None
            research.workspace.mkdir(parents=True, exist_ok=True)
        research.reset_workspace(parent_id)
        inputs.mkdir(parents=True)
        (inputs / "prompt.md").write_text(prompt, encoding="utf-8")
        described["restored"] = restored
        write_json(inputs / "inputs.json", described)  # saved before the agent runs, completed after
        try:
            agent.run(
                prompt,
                workspace=research.root / "workspace",
                evidence=evidence,
                readonly={context: "/context", (history or research.root / "history"): "/history"},
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
        cost = native_agent_cost(evidence) if settled else None
        ledger.finish(key, status=status, cost_usd=cost)
        _index_trajectory(research, evidence)
        _log_start(research, invocation, evidence, status, _export(research, invocation) if retain else None)
        if inputs.is_dir():
            write_json(inputs / "inputs.json", {**described, "submissions": recorder.submissions})
    return {"status": status, "submitted": recorder.attempts > 0, "error": error, "evidence": str(evidence),
            "candidates": [entry["candidate_id"] for entry in recorder.submissions]}


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
        "trajectory_sha256": sha256_file(trajectory) if trajectory.is_file() else None,
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


def _index_trajectory(research, evidence: Path) -> None:
    metadata = evidence / "trajectory-metadata.json"
    if not metadata.exists():
        return
    path = research.root / "agent/trajectory-index.json"
    index = json.loads(path.read_text()) if path.exists() else {"trajectories": []}
    index["trajectories"].append(json.loads(metadata.read_text()))
    write_json(path, index)
