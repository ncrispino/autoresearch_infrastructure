from __future__ import annotations

import json
import os

import pytest

from autoresearch_infrastructure.persistence import write_json
from autoresearch_infrastructure.validity import VerdictError, read_verdict

HASH_A = "a" * 64
HASH_B = "b" * 64
CRITERIA = ("scope", "runnable_reset")


def payload():
    return {
        "candidate_hash": HASH_A,
        "contract_hash": HASH_B,
        "verdict": "valid",
        "reasons": ["checked"],
        "checks": [
            {"criterion": item, "status": "pass", "evidence_paths": [f"/evidence/{item}.txt"]}
            for item in CRITERIA
        ],
    }


def evidence_roots(tmp_path):
    root = tmp_path / "evidence"
    root.mkdir(exist_ok=True)
    for criterion in CRITERIA:
        (root / f"{criterion}.txt").write_text("checked\n")
    return {"/evidence": root}


def test_reads_hash_bound_regular_file(tmp_path):
    path = tmp_path / "validity" / "verdict.json"
    write_json(path, payload())
    verdict = read_verdict(path, HASH_A, HASH_B, CRITERIA, evidence_roots(tmp_path))
    assert verdict.verdict == "valid"
    assert [check.criterion for check in verdict.checks] == list(CRITERIA)


@pytest.mark.parametrize(
    ("mutate", "code"),
    [
        (lambda value: value.update(candidate_hash="c" * 64), "candidate_hash_mismatch"),
        (lambda value: value.update(contract_hash="c" * 64), "contract_hash_mismatch"),
        (lambda value: value.update(extra=True), "invalid_schema"),
        (lambda value: value.update(verdict="maybe"), "invalid_verdict"),
        (lambda value: value.update(verdict="inconclusive"), "invalid_verdict"),
        (lambda value: value["checks"][0].update(status="inconclusive"), "invalid_check_status"),
        (lambda value: value.update(reasons=[]), "invalid_reasons"),
        (lambda value: value["checks"].pop(), "missing_criterion"),
        (lambda value: value["checks"].append(value["checks"][0]), "duplicate_criterion"),
        (lambda value: value["checks"][0].update(evidence_paths=[]), "passing_check_without_evidence"),
        (lambda value: value["checks"][0].update(status="fail"), "valid_with_nonpassing_check"),
        (lambda value: value["checks"][0].update(reason=""), "invalid_check_reason"),
        (lambda value: value["checks"][0].update(note="x"), "invalid_check_schema"),
    ],
)
def test_rejects_invalid_schema_and_binding(tmp_path, mutate, code):
    value = payload()
    mutate(value)
    path = tmp_path / "verdict.json"
    write_json(path, value)
    with pytest.raises(VerdictError, match=code) as raised:
        read_verdict(path, HASH_A, HASH_B, CRITERIA, evidence_roots(tmp_path))
    # Only a verdict bound to another candidate or setup is refused outright; the rest is the reviewer's form.
    assert raised.value.correctable == ("mismatch" not in code)


def test_each_check_may_carry_its_own_reason(tmp_path):
    value = payload()
    value["checks"][0]["reason"] = "Only the candidate value changed."
    path = tmp_path / "verdict.json"
    write_json(path, value)
    verdict = read_verdict(path, HASH_A, HASH_B, CRITERIA, evidence_roots(tmp_path))
    assert [check.reason for check in verdict.checks] == ["Only the candidate value changed.", None]
    saved = verdict.to_dict()["checks"]
    assert saved[0]["reason"] == "Only the candidate value changed." and "reason" not in saved[1]


def test_malformed_json_says_where(tmp_path):
    path = tmp_path / "verdict.json"
    path.write_text(json.dumps(payload())[:-1] + ")}")
    with pytest.raises(VerdictError, match="malformed_json") as raised:
        read_verdict(path, HASH_A, HASH_B, CRITERIA, evidence_roots(tmp_path))
    assert raised.value.code == "malformed_json" and "line 1 column" in raised.value.detail
    assert raised.value.correctable


