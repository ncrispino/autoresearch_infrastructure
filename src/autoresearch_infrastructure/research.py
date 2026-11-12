"""One experiment: saved candidates, their validity, and their measurements."""

from __future__ import annotations

import fcntl
import json
import os
import re
import shutil
import sys
import threading
import time
from contextlib import ExitStack
from dataclasses import asdict
from pathlib import Path
from statistics import mean
from types import SimpleNamespace
from typing import Callable

from .api import (
    Evaluation,
    FunctionEvaluator,
    Limits,
    Metric,
    MetricSchema,
    Objective,
    Validity,
    ValidityContext,
    code_identity,
)
from .ledger import StartLedger
from .lifecycle import EvaluationLifecycle
from .persistence import digest, write_json
from .review import LLMValidity
from .snapshots import record_snapshot
from .source_tree import evidence_digest, materialize, python_tree_hash, snapshot
from .validity import ValidityVerdict

_SESSION = "research"
_SAFE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}")
BASELINE = "candidate-0001"


class Research(EvaluationLifecycle):
    """A frozen objective, evaluator and validity check over one set of starting files.

    Edit ``workspace``, then call ``record_candidate()``. Each call saves the files
    as a new candidate, checks validity, and evaluates every task, condition and
    repeat. Reopening the same output with the same setup resumes it; a changed
    setup needs a fresh output.

    One process writes an output at a time. The evaluator and validity callbacks
    are trusted Python: this class never imports or runs candidate files, so an
    evaluator that executes them must isolate that execution itself.
    """

    SCHEMA = "autoresearch_infrastructure-1"

    def __init__(
        self,
        output: Path,
        *,
        seed: Path,
        objective: Metric | Objective | Callable,
        evaluator,
        validity,
        evaluation_config: dict,
        metrics: tuple[Metric, ...] | None = None,
        limits: Limits = Limits(),
        tasks: tuple[str, ...] = ("default",),
        conditions: tuple[str, ...] = ("default",),
        progress: Callable[[dict], None] | None = None,
    ):
        if progress is not None and not callable(progress):
            raise ValueError("progress must be a callable observer or None")
        names = (*tasks, *conditions)
        if not tasks or not conditions or len(set(tasks)) != len(tasks) or len(set(conditions)) != len(
            conditions
        ) or any(not _SAFE.fullmatch(name) for name in names):
            raise ValueError("Tasks and conditions must be unique safe identifiers")
        if not isinstance(objective, Objective):
            objective = Objective(objective)
        if metrics is None:
            metrics = objective.metrics
        if metrics is None and not isinstance(objective.score, Metric):
            raise ValueError("A score function requires an explicit metrics=(...) declaration")
        declared = metrics if metrics is not None else tuple(dict.fromkeys(objective.referenced_metrics))
        self.schema = MetricSchema(declared)
        if any(m not in self.schema.metrics for m in objective.referenced_metrics):
            raise ValueError("The objective and tie-breakers must use declared metric definitions")

        self.metrics = self.schema.metrics
        self.objective = objective
        self.config = limits
        self.tasks, self.conditions = tuple(tasks), tuple(conditions)
        self.output = Path(output).resolve()
        self.root = self.output / "sessions" / _SESSION
        self.state_path = self.output / "state.json"
        self.evaluator = FunctionEvaluator(evaluator, self.schema)
        self._display = _Display(progress)
        self._lock = threading.RLock()
        self._owner_pid = os.getpid()
        self._recording = False

        seed_payload = snapshot(Path(seed))
        seed_hash = digest(seed_payload)
        if isinstance(validity, LLMValidity):
            validity = validity.for_output(self.output, baseline=self.root / "seed", baseline_hash=seed_hash)
        self.validity = validity
        contract = {
            "objective": objective.identity(),
            "metrics": [asdict(m) for m in self.metrics],
            "limits": asdict(limits),
            "evaluation_config": evaluation_config,
            "seed_hash": seed_hash,
            "tasks": tasks,
            "conditions": conditions,
            "evaluator": code_identity(evaluator),
            "validity": code_identity(validity),
            "runtime": python_tree_hash(Path(__file__).parent),
            "python": sys.version,
        }
        self.contract_hash = digest(contract)

        self.output.mkdir(parents=True, exist_ok=True)
        self._writer = (self.output / ".writer.lock").open("a+")
        self._allocations = ExitStack()
        # Shared ledgers stay owned by this controller until it closes.
        self._ledgers = {
            ledger.path: ledger
            for ledger in (getattr(evaluator, "ledger", None), getattr(validity, "ledger", None))
            if isinstance(ledger, StartLedger)
        }
        try:
            for path in sorted(self._ledgers):
                self._allocations.enter_context(self._ledgers[path].allocation_owner())
            fcntl.flock(self._writer, fcntl.LOCK_EX | fcntl.LOCK_NB)
            if self.state_path.exists():
                self._resume(seed_hash)
            else:
                self._create(seed_payload, contract)
            self._write_history_index()
        except BaseException:
            self.close()
            raise

    # ---- Opening and closing an output

    def _resume(self, seed_hash: str) -> None:
        self.state = json.loads(self.state_path.read_text())
        if self.state.get("schema_version") != self.SCHEMA:
            raise ValueError("Unsupported research state")
        if self._session(_SESSION)["contract_hash"] != self.contract_hash:
            raise ValueError("Experiment setup changed; use a fresh output")
        if digest(snapshot(self.root / "seed")) != seed_hash:
            raise ValueError("Saved starting files changed")
        self._recover_interrupted_trial_starts()

    def _create(self, seed_payload: dict[str, str], contract: dict) -> None:
        if any(path.name != ".writer.lock" for path in self.output.iterdir()):
            raise ValueError("Output has partial or unrelated files; use a fresh output")
        materialize(self.root / "seed", seed_payload)
        self._freeze_tree(self.root / "seed")
        materialize(self.workspace, seed_payload)
        session = {
            "spec": {
                "session_id": _SESSION,
                "role": "research",
                "instance_id": _SESSION,
                "assignment_id": self.objective.label,
                "objective": self.objective.identity(),
                "metrics": [asdict(m) for m in self.metrics],
                "affected_tasks": list(self.tasks),
            },
            "contract_hash": self.contract_hash,
            "status": "active",
            "phase_index": 0,
            "candidates": {},
            "new_proposals": 0,
            "valid_evaluated": 0,
            "selected": {},
        }
        limit = self.config.max_evaluations
        self.state = {
            "schema_version": self.SCHEMA,
            "evidence_mode": "local evaluation",
            "config": {**asdict(self.config), "max_solver_trial_starts": limit},
            "solver_trial_starts": 0,
            "reservations": {},
            "sessions": {_SESSION: session},
            "instances": {
                _SESSION: {
                    "solver_trial_start_limit": limit,
                    "solver_trial_starts": 0,
                    "session_ids": [_SESSION],
                }
            },
            "phase": {"index": 0, "role": "research", "status": "active"},
        }
        write_json(self.output / "contract.json", contract)
        self._save_state()

    def close(self):
        writer = getattr(self, "_writer", None)
        if writer is not None and not writer.closed:
            writer.close()
        allocations = getattr(self, "_allocations", None)
        if allocations is not None:
            allocations.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

    # ---- Reading the experiment

    @property
    def workspace(self) -> Path:
        """The editable files that the next ``record_candidate()`` call saves."""
        return self.root / "workspace/candidate"

    @property
    def candidates(self) -> dict[str, dict]:
        """Saved candidate records by ID, in the order they were recorded."""
        return self._session(_SESSION)["candidates"]

    @property
    def proposals(self) -> int:
        """Recorded candidates, including the baseline."""
        return self._session(_SESSION)["new_proposals"]

    @property
    def selected(self) -> str | None:
        """The best valid, feasible, fully measured candidate so far."""
        return self._session(_SESSION)["selected"].get(self.contract_hash)

    def evidence(self, candidate_id: str) -> dict:
        """Validity and measurements of a saved candidate; empty before its review."""
        return self.candidates[candidate_id]["evidence"].get(self.contract_hash, {})

    def measured(self, candidate_id: str) -> bool:
        """Whether a saved candidate is valid and has every requested measurement."""
        record = self.candidates.get(candidate_id)
        return bool(record) and record["status"] == "valid" and (
            self.evidence(candidate_id).get("evaluation") == "completed"
        )

    def candidate_files(self, candidate_id: str) -> Path:
        """The read-only snapshot of a saved candidate, checked against its hash."""
        return self._verified_candidate_path(_SESSION, candidate_id)

    def reset_workspace(self, files: str | dict[str, str]) -> None:
        """Replace the workspace with a saved candidate's files or an exact file mapping."""
        payload = snapshot(self.candidate_files(files)) if isinstance(files, str) else files
        shutil.rmtree(self.workspace)
        materialize(self.workspace, payload)

    def check_metrics(self, sample: Evaluation) -> dict:
        """Score sample measurements without running validity or the evaluator.

        Use this to confirm the metric names, bounds and objective before spending
        any candidate or evaluation budget.
        """
        if not isinstance(sample, Evaluation) or type(sample.completed) is not bool:
            raise ValueError("Metric check needs an Evaluation with boolean completed")
        encoded = self.schema.encode(sample.metrics)
        values = self.schema.decode(encoded)
        score, key = self.objective.assess(values, sample.completed)
        bounded = [m for m in self.metrics if m.minimum is not None or m.maximum is not None]
        return {
            "status": "valid",
            "metrics": encoded,
            "score": score,
            "feasible": key is not None,
            "referenced_metrics": [m.name for m in self.metrics if m in values.reads],
            "constraint_metrics": [m.name for m in bounded],
            "diagnostic_metrics": [
                m.name for m in self.metrics if m not in values.reads and m not in bounded
            ],
            "launches_evaluator": False,
            "launches_validity": False,
        }

    def budget_summary(self) -> dict:
        """Candidates, evaluations, reviews and shared starts: limits, use and remainder."""
        self._require_owner()
        with self._lock:
            cases = len(self.tasks) * len(self.conditions)
            limits = self.config
            shared = [
                {
                    "ledger": str(ledger.path),
                    "starts": {
                        kind: {"limit": limit, "used": counts[kind], "remaining": limit - counts[kind]}
                        for kind, limit in ledger.limits.items()
                    },
                }
                for ledger in self._ledgers.values()
                for counts in [ledger.snapshot()["counts"]]
            ]
            return {
                "cases_per_repeat": cases,
                "selection_repeats": limits.selection_repeats,
                "max_repeats": limits.max_repeats,
                "evaluations_for_selection": cases * limits.selection_repeats,
                "candidates": {
                    "limit": limits.max_candidates,
                    "used": self.proposals,
                    "remaining": limits.max_candidates - self.proposals,
                    "including_baseline": True,
                },
                "evaluations": {
                    "limit": limits.max_evaluations,
                    "used": self.state["solver_trial_starts"],
                    "reserved": self.remaining_trial_starts - self.available_trial_start_capacity,
                    "remaining": self.available_trial_start_capacity,
                },
                "shared_budgets": shared,
                "reviews": self.validity.review_budget() if isinstance(self.validity, LLMValidity) else None,
            }

    # ---- Recording a candidate

    def record_candidate(
        self,
        candidate="workspace",
        total_repeats=1,
        parent_id=None,
        proposal=None,
        evaluation=None,
        session_id=_SESSION,
    ) -> dict:
        """Save the workspace as a new candidate, or add repeats to a saved one.

        ``candidate="workspace"`` snapshots the current files. A saved candidate ID
        evaluates only the repeats that candidate is still missing.
        """
        self._require_owner()
        if type(total_repeats) is not int or not 1 <= total_repeats <= self.config.max_repeats:
            raise ValueError("Repeat count is outside limits")
        if evaluation is not None:
            raise ValueError("This experiment evaluates all configured cases and repeats")
        with self._lock:
            if self._writer.closed:
                raise ValueError("Research controller is closed")
            if self._recording:
                raise ValueError("A candidate evaluation is already running")
            self._recording = True
            self._display.start_panel()
            try:
                result = self._record(session_id, candidate, total_repeats, parent_id, proposal)
                self._notify(_final_stage(result), measured=result["evidence"])
                return result
            except BaseException:
                self._notify("error")
                raise
            finally:
                self._recording = False

    def _record(self, session_id, candidate, repeats, parent_id, proposal):
        session = self._session(session_id)
        if candidate == "workspace":
            candidate = self._save_workspace(session, parent_id, proposal)
        elif parent_id is not None or proposal is not None:
            raise ValueError("Saved candidate requests omit parent_id and proposal")
        elif candidate not in session["candidates"]:
            raise ValueError("Unknown candidate")
        record = session["candidates"][candidate]
        self._display.candidate = candidate
        self._notify("recording")

        path = self.candidate_files(candidate)
        evidence = record["evidence"].get(self.contract_hash)
        # A review that failed inside the controller is retried; a verdict is final.
        if evidence is None or evidence["validity"].get("controller_error"):
            evidence = self._review(record, path)
        if evidence["validity"]["verdict"] == "valid":
            self._verify_saved_cells(evidence)
            self._evaluate(session_id, candidate, SimpleNamespace(role="research"), self.contract_hash, repeats)
        self._save_state()
        return {
            "candidate_id": candidate,
            "source_hash": record["source_hash"],
            "contract_hash": self.contract_hash,
            "status": record["status"],
            "evidence": evidence,
            "selected": session["selected"].get(self.contract_hash),
            "remaining_evaluations": self.remaining_trial_starts,
        }

    def _save_workspace(self, session, parent_id, proposal) -> str:
        if session["new_proposals"] >= self.config.max_candidates:
            raise ValueError("Candidate proposal limit reached")
        if proposal is not None and not isinstance(proposal, dict):
            raise ValueError("proposal must be JSON metadata")
        digest(proposal)  # Reject metadata that cannot be saved before changing state.
        parent = self._candidate_payload(_SESSION, parent_id)
        ignored = []
        payload = snapshot(self.workspace, ignored)
        session["new_proposals"] += 1
        candidate = f"candidate-{session['new_proposals']:04d}"
        record = {
            "id": candidate,
            "parent_id": parent_id,
            "proposal": proposal,
            "source_hash": digest(payload),
            "scope": list(self.tasks),
            "status": "proposed",
            "created_at": time.time(),
            "evidence": {},
        }
        session["candidates"][candidate] = record
        self._save_state()
        directory = self._candidate_dir(_SESSION, candidate)
        try:
            directory.mkdir(parents=True)
            record_snapshot(directory, payload, parent, ignored=ignored)
            self._freeze_tree(directory / "snapshot")
        except Exception as exc:
            record.update(status="invalid", reasons=[f"Snapshot recording failed: {exc}"])
            self._save_state()
            raise
        return candidate

    def _review(self, record: dict, path: Path) -> dict:
        """Run the validity check once and save its verdict as this candidate's evidence."""
        candidate = record["id"]
        attempts = record.setdefault("validity_attempts", [])
        attempt = {
            "number": len(attempts) + 1,
            "status": "running",
            "contract_hash": self.contract_hash,
            "started_at": time.time(),
        }
        attempts.append(attempt)
        self._save_state()
        self._notify("validity")
        directory = (
            self._candidate_dir(_SESSION, candidate)
            / "validity"
            / self.contract_hash
            / f"attempt-{attempt['number']:04d}"
        )
        directory.mkdir(parents=True, exist_ok=True)
        context = ValidityContext(record["source_hash"], self.contract_hash, directory)
        controller_error = None
        try:
            verdict = self._run_external(self.validity, path, context)
            if not isinstance(verdict, (Validity, ValidityVerdict)):
                raise ValueError("Validity callback must return Validity")
            if isinstance(verdict, ValidityVerdict) and (
                verdict.candidate_hash != context.candidate_hash
                or verdict.contract_hash != context.contract_hash
            ):
                raise ValueError("Validity verdict is bound to a different candidate or contract")
            self.candidate_files(candidate)
        except Exception as exc:
            # A check that cannot finish never admits the candidate.
            verdict = Validity("invalid", (f"Validity failed: {type(exc).__name__}: {exc}",))
            controller_error = type(exc).__name__
        saved = verdict.to_dict() if isinstance(verdict, ValidityVerdict) else asdict(verdict)
        saved.update(candidate_hash=record["source_hash"], contract_hash=self.contract_hash)
        if controller_error:
            saved["controller_error"] = controller_error
        evidence = {
            "contract_hash": self.contract_hash,
            "validity": saved,
            "evaluation": "not_started",
            "cells": {},
            "total_repeats": 0,
            "objective": None,
            "metrics": None,
        }
        attempt.update(status=verdict.verdict, finished_at=time.time(), verdict=saved)
        write_json(directory / "accepted-verdict.json", saved)
        record["evidence"][self.contract_hash] = evidence
        record["status"] = verdict.verdict
        self._save_state()
        return evidence

    def _verify_saved_cells(self, evidence: dict) -> None:
        """Completed measurements are reused only while their saved files are unchanged."""
        for cell in evidence["cells"].values():
            if cell["status"] != "completed":
                continue
            saved = self._load_trial_start_result(
                self.output / cell["trial_starts"][-1]["result_path"],
                cell["task_id"],
                cell["condition"],
                cell["repeat"],
                self.contract_hash,
            )
            if saved != cell["result"]:
                raise ValueError("Saved evaluation or artifacts changed")

    def _require_owner(self) -> None:
        if os.getpid() != self._owner_pid:
            raise RuntimeError("Experiment controller belongs to another process; open a new Research instance")

    # ---- Hooks called by the shared evaluation lifecycle

    def _session(self, session_id):
        if session_id != _SESSION:
            raise ValueError("Unknown research session")
        return self.state["sessions"][session_id]

    def _conditions(self, role):
        return self.conditions

    def _save_state(self) -> None:
        with self._lock:
            super()._save_state()
            self._write_history_index()

    def _reserve_evaluation(self, session_id, candidate_id, contract_hash, spec, cells):
        reserve = getattr(self.evaluator.function, "reserve_evaluations", None)
        if reserve is not None:
            reserve(len(cells))
        return self._create_reservation(session_id, candidate_id, contract_hash, cells)

    def _start_evaluation(self, session_id, spec, reservation_id, key):
        self._consume_reservation(reservation_id, key)

    def _load_trial_start_result(self, path, task_id, condition, repeat, contract_hash):
        """Return a saved cell result, or None unless every byte it names is intact."""
        expected = {
            "task_id": task_id,
            "condition": condition,
            "repeat": repeat,
            "contract_hash": contract_hash,
        }
        try:
            if not path.is_file() or path.is_symlink() or path.stat().st_size > 1_000_000:
                return None
            value = json.loads(path.read_text())
            if any(value.get(key) != wanted for key, wanted in expected.items()):
                return None
            result = value["result"]
            self.schema.decode(result["metrics"])
            if type(result["completed"]) is not bool:
                return None
            candidate_root = next(p for p in path.parents if p.name.startswith("candidate-"))
            if digest(snapshot(candidate_root / "snapshot")) != result["candidate_hash"]:
                return None
            if evidence_digest(path.parent / "artifacts") != result["artifacts_sha256"]:
                return None
            if "trajectory" in result:
                wrapper = json.loads(path.with_name("trajectory.json").read_text())
                if wrapper.get("solver_trajectory") != result["trajectory"]:
                    return None
            return result
        except (OSError, ValueError, KeyError, TypeError, StopIteration):
            return None

    def _complete_evidence(self, session_id, candidate_id, contract_hash, total_repeats, requested_tasks=None):
        """Aggregate the complete repeats, score them, and reselect the best candidate."""
        session = self._session(session_id)
        evidence = session["candidates"][candidate_id]["evidence"][contract_hash]

        def cells(repeats):
            return [
                evidence["cells"].get(self._cell_key(task, condition, repeat), {})
                for task in self.tasks
                for condition in self.conditions
                for repeat in range(1, repeats + 1)
            ]

        def complete(repeats):
            return all(cell.get("status") == "completed" for cell in cells(repeats))

        full = max((r for r in range(1, self.config.max_repeats + 1) if complete(r)), default=0)
        evidence["total_repeats"] = full
        evidence["evaluation"] = "completed" if complete(total_repeats) else "incomplete"
        if full:
            self._score(evidence, [cell["result"] for cell in cells(full)])
            if evidence["evaluation"] == "completed":
                evidence.pop("error", None)
        evidence["evaluation_panel"] = {
            "tasks": list(self.tasks),
            "total_repeats": total_repeats,
            "status": evidence["evaluation"],
            "exploratory": False,
            "metrics": evidence.get("metrics") if complete(total_repeats) else None,
        }
        self._select(session, contract_hash)
        self._save_state()

    def _score(self, evidence: dict, results: list[dict]) -> None:
        """Average each metric over the complete repeats, then apply bounds and the objective."""
        metrics = {m.name: mean(result["metrics"][m.name] for result in results) for m in self.metrics}
        completed = all(result["completed"] for result in results)
        failures = self._constraint_failures(evidence, metrics)
        values = self.schema.decode(metrics)
        try:
            score, key = self.objective.assess(values, completed)
        except Exception as exc:
            score, key = None, None
            evidence.update(evaluation="incomplete", error=f"objective_error: {type(exc).__name__}: {exc}")
        if failures:
            key = None
        evidence.update(
            metrics=metrics,
            task_completed=completed,
            feasible=key is not None,
            constraint_failures=failures,
            objective=list(key) if key is not None else None,
            score=score,
            objective_metrics=[m.name for m in self.metrics if m in values.reads] if score is not None else [],
        )

    def _constraint_failures(self, evidence: dict, means: dict[str, float]) -> list[str]:
        failures = []
        for metric in self.metrics:
            if metric.bounds_scope == "mean":
                measurements = [("mean", means[metric.name])]
            else:
                measurements = [
                    (
                        f"case {cell['task_id']}, {cell['condition']}, repeat {cell['repeat']}",
                        cell["result"]["metrics"][metric.name],
                    )
                    for cell in evidence["cells"].values()
                    if cell["status"] == "completed"
                ]
            for label, value in measurements:
                if metric.minimum is not None and value < metric.minimum:
                    failures.append(f"{metric.name}={value} is below minimum {metric.minimum} ({label})")
                elif metric.maximum is not None and value > metric.maximum:
                    failures.append(f"{metric.name}={value} is above maximum {metric.maximum} ({label})")
        return failures

    def _select(self, session: dict, contract_hash: str) -> None:
        eligible = [
            (tuple(item["objective"]), candidate_id)
            for candidate_id, record in session["candidates"].items()
            for item in [record["evidence"].get(contract_hash, {})]
            if item.get("validity", {}).get("verdict") == "valid"
            and item.get("total_repeats", 0) >= self.config.selection_repeats
            and item.get("evaluation") == "completed"
            and item.get("feasible")
        ]
        if eligible:
            session["selected"][contract_hash] = max(eligible, key=lambda item: item[0])[1]
        else:
            session["selected"].pop(contract_hash, None)

    # ---- History index for researchers

    def _write_history_index(self) -> None:
        """Publish ``history/index.json`` for researchers; the controller never reads it back."""
        candidates = []
        for record in self.candidates.values():
            evidence = self.evidence(record["id"])
            validity = evidence.get("validity", {})
            measured = evidence.get("evaluation") == "completed"
            directory = f"candidates/{record['id']}"
            attempts = [
                entry
                for cell in evidence.get("cells", {}).values()
                for attempt in cell.get("trial_starts", [])
                if (entry := self._attempt_entry(cell, attempt)) is not None
            ]
            candidates.append({
                "id": record["id"],
                "parent_id": record["parent_id"],
                "proposal": record["proposal"],
                "status": record["status"],
                "validity": {
                    "verdict": validity.get("verdict"),
                    "reasons": validity.get("reasons", record.get("reasons", [])),
                },
                "evaluation": {
                    "status": evidence.get("evaluation", "not_started"),
                    "metrics": evidence.get("metrics") if measured else None,
                    "score": evidence.get("score") if measured else None,
                    "completed": evidence.get("task_completed") if measured else None,
                    "feasible": evidence.get("feasible") if measured else None,
                },
                "paths": {
                    "snapshot": f"{directory}/snapshot",
                    "diff": f"{directory}/diff.patch",
                    "validity": f"{directory}/validity/{self.contract_hash}",
                    "evaluations": f"{directory}/evaluations/{self.contract_hash}",
                },
                "attempts": attempts,
            })
        write_json(self.root / "history/index.json", {
            "objective": self.objective.identity(),
            "selected_candidate": self.selected,
            "candidates": candidates,
        })

    def _attempt_entry(self, cell: dict, attempt: dict) -> dict | None:
        """One evaluation attempt with paths relative to the researcher's /history mount."""
        result_path = attempt.get("result_path")
        if not isinstance(result_path, str):
            return None
        try:
            relative = Path(result_path).relative_to(self.root.relative_to(self.output) / "history")
        except ValueError:
            return None
        if ".." in relative.parts:
            return None
        artifacts = relative.parent / "artifacts"
        result = attempt.get("result", {}) if attempt.get("status") == "completed" else {}
        paths = {"result": relative.as_posix(), "artifacts": artifacts.as_posix()}
        trajectory = result.get("trajectory", {}).get("path")
        if isinstance(trajectory, str) and trajectory.startswith("/history/"):
            native = Path(trajectory.removeprefix("/history/"))
            if ".." not in native.parts and native.is_relative_to(artifacts):
                paths["trajectory"] = native.as_posix()
        return {
            "task_id": cell["task_id"],
            "condition": cell["condition"],
            "repeat": cell["repeat"],
            "number": attempt["number"],
            "status": attempt["status"],
            "completed": result.get("completed"),
            "metrics": result.get("metrics"),
            "error": attempt.get("error"),
            "paths": paths,
        }

    # ---- Progress display

    def _notify(self, stage, *, measured=None, **details):
        if not self._display.enabled:
            return
        if measured and measured.get("metrics") and measured.get("task_completed") is True:
            self._display.metrics = dict(measured["metrics"])
        try:
            selected_metrics = self.evidence(self.selected).get("metrics") if self.selected else None
            self._display.publish(
                stage,
                candidates_recorded=self.proposals,
                candidate_limit=self.config.max_candidates,
                evaluations_started=self.state["solver_trial_starts"],
                evaluation_limit=self.config.max_evaluations,
                selected=self.selected,
                selected_metrics=selected_metrics,
                **details,
            )
        except Exception:
            pass  # A display failure must not change what is recorded.

    def _evaluation_progress(self, stage, job, total, result=None):
        if not self._display.enabled:
            return
        _, task, condition, repeat, _, _ = job
        stage = self._display.cell(stage, total, result)
        self._notify(stage, task=task, condition=condition, repeat=repeat)


