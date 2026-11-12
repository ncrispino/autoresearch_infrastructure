"""Run a Harbor agent in a locked-down container that sees a few mounted directories.

Researchers and reviewers both run this way. There is no Harbor grader: a
researcher's output is the candidate it records, and a reviewer's is its verdict file.
"""

from __future__ import annotations

import asyncio
import json
import math
import os
import re
import tempfile
from contextlib import ExitStack
from pathlib import Path
from uuid import uuid4

from . import mcp_proxy
from .persistence import write_json
from .tool_server import CandidateToolServer
from .traces import capture_atif


def immutable_image(image, role: str) -> str:
    """Require an image ID or digest, so a retagged image cannot change a running study."""
    if not isinstance(image, str) or not re.fullmatch(r"(?:[^@\s]+@)?sha256:[0-9a-f]{64}", image):
        raise ValueError(f"{role} requires an immutable image ID or repository@sha256 digest")
    return image


def native_agent_config(value, *, default_timeout, researcher=False):
    """Validate a Harbor agent configuration and return a private copy.

    Call this before any paid start so that a bad configuration costs nothing.
    """
    from harbor.agents.factory import AgentFactory
    from harbor.models.trial.config import AgentConfig
    from harbor.utils.env import resolve_env_vars

    agent = AgentConfig.model_validate(value).model_copy(deep=True)
    implementation = AgentFactory.get_agent_class_from_config(agent)
    implementation.parse_options(agent.kwargs, resolve_env_vars(agent.env))
    if researcher and (
        not implementation.capabilities.mcp_servers or not implementation.capabilities.atif
    ):
        raise ValueError("A researcher requires a native Harbor agent supporting MCP servers and ATIF")
    if not agent.model_name:
        raise ValueError("Declare the native agent's model_name")
    if agent.override_timeout_sec is None:
        agent.override_timeout_sec = default_timeout
    if agent.override_timeout_sec is None:
        raise ValueError("Declare the native agent's override_timeout_sec")
    for name in ("override_timeout_sec", "override_setup_timeout_sec", "max_timeout_sec"):
        timeout = getattr(agent, name)
        if timeout is not None and (
            type(timeout) not in (int, float) or not math.isfinite(timeout) or not 0 < timeout <= 7200
        ):
            raise ValueError(f"Native agent {name} must be positive and at most 7200 seconds")
    if any(server.name == "autoresearch" for server in agent.mcp_servers):
        raise ValueError("The autoresearch MCP server name is reserved for the candidate recorder")
    return agent


def _save_native_trajectory(jobs_root, job_name, evidence):
    """Copy the agent's trajectory into the evidence directory, even after a failed job."""
    job = jobs_root / job_name
    paths = sorted(job.glob("*/agent/trajectory.json"))
    if not paths:
        return
    if len(paths) != 1:
        raise RuntimeError("Filesystem job must retain at most one native trajectory")
    trajectory = paths[0]
    metadata = capture_atif(trajectory, evidence / "trajectory.json", harbor_validation=True)
    metadata.update(
        format="ATIF", schema_version=json.loads((evidence / "trajectory.json").read_text())["schema_version"],
        viewer_job=job_name, viewer_trial=trajectory.parent.parent.name,
    )
    write_json(evidence / "trajectory-metadata.json", metadata)


