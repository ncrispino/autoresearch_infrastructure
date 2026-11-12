"""Bounded program package validation; candidate modules are never imported here."""
from __future__ import annotations

import ast
import re
import tomllib
from pathlib import Path
from typing import Any

from autoresearch_infrastructure.source_tree import snapshot

_FIELDS = {"id", "description", "entrypoint", "kwargs", "schema_version"}


def is_program(path: Path, *, manifest_name: str = "program.toml") -> bool:
    manifest = path / manifest_name
    if manifest.is_symlink() or not manifest.is_file():
        return False
    if manifest.stat().st_size > 100_000:
        raise ValueError("Program manifest exceeds 100 KB")
    try:
        return "entrypoint" in tomllib.loads(manifest.read_text(encoding="utf-8"))
    except (tomllib.TOMLDecodeError, UnicodeError) as exc:
        raise ValueError("Invalid program manifest") from exc


def program_manifest(path: Path, program_id: str | None = None, *, manifest_name: str = "program.toml") -> dict[str, Any]:
    payload = snapshot(path)
    try:
        raw = tomllib.loads(payload[manifest_name])
    except (KeyError, tomllib.TOMLDecodeError) as exc:
        raise ValueError("Program requires a valid program.toml") from exc
    if set(raw) - _FIELDS:
        raise ValueError(f"Unknown program fields: {sorted(set(raw) - _FIELDS)}")
    if not isinstance(raw.get("id"), str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", raw["id"]):
        raise ValueError("Program requires a safe ID")
    if program_id is not None and raw["id"] != program_id:
        raise ValueError("Program ID does not match registered package")
    if not isinstance(raw.get("description"), str) or not raw["description"].strip():
        raise ValueError("Program requires a description")
    if type(raw.get("schema_version", 1)) is not int or raw.get("schema_version", 1) != 1:
        raise ValueError("Unsupported program schema version")
    entrypoint = raw.get("entrypoint")
    if not isinstance(entrypoint, str) or entrypoint.count(":") != 1:
        raise ValueError("Program entrypoint must be package-relative Python path:function")
    filename, function = entrypoint.split(":")
    relative = Path(filename)
    if (
        relative.is_absolute() or ".." in relative.parts or not relative.parts
        or relative.suffix != ".py" or relative.as_posix() != filename
        or not function.isidentifier() or filename not in payload
    ):
        raise ValueError("Unsafe or missing program entrypoint")
    for name, source in payload.items():
        if name.endswith(".py"):
            try:
                tree = ast.parse(source, filename=name)
            except SyntaxError as exc:
                raise ValueError(f"Invalid Python source: {name}") from exc
            if name == filename and not any(
                isinstance(node, ast.AsyncFunctionDef) and node.name == function for node in tree.body
            ):
                raise ValueError("Program entrypoint must declare an async function")
    if not isinstance(raw.get("kwargs", {}), dict):
        raise ValueError("Program kwargs must be a table")
    for hook in ("pre_tool", "post_tool"):
        reference = raw.get("kwargs", {}).get(hook)
        if reference is None:
            continue
        if not isinstance(reference, str) or reference.count(":") != 1:
            raise ValueError(f"Static {hook} requires a package-relative Python path:function")
        hook_file, hook_name = reference.split(":")
        hook_path = Path(hook_file)
        if (
            hook_path.is_absolute() or ".." in hook_path.parts or hook_path.suffix != ".py"
            or hook_path.as_posix() != hook_file or hook_file not in payload
            or not hook_name.isidentifier()
        ):
            raise ValueError(f"Unsafe or missing {hook} callback")
        tree = ast.parse(payload[hook_file], filename=hook_file)
        if not any(isinstance(node, ast.AsyncFunctionDef) and node.name == hook_name for node in tree.body):
            raise ValueError(f"Static {hook} must declare an async callback")
    return raw


def validate_program(path: Path, program_id: str, *, harness: str | None = None, manifest_name: str = "program.toml") -> None:
    raw = program_manifest(path, program_id, manifest_name=manifest_name)
    if harness is not None:
        from autoresearch_infrastructure.harnesses.settings import validate_kwargs

        validate_kwargs(harness, raw.get("kwargs", {}), package_root=path)