def _final_stage(result: dict) -> str:
    if result["status"] == "invalid":
        return "invalid"
    evidence = result["evidence"]
    if evidence["evaluation"] == "completed" and evidence.get("task_completed") is False:
        return "incomplete"
    return evidence["evaluation"]


class _Display:
    """Builds progress events for an optional observer.

    Events describe the run for people. They never decide admission, charging or
    saved evidence.
    """

    def __init__(self, observer: Callable[[dict], None] | None):
        self.observer = observer
        self.candidate: str | None = None
        self.metrics: dict = {}
        self._lock = threading.Lock()
        self._sequence = 0
        self._panel_total = 0
        self._panel_measured = 0

    @property
    def enabled(self) -> bool:
        return self.observer is not None

    def start_panel(self) -> None:
        self._panel_total = self._panel_measured = 0

    def cell(self, stage: str, total: int, result: dict | None) -> str:
        """Count one evaluation cell and return the stage to display for it."""
        with self._lock:
            self._panel_total = total
            if stage != "measured":
                return stage
            if result.get("completed") is not True:
                return "evaluation_incomplete"
            self._panel_measured += 1
            self.metrics = dict(result.get("metrics") or {})
            return stage

    def publish(self, stage, *, candidates_recorded, selected_metrics, **details) -> None:
        with self._lock:
            self._sequence += 1
            number = int(self.candidate.rsplit("-", 1)[1]) if self.candidate else candidates_recorded
            event = {
                "sequence": self._sequence,
                "stage": stage,
                "candidate_id": self.candidate,
                "candidate_number": number,
                "candidates_recorded": candidates_recorded,
                "panel_total": self._panel_total,
                "panel_measured": self._panel_measured,
                "metrics": dict(self.metrics or selected_metrics or {}),
                **details,
            }
        self.observer(event)
