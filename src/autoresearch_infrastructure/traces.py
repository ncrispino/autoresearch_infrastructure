"""Check and copy ATIF trajectory files. Harbor is needed only for full validation."""

import hashlib
import json
from pathlib import Path

from .persistence import write_bytes

MAX_ATIF_BYTES = 50_000_000


def atif_metadata(path: Path, *, harbor_validation: bool = False) -> dict:
    if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_ATIF_BYTES:
        raise ValueError("ATIF must be a bounded regular file")
    raw = path.read_bytes()
    document = json.loads(raw)
    if (
        not isinstance(document, dict)
        or not isinstance(document.get("agent"), dict)
        or not isinstance(document.get("steps"), list)
        or not str(document.get("schema_version", "")).startswith("ATIF-")
    ):
        raise ValueError("Malformed ATIF envelope")
    if harbor_validation:
        from harbor.utils.trajectory_validator import TrajectoryValidator

        validator = TrajectoryValidator()
        if not validator.validate(path):
            raise ValueError("Invalid Harbor ATIF: " + "; ".join(validator.get_errors()[:5]))
    return {
        "sha256": hashlib.sha256(raw).hexdigest(),
        "bytes": len(raw),
        "agent": document["agent"],
        "session_id": document.get("session_id"),
    }


def capture_atif(source: Path, destination: Path, *, harbor_validation: bool = False) -> dict:
    metadata = atif_metadata(source, harbor_validation=harbor_validation)
    raw = source.read_bytes()
    if hashlib.sha256(raw).hexdigest() != metadata["sha256"]:
        raise ValueError("ATIF changed during capture")
    write_bytes(destination, raw)
    return metadata


def atif_media_paths(document: dict) -> tuple[Path, ...]:
    """Return safe local media paths referenced by an ATIF document."""
    found: set[Path] = set()

    def visit(value) -> None:
        if isinstance(value, dict):
            if value.get("type") in {"image", "audio"}:
                source = value.get("source")
                media = source.get("path") if isinstance(source, dict) else None
                if isinstance(media, str) and "://" not in media:
                    path = Path(media)
                    if path.is_absolute() or ".." in path.parts or not path.parts:
                        raise RuntimeError("Harbor ATIF contains an unsafe local media path")
                    found.add(path)
            for child in value.values():
                visit(child)
        elif isinstance(value, list):
            for child in value:
                visit(child)

    visit(document)
    return tuple(sorted(found, key=lambda path: path.as_posix()))
