"""Bounded run inspection, candidate comparison and optional Harbor rendering."""

from __future__ import annotations

import base64
import hashlib
import json
import math
import os
import re
import socket
import stat
from datetime import datetime
from pathlib import Path
from urllib.parse import quote

from autoresearch_infrastructure.persistence import digest
from autoresearch_infrastructure.source_tree import diff_trees, snapshot

MAX_VIEWER_JSON_BYTES = 50_000_000
MAX_PREVIEW_TRAJECTORY_BYTES = 50_000_000
MAX_CANDIDATE_DIFF_BYTES = 1_000_000
MAX_CANDIDATE_COMPARISON_TOTAL_BYTES = 20_000_000
MAX_PREVIEW_IMAGE_BYTES = 1_000_000
MAX_PREVIEW_IMAGE_TOTAL_BYTES = 4_000_000
_IMAGE_TYPES = {".svg": "image/svg+xml", ".png": "image/png", ".jpg": "image/jpeg", ".jpeg": "image/jpeg"}
_SAFE_COMPONENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,119}")
_HASH = re.compile(r"[0-9a-f]{64}")
_CANDIDATE_ID = re.compile(r"candidate-(\d+)")
_STATUS_TONES = {
    "valid": "good",
    "completed": "good",
    "active": "good",
    "pass": "good",
    "passed": "good",
    "invalid": "bad",
    "error": "bad",
    "failed": "bad",
    "infrastructure_error": "bad",
    "timeout": "bad",
    "resource_exhausted": "bad",
}


def _read_bounded(path: Path, limit: int | None = None) -> bytes:
    if limit is None:
        limit = MAX_VIEWER_JSON_BYTES
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise ValueError(f"Missing viewer data: {path}") from exc
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode) or before.st_size > limit:
        raise ValueError(f"Viewer data must be a bounded regular file: {path}")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise ValueError(f"Viewer data changed while opening: {path}")
        chunks = bytearray()
        while len(chunks) <= limit:
            chunk = os.read(descriptor, min(65_536, limit + 1 - len(chunks)))
            if not chunk:
                break
            chunks.extend(chunk)
        after = os.fstat(descriptor)
        if len(chunks) > limit or (opened.st_size, opened.st_mtime_ns) != (
            after.st_size,
            after.st_mtime_ns,
        ):
            raise ValueError(f"Viewer data changed or exceeded its limit: {path}")
    finally:
        os.close(descriptor)
    return bytes(chunks)


def _read_json(path: Path) -> dict:
    try:
        value = json.loads(_read_bounded(path))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Viewer data is not valid UTF-8 JSON: {path}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"Viewer data must contain a JSON object: {path}")
    return value


def _candidate_sequence(candidate_id: str) -> int | None:
    match = _CANDIDATE_ID.fullmatch(candidate_id)
    return int(match.group(1)) if match else None


def _candidate_diff(path: Path, warnings: list[str]) -> str | None:
    if not path.exists():
        return None
    try:
        before = path.lstat()
        if (
            not stat.S_ISREG(before.st_mode)
            or stat.S_ISLNK(before.st_mode)
            or before.st_size > MAX_CANDIDATE_DIFF_BYTES
        ):
            raise ValueError(f"Candidate diff must be a bounded regular file: {path}")
        return _read_bounded(path, MAX_CANDIDATE_DIFF_BYTES).decode("utf-8")
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        warnings.append(f"Candidate diff unavailable: {exc}")
        return None


def _comparison_snapshot(
    path: Path,
    expected_hash: object,
    label: str,
    warnings: list[str],
) -> dict[str, str] | None:
    if not path.exists():
        return None
    try:
        payload = snapshot(path)
    except (OSError, UnicodeDecodeError, ValueError) as exc:
        warnings.append(f"Candidate comparison source unavailable for {label}: {exc}")
        return None
    if isinstance(expected_hash, str) and _HASH.fullmatch(expected_hash):
        if digest(payload) != expected_hash:
            warnings.append(f"Candidate comparison source hash changed for {label}")
            return None
    return payload


def _comparison_diff(
    base: dict[str, str] | None,
    candidate: dict[str, str] | None,
    *,
    base_id: str,
    candidate_id: str,
    remaining_bytes: list[int],
    warnings: list[str],
) -> str | None:
    if base is None or candidate is None:
        return None
    patch = diff_trees(base, candidate)
    size = len(patch.encode("utf-8"))
    if size > MAX_CANDIDATE_DIFF_BYTES:
        warnings.append(f"Candidate comparison diff exceeds its limit: {candidate_id} vs {base_id}")
        return None
    if size > remaining_bytes[0]:
        warning = "Candidate comparison diff budget exhausted"
        if warning not in warnings:
            warnings.append(warning)
        return None
    remaining_bytes[0] -= size
    return patch


