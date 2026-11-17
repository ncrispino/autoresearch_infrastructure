"""What one researcher start sees of the experiment's records.

The canonical history under ``sessions/research/history`` is the controller's
evidence and is never edited for a researcher. With the default memory a start
mounts it directly. Any other policy builds a separate view for that start:
copies of the visible candidates, an index listing only them, the preloaded
records and the selected conversations. The view is deleted after the start;
its manifest is kept in the start's input record.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path

from .inputs import Memory, Start
from .persistence import write_json
from .search import researcher_starts


def prepare(research, memory: Memory, *, number: int, parent_id: str, repeats: int,
            preloaded: Path | None, view: Path) -> tuple[Start, Path | None, Path | None, dict]:
    """Choose what this start sees.

    Returns the ``Start`` for the presenter, the directory to mount at
    ``/history`` (``None`` for the canonical history), the conversation to load
    natively (or ``None``), and the manifest to save with the start.
    """
    canonical = research.root / "history"
    index = json.loads((canonical / "index.json").read_text())
    order = [entry["id"] for entry in index["candidates"]]
    hashes = {cid: research.candidates[cid]["source_hash"] for cid in order}
    carried = _conversations(research, memory)
    manifest = {"memory": memory.identity(), "conversations": carried}
    load = None
    if memory.conversation_mode == "native" and carried:
        load = research.root / carried[-1]["trajectory"]
        _check(load, carried[-1]["sha256"])
    if memory.is_default:
        manifest["history"] = {"view": "canonical", "candidates": hashes,
                               "index_sha256": _sha256(canonical / "index.json")}
        return Start(number, parent_id, repeats), None, load, manifest

    if memory.history == "all":
        visible = order
    elif memory.history == "none":
        visible = []
    else:
        visible = order[-memory.history:]
    if view.exists():
        _remove(view)
    view.mkdir(parents=True)
    for cid in visible:
        _copy(canonical / "candidates" / cid, view / "candidates" / cid)
    index["candidates"] = [entry for entry in index["candidates"] if entry["id"] in visible]
    if index.get("selected_candidate") not in visible:
        index["selected_candidate"] = None
    write_json(view / "index.json", index)
    if preloaded is not None:
        _copy(preloaded, view / "preloaded")
    files = memory.conversation_mode == "files" and carried
    if files:
        for number_, entry in enumerate(carried, 1):
            source = research.root / entry["trajectory"]
            _check(source, entry["sha256"])
            target = view / "conversations" / f"{number_:04d}-{entry['invocation']}.json"
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(source, target)
    manifest["history"] = {"view": "filtered", "candidates": {cid: hashes[cid] for cid in visible},
                           "index_sha256": _sha256(view / "index.json"), "preloaded": preloaded is not None}
    start = Start(
        number, parent_id, repeats,
        visible=None if memory.history == "all" else tuple(visible),
        preloaded=preloaded is not None,
        conversations=tuple(entry["invocation"] for entry in carried),
        conversation_mode=memory.conversation_mode,
    )
    return start, view, load, manifest


def discard(view: Path | None) -> None:
    if view is not None and view.exists():
        _remove(view)


def _conversations(research, memory: Memory) -> list[dict]:
    """The conversations of the last ``k`` starts that saved one, oldest first."""
    if memory.conversations == 0:
        return []
    saved = [{"invocation": s["invocation"], "trajectory": s["trajectory"], "sha256": s["trajectory_sha256"]}
             for s in researcher_starts(research) if s.get("trajectory")]
    return saved if memory.conversations == "all" else saved[-memory.conversations:]


def _copy(source: Path, target: Path) -> None:
    """Copy a tree as new, writable files, so the view never shares inodes with evidence."""
    for current, dirs, files in os.walk(source, followlinks=False):
        relative = Path(current).relative_to(source)
        (target / relative).mkdir(parents=True, exist_ok=True)
        for name in files:
            path = Path(current) / name
            if path.is_symlink() or not path.is_file():
                raise ValueError(f"Memory views copy regular files only: {path}")
            shutil.copyfile(path, target / relative / name)


def _remove(path: Path) -> None:
    for current, dirs, _ in os.walk(path):
        for name in dirs:
            (Path(current) / name).chmod(0o755)
    shutil.rmtree(path)


def _check(path: Path, expected: str) -> None:
    if _sha256(path) != expected:
        raise ValueError(f"A saved researcher conversation changed: {path}")


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()
