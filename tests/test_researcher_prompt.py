"""Researcher instructions follow the configured objective and frozen template."""

import json
import tomllib
from pathlib import Path
from string import Template
from types import SimpleNamespace

import pytest

from autoresearch_infrastructure import harbor_harness, pilot
from autoresearch_infrastructure.source_tree import materialize

pytest.importorskip("harbor")


def test_prompt_uses_selected_parent_budget_and_only_configured_tie_breaker():
    plan = {"max_sessions_per_trial": 3, "screen": {"repeats": 2, "cases": 1},
            "harness": {"include_solver_tokens": True},
            "research_guidance": "Use public formulas; $parent_id is literal advice."}
    prompt = pilot.researcher_prompt(plan, "candidate-0004")
    assert "starting version is `candidate-0004`" in prompt
    assert "/history/candidates/candidate-0004/" in prompt
    assert "at most 3 solver sessions" in prompt
    assert "runs 2 repeat(s)" in prompt
    assert '"parent_id":' not in prompt and '"total_repeats":' not in prompt
    assert "python /opt/autoresearch/mcp_proxy.py --call /tmp/proposal.json" in prompt
    assert "native trajectory paths linked in `/history/index.json`" in prompt
    assert "- Score: `success_rate`. Higher is better." in prompt
    assert "When scores tie: `solver_tokens` (lower is better)." in prompt
    assert plan["research_guidance"] in prompt  # Advice is not another template.
    plan["harness"]["include_solver_tokens"] = False
    del plan["research_guidance"]
    prompt = pilot.researcher_prompt(plan, "candidate-0001")
    assert "When scores tie" not in prompt
    assert "## Task advice" not in prompt


def test_terminal_submission_example_writes_valid_arguments_without_shell_quoting(tmp_path, monkeypatch):
    prompt = pilot.researcher_prompt({"max_sessions_per_trial": 1, "screen": {"repeats": 1, "cases": 1}}, "candidate-0004")
    script = prompt.split("python - <<'PY'\n", 1)[1].split("\nPY", 1)[0]
    # Keep this fixture in its temporary directory while preserving the copyable script.
    monkeypatch.chdir(tmp_path)
    script = script.replace('"/tmp/proposal.json"', '"proposal.json"')
    exec(compile(script, "submission-example", "exec"), {})
    assert json.loads((tmp_path / "proposal.json").read_text()) == {
        "candidate": "workspace", "proposal": {
            "hypothesis": "What should improve?", "mechanism": "What changed and why?",
        },
    }


def test_template_change_requires_fresh_evidence_without_refunding_starts(tmp_path, monkeypatch):
    plan_path = Path(__file__).parents[1] / "examples/task-bank/spreadsheet-canary/search.toml"
    plan = tomllib.loads(plan_path.read_text())
    tasks = tmp_path / "tasks"
    for name in plan["benchmark"]["screen_task_ids"] + plan["benchmark"]["confirmation_task_ids"]:
        materialize(tasks / name, {"instruction.md": "Fixture benchmark task"})
    monkeypatch.setattr(pilot.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout="sha256:" + "a" * 64))

    def overlay(task, destination, *args, **kwargs):
        materialize(destination, {"compose.json": "fixture"})
        return destination / "compose.json"

    monkeypatch.setattr(pilot, "ca_build_overlay", overlay)
    old = tmp_path / "old"
    _, _, _, old_identity, ledger, _ = pilot.prepare(plan_path, tasks, old, "fixture")
    key = ledger.start("researcher", str(old))
    ledger.finish(key, status="failed")
    original_ledger = ledger.path.read_bytes()
    monkeypatch.setattr(harbor_harness, "_RESEARCHER_GOAL",
                        Template(harbor_harness._RESEARCHER_GOAL.template + "- A new environment note.\n"))
    with pytest.raises(ValueError, match="plan/runtime changed"):
        pilot.prepare(plan_path, tasks, old, "fixture", ledger.path)
    _, _, _, new_identity, continued, _ = pilot.prepare(plan_path, tasks, tmp_path / "new", "fixture", ledger.path)
    assert new_identity["runtime"] != old_identity["runtime"]
    assert new_identity["allocation_identity"] == old_identity["allocation_identity"]
    assert continued.snapshot()["counts"]["researcher"] == 1
    assert ledger.path.read_bytes() == original_ledger
    assert json.loads((old / "plan.json").read_text())["runtime"] == old_identity["runtime"]
