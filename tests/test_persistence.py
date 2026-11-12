import json

import pytest

from autoresearch_infrastructure.persistence import digest, write_bytes, write_json


def test_exact_bytes_and_json_share_durable_replacement(tmp_path):
    path = tmp_path / "evidence" / "trajectory.json"
    original = b'{"message":"original"}\n'
    write_bytes(path, original)
    assert path.read_bytes() == original

    write_json(path, {"message": "café"})
    assert json.loads(path.read_bytes()) == {"message": "café"}
    assert list(path.parent.iterdir()) == [path]


def test_json_rejects_nonfinite_values_without_replacing_evidence(tmp_path):
    path = tmp_path / "result.json"
    write_bytes(path, b"original\n")

    with pytest.raises(ValueError):
        write_json(path, {"value": float("nan")})

    assert path.read_bytes() == b"original\n"
    assert digest({"a": 1, "b": 2}) == digest({"b": 2, "a": 1})
