"""Evaluation of a saved candidate over its tasks, conditions and repeats.

A *cell* is one (task, condition, repeat). Each attempt to measure a cell is a
*trial start*. Starts are reserved for the whole requested panel before any cell
runs, charged when a cell actually begins, and never refunded.

A controller subclasses ``EvaluationLifecycle`` and supplies ``_session``,
``_conditions``, ``_reserve_evaluation``, ``_start_evaluation``,
``_load_trial_start_result`` and ``_complete_evidence``.
"""

from __future__ import annotations

import os
import re
import time
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

from .persistence import digest, write_json
from .source_tree import snapshot


class EvaluationLifecycle:
    def _run_external(self, operation, *args):
        """Run a slow operation with the state lock released, so other sessions can proceed."""
        self._lock.release()
        try:
            return operation(*args)
        finally:
            self._lock.acquire()

    @property
    def remaining_trial_starts(self) -> int:
        return self.config.max_solver_trial_starts - self.state["solver_trial_starts"]

    @property
    def available_trial_start_capacity(self) -> int:
        reserved = sum(
            len(item["remaining_cells"])
            for item in self.state.get("reservations", {}).values()
            if item["status"] == "active"
        )
        return self.remaining_trial_starts - reserved

    def _instance_id(self, session: dict) -> str:
        return session["spec"].get("instance_id") or session["spec"]["session_id"]

    def remaining_instance_trial_starts(self, session_id: str) -> int:
        session = self._session(session_id)
        instance = self.state["instances"][self._instance_id(session)]
        return instance["solver_trial_start_limit"] - instance["solver_trial_starts"]

    def available_instance_trial_start_capacity(self, session_id: str) -> int:
        instance_id = self._instance_id(self._session(session_id))
        reserved = sum(
            len(item["remaining_cells"])
            for item in self.state.get("reservations", {}).values()
            if item["status"] == "active" and item.get("instance_id") == instance_id
        )
        instance = self.state["instances"][instance_id]
        return instance["solver_trial_start_limit"] - instance["solver_trial_starts"] - reserved

    def _save_state(self) -> None:
        with self._lock:
            write_json(self.state_path, self.state)

    def _recover_interrupted_trial_starts(self) -> None:
        """Close what a stopped controller left open.

        Active reservations are released. A trial start left ``running`` is kept
        if its result was saved intact, and marked interrupted otherwise.
        """
        changed = False
        for reservation in self.state.get("reservations", {}).values():
            if reservation.get("status") == "active":
                reservation["status"] = "released"
                changed = True
        for session in self.state.get("sessions", {}).values():
            for evidence in session.get("baseline", {}).values():
                if self._recover_cells(evidence):
                    evidence.update(evaluation="incomplete", error="interrupted")
                    changed = True
            for candidate in session.get("candidates", {}).values():
                for evidence in candidate.get("evidence", {}).values():
                    changed |= self._recover_cells(evidence)
                    if evidence.get("evaluation") == "running":
                        evidence.update(evaluation="incomplete", error="interrupted")
                        changed = True
        if changed:
            self._save_state()

    def _recover_cells(self, evidence: dict) -> bool:
        recovered = False
        for cell in evidence.get("cells", {}).values():
            for trial_start in cell.get("trial_starts", []):
                if trial_start.get("status") != "running":
                    continue
                result = self._load_trial_start_result(
                    self.output / trial_start["result_path"],
                    cell["task_id"],
                    cell["condition"],
                    cell["repeat"],
                    evidence["contract_hash"],
                )
                if result is not None:
                    trial_start.update(status="completed", finished_at=time.time(), result=result)
                    cell.update(status="completed", result=result)
                else:
                    trial_start.update(status="interrupted", finished_at=time.time())
                    cell["status"] = "missing"
                recovered = True
        return recovered

    def dispatch_record_candidate(self, session_id: str, arguments: dict) -> dict:
        unknown = set(arguments) - {"candidate", "total_repeats", "parent_id", "proposal", "evaluation"}
        candidate = arguments.get("candidate")
        repeats = arguments.get("total_repeats", self.config.exploratory_repeats)
        parent_id = arguments.get("parent_id")
        proposal = arguments.get("proposal")
        candidate_pattern = r"candidate-[0-9]{4,12}"
        if (
            unknown
            or not isinstance(candidate, str)
            or (candidate != "workspace" and not re.fullmatch(candidate_pattern, candidate))
            or type(repeats) is not int
            or not 1 <= repeats <= self.config.max_repeats
            or (
                parent_id is not None
                and (
                    not isinstance(parent_id, str)
                    or (parent_id != "seed" and not re.fullmatch(candidate_pattern, parent_id))
                )
            )
        ):
            raise ValueError("Invalid record_candidate arguments")
        return self.record_candidate(
            session_id=session_id,
            candidate=candidate,
            total_repeats=repeats,
            parent_id=parent_id,
            proposal=proposal,
            evaluation=arguments.get("evaluation"),
        )

    def _run_evaluator_panel(
        self, jobs: list[tuple], before_start=None
    ) -> list[tuple[dict | None, BaseException | None]]:
        """Run every job concurrently and return each (result, error) in job order."""
        if not jobs:
            return []

        def run(job: tuple) -> tuple[dict | None, BaseException | None]:
            try:
                if before_start is not None:
                    before_start(job)
                self._evaluation_progress("evaluating", job, len(jobs))
                result = self.evaluator(*job)
                self._evaluation_progress("measured", job, len(jobs), result)
                return result, None
            except BaseException as exc:
                self._evaluation_progress("evaluation_error", job, len(jobs))
                return None, exc

        with ThreadPoolExecutor(
            max_workers=min(len(jobs), self.config.max_parallel_solver_trials),
            thread_name_prefix="research-evaluation",
        ) as executor:
            futures = [executor.submit(run, job) for job in jobs]
            return [future.result() for future in futures]

    def _run_reserved_evaluator_panel(self, prepared, start_evaluation):
        """Run reserved cells, charging each one when its worker begins.

        The start and its charge are saved together before the evaluator runs, so
        a cell that fails after starting stays charged even without a result file.
        A cell still queued keeps its reservation and has no start to recover.
        """
        by_result_path = {item[2]: item for item in prepared}

        def start(job):
            cell, trial_start, result_path, _ = by_result_path[job[-1]]
            with self._lock:
                trial_start.update(status="running", started_at=time.time())
                cell["trial_starts"].append(trial_start)
                cell["status"] = "running"
                start_evaluation(self._cell_key(cell["task_id"], cell["condition"], cell["repeat"]))
                self._save_state()
            result_path.parent.mkdir(parents=True, exist_ok=True)

        return self._run_evaluator_panel([item[3] for item in prepared], before_start=start)

    def _evaluation_progress(self, stage, job, total, result=None):
        """Display hook. The default shows nothing."""

    def _candidate_dir(self, session_id: str, candidate_id: str) -> Path:
        return self.output / "sessions" / session_id / "history" / "candidates" / candidate_id

    def _candidate_payload(self, session_id: str, candidate_id: str | None) -> dict[str, str]:
        if candidate_id in {None, "seed"}:
            return snapshot(self.output / "sessions" / session_id / "seed")
        session = self._session(session_id)
        if candidate_id not in session["candidates"]:
            raise ValueError("Parent candidate is not in this researcher's tree")
        payload = snapshot(self._candidate_dir(session_id, candidate_id) / "snapshot")
        if digest(payload) != session["candidates"][candidate_id].get("source_hash"):
            raise ValueError("Parent candidate snapshot changed")
        return payload

    @staticmethod
    def _freeze_tree(root: Path) -> None:
        for current, dirs, files in os.walk(root, topdown=False):
            for name in files:
                (Path(current) / name).chmod(0o444)
            for name in dirs:
                (Path(current) / name).chmod(0o555)
        root.chmod(0o555)

    def _verified_candidate_path(self, session_id: str, candidate_id: str) -> Path:
        path = self._candidate_dir(session_id, candidate_id) / "snapshot"
        record = self._session(session_id)["candidates"][candidate_id]
        if digest(snapshot(path)) != record["source_hash"]:
            raise ValueError("Saved candidate snapshot changed")
        return path

    @staticmethod
    def _cell_key(task_id: str, condition: str, repeat: int) -> str:
        return f"{task_id}/{condition}/{repeat}"

    def _evaluate(
        self,
        session_id: str,
        candidate_id: str,
        spec,
        contract_hash: str,
        total_repeats: int,
        evaluation_tasks: tuple[str, ...] | None = None,
    ) -> None:
        session = self._session(session_id)
        record = session["candidates"][candidate_id]
        evidence = record["evidence"][contract_hash]
        requested_tasks = evaluation_tasks or tuple(record["scope"])
        evidence["requested_tasks"] = list(requested_tasks)
        evidence["requested_total_repeats"] = total_repeats
        evidence["evaluation_panel"] = {
            "tasks": list(requested_tasks),
            "total_repeats": total_repeats,
            "status": "incomplete",
            "exploratory": set(requested_tasks) != set(record["scope"]),
            "metrics": None,
        }
        missing = []
        for task_id in requested_tasks:
            for repeat in range(1, total_repeats + 1):
                for condition in self._conditions(spec.role):
                    key = self._cell_key(task_id, condition, repeat)
                    if evidence["cells"].get(key, {}).get("status") != "completed":
                        missing.append((key, task_id, condition, repeat))
        if not missing:
            self._complete_evidence(session_id, candidate_id, contract_hash, total_repeats, requested_tasks)
            return
        reservation_id, reservation_error = self._reserve_evaluation(
            session_id, candidate_id, contract_hash, spec, [item[0] for item in missing]
        )
        if reservation_error:
            evidence.update(
                evaluation="incomplete", error=reservation_error, requested_total_repeats=total_repeats
            )
            self._save_state()
            return
        evidence["evaluation"] = "running"
        evidence["requested_total_repeats"] = total_repeats
        self._save_state()
        candidate_path = self._verified_candidate_path(session_id, candidate_id)
        prepared = []
        for key, task_id, condition, repeat in missing:
            cell = evidence["cells"].setdefault(
                key,
                {
                    "task_id": task_id,
                    "condition": condition,
                    "repeat": repeat,
                    "status": "missing",
                    "trial_starts": [],
                },
            )
            trial_start = {
                "number": len(cell["trial_starts"]) + 1,
            }
            trial_start_root = (
                self._candidate_dir(session_id, candidate_id)
                / "evaluations"
                / contract_hash
                / "cells"
                / key
                / f"trial-start-{trial_start['number']}"
            )
            result_path = trial_start_root / "result.json"
            trial_start["result_path"] = result_path.relative_to(self.output).as_posix()
            prepared.append(
                (
                    cell,
                    trial_start,
                    result_path,
                    (candidate_path, task_id, condition, repeat, contract_hash, result_path),
                )
            )
        self._save_state()
        outcomes = self._run_external(
            self._run_reserved_evaluator_panel,
            prepared,
            partial(self._start_evaluation, session_id, spec, reservation_id),
        )
        fatal = next(
            (error for _, error in outcomes if error is not None and not isinstance(error, Exception)),
            None,
        )
        if fatal is not None:
            raise fatal
        candidate_error: Exception | None = None
        try:
            self._verified_candidate_path(session_id, candidate_id)
        except Exception as exc:
            candidate_error = exc
        failed = False
        for (cell, trial_start, result_path, job), (returned, error) in zip(prepared, outcomes):
            _, task_id, condition, repeat, _, _ = job
            try:
                if candidate_error is not None:
                    raise candidate_error
                if error is not None:
                    raise error
                result = self._load_trial_start_result(result_path, task_id, condition, repeat, contract_hash)
                if result is None or returned != result:
                    raise ValueError("Evaluator did not durably persist the returned cell result")
                trial_start.update(status="completed", finished_at=time.time(), result=result)
                cell.update(status="completed", result=result)
            except Exception as exc:
                trial_start.update(
                    status="infrastructure_error", finished_at=time.time(), error=type(exc).__name__
                )
                cell["status"] = "missing"
                failed = True
        if failed:
            evidence.update(evaluation="incomplete", error="infrastructure_error")
            if reservation_id is not None:
                self._release_reservation(reservation_id)
            self._save_state()
            return
        self._complete_evidence(session_id, candidate_id, contract_hash, total_repeats, requested_tasks)

    def _consume_reservation(self, reservation_id: str, cell: str) -> None:
        with self._lock:
            reservation = self.state["reservations"][reservation_id]
            if reservation["status"] != "active" or cell not in reservation["remaining_cells"]:
                raise ValueError("Solver reservation is not active for this cell")
            reservation["remaining_cells"].remove(cell)
            self.state["solver_trial_starts"] += 1
            self.state["instances"][reservation["instance_id"]]["solver_trial_starts"] += 1
            if not reservation["remaining_cells"]:
                reservation["status"] = "completed"
            self._save_state()

    def _release_reservation(self, reservation_id: str) -> None:
        with self._lock:
            reservation = self.state["reservations"].get(reservation_id)
            if reservation and reservation["status"] == "active":
                reservation["status"] = "released"
                self._save_state()

    def _create_reservation(self, session_id, candidate_id, contract_hash, cells):
        with self._lock:
            instance_id = self._instance_id(self._session(session_id))
            if len(cells) > self.available_trial_start_capacity:
                return None, "global_solver_trial_start_limit"
            if len(cells) > self.available_instance_trial_start_capacity(session_id):
                return None, "instance_solver_trial_start_limit"
            reservation_id = digest(
                {
                    "session": session_id,
                    "candidate": candidate_id,
                    "contract": contract_hash,
                    "cells": cells,
                    "time": time.time_ns(),
                }
            )
            self.state["reservations"][reservation_id] = {
                "session_id": session_id,
                "candidate_id": candidate_id,
                "contract_hash": contract_hash,
                "instance_id": instance_id,
                "remaining_cells": list(cells),
                "status": "active",
            }
            self._save_state()
            return reservation_id, None
