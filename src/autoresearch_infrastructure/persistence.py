"""Content hashes and atomic file writes."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
from pathlib import Path
from typing import Any


def canonical(value: Any) -> bytes:
    return json.dumps(
        value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False
    ).encode()


def digest(value: Any) -> str:
    return hashlib.sha256(canonical(value)).hexdigest()


def write_bytes(path: Path, data: bytes) -> None:
    """Replace the file atomically and flush it to disk, so a crash leaves the old or the new file."""
    path.parent.mkdir(parents=True, exist_ok=True)
    name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, delete=False) as stream:
            name = stream.name
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
        name = None
        directory = os.open(path.parent, os.O_RDONLY | getattr(os, "O_DIRECTORY", 0))
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if name is not None:
            Path(name).unlink(missing_ok=True)


def write_json(path: Path, value: Any) -> None:
    """Write a value, or a pydantic model, as indented JSON with ``write_bytes``."""
    data = value.model_dump(mode="json") if hasattr(value, "model_dump") else value
    text = json.dumps(data, indent=2, ensure_ascii=False, allow_nan=False) + "\n"
    write_bytes(path, text.encode("utf-8"))