def _safe_child(root: Path, relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or not path.parts or ".." in path.parts:
        raise ValueError("Viewer state contains an unsafe evidence path")
    target = root / path
    current = root
    for part in path.parts:
        current = current / part
        if current.is_symlink():
            raise ValueError("Viewer state evidence path contains a symlink")
    resolved_root = root.resolve()
    resolved = target.resolve()
    if resolved != resolved_root and resolved_root not in resolved.parents:
        raise ValueError("Viewer state evidence escapes the run output")
    return target


def _trajectory_link(metadata: dict, jobs: Path) -> str | None:
    job = metadata.get("viewer_job")
    trial = metadata.get("viewer_trial")
    expected_hash = metadata.get("sha256")
    if (
        not isinstance(job, str)
        or not isinstance(trial, str)
        or not isinstance(expected_hash, str)
        or not _HASH.fullmatch(expected_hash)
    ):
        return None
    if not _SAFE_COMPONENT.fullmatch(job) or not _SAFE_COMPONENT.fullmatch(trial):
        return None
    trial_root = jobs / job / trial
    trajectory = trial_root / "agent/trajectory.json"
    if any(path.is_symlink() for path in (jobs, jobs / job, trial_root, trial_root / "agent", trajectory)):
        return None
    if not trajectory.is_file():
        return None
    try:
        data = _read_bounded(trajectory)
    except ValueError:
        return None
    if hashlib.sha256(data).hexdigest() != expected_hash:
        return None
    return f"/jobs/{quote(job, safe='')}/trials/{quote(trial, safe='')}"


def _nonnegative_number(value: object) -> int | float | None:
    if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
        return None
    return value


def _trajectory_usage(metadata: dict, jobs: Path) -> dict | None:
    """Read cost and token totals only from the hash-verified ATIF used by the viewer."""
    if _trajectory_link(metadata, jobs) is None:
        return None
    trajectory = jobs / metadata["viewer_job"] / metadata["viewer_trial"] / "agent/trajectory.json"
    try:
        document = json.loads(_read_bounded(trajectory))
    except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
        return None
    return _document_usage(document)


def _document_usage(document: object) -> dict | None:
    if not isinstance(document, dict):
        return None
    final = document.get("final_metrics")
    final = final if isinstance(final, dict) else {}
    steps = document.get("steps") if isinstance(document.get("steps"), list) else []

    def total(final_key: str, step_key: str) -> int | float | None:
        value = _nonnegative_number(final.get(final_key))
        if value is not None:
            return value
        values = [
            parsed
            for step in steps
            if isinstance(step, dict) and isinstance(step.get("metrics"), dict)
            if (parsed := _nonnegative_number(step["metrics"].get(step_key))) is not None
        ]
        return sum(values) if values else None

    model_calls = _nonnegative_number(final.get("total_llm_call_count"))
    if model_calls is None:
        calls = [
            parsed
            for step in steps
            if isinstance(step, dict)
            if (parsed := _nonnegative_number(step.get("llm_call_count"))) is not None
        ]
        model_calls = sum(calls) if calls else None
    return {
        "cost_usd": total("total_cost_usd", "cost_usd"),
        "prompt_tokens": total("total_prompt_tokens", "prompt_tokens"),
        "completion_tokens": total("total_completion_tokens", "completion_tokens"),
        "cached_tokens": total("total_cached_tokens", "cached_tokens"),
        "model_calls": model_calls,
    }


def _enrich_metadata(value: dict, jobs: Path, warnings: list[str], source: Path) -> dict:
    link = _trajectory_link(value, jobs)
    if link is None:
        warnings.append(f"Trajectory metadata has no viewable Harbor trial: {source}")
    timing = {"started_at": None, "finished_at": None, "basis": "harbor_trial"}
    if link is not None:
        try:
            result = _safe_child(jobs, f"{value['viewer_job']}/{value['viewer_trial']}/result.json")
            if result.exists():
                saved = _read_json(result)
                for key in ("started_at", "finished_at"):
                    timing[key] = _iso_timestamp(saved.get(key))
        except (ValueError, OSError) as exc:
            warnings.append(f"Ignored Harbor trial timing: {exc}")
    return {
        **value,
        "viewer_url": link,
        "timing": timing,
        "usage": _trajectory_usage(value, jobs),
    }


def _metadata(path: Path, jobs: Path, warnings: list[str]) -> dict | None:
    if not path.exists():
        return None
    try:
        value = _read_json(path)
    except ValueError as exc:
        warnings.append(str(exc))
        return None
    return _enrich_metadata(value, jobs, warnings, path)


def _researcher_metadata(session_root: Path, jobs: Path, warnings: list[str]) -> list[dict]:
    path = session_root / "agent/trajectory-index.json"
    if not path.exists():
        return []
    try:
        value = _read_json(path)
    except ValueError as exc:
        warnings.append(str(exc))
        return []
    entries = value.get("trajectories")
    if not isinstance(entries, list):
        warnings.append(f"Researcher trajectory index is malformed: {path}")
        return []
    rendered = []
    for index, entry in enumerate(entries):
        if not isinstance(entry, dict):
            warnings.append(f"Ignored malformed researcher trajectory {index + 1}: {path}")
            continue
        rendered.append(_enrich_metadata(entry, jobs, warnings, path))
    return rendered


def _solver_wrapper(output: Path, result_path: str, warnings: list[str]) -> tuple[Path, dict] | None:
    try:
        result = _safe_child(output, result_path)
        wrapper = result.parent / "trajectory.json"
        if not wrapper.exists():
            return None
        return wrapper, _read_json(wrapper)
    except ValueError as exc:
        warnings.append(str(exc))
        return None


def _saved_solver_metadata(output: Path, wrapper: Path, value: dict, jobs: Path, warnings: list[str]) -> dict:
    """Resolve only hash-bound /history ATIF belonging to this evaluation session."""
    link = _trajectory_link(value, jobs)
    rendered = {
        **{
            key: item
            for key, item in value.items()
            if key not in {"saved_path", "saved_key", "native_session_id", "viewer_url", "usage"}
        },
        "viewer_url": link,
        "usage": _trajectory_usage(value, jobs),
    }
    relative = wrapper.relative_to(output).parts
    path = value.get("path")
    expected = value.get("sha256")
    if not isinstance(path, str) or not path.startswith("/history/"):
        if link is None:
            warnings.append(f"Solver evidence has no viewable Harbor trial or saved trajectory: {wrapper}")
        return rendered
    try:
        if len(relative) < 4 or relative[0] != "sessions" or relative[2] != "history":
            raise ValueError("Solver wrapper is outside its session history")
        if not isinstance(expected, str) or not _HASH.fullmatch(expected):
            raise ValueError("Solver ATIF has no valid evidence hash")
        session_root = output / "sessions" / relative[1]
        source = _safe_child(session_root, path.lstrip("/"))
        raw = _read_bounded(source)
        if hashlib.sha256(raw).hexdigest() != expected:
            raise ValueError("Stored solver ATIF hash mismatch")
        document = json.loads(raw)
        if (
            not isinstance(document, dict)
            or not isinstance(document.get("agent"), dict)
            or not isinstance(document.get("steps"), list)
        ):
            raise ValueError("Stored solver ATIF is malformed")
        rendered.update(
            saved_path=source.relative_to(output).as_posix(),
            saved_key=f"/api/research/trajectories/{expected}",
            native_session_id=document.get("session_id"),
            usage=_document_usage(document),
        )
    except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        warnings.append(f"Ignored stored solver ATIF: {exc}: {wrapper}")
        # Unverified metadata usage is never authoritative evidence.
    return rendered


def _solver_observation(output: Path, result_path: str, jobs: Path, warnings: list[str]) -> dict | None:
    resolved = _solver_wrapper(output, result_path, warnings)
    if resolved is None:
        return None
    wrapper, document = resolved
    value = document.get("solver_trajectory")
    if not isinstance(value, dict):
        return None
    return _saved_solver_metadata(output, wrapper, value, jobs, warnings)


def _solver_sessions(output: Path, result_path: str, jobs: Path, warnings: list[str]) -> list[dict]:
    resolved = _solver_wrapper(output, result_path, warnings)
    if resolved is None:
        return []
    wrapper, document = resolved
    entries = document.get("solver_sessions", [])
    if not isinstance(entries, list) or len(entries) > 1000:
        warnings.append(f"Ignored malformed solver session inventory: {wrapper}")
        return []
    rendered = []
    seen = set()
    for entry in entries:
        if not isinstance(entry, dict):
            warnings.append(f"Ignored malformed solver session: {wrapper}")
            continue
        session_id = entry.get("session_id")
        if not isinstance(session_id, str) or not _SAFE_COMPONENT.fullmatch(session_id) or session_id in seen:
            warnings.append(f"Ignored missing/duplicate solver session identity: {wrapper}")
            continue
        seen.add(session_id)
        metadata = entry.get("trajectory")
        trajectory = (
            _saved_solver_metadata(output, wrapper, metadata, jobs, warnings)
            if isinstance(metadata, dict)
            else None
        )
        rendered.append(
            {
                "session_id": session_id,
                "parent_id": entry.get("parent_id"),
                "status": entry.get("status"),
                "role": entry.get("role", "solver"),
                "trajectory": trajectory,
            }
        )
    return rendered


def _attempt_trajectories(attempt: dict) -> list[dict]:
    """Inventory primary and helper ATIF once even when primary copies a final session."""
    trajectories: dict[str, dict] = {}
    metadata = [
        entry.get("trajectory") for entry in attempt.get("solver_sessions", []) if isinstance(entry, dict)
    ]
    metadata.append(attempt.get("trajectory"))
    for value in metadata:
        if not isinstance(value, dict):
            continue
        identity = str(value.get("sha256") or value.get("viewer_url") or value.get("saved_path"))
        previous = trajectories.get(identity)
        if previous is None or (previous.get("usage") is None and value.get("usage") is not None):
            trajectories[identity] = value
    return list(trajectories.values())


def _cost_summary(sessions: list[dict]) -> dict:
    groups: dict[str, list[dict]] = {"researcher": [], "validator": [], "solver": []}
    for session in sessions:
        groups["researcher"].extend(session.get("researcher_trajectories", []))
        records = [*session.get("baseline", [])]
        records.extend(
            record for candidate in session.get("candidates", []) for record in candidate.get("evidence", [])
        )
        groups["validator"].extend(
            record["validator_trajectory"]
            for record in records
            if isinstance(record.get("validator_trajectory"), dict)
        )
        groups["solver"].extend(
            trajectory
            for record in records
            for cell in record.get("cells", [])
            for trial_start in cell.get("trial_starts", [])
            for trajectory in _attempt_trajectories(trial_start)
        )

    seen: set[tuple[str, str]] = set()
    summary = {}
    for role, trajectories in groups.items():
        row = {
            "cost_usd": 0.0,
            "trajectory_count": 0,
            "priced_trajectory_count": 0,
            "prompt_tokens": 0,
            "completion_tokens": 0,
            "cached_tokens": 0,
            "model_calls": 0,
        }
        for trajectory in trajectories:
            identity = (role, str(trajectory.get("sha256") or trajectory.get("viewer_url")))
            if identity in seen:
                continue
            seen.add(identity)
            row["trajectory_count"] += 1
            usage = trajectory.get("usage")
            if not isinstance(usage, dict):
                continue
            cost = _nonnegative_number(usage.get("cost_usd"))
            if cost is not None:
                row["cost_usd"] += cost
                row["priced_trajectory_count"] += 1
            for key in ("prompt_tokens", "completion_tokens", "cached_tokens", "model_calls"):
                value = _nonnegative_number(usage.get(key))
                if value is not None:
                    row[key] += value
        summary[role] = row
    summary["total"] = {
        key: sum(summary[role][key] for role in groups) for key in next(iter(summary.values()))
    }
    return summary


def _timestamp(value: object) -> float | int | None:
    if type(value) not in {int, float}:
        return None
    if type(value) is float and not math.isfinite(value):
        return None
    return value if 0 <= value <= 253_402_300_799 else None


def _iso_timestamp(value: object) -> float | int | None:
    if not isinstance(value, str):
        return None
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return _timestamp(parsed.timestamp())
    except (ValueError, OverflowError, OSError):
        return None


def _trial_start_timing(records: list[dict]) -> dict:
    trial_starts = [
        trial_start
        for record in records
        for cell in record.get("cells", [])
        for trial_start in cell.get("trial_starts", [])
    ]
    starts = [a["started_at"] for a in trial_starts if a.get("started_at") is not None]
    ends = [a["finished_at"] for a in trial_starts if a.get("finished_at") is not None]
    return {
        "started_at": min(starts, default=None),
        "finished_at": max(ends, default=None),
        "basis": "solver_trial_starts",
        "trial_start_count": len(trial_starts),
        "timestamped_trial_start_count": sum(
            a.get("started_at") is not None or a.get("finished_at") is not None for a in trial_starts
        ),
    }


def _provenance(sessions: list[dict]) -> dict:
    """Describe saved identities without reconstructing missing historical inputs."""
    nodes: dict[str, dict] = {}
    edges: list[dict] = []
    sources: dict[str, list[str]] = {}
    for session in sessions:
        sid = session["id"]
        for candidate in session["candidates"]:
            cid = candidate["id"]
            node_id = f"candidate:{sid}:{cid}"
            nodes[node_id] = {
                "id": node_id,
                "kind": "candidate",
                "label": cid,
                "session_id": sid,
                "candidate_id": cid,
                "source_hash": candidate["source_hash"],
                "timing": candidate["timing"],
            }
            value = candidate["source_hash"]
            if isinstance(value, str) and _HASH.fullmatch(value):
                sources.setdefault(value, []).append(node_id)
    for session in sessions:
        sid = session["id"]
        records: dict[str, list[dict]] = {}
        for record in session["baseline"]:
            records.setdefault(record["contract"], []).append(record)
        for candidate in session["candidates"]:
            child = f"candidate:{sid}:{candidate['id']}"
            parent = candidate["parent_id"]
            parent_node = f"candidate:{sid}:{parent}"
            if parent is None or parent == "seed":
                parent_node = f"seed:{sid}"
                nodes[parent_node] = {
                    "id": parent_node,
                    "kind": "seed",
                    "label": "Session seed",
                    "session_id": sid,
                }
            elif parent_node not in nodes:
                nodes[parent_node] = {
                    "id": parent_node,
                    "kind": "missing_parent",
                    "label": str(parent),
                    "session_id": sid,
                    "candidate_id": parent,
                }
            edges.append({"source": parent_node, "target": child, "kind": "parent"})
            for record in candidate["evidence"]:
                contract = record["contract"]
                records.setdefault(contract, []).append(record)
                edges.append(
                    {
                        "source": child,
                        "target": f"contract:{sid}:{contract}",
                        "kind": "evaluated_under",
                    }
                )
        current = session["contract_hash"]
        if isinstance(current, str) and _HASH.fullmatch(current):
            records.setdefault(current, [])
        session["contracts"] = []
        for contract, evidence in sorted(records.items()):
            is_current = contract == current
            entry = {
                "hash": contract,
                "current": is_current,
                "inputs": session["inputs"] if is_current else None,
                "inputs_status": "current" if is_current else "historical_unknown",
                "timing": _trial_start_timing(evidence),
            }
            session["contracts"].append(entry)
            target = f"contract:{sid}:{contract}"
            nodes[target] = {
                "id": target,
                "kind": "contract",
                "label": contract[:12],
                "session_id": sid,
                "contract_hash": contract,
                **entry,
            }
            if not is_current:
                continue
            inputs = session["inputs"]
            values = [("opponent", None, inputs["opponent_hash"])]
            values.extend(("task", task, value) for task, value in inputs["task_hashes"].items())
            for kind, task, value in values:
                if not isinstance(value, str) or not value:
                    continue
                matches = sources.get(value, []) if kind == "opponent" else []
                if matches:
                    for source in matches:
                        edges.append(
                            {
                                "source": source,
                                "target": target,
                                "kind": "opponent_hash_match",
                                "qualifier": "content_match",
                                "match_count": len(matches),
                                "hash": value,
                            }
                        )
                else:
                    key = json.dumps([kind, task, value], separators=(",", ":"))
                    source = "external:" + hashlib.sha256(key.encode()).hexdigest()
                    nodes[source] = {
                        "id": source,
                        "kind": "external_input",
                        "label": task or "Opponent input",
                        "input_kind": kind,
                        "task_id": task,
                        "hash": value,
                    }
                    edges.append({"source": source, "target": target, "kind": f"{kind}_input"})
    return {"nodes": list(nodes.values()), "edges": edges}


def _trial_starts(output: Path, cells: dict, jobs: Path, warnings: list[str]) -> tuple[list[dict], set[str]]:
    rendered: list[dict] = []
    tasks: set[str] = set()
    if not isinstance(cells, dict):
        warnings.append("Ignored malformed evaluation cells")
        return rendered, tasks
    for cell_key, cell in sorted(cells.items()):
        if not isinstance(cell, dict):
            warnings.append(f"Ignored malformed evaluation cell {cell_key!r}")
            continue
        task = cell.get("task_id")
        if isinstance(task, str):
            tasks.add(task)
        trial_starts = []
        for trial_start in cell.get("trial_starts", []):
            if not isinstance(trial_start, dict):
                continue
            observation = None
            result_path = trial_start.get("result_path")
            if isinstance(result_path, str):
                observation = _solver_observation(output, result_path, jobs, warnings)
            trial_starts.append(
                {
                    "number": trial_start.get("number"),
                    "result_path": result_path,
                    "status": trial_start.get("status"),
                    "result": trial_start.get("result"),
                    "error": trial_start.get("error"),
                    "started_at": _timestamp(trial_start.get("started_at")),
                    "finished_at": _timestamp(trial_start.get("finished_at")),
                    "trajectory": observation,
                    "solver_sessions": (
                        _solver_sessions(output, result_path, jobs, warnings)
                        if isinstance(result_path, str)
                        else []
                    ),
                }
            )
        rendered.append(
            {
                "key": cell_key,
                "task": task,
                "condition": cell.get("condition"),
                "repeat": cell.get("repeat"),
                "status": cell.get("status"),
                "result": cell.get("result"),
                "trial_starts": trial_starts,
            }
        )
    return rendered, tasks


def _artifact_images(root: Path, expected: str, remaining: list[int], warnings: list[str] | None = None) -> list[dict]:
    """Capture images only if the exact bytes reproduce the evaluator's tree seal.

    Rebuild the bounded artifact manifest while reading each file once, retaining
    only image bytes. Hashing first and reading images later would permit a changed
    file to be displayed under its earlier seal. Limits match evidence_digest.
    """
    if root.is_symlink() or not root.is_dir():
        raise ValueError("Artifacts must be a regular directory")
    manifest = {}
    images = []
    total = 0
    selected_bytes = 0
    omitted = []
    for current_value, directories, files in os.walk(root, topdown=True, followlinks=False):
        current = Path(current_value)
        for name in directories:
            if not stat.S_ISDIR((current / name).lstat().st_mode):
                raise ValueError("Artifact directory contains a link or special file")
        directories[:] = sorted(directories)
        for name in sorted(files):
            path = current / name
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError("Artifact contains a link or special file")
            total += info.st_size
            if info.st_size > 8_000_000 or total > 20_000_000 or len(manifest) >= 2_000:
                raise ValueError("Artifact tree exceeds digest limits")
            raw = _read_bounded(path, 8_000_000)
            relative = path.relative_to(root).as_posix()
            sha256 = hashlib.sha256(raw).hexdigest()
            manifest[relative] = {"bytes": len(raw), "sha256": sha256}
            mime = _IMAGE_TYPES.get(path.suffix.lower())
            if mime and len(raw) <= MAX_PREVIEW_IMAGE_BYTES and selected_bytes + len(raw) <= remaining[0]:
                images.append({
                    "path": relative, "bytes": len(raw), "sha256": sha256,
                    "data_url": f"data:{mime};base64," + base64.b64encode(raw).decode("ascii"),
                })
                selected_bytes += len(raw)
            elif mime:
                omitted.append(relative)
    if digest(manifest) != expected:
        raise ValueError("Artifact tree hash mismatch")
    remaining[0] -= selected_bytes
    if omitted and warnings is not None:
        warnings.append("Image previews omitted above size limits: " + ", ".join(omitted))
    return images


def _candidate_artifact_images(output, records, candidate_root, candidate_hash, warnings, remaining):
    """Bind an image inventory to a completed result in this candidate's history."""
    for record in records:
        for cell in record["cells"]:
            for attempt in cell["trial_starts"]:
                result = attempt.get("result")
                if attempt["status"] != "completed" or not isinstance(result, dict) or result.get("completed") is not True:
                    continue
                expected = result.get("artifacts_sha256")
                if not isinstance(expected, str) or not _HASH.fullmatch(expected):
                    continue
                try:
                    path = _safe_child(output, attempt["result_path"])
                    if not path.is_relative_to(candidate_root / "evaluations"):
                        raise ValueError("Artifact result is outside its candidate evaluations")
                    wrapper = json.loads(_read_bounded(path, 1_000_000))
                    if not isinstance(wrapper, dict):
                        raise ValueError("Artifact result wrapper must be an object")
                    if wrapper.get("result") != result or result.get("candidate_hash") != candidate_hash or any(
                        wrapper.get(name) != value for name, value in {
                            "contract_hash": record["contract"], "task_id": cell["task"],
                            "condition": cell["condition"], "repeat": cell["repeat"],
                        }.items()
                    ):
                        raise ValueError("Artifact result does not match its saved evaluation")
                    attempt["artifact_images"] = _artifact_images(path.parent / "artifacts", expected, remaining, warnings)
                except (OSError, ValueError, KeyError, TypeError) as exc:
                    warnings.append(f"Artifact previews unavailable: {exc}")


def _evidence_records(
    output: Path,
    evidence: dict,
    jobs: Path,
    validator_root: Path | None,
    warnings: list[str],
) -> tuple[list[dict], set[str]]:
    records = []
    tasks: set[str] = set()
    if not isinstance(evidence, dict):
        warnings.append("Ignored malformed evidence records")
        return records, tasks
    for contract, record in sorted(evidence.items()):
        if not _HASH.fullmatch(contract) or not isinstance(record, dict):
            warnings.append(f"Ignored malformed evidence contract {contract!r}")
            continue
        cells, cell_tasks = _trial_starts(output, record.get("cells", {}), jobs, warnings)
        tasks.update(cell_tasks)
        validator = None
        if validator_root is not None:
            metadata_path = validator_root / "validity" / contract / "agent/trajectory-metadata.json"
            if not metadata_path.exists():
                attempts = sorted(
                    (validator_root / "validity" / contract).glob("attempt-*/agent/trajectory-metadata.json")
                )
                if attempts:
                    metadata_path = attempts[-1]
            validator = _metadata(metadata_path, jobs, warnings)
        validity = record.get("validity") if isinstance(record.get("validity"), dict) else {}
        records.append(
            {
                "contract": contract,
                "evaluation": record.get("evaluation"),
                "validity": validity.get("verdict"),
                "validity_reasons": validity.get("reasons", []),
                "validity_checks": validity.get("checks", []),
                "validity_record": validity,
                "total_repeats": record.get("total_repeats"),
                "requested_total_repeats": record.get("requested_total_repeats"),
                "evaluation_panel": (
                    record.get("evaluation_panel")
                    if isinstance(record.get("evaluation_panel"), dict)
                    else None
                ),
                "objective": record.get("objective"),
                "score": record.get("score"),
                "objective_metrics": record.get("objective_metrics", []),
                "feasible": record.get("feasible"),
                "constraint_failures": record.get("constraint_failures", []),
                "task_completed": record.get("task_completed"),
                "metrics": record.get("metrics", {}),
                "readiness": record.get("readiness"),
                "cells": cells,
                "validator_trajectory": validator,
                "timing": _trial_start_timing([{"cells": cells}]),
            }
        )
    return records, tasks


def _attach_approved_partials(
    output: Path, sessions: list[dict], jobs: Path, warnings: list[str], manifest: Path, state_hash: str
) -> str:
    """Attach only an explicit controller-approved inventory; never scan task artifacts."""
    raw = _read_bounded(manifest, 1_000_000)
    try:
        inventory = json.loads(raw)
    except (ValueError, UnicodeDecodeError) as exc:
        raise ValueError("Invalid partial-evidence inventory JSON") from exc
    if (
        not isinstance(inventory, dict)
        or type(inventory.get("schema_version")) is not int
        or inventory["schema_version"] != 1
    ):
        raise ValueError("Invalid partial-evidence inventory schema")
    expected_state = inventory.get("run_state_sha256")
    if expected_state != state_hash:
        raise ValueError("Partial-evidence inventory does not match the saved run state")
    records = inventory.get("records")
    if not isinstance(records, list) or len(records) > 1000:
        raise ValueError("Invalid bounded partial-evidence inventory")
    approved = {}
    for record in records:
        if not isinstance(record, dict) or not isinstance(record.get("result_path"), str):
            raise ValueError("Invalid partial-evidence record")
        key = record["result_path"]
        _safe_child(output, key)
        if key in approved:
            raise ValueError("Duplicate partial-evidence evaluation identity")
        entries = record.get("solver_sessions")
        if not isinstance(entries, list) or len(entries) > 1000:
            raise ValueError("Invalid bounded partial solver session inventory")
        approved[key] = record
    for session in sessions:
        for candidate in session.get("candidates", []):
            for evidence in candidate.get("evidence", []):
                for cell in evidence.get("cells", []):
                    for attempt in cell.get("trial_starts", []):
                        key = attempt.get("result_path")
                        record = approved.get(key)
                        if record is None or attempt.get("trajectory") or attempt.get("solver_sessions"):
                            continue
                        if record.get("candidate_hash") != candidate.get("source_hash") or record.get(
                            "contract_hash"
                        ) != evidence.get("contract"):
                            warnings.append(
                                f"Ignored partial evidence with mismatched candidate/contract: {key}"
                            )
                            continue
                        wrapper = _safe_child(output, key).with_name("trajectory.json")
                        artifacts = wrapper.parent / "artifacts"
                        seen = set()
                        for entry in record["solver_sessions"]:
                            try:
                                if not isinstance(entry, dict):
                                    raise ValueError("Invalid partial solver entry")
                                sid, metadata = entry.get("session_id"), entry.get("trajectory")
                                if (
                                    not isinstance(sid, str)
                                    or not _SAFE_COMPONENT.fullmatch(sid)
                                    or sid in seen
                                    or not isinstance(metadata, dict)
                                ):
                                    raise ValueError("Invalid partial solver identity/metadata")
                                if entry.get("status") not in {"completed", "failed"} or entry.get(
                                    "role", "solver"
                                ) not in {"solver", "helper"}:
                                    raise ValueError("Invalid partial native completion status")
                                path = metadata.get("path")
                                if not isinstance(path, str) or not path.startswith("/history/"):
                                    raise ValueError("Partial native evidence needs a session-history path")
                                source = _safe_child(output / "sessions" / session["id"], path.lstrip("/"))
                                if not source.resolve().is_relative_to(artifacts.resolve()):
                                    raise ValueError(
                                        "Partial native evidence is outside its evaluation artifacts"
                                    )
                                rendered = _saved_solver_metadata(output, wrapper, metadata, jobs, warnings)
                                if not rendered.get("saved_key"):
                                    continue
                                seen.add(sid)
                                attempt["solver_sessions"].append(
                                    {
                                        "session_id": sid,
                                        "parent_id": entry.get("parent_id"),
                                        "status": entry["status"],
                                        "role": entry.get("role", "solver"),
                                        "evidence_basis": "controller-approved partial inventory",
                                        "trajectory": rendered,
                                    }
                                )
                            except (ValueError, OSError) as exc:
                                warnings.append(f"Ignored approved partial native evidence: {exc}: {key}")
        session["costs"] = _cost_summary([session])
    return hashlib.sha256(raw).hexdigest()


def build_index(output: Path, *, partial_evidence: Path | None = None) -> dict:
    """Build bounded navigation data from controller state and saved evidence."""
    if output.is_symlink() or not output.is_dir():
        raise ValueError(f"Run output is not a directory: {output}")
    output = output.resolve()
    state = _read_json(output / "state.json")
    jobs = output / "harbor-jobs"
    warnings: list[str] = []
    promotion_receipts: dict[tuple[str, str, str], dict] = {}
    promotions = output / "promotions"
    if promotions.is_dir() and not promotions.is_symlink():
        for directory in sorted(promotions.iterdir()):
            if not directory.is_dir() or directory.is_symlink() or not _HASH.fullmatch(directory.name):
                continue
            try:
                receipt = _read_json(directory / "promotion.json")
                source = receipt.get("source")
                if not isinstance(source, dict):
                    raise ValueError("Promotion receipt source is malformed")
                key = (source.get("session_id"), source.get("candidate_id"), source.get("candidate_sha256"))
                if not all(isinstance(item, str) for item in key):
                    raise ValueError("Promotion receipt identity is malformed")
                promotion_receipts[key] = receipt
            except ValueError as exc:
                warnings.append(str(exc))
    sessions = []
    live_contract = False
    all_tasks: set[str] = set()
    comparison_bytes = [MAX_CANDIDATE_COMPARISON_TOTAL_BYTES]
    image_bytes = [MAX_PREVIEW_IMAGE_TOTAL_BYTES]
    raw_sessions = state.get("sessions", {})
    if not isinstance(raw_sessions, dict):
        raise ValueError("Autoresearch state sessions must be an object")
    for session_id, session in sorted(raw_sessions.items()):
        if not _SAFE_COMPONENT.fullmatch(session_id) or not isinstance(session, dict):
            warnings.append(f"Ignored malformed session {session_id!r}")
            continue
        spec = session.get("spec") if isinstance(session.get("spec"), dict) else {}
        frozen_contract = spec.get("frozen_contract")
        live_contract = live_contract or isinstance(frozen_contract, dict)
        session_root = output / "sessions" / session_id
        researchers = _researcher_metadata(session_root, jobs, warnings)
        researcher = researchers[-1] if researchers else None
        baseline, baseline_tasks = _evidence_records(
            output,
            session.get("baseline", {}),
            jobs,
            None,
            warnings,
        )
        all_tasks.update(baseline_tasks)
        candidates = []
        raw_candidates = session.get("candidates", {})
        if not isinstance(raw_candidates, dict):
            raw_candidates = {}
            warnings.append(f"Session {session_id} has malformed candidates")
        active_contract = session.get("contract_hash")
        approach_counts: dict[str, int] = {}
        for candidate in raw_candidates.values():
            if not isinstance(candidate, dict):
                continue
            proposal = candidate.get("proposal")
            approach = proposal.get("approach_id") if isinstance(proposal, dict) else None
            evidence = candidate.get("evidence")
            record = evidence.get(active_contract) if isinstance(evidence, dict) else None
            validity = record.get("validity") if isinstance(record, dict) else None
            if (
                isinstance(approach, str)
                and isinstance(record, dict)
                and isinstance(validity, dict)
                and validity.get("verdict") == "valid"
                and record.get("evaluation") == "completed"
            ):
                approach_counts[approach] = approach_counts.get(approach, 0) + 1
        ordered_candidates = sorted(
            raw_candidates.items(),
            key=lambda item: (
                _candidate_sequence(item[0]) is None,
                _candidate_sequence(item[0]) or 0,
                item[0],
            ),
        )
        comparison_sources: list[tuple[str, dict[str, str] | None]] = [
            (
                "seed",
                _comparison_snapshot(session_root / "seed", None, f"{session_id}/seed", warnings),
            )
        ]
        for candidate_id, candidate in ordered_candidates:
            if not _SAFE_COMPONENT.fullmatch(candidate_id) or not isinstance(candidate, dict):
                warnings.append(f"Ignored malformed candidate {candidate_id!r} in {session_id}")
                continue
            candidate_root = output / "sessions" / session_id / "history/candidates" / candidate_id
            records, candidate_tasks = _evidence_records(
                output,
                candidate.get("evidence", {}),
                jobs,
                candidate_root,
                warnings,
            )
            all_tasks.update(candidate_tasks)
            proposal = candidate.get("proposal")
            approach = proposal.get("approach_id") if isinstance(proposal, dict) else None
            candidate_payload = _comparison_snapshot(
                candidate_root / "snapshot",
                candidate.get("source_hash"),
                f"{session_id}/{candidate_id}",
                warnings,
            )
            if candidate_payload is not None:
                _candidate_artifact_images(
                    output, records, candidate_root, candidate.get("source_hash"), warnings, image_bytes,
                )
            parent_id = candidate.get("parent_id") or "seed"
            comparisons = [
                {
                    "base_id": base_id,
                    "is_default": index == len(comparison_sources) - 1,
                    "is_parent": base_id == parent_id,
                    "diff_patch": _comparison_diff(
                        base_payload,
                        candidate_payload,
                        base_id=base_id,
                        candidate_id=candidate_id,
                        remaining_bytes=comparison_bytes,
                        warnings=warnings,
                    ),
                }
                for index, (base_id, base_payload) in enumerate(comparison_sources)
            ]
            candidates.append(
                {
                    "id": candidate_id,
                    "sequence": _candidate_sequence(candidate_id),
                    "parent_id": candidate.get("parent_id"),
                    "status": candidate.get("status"),
                    "proposal": proposal if isinstance(proposal, dict) else None,
                    "completed_valid_for_approach": approach_counts.get(approach, 0),
                    "created_at": _timestamp(candidate.get("created_at")),
                    "timing": {
                        "created_at": _timestamp(candidate.get("created_at")),
                        "basis": "candidate_record",
                    },
                    "scope": candidate.get("scope", []),
                    "source_hash": candidate.get("source_hash"),
                    "promotion": candidate.get("promotion")
                    if isinstance(candidate.get("promotion"), dict)
                    else None,
                    "promotion_receipt": promotion_receipts.get(
                        (session_id, candidate_id, candidate.get("source_hash"))
                    ),
                    "reasons": candidate.get("reasons", []),
                    "structural_errors": candidate.get("structural_errors", []),
                    "diff_patch": _candidate_diff(candidate_root / "diff.patch", warnings),
                    "comparisons": comparisons,
                    "evidence": records,
                }
            )
            comparison_sources.append((candidate_id, candidate_payload))
        affected = spec.get("affected_tasks", [])
        if isinstance(affected, list):
            all_tasks.update(item for item in affected if isinstance(item, str))
        sessions.append(
            {
                "id": session_id,
                "role": spec.get("role"),
                "environment_mode": spec.get("environment_mode"),
                "admission_only": spec.get("admission_only", False),
                "assignment": spec.get("assignment_id"),
                "objective_policy": spec.get("objective"),
                "metrics": spec.get("metrics", []),
                "affected_tasks": affected,
                "status": session.get("status"),
                "phase_index": session.get("phase_index"),
                "instance_id": session.get("instance_id") or spec.get("instance_id") or session_id,
                "instance_sequence": session.get("instance_sequence"),
                "selected": session.get("selected", {}),
                "contract_hash": session.get("contract_hash"),
                "new_proposals": session.get("new_proposals", 0),
                "valid_evaluated": session.get("valid_evaluated", 0),
                "inputs": {
                    "opponent_hash": spec.get("opponent_hash"),
                    "task_hashes": spec.get("task_hashes")
                    if isinstance(spec.get("task_hashes"), dict)
                    else {},
                    "context_hash": session.get("context_hash"),
                    "policy_hash": session.get("policy_hash"),
                },
                "execution": frozen_contract,
                "researcher_trajectory": researcher,
                "researcher_trajectories": researchers,
                "baseline": baseline,
                "candidates": candidates,
            }
        )
    for session in sessions:
        session["costs"] = _cost_summary([session])
    job_names = []
    if jobs.is_dir() and not jobs.is_symlink():
        job_names = sorted(
            item.name
            for item in jobs.iterdir()
            if item.is_dir()
            and not item.is_symlink()
            and _SAFE_COMPONENT.fullmatch(item.name)
            and (item / "config.json").is_file()
            and not (item / "config.json").is_symlink()
        )
    config = state.get("config") if isinstance(state.get("config"), dict) else {}
    raw_instances = state.get("instances", {})
    instances = []
    session_ids = {item["id"] for item in sessions}
    if isinstance(raw_instances, dict):
        for instance_id, instance in raw_instances.items():
            if not _SAFE_COMPONENT.fullmatch(instance_id) or not isinstance(instance, dict):
                warnings.append(f"Ignored malformed research instance {instance_id!r}")
                continue
            listed = instance.get("session_ids", [])
            if not isinstance(listed, list):
                warnings.append(f"Research instance {instance_id} has malformed session_ids")
                listed = []
            missing = [item for item in listed if item not in session_ids]
            if missing:
                warnings.append(
                    f"Research instance {instance_id} references unavailable sessions: {', '.join(missing)}"
                )
            reserved = sum(
                len(item.get("remaining_cells", []))
                for item in state.get("reservations", {}).values()
                if isinstance(item, dict)
                and item.get("status") == "active"
                and item.get("instance_id") == instance_id
            )
            limit = instance.get("solver_trial_start_limit")
            used = instance.get("solver_trial_starts", 0)
            instances.append(
                {
                    "id": instance_id,
                    "assignment": instance.get("assignment_id"),
                    "status": instance.get("status"),
                    "session_ids": [item for item in listed if item in session_ids],
                    "current_session_id": instance.get("current_session_id"),
                    "budget": {
                        "limit": limit,
                        "used": used,
                        "reserved": reserved,
                        "remaining": limit - used - reserved
                        if isinstance(limit, int) and isinstance(used, int)
                        else None,
                    },
                }
            )
    partial_hash = (
        _attach_approved_partials(output, sessions, jobs, warnings, partial_evidence, digest(state))
        if partial_evidence is not None
        else None
    )
    graph = _provenance(sessions)
    costs = _cost_summary(sessions)
    return {
        "run": {
            "phase": state.get("phase"),
            "solver_trial_starts": state.get("solver_trial_starts", 0),
            "max_solver_trial_starts": config.get("max_solver_trial_starts"),
            "stop_requested": state.get("stop_requested", False),
            "mode": state.get("evidence_mode", "live" if live_contract else "offline fixture"),
            "offline": not live_contract,
            "output": str(output),
            "max_repeats": config.get("max_repeats"),
            "max_valid_candidates_per_approach": config.get("max_valid_candidates_per_approach"),
            "reserved_solver_trial_starts": sum(
                len(item.get("remaining_cells", []))
                for item in state.get("reservations", {}).values()
                if isinstance(item, dict) and item.get("status") == "active"
            ),
            "costs": costs,
            "partial_evidence_sha256": partial_hash,
        },
        "tasks": sorted(all_tasks),
        "sessions": sessions,
        "instances": instances,
        "graph": graph,
        "harbor_jobs": job_names,
        "warnings": warnings,
    }


_DASHBOARD_HTML = Path(__file__).with_name("viewer.html").read_text(encoding="utf-8")


def dashboard_html(data: dict | None = None, *, template: str | None = None) -> str:
    payload = "null" if data is None else json.dumps(data, separators=(",", ":")).replace("<", "\\u003c")
    tones = json.dumps(_STATUS_TONES, separators=(",", ":"))
    return (
        (template or _DASHBOARD_HTML).replace("__STATUS_TONES__", tones).replace("__RESEARCH_DATA__", payload)
    )


def _snapshot_trajectories(data: dict, output: Path) -> dict[str, dict]:
    """Embed only hash-bound ATIF referenced by the rendered controller evidence."""
    saved: dict[str, dict] = {}
    total = 0

    def visit(value: object) -> None:
        nonlocal total
        if isinstance(value, list):
            for item in value:
                visit(item)
            return
        if not isinstance(value, dict):
            return
        stored = value.get("saved_path")
        key = value.get("saved_key")
        expected = value.get("sha256")
        if (
            isinstance(stored, str)
            and isinstance(expected, str)
            and _HASH.fullmatch(expected)
            and key == f"/api/research/trajectories/{expected}"
            and key not in saved
        ):
            path = _safe_child(output, stored)
            raw = _read_bounded(path)
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError(f"Trajectory changed while writing preview: {path}")
            total += len(raw)
            if total > MAX_PREVIEW_TRAJECTORY_BYTES:
                raise ValueError("Preview trajectories exceed the 50 MB aggregate limit")
            saved[key] = {"sha256": expected, "raw": raw.decode("utf-8")}
        link = value.get("viewer_url")
        expected = value.get("sha256")
        job = value.get("viewer_job")
        trial = value.get("viewer_trial")
        if (
            isinstance(link, str)
            and isinstance(expected, str)
            and _HASH.fullmatch(expected)
            and isinstance(job, str)
            and _SAFE_COMPONENT.fullmatch(job)
            and isinstance(trial, str)
            and _SAFE_COMPONENT.fullmatch(trial)
            and link == f"/jobs/{quote(job, safe='')}/trials/{quote(trial, safe='')}"
            and link not in saved
            and not (isinstance(key, str) and key in saved)
        ):
            jobs = output / "harbor-jobs"
            trial_root = jobs / job / trial
            agent = trial_root / "agent"
            path = agent / "trajectory.json"
            if any(parent.is_symlink() for parent in (jobs, jobs / job, trial_root, agent)):
                raise ValueError(f"Trajectory parent changed while writing preview: {path}")
            raw = _read_bounded(path)
            if hashlib.sha256(raw).hexdigest() != expected:
                raise ValueError(f"Trajectory changed while writing preview: {path}")
            try:
                parsed = json.loads(raw)
            except (UnicodeDecodeError, json.JSONDecodeError) as exc:
                raise ValueError(f"Trajectory is not valid UTF-8 JSON: {path}") from exc
            if not isinstance(parsed, dict):
                raise ValueError(f"Trajectory must contain a JSON object: {path}")
            total += len(raw)
            if total > MAX_PREVIEW_TRAJECTORY_BYTES:
                raise ValueError("Preview trajectories exceed the 50 MB aggregate limit")
            saved[link] = {"sha256": expected, "raw": raw.decode("utf-8")}
        for item in value.values():
            visit(item)

    visit(data.get("sessions", []))
    return saved


def write_html_preview(
    output: Path, destination: Path, *, template: str | None = None, partial_evidence: Path | None = None
) -> dict:
    """Write a dashboard snapshot with hash-verified stored ATIF inspection."""
    data = build_index(output, partial_evidence=partial_evidence)
    output = output.resolve()
    data["snapshot_trajectories"] = _snapshot_trajectories(data, output)
    destination = destination.resolve()
    destination.parent.mkdir(parents=True, exist_ok=True)
    try:
        with destination.open("x", encoding="utf-8") as stream:
            stream.write(dashboard_html(data, template=template))
    except FileExistsError as exc:
        raise ValueError(f"HTML preview already exists: {destination}") from exc
    return {
        "status": "written",
        "preview": str(destination),
        "sessions": len(data["sessions"]),
        "tasks": len(data["tasks"]),
        "harbor_jobs": len(data["harbor_jobs"]),
        "trajectories": len(data["snapshot_trajectories"]),
    }


def create_harbor_viewer_app(jobs: Path, *, title: str = "Harbor trajectory viewer"):
    """Create Harbor's packaged UI with only its finished-job read routes."""
    from fastapi.responses import FileResponse
    from fastapi.staticfiles import StaticFiles
    from harbor.cli.view import STATIC_DIR
    from harbor.viewer import create_app

    app = create_app(jobs.resolve(), mode="jobs", static_dir=None)
    app.title = title
    read_routes = {
        "health_check",
        "get_config",
        "get_model_pricing",
        "get_job_filters",
        "list_jobs",
        "get_job",
        "get_job_analysis",
        "get_comparison_data",
        "get_job_config",
        "get_task_filters",
        "list_tasks",
        "trial_navigation",
        "chart_trials",
        "list_trials",
        "get_trial",
        "get_trajectory",
        "get_interaction",
        "get_verifier_output",
        "list_trial_files",
        "get_recording",
        "get_recording_file",
        "get_trial_file",
        "get_artifacts",
        "get_agent_logs",
    }
    allowed_methods = {"GET", "HEAD", "OPTIONS"}
    app.router.routes = [
        route
        for route in app.router.routes
        if getattr(route, "name", None) in read_routes
        and set(getattr(route, "methods", ()) or ()).issubset(allowed_methods)
    ]
    if STATIC_DIR.is_dir():
        assets = STATIC_DIR / "assets"
        fonts = STATIC_DIR / "fonts"
        if assets.is_dir():
            app.mount("/assets", StaticFiles(directory=assets), name="harbor-assets")
        if fonts.is_dir():
            app.mount("/fonts", StaticFiles(directory=fonts), name="harbor-fonts")

        @app.get("/favicon.ico")
        def favicon() -> FileResponse:
            return FileResponse(STATIC_DIR / "favicon.ico")

        @app.get("/favicon.svg")
        def favicon_svg() -> FileResponse:
            return FileResponse(STATIC_DIR / "favicon.svg", media_type="image/svg+xml")

        @app.get("/jobs")
        @app.get("/jobs/{path:path}")
        def harbor_spa(path: str = "") -> FileResponse:
            del path
            return FileResponse(STATIC_DIR / "index.html")

    return app


def create_viewer_app(output: Path, *, template: str | None = None, partial_evidence: Path | None = None):
    """Create one read-only app with research navigation and Harbor rendering."""
    from fastapi import HTTPException
    from fastapi.responses import HTMLResponse, Response

    build_index(output, partial_evidence=partial_evidence)
    output = output.resolve()
    jobs = output / "harbor-jobs"
    app = create_harbor_viewer_app(jobs, title="Research trajectory viewer")

    @app.get("/api/research")
    def research_index() -> dict:
        return build_index(output, partial_evidence=partial_evidence)

    @app.get("/api/research/trajectories/{sha256}")
    def saved_solver_trajectory(sha256: str):
        if not _HASH.fullmatch(sha256):
            raise HTTPException(status_code=404, detail="Unknown saved trajectory")
        data = build_index(output, partial_evidence=partial_evidence)
        for session in data["sessions"]:
            records = [
                *session["baseline"],
                *[record for candidate in session["candidates"] for record in candidate["evidence"]],
            ]
            for record in records:
                for cell in record["cells"]:
                    for trial_start in cell["trial_starts"]:
                        for metadata in _attempt_trajectories(trial_start):
                            if metadata.get("sha256") != sha256 or not metadata.get("saved_path"):
                                continue
                            source = _safe_child(output, metadata["saved_path"])
                            raw = _read_bounded(source)
                            if hashlib.sha256(raw).hexdigest() != sha256:
                                raise HTTPException(status_code=409, detail="Saved trajectory changed")
                            return Response(raw, media_type="application/json")
        raise HTTPException(status_code=404, detail="Unknown saved trajectory")

    @app.get("/", response_class=HTMLResponse)
    def dashboard() -> str:
        return dashboard_html(template=template)

    return app


def _port_range(value: str) -> tuple[int, int]:
    parts = value.split("-", 1)
    try:
        start, end = (int(parts[0]), int(parts[-1]))
    except ValueError as exc:
        raise ValueError("Viewer port must be a port or inclusive port range") from exc
    if not 1 <= start <= end <= 65_535:
        raise ValueError("Viewer port range must be between 1 and 65535")
    return start, end


def _available_port(host: str, value: str) -> int:
    start, end = _port_range(value)
    for port in range(start, end + 1):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as candidate:
            try:
                candidate.bind((host, port))
            except OSError:
                continue
            return port
    raise RuntimeError(f"No available viewer port in {start}-{end}")


def run_viewer(output: Path, port: str, *, partial_evidence: Path | None = None) -> None:
    import uvicorn

    host = "127.0.0.1"
    selected = _available_port(host, port)
    app = create_viewer_app(output, partial_evidence=partial_evidence)
    print(f"Research dashboard: http://{host}:{selected}")
    print(f"Harbor jobs: http://{host}:{selected}/jobs")
    uvicorn.run(app, host=host, port=selected, log_level="info")
