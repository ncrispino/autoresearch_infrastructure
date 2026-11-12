import json
from datetime import datetime, timezone

import pytest

pytest.importorskip("harbor")
from harbor.models.job.result import JobResult
from harbor.models.trial.result import TrialResult
from harbor.viewer.scanner import JobScanner
from harbor.viewer.server import create_app

from autoresearch_infrastructure import harbor as trajectories


def test_controller_agent_atif_is_the_same_file_used_by_harbor_viewer(tmp_path):
    native = tmp_path / "native"
    session = native / "sessions/2026/10/21"
    session.mkdir(parents=True)
    events = [
        {
            "type": "session_meta",
            "payload": {"id": "researcher-session", "cli_version": "0.159.2"},
        },
        {
            "type": "response_item",
            "timestamp": "2026-10-21T00:00:00Z",
            "payload": {
                "type": "message",
                "role": "assistant",
                "content": [{"type": "output_text", "text": "finished"}],
            },
        },
    ]
    (session / "rollout-2026-10-21T00-00-00-fixture.jsonl").write_text(
        "\n".join(json.dumps(event) for event in events) + "\n"
    )
    evidence = tmp_path / "agent/trajectory.json"
    jobs = tmp_path / "harbor-jobs"
    now = datetime.now(timezone.utc)

    metadata = trajectories.publish_codex_atif(
        native,
        evidence,
        jobs,
        role="researcher",
        scope="task-a",
        version="0.159.2",
        model="openai/researcher-model",
        started_at=now,
        finished_at=now,
    )
    second = trajectories.publish_codex_atif(
        native,
        tmp_path / "agent-2/trajectory.json",
        jobs,
        role="researcher",
        scope="task-b",
        version="0.159.2",
        model="openai/researcher-model",
        started_at=now,
        finished_at=now,
    )

    trial = jobs / metadata["viewer_job"] / metadata["viewer_trial"]
    assert evidence.read_bytes() == (trial / "agent/trajectory.json").read_bytes()
    TrialResult.model_validate_json((trial / "result.json").read_text())
    JobResult.model_validate_json((trial.parent / "result.json").read_text())
    scanner = JobScanner(jobs)
    job = scanner.get_job_result(metadata["viewer_job"])
    assert job is not None and job.n_total_trials == 2
    assert len(scanner.get_job_config(metadata["viewer_job"]).tasks) == 2
    assert scanner.get_trial_result(metadata["viewer_job"], metadata["viewer_trial"]) is not None
    assert scanner.get_trial_result(second["viewer_job"], second["viewer_trial"]) is not None
    assert json.loads(evidence.read_bytes())["steps"][0]["message"] == "finished"
    endpoint = next(
        route.endpoint
        for route in create_app(jobs).routes
        if route.path == "/api/jobs/{job_name}/trials/{trial_name}/trajectory"
    )
    viewed = endpoint(metadata["viewer_job"], metadata["viewer_trial"], None)
    assert viewed["steps"][0]["message"] == "finished"


def test_controller_agent_atif_rejects_model_created_native_symlinks(tmp_path):
    native = tmp_path / "native"
    (native / "sessions").mkdir(parents=True)
    (native / "sessions/escape").symlink_to(tmp_path, target_is_directory=True)

    with pytest.raises(RuntimeError, match="symlink"):
        trajectories.publish_codex_atif(
            native,
            tmp_path / "trajectory.json",
            tmp_path / "harbor-jobs",
            role="validator",
            scope="candidate",
            version="0.159.2",
            model="model",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
        )


def test_controller_agent_rejects_non_codex_converter_output(tmp_path, monkeypatch):
    native = tmp_path / "native"
    (native / "sessions").mkdir(parents=True)

    class Document:
        schema_version = "ATIF-v1.7"
        steps = []

        def to_json_dict(self):
            return {
                "schema_version": self.schema_version,
                "session_id": "other",
                "agent": {"name": "other-agent", "version": "1"},
                "steps": [],
            }

    class Converter:
        def __init__(self, *args, **kwargs):
            pass

        def convert_trajectory(self, path):
            return Document()

    monkeypatch.setattr(trajectories, "Codex", Converter)

    with pytest.raises(RuntimeError, match="unexpected agent identity"):
        trajectories.publish_codex_atif(
            native,
            tmp_path / "trajectory.json",
            tmp_path / "harbor-jobs",
            role="researcher",
            scope="task",
            version="0.159.2",
            model="model",
            started_at=datetime.now(timezone.utc),
            finished_at=datetime.now(timezone.utc),
        )
