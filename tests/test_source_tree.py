from __future__ import annotations

import os

import pytest

from autoresearch_infrastructure.source_tree import (
    diff_trees,
    evaluation_evidence_digest,
    evidence_digest,
    evidence_snapshot,
    materialize,
    snapshot,
)


def test_snapshot_rejects_symlinks_hardlinks_and_nonregular_files(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "regular.txt").write_text("content\n")

    link = source / "link.txt"
    link.symlink_to("regular.txt")
    with pytest.raises(ValueError, match="Links and special files"):
        snapshot(source)
    link.unlink()

    hardlink = source / "hardlink.txt"
    os.link(source / "regular.txt", hardlink)
    with pytest.raises(ValueError, match="Links and special files"):
        snapshot(source)
    hardlink.unlink()

    fifo = source / "events.fifo"
    os.mkfifo(fifo)
    with pytest.raises(ValueError, match="Links and special files"):
        snapshot(source)


def test_snapshot_ignores_generated_clutter_but_keeps_source(tmp_path):
    source = tmp_path / "source"
    (source / "task").mkdir(parents=True)
    (source / "task/runtime.py").write_text("RUNNABLE = True\n")
    ignored_files = {
        ".git/config": "metadata\n",
        ".venv/lib/package.py": "installed\n",
        "node_modules/pkg/index.js": "generated\n",
        "__pycache__/runtime.pyc": "cache\n",
        ".pytest_cache/state": "cache\n",
        "debug.log": "log\n",
        "scratch.tmp": "temporary\n",
    }
    for relative, contents in ignored_files.items():
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(contents)

    ignored: list[str] = []
    assert snapshot(source, ignored) == {"task/runtime.py": "RUNNABLE = True\n"}
    assert ignored == [
        ".git/",
        ".pytest_cache/",
        ".venv/",
        "__pycache__/",
        "node_modules/",
        "debug.log",
        "scratch.tmp",
    ]


def test_snapshot_rejects_non_utf8_source(tmp_path):
    source = tmp_path / "source"
    source.mkdir()
    (source / "binary.dat").write_bytes(b"\xff")
    with pytest.raises(ValueError, match="not UTF-8"):
        snapshot(source)


@pytest.mark.parametrize("name", ["../escape.txt", "/absolute.txt", "", "."])
def test_materialize_rejects_paths_outside_destination(tmp_path, name):
    destination = tmp_path / "destination"
    with pytest.raises(ValueError, match="Unsafe snapshot path"):
        materialize(destination, {name: "forbidden\n"})
    assert not (tmp_path / "escape.txt").exists()


def test_materialize_does_not_follow_a_destination_symlink(tmp_path):
    target = tmp_path / "target"
    target.mkdir()
    destination = tmp_path / "destination"
    destination.symlink_to(target, target_is_directory=True)

    with pytest.raises(FileExistsError):
        materialize(destination, {"task/source.txt": "forbidden\n"})
    assert not list(target.iterdir())


@pytest.mark.parametrize(
    "limits",
    [
        {"max_file_bytes": 3},
        {"max_total_bytes": 7},
        {"max_files": 1},
    ],
)
def test_snapshot_enforces_file_tree_and_count_limits(tmp_path, limits):
    source = tmp_path / "source"
    source.mkdir()
    (source / "first.txt").write_text("1234")
    (source / "second.txt").write_text("5678")

    with pytest.raises(ValueError, match="snapshot limits"):
        snapshot(source, **limits)


def test_evidence_snapshot_accepts_bounded_atif_transcript_larger_than_source_limit(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    transcript = "x" * 2_500_000
    (evidence / "trajectory.json").write_text(transcript)

    assert evidence_snapshot(evidence) == {"trajectory.json": transcript}
    with pytest.raises(ValueError, match="snapshot limits"):
        evidence_snapshot(evidence, max_file_bytes=2_000_000)


def test_evidence_digest_binds_binary_runtime_artifacts_and_limits(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    artifact = evidence / "trajectory.pyc"
    artifact.write_bytes(b"\xff\x00runtime")

    before = evidence_digest(evidence)
    artifact.write_bytes(b"\xff\x00changed")
    assert evidence_digest(evidence) != before
    with pytest.raises(ValueError, match="digest limits"):
        evidence_digest(evidence, max_file_bytes=2)


def test_evaluation_evidence_digest_accepts_allowed_large_atif(tmp_path):
    evidence = tmp_path / "evidence"
    evidence.mkdir()
    (evidence / "agent-trajectory.json").write_bytes(b"x" * 8_000_001)

    assert len(evaluation_evidence_digest(evidence, cells=1)) == 64


def test_snapshot_and_materialize_preserve_unicode_and_diff_actual_changes(tmp_path):
    parent = {"deleted.txt": "gone\n", "task/source.txt": "original\n"}
    candidate = {"added.txt": "café\n", "task/source.txt": "changed\n"}
    destination = tmp_path / "tree"

    materialize(destination, candidate)

    assert snapshot(destination) == candidate
    difference = diff_trees(parent, candidate)
    assert "+café\n" in difference
    assert "-gone\n" in difference
    assert "-original\n+changed\n" in difference