def test_missing_and_symlink_fail_closed(tmp_path):
    missing = tmp_path / "missing.json"
    with pytest.raises(VerdictError, match="missing_file"):
        read_verdict(missing, HASH_A, HASH_B, CRITERIA, evidence_roots(tmp_path))
    target = tmp_path / "target.json"
    write_json(target, payload())
    link = tmp_path / "link.json"
    link.symlink_to(target)
    with pytest.raises(VerdictError, match="not_regular_file"):
        read_verdict(link, HASH_A, HASH_B, CRITERIA, evidence_roots(tmp_path))


def test_hardlink_fifo_oversize_and_invalid_utf8_are_rejected(tmp_path):
    target = tmp_path / "target.json"
    write_json(target, payload())
    hardlink = tmp_path / "hardlink.json"
    os.link(target, hardlink)
    with pytest.raises(VerdictError, match="multiple_links"):
        read_verdict(hardlink, HASH_A, HASH_B, CRITERIA, evidence_roots(tmp_path))
    fifo = tmp_path / "fifo"
    os.mkfifo(fifo)
    with pytest.raises(VerdictError, match="not_regular_file"):
        read_verdict(fifo, HASH_A, HASH_B, CRITERIA, evidence_roots(tmp_path))
    oversized = tmp_path / "oversized.json"
    oversized.write_bytes(b"x" * 65_537)
    with pytest.raises(VerdictError, match="oversized_file"):
        read_verdict(oversized, HASH_A, HASH_B, CRITERIA, evidence_roots(tmp_path))
    invalid = tmp_path / "invalid.json"
    invalid.write_bytes(b"\xff")
    with pytest.raises(VerdictError, match="invalid_utf8"):
        read_verdict(invalid, HASH_A, HASH_B, CRITERIA, evidence_roots(tmp_path))


@pytest.mark.parametrize(
    "text",
    [
        "{",
        '{"candidate_hash":"a","candidate_hash":"b"}',
        json.dumps(payload()).replace('"checks":', '"number": NaN, "checks":'),
    ],
)
def test_malformed_partial_duplicate_and_nonfinite_json_are_rejected(tmp_path, text):
    path = tmp_path / "verdict.json"
    path.write_text(text)
    with pytest.raises(VerdictError):
        read_verdict(path, HASH_A, HASH_B, CRITERIA, evidence_roots(tmp_path))


def test_passing_check_requires_existing_regular_evidence(tmp_path):
    value = payload()
    value["checks"][0]["evidence_paths"] = ["/evidence/missing.txt"]
    path = tmp_path / "verdict.json"
    write_json(path, value)
    with pytest.raises(VerdictError, match="missing_evidence"):
        read_verdict(path, HASH_A, HASH_B, CRITERIA, evidence_roots(tmp_path))


def test_public_validity_records_are_binary():
    from autoresearch_infrastructure import Validity
    from autoresearch_infrastructure.validity import ValidityCheck, ValidityVerdict

    with pytest.raises(ValueError):
        Validity("inconclusive")
    with pytest.raises(ValueError):
        ValidityVerdict(HASH_A, HASH_B, "inconclusive", ("uncertain",), ())
    with pytest.raises(ValueError):
        ValidityCheck("scope", "inconclusive", ())


def test_an_empty_candidate_may_be_cited_as_a_whole_and_nothing_else_may(tmp_path):
    empty = tmp_path / "empty-candidate"
    empty.mkdir()
    value = payload()
    for check in value["checks"]:
        check["evidence_paths"] = ["/review/candidate"]
    path = tmp_path / "verdict.json"
    write_json(path, value)
    verdict = read_verdict(path, HASH_A, HASH_B, CRITERIA, {"/review/candidate": empty})
    assert verdict.verdict == "valid"
    (empty / "select_subset.py").write_text("def select(): ...\n")  # once it holds a file, cite the file
    with pytest.raises(VerdictError, match="unsafe_evidence_file"):
        read_verdict(path, HASH_A, HASH_B, CRITERIA, {"/review/candidate": empty})
    (empty / "select_subset.py").unlink()
    (empty / "nested").mkdir()
    value["checks"][0]["evidence_paths"] = ["/review/candidate/nested"]  # only the root, never a subdirectory
    write_json(path, value)
    with pytest.raises(VerdictError, match="unsafe_evidence_file"):
        read_verdict(path, HASH_A, HASH_B, CRITERIA, {"/review/candidate": empty})
