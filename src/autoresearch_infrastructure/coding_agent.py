"""Run one Harbor coding agent as a researcher.

This module starts a single agent and connects it to a candidate recorder. The
caller decides how many researchers to start and counts those starts.
"""

from __future__ import annotations

import json
import math
from pathlib import Path

from .harbor_filesystem import immutable_image, native_agent_config, run_filesystem_agent


def native_agent_cost(evidence):
    """The cost an agent reported in its trajectory, or None when it reported none."""
    path = evidence / "trajectory.json"
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text()).get("final_metrics", {}).get("total_cost_usd")
    except (ValueError, OSError, AttributeError):
        return None
    return value if type(value) in (int, float) and math.isfinite(value) and value >= 0 else None


class CodingAgent:
    """A Harbor agent that edits a workspace and submits candidates through ``record_candidate``.

    ``agent`` is a Harbor ``AgentConfig`` or the equivalent dictionary. The agent
    must support MCP servers and write ATIF trajectories. ``image`` is the ID of a
    container image that has what the agent needs to run.
    """

    def __init__(self, *, agent, image):
        self.agent = native_agent_config(agent, default_timeout=600, researcher=True)
        self.image = immutable_image(image, "A researcher")

    def run(
        self,
        prompt: str,
        *,
        workspace: Path,
        evidence: Path,
        readonly=None,
        recorder=None,
        role="researcher",
        jobs_root: Path | None = None,
    ):
        if recorder is None:
            raise ValueError("A researcher requires a candidate recorder")
        if not callable(getattr(recorder, "dispatch_record_candidate", None)):
            raise ValueError("The candidate recorder must expose dispatch_record_candidate")
        workspace, evidence = Path(workspace), Path(evidence)
        workspace.mkdir(parents=True, exist_ok=True)
        evidence.mkdir(parents=True, exist_ok=False)
        trial = run_filesystem_agent(
            agent=self.agent, image=self.image, instruction=prompt,
            workspace=workspace, evidence=evidence, readonly=readonly or {}, recorder=recorder,
            role=role, jobs_root=jobs_root or evidence / "harbor-jobs",
        )
        if trial.exception_info is not None:
            raise RuntimeError("Native Harbor researcher failed: " + trial.exception_info.exception_type)
        metadata = evidence / "trajectory-metadata.json"
        if not metadata.is_file():
            raise RuntimeError("Native Harbor researcher did not retain its ATIF trajectory")
        return json.loads(metadata.read_text())
