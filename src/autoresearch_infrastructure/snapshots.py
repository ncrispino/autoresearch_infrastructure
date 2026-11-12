"""Save a candidate's files and its diff from its parent."""

from pathlib import Path

from .persistence import digest, write_json
from .source_tree import diff_trees, materialize


def record_snapshot(destination: Path, payload: dict[str, str], parent: dict[str, str], *, ignored=()) -> str:
    """Write the candidate's files, the list of ignored paths and ``diff.patch``; return the files' hash."""
    materialize(destination / "snapshot", payload)
    write_json(destination / "snapshot-ignored.json", {"paths": list(ignored)})
    (destination / "diff.patch").write_text(diff_trees(parent, payload), encoding="utf-8")
    return digest(payload)
