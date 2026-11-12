"""Human progress is optional, detached from results, and never leaks terminal controls to pipes."""

import io
import os

import pytest

import autoresearch_infrastructure as ari
from autoresearch_infrastructure.progress import TerminalProgress

ERROR = ari.Metric("error", direction="min")


def valid(candidate, context):
    return ari.Validity()


def invalid(candidate, context):
    return ari.Validity("invalid", ("Fixture rejection",))


def evaluate(candidate, context):
    if context.task == "broken":
        raise RuntimeError("Fixture measurement failure")
    if (candidate / "value").read_text() == "incomplete":
        return ari.Evaluation({ERROR: 99}, completed=False)
    return ari.Evaluation({ERROR: float((candidate / "value").read_text())})


def make(tmp_path, *, progress=None, tasks=("a", "b"), validity=valid):
    seed = tmp_path / "seed"
    seed.mkdir(parents=True, exist_ok=True)
    (seed / "value").write_text("2")
    return ari.Research(
        tmp_path / "run", seed=seed, objective=ERROR, evaluator=evaluate, validity=validity,
        evaluation_config={}, tasks=tasks, progress=progress,
        limits=ari.Limits(max_candidates=3, max_evaluations=6),
    )


def test_progress_reports_validity_measured_cells_and_authoritative_counts(tmp_path):
    events = []
    with make(tmp_path, progress=events.append) as research:
        result = research.record_candidate()
        assert result["evidence"]["metrics"] == {"error": 2}
        assert [e["stage"] for e in events[:2]] == ["recording", "validity"]
        assert sum(e["stage"] == "evaluating" for e in events) == 2
        assert sum(e["stage"] == "measured" for e in events) == 2
        assert events[-1] == {
            "sequence": events[-1]["sequence"], "stage": "completed", "candidate_id": "candidate-0001",
            "candidate_number": 1, "candidates_recorded": 1, "candidate_limit": 3,
            "evaluations_started": 2, "evaluation_limit": 6, "panel_total": 2, "panel_measured": 2,
            "metrics": {"error": 2}, "selected": "candidate-0001",
        }


def test_invalid_and_failed_evaluations_never_invent_progress_metrics_or_refund_starts(tmp_path):
    rejected = []
    with make(tmp_path / "invalid", progress=rejected.append, validity=invalid) as research:
        result = research.record_candidate()
        assert result["status"] == "invalid"
        assert rejected[-1]["stage"] == "invalid"
        assert rejected[-1]["metrics"] == {} and rejected[-1]["evaluations_started"] == 0
    failed = []
    with make(tmp_path / "failed", progress=failed.append, tasks=("broken",)) as research:
        result = research.record_candidate()
        assert result["evidence"]["evaluation"] == "incomplete"
        assert [e["stage"] for e in failed[-2:]] == ["evaluation_error", "incomplete"]
        assert failed[-1]["panel_measured"] == 0 and failed[-1]["metrics"] == {}
        assert failed[-1]["evaluations_started"] == research.budget_summary()["evaluations"]["used"] == 1


def test_observer_failure_and_mutation_do_not_change_experiment_results_or_identity(tmp_path):
    def broken_observer(event):
        event["metrics"]["error"] = 1000
        raise RuntimeError("Display is unavailable")

    with make(tmp_path, progress=broken_observer) as research:
        result = research.record_candidate()
        identity = research.contract_hash
        assert result["evidence"]["metrics"] == {"error": 2}
    events = []
    with make(tmp_path, progress=events.append) as resumed:
        assert resumed.contract_hash == identity
        resumed.record_candidate("candidate-0001")
        assert events[-1]["metrics"] == {"error": 2}
        assert events[-1]["evaluations_started"] == 2


def test_incomplete_return_does_not_advertise_measured_cells_or_replace_completed_metrics(tmp_path):
    events = []
    with make(tmp_path, progress=events.append, tasks=("a",)) as research:
        baseline = research.record_candidate()
        assert baseline["evidence"]["task_completed"] is True
        events.clear()
        (research.workspace / "value").write_text("incomplete")
        result = research.record_candidate()
        assert result["evidence"]["task_completed"] is False
        assert result["evidence"]["metrics"] == {"error": 99}
        assert result["selected"] == baseline["candidate_id"]
        assert "measured" not in [event["stage"] for event in events]
        assert [event["stage"] for event in events[-2:]] == ["evaluation_incomplete", "incomplete"]
        assert all(event["panel_measured"] == 0 for event in events)
        assert all(event["metrics"] == {"error": 2} for event in events)
        assert events[-1]["evaluations_started"] == 2


def test_core_is_silent_and_callback_errors_leave_recording_reusable(tmp_path, capsys):
    with make(tmp_path) as research:
        research.record_candidate()
        with pytest.raises(ValueError, match="Unknown candidate"):
            research.record_candidate("unknown")
        (research.workspace / "value").write_text("1")
        assert research.record_candidate()["evidence"]["metrics"] == {"error": 1}
    captured = capsys.readouterr()
    assert captured.out == captured.err == ""


class TTY(io.StringIO):
    def isatty(self):
        return True


def event(sequence=1):
    return {
        "sequence": sequence, "stage": "evaluating", "candidate_number": 1, "candidate_limit": 3,
        "evaluations_started": 2, "evaluation_limit": 6, "panel_measured": 1, "panel_total": 2,
        "metrics": {"error": 2}, "task": "a", "condition": "default", "repeat": 1,
    }


@pytest.mark.parametrize("stream,enabled", [(io.StringIO(), True), (TTY(), False)])
def test_pipe_or_explicit_machine_mode_has_no_display_output(stream, enabled, monkeypatch):
    monkeypatch.setenv("TERM", "xterm")
    with TerminalProgress(stream=stream, enabled=enabled) as progress:
        progress(event())
    assert stream.getvalue() == ""


def test_terminal_display_closes_after_error_ignores_stale_events_and_sanitizes_names(monkeypatch):
    monkeypatch.setenv("TERM", "xterm")
    stream = TTY()
    with pytest.raises(RuntimeError, match="Stop"):
        with TerminalProgress(stream=stream) as progress:
            progress(event(2))
            before = stream.getvalue()
            progress(event(1))
            assert stream.getvalue() == before
            progress(event(3) | {"task": "unsafe\x1b[2J\nname"})
            raise RuntimeError("Stop")
    output = stream.getvalue()
    assert "candidate 1/3" in output and "starts 2/6" in output and "error=2" in output
    assert "\x1b[2J" not in output and output.endswith("\n")
    size = len(output)
    progress(event(4))
    progress.close()
    assert len(stream.getvalue()) == size


@pytest.mark.parametrize("stage", ["validity", "evaluating"])
def test_terminal_display_keeps_phase_and_counters_at_eighty_columns(monkeypatch, stage):
    monkeypatch.setenv("TERM", "xterm")
    monkeypatch.setattr("autoresearch_infrastructure.progress.shutil.get_terminal_size",
                        lambda **kwargs: os.terminal_size((80, 24)))
    stream = TTY()
    with TerminalProgress(stream=stream) as progress:
        progress(event() | {
            "stage": stage, "candidate_number": 4, "candidate_limit": 5,
            "task": "long-task-name-that-must-not-hide-the-phase-or-counters",
            "evaluations_started": 4, "evaluation_limit": 4, "panel_measured": 1, "panel_total": 1,
        })
    first = stream.getvalue().split("\n")[0].replace("\r", "").replace("\x1b[2K", "")
    assert len(first) < 80
    assert "candidate 4/5" in first and stage in first
    assert "starts 4/4" in first and "measured 1/1" in first
