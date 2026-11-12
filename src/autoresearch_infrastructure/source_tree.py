"""One bounded text-tree format for candidate snapshots and registered packages."""

from __future__ import annotations

import difflib
import fnmatch
import hashlib
import os
import stat
from pathlib import Path

from .persistence import digest

IGNORED_DIRS = frozenset(
    {
        ".cache",
        ".git",
        ".hg",
        ".idea",
        ".mypy_cache",
        ".next",
        ".nox",
        ".pytest_cache",
        ".ruff_cache",
        ".svn",
        ".tox",
        ".venv",
        ".vscode",
        "__pycache__",
        "build",
        "coverage",
        "dist",
        "htmlcov",
        "node_modules",
        "venv",
    }
)
IGNORED_FILES = frozenset(
    {
        ".DS_Store",
        ".coverage",
        "*.log",
        "*.pyc",
        "*.pyo",
        "*.swp",
        "*.swo",
        "*.temp",
        "*.tmp",
        "*~",
        "Thumbs.db",
        "coverage.xml",
        "npm-debug.log*",
        "pnpm-debug.log*",
        "yarn-debug.log*",
    }
)

TASK_CONTEXT_MAX_FILE_BYTES = 256 * 1024 * 1024
TASK_CONTEXT_MAX_TOTAL_BYTES = 512 * 1024 * 1024
TASK_CONTEXT_MAX_FILES = 2_000


def python_tree_hash(root: Path) -> str:
    """Hash regular Python source files below one trusted source root."""
    payload = {
        path.relative_to(root).as_posix(): path.read_text(encoding="utf-8")
        for path in sorted(root.rglob("*.py"))
        if path.is_file() and not path.is_symlink()
    }
    if not payload:
        raise ValueError("Cannot bind an empty Python source tree")
    return digest(payload)


def snapshot(
    root: Path,
    ignored: list[str] | None = None,
    *,
    max_file_bytes: int = 2_000_000,
    max_total_bytes: int = 20_000_000,
    max_files: int = 2_000,
) -> dict[str, str]:
    """Read UTF-8 source with one ignore policy; reject links and oversized trees.

    Both submitted candidates and trusted package copies use these limits by default,
    so their content hashes cannot disagree about generated or editor clutter.
    """
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"Source tree must be a regular directory: {root}")
    result: dict[str, str] = {}
    total = 0
    for current_value, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current = Path(current_value)
        kept = []
        for name in sorted(dirs):
            path = current / name
            relative = path.relative_to(root).as_posix()
            if name in IGNORED_DIRS:
                if ignored is not None:
                    ignored.append(relative + "/")
                continue
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise ValueError(f"Links and special files are forbidden: {relative}")
            kept.append(name)
        dirs[:] = kept
        for name in sorted(files):
            path = current / name
            relative = path.relative_to(root).as_posix()
            if any(fnmatch.fnmatchcase(name, pattern) for pattern in IGNORED_FILES):
                if ignored is not None:
                    ignored.append(relative)
                continue
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError(f"Links and special files are forbidden: {relative}")
            total += info.st_size
            if info.st_size > max_file_bytes or total > max_total_bytes or len(result) >= max_files:
                raise ValueError("Source tree exceeds snapshot limits")
            try:
                result[relative] = path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"Source file is not UTF-8 text: {relative}") from exc
    return result


def evidence_snapshot(
    root: Path,
    *,
    max_file_bytes: int = 8_000_000,
    max_total_bytes: int = 20_000_000,
    max_files: int = 2_000,
) -> dict[str, str]:
    """Read every regular UTF-8 evidence file without source ignore rules.

    Evidence includes complete ATIF transcripts, which are routinely larger than a
    candidate source file. The separate total and file-count bounds still cap the tree.
    """
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"Evidence tree must be a regular directory: {root}")
    result: dict[str, str] = {}
    total = 0
    for current_value, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current = Path(current_value)
        for name in sorted(dirs):
            path = current / name
            relative = path.relative_to(root).as_posix()
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise ValueError(f"Links and special files are forbidden: {relative}")
        dirs[:] = sorted(dirs)
        for name in sorted(files):
            path = current / name
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError(f"Links and special files are forbidden: {relative}")
            total += info.st_size
            if info.st_size > max_file_bytes or total > max_total_bytes or len(result) >= max_files:
                raise ValueError("Evidence tree exceeds snapshot limits")
            try:
                result[relative] = path.read_text(encoding="utf-8")
            except UnicodeDecodeError as exc:
                raise ValueError(f"Evidence file is not UTF-8 text: {relative}") from exc
    return result