def run_filesystem_agent(
    *, agent, image, instruction, readonly, workspace, evidence, jobs_root,
    recorder=None, role="validity",
):
    """Run the agent once and return Harbor's trial result.

    ``readonly`` maps host paths to read-only mount points. ``workspace`` is
    mounted read-write at ``/workspace``. With a ``recorder``, the agent also
    gets an MCP server named ``autoresearch`` whose ``record_candidate`` tool
    reaches the recorder through a mounted socket. The agent's logs and
    trajectory are saved under ``evidence``.

    The container has a read-only root filesystem, no capabilities, and no
    access to the Docker socket or to anything not mounted here.
    """
    from harbor.agents.factory import AgentFactory
    from harbor.job import Job
    from harbor.models.job.config import JobConfig, RetryConfig
    from harbor.models.task.config import MCPServerConfig
    from harbor.models.task.config import TaskConfig as FilesystemTask
    from harbor.models.trial.config import AgentConfig, EnvironmentConfig, TaskConfig, VerifierConfig

    agent = AgentConfig.model_validate(agent).model_copy(deep=True)
    if not isinstance(role, str) or not re.fullmatch(r"[a-z][a-z0-9-]{0,39}", role):
        raise ValueError("Filesystem agent role must be a safe job label")
    if any(server.name == "autoresearch" for server in agent.mcp_servers):
        raise ValueError("The autoresearch MCP server name is reserved for the candidate recorder")
    if recorder is not None:
        if not AgentFactory.get_agent_class_from_config(agent).capabilities.mcp_servers:
            raise ValueError("A candidate recorder requires native Harbor MCP support")
        if not callable(getattr(recorder, "dispatch_record_candidate", None)):
            raise ValueError("The candidate recorder must expose dispatch_record_candidate")
    evidence, workspace, jobs_root = Path(evidence), Path(workspace), Path(jobs_root)
    evidence.mkdir(parents=True, exist_ok=True)
    task = evidence / "filesystem-task"
    (task / "environment").mkdir(parents=True)
    (task / "instruction.md").write_text(instruction, encoding="utf-8")
    (task / "task.toml").write_text(FilesystemTask(
        environment={"docker_image": image, "cpus": 1, "memory_mb": 4096, "workdir": "/workspace",
                     "skills_dir": "/tmp/harbor-skills"},
    ).model_dump_toml(), encoding="utf-8")
    isolation = task / "environment/isolation.json"
    environment = {"HOME": "/tmp"}
    if recorder is not None:
        environment["AUTORESEARCH_SOCKET"] = "/run/autoresearch/controller.sock"
    write_json(isolation, {"services": {"main": {
        "user": f"{os.getuid()}:{os.getgid()}",
        "read_only": True, "cap_drop": ["ALL"], "security_opt": ["no-new-privileges:true"],
        "pids_limit": 512, "init": True, "environment": environment,
        "tmpfs": ["/tmp:rw,nosuid,size=1g,mode=1777", "/installed-agent:rw,nosuid,size=128m,mode=1777"],
    }}})
    mounts = []
    for source, target in readonly.items():
        if target in {"/", "/workspace", "/run/autoresearch", "/opt/autoresearch/mcp_proxy.py"}:
            raise ValueError("Read-only input shadows a reserved filesystem-agent mount")
        mounts.append({"type": "bind", "source": str(Path(source).resolve()), "target": target, "read_only": True})
    mounts.append({"type": "bind", "source": str(workspace.resolve()), "target": "/workspace"})
    with ExitStack() as stack:
        if recorder is not None:
            socket_dir = Path(stack.enter_context(tempfile.TemporaryDirectory(prefix="ari-agent-socket-")))
            socket_path = socket_dir / "controller.sock"
            stack.enter_context(CandidateToolServer(recorder, "research", socket_path))
            mounts.extend([
                {"type": "bind", "source": str(socket_dir), "target": "/run/autoresearch", "read_only": True},
                {"type": "bind", "source": str(Path(mcp_proxy.__file__).resolve()),
                 "target": "/opt/autoresearch/mcp_proxy.py", "read_only": True},
            ])
            # `env` sets the socket path for the proxy using only Harbor's own
            # command-and-arguments form, which every agent already understands.
            agent.mcp_servers.append(MCPServerConfig(
                name="autoresearch", transport="stdio", command="env",
                args=["AUTORESEARCH_SOCKET=/run/autoresearch/controller.sock", "python",
                      "/opt/autoresearch/mcp_proxy.py"],
            ))
        config = JobConfig(
            job_name=role + "-" + uuid4().hex, jobs_dir=jobs_root,
            agents=[agent], tasks=[TaskConfig(path=task)],
            environment=EnvironmentConfig(mounts=mounts, extra_docker_compose=[isolation]),
            verifier=VerifierConfig(disable=True), n_attempts=1, n_concurrent_trials=1,
            retry=RetryConfig(max_retries=0), quiet=True,
        )
        write_json(evidence / "job-config.json", config)

        async def execute():
            job = await Job.create(config)
            return await job.run()

        try:
            result = asyncio.run(execute())
        finally:
            _save_native_trajectory(jobs_root, config.job_name, evidence)
        if len(result.trial_results) != 1:
            raise RuntimeError("Filesystem agent must produce exactly one Harbor trial")
        return result.trial_results[0]