def evidence_digest(
    root: Path,
    *,
    max_file_bytes: int = 8_000_000,
    max_total_bytes: int = 20_000_000,
    max_files: int = 2_000,
) -> str:
    """Hash every bounded regular evidence file, including binary runtime artifacts."""
    if root.is_symlink() or not root.is_dir():
        raise ValueError(f"Evidence tree must be a regular directory: {root}")
    manifest: dict[str, dict[str, int | str]] = {}
    total = 0
    for current_value, dirs, files in os.walk(root, topdown=True, followlinks=False):
        current = Path(current_value)
        for name in sorted(dirs):
            path = current / name
            relative = path.relative_to(root).as_posix()
            if not stat.S_ISDIR(path.lstat().st_mode):
                raise ValueError(f"Links and special files are forbidden: {relative}")
        dirs[:] = sorted(dirs)
        for name in sorted(files):
            path = current / name
            relative = path.relative_to(root).as_posix()
            info = path.lstat()
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError(f"Links and special files are forbidden: {relative}")
            total += info.st_size
            if info.st_size > max_file_bytes or total > max_total_bytes or len(manifest) >= max_files:
                raise ValueError("Evidence tree exceeds digest limits")
            file_hash = hashlib.sha256()
            read = 0
            with path.open("rb") as stream:
                while chunk := stream.read(1_048_576):
                    read += len(chunk)
                    file_hash.update(chunk)
            if read != info.st_size:
                raise ValueError(f"Evidence file changed while hashing: {relative}")
            manifest[relative] = {"bytes": read, "sha256": file_hash.hexdigest()}
    return digest(manifest)


def evaluation_evidence_digest(root: Path, *, cells: int) -> str:
    """Seal bounded solver evidence, including allowed ATIF and media per cell."""
    if type(cells) is not int or cells < 1:
        raise ValueError("Evaluation evidence requires a positive cell count")
    return evidence_digest(
        root,
        max_file_bytes=50_000_000,
        max_total_bytes=110_000_000 * cells,
        max_files=2_000 * cells,
    )


def task_context_digest(root: Path) -> str:
    """Seal larger read-only task context under one fixed, bounded policy."""
    return evidence_digest(
        root,
        max_file_bytes=TASK_CONTEXT_MAX_FILE_BYTES,
        max_total_bytes=TASK_CONTEXT_MAX_TOTAL_BYTES,
        max_files=TASK_CONTEXT_MAX_FILES,
    )


def materialize(root: Path, payload: dict[str, str]) -> None:
    """Write a saved source tree into a fresh directory, rejecting unsafe paths."""
    paths = {}
    for name in payload:
        path = Path(name)
        if not path.parts or path.is_absolute() or ".." in path.parts:
            raise ValueError(f"Unsafe snapshot path: {name}")
        paths[name] = path
    root.mkdir(parents=True, exist_ok=False)
    for name, value in sorted(payload.items()):
        path = root / paths[name]
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(value, encoding="utf-8")


def diff_trees(parent: dict[str, str], candidate: dict[str, str]) -> str:
    """Describe actual source changes, including additions and deletions."""
    return "".join(
        "".join(
            difflib.unified_diff(
                parent.get(name, "").splitlines(True),
                candidate.get(name, "").splitlines(True),
                fromfile="parent/" + name,
                tofile="candidate/" + name,
            )
        )
        for name in sorted(parent.keys() | candidate.keys())
        if parent.get(name) != candidate.get(name)
    )
