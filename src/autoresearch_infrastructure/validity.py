from __future__ import annotations

import json
import os
import stat
from dataclasses import asdict, dataclass
from pathlib import Path

MAX_VERDICT_BYTES = 65_536
MAX_REASONS = 32
MAX_CHECKS = 32
MAX_TEXT = 4_096


class VerdictError(ValueError):
    """A verdict file that cannot be accepted. ``code`` names the rule it broke; ``detail`` says where."""

    def __init__(self, code: str, detail: str | None = None):
        super().__init__(f"{code}: {detail}" if detail else code)
        self.code = code
        self.detail = detail

    @property
    def correctable(self) -> bool:
        """Whether the error is in the verdict's form, which the reviewer can fix by rewriting it.

        A form error says nothing about the candidate. Hash mismatches and unsafe
        files or paths are not correctable: they are refused outright.
        """
        return self.code in CORRECTABLE_VERDICT_ERRORS


# Errors in what the reviewer wrote, as opposed to a verdict bound to another candidate or an unsafe file.
CORRECTABLE_VERDICT_ERRORS = frozenset({
    "missing_file", "invalid_utf8", "malformed_json", "invalid_schema", "invalid_verdict", "invalid_reasons",
    "invalid_checks", "invalid_check_schema", "invalid_criterion", "duplicate_criterion", "unknown_criterion",
    "missing_criterion", "invalid_check_status", "invalid_check_reason", "invalid_evidence_paths",
    "invalid_evidence_path", "passing_check_without_evidence", "missing_evidence",
    "evidence_path_outside_review", "valid_with_nonpassing_check", "invalid_without_failed_check",
    "invalid_candidate_hash", "invalid_contract_hash",
})


@dataclass(frozen=True)
class ValidityCheck:
    criterion: str
    status: str
    evidence_paths: tuple[str, ...]
    reason: str | None = None  # the reviewer's reason for this criterion's status, when it gave one

    def __post_init__(self):
        if self.status not in {"pass", "fail"}:
            raise ValueError("Validity checks must pass or fail")


@dataclass(frozen=True)
class ValidityVerdict:
    candidate_hash: str
    contract_hash: str
    verdict: str
    reasons: tuple[str, ...]
    checks: tuple[ValidityCheck, ...]

    def __post_init__(self):
        if self.verdict not in {"valid", "invalid"}:
            raise ValueError("Validity verdict must be valid or invalid")

    def to_dict(self) -> dict:
        return {
            **asdict(self),
            "reasons": list(self.reasons),
            "checks": [
                {
                    key: value
                    for key, value in {**asdict(check), "evidence_paths": list(check.evidence_paths)}.items()
                    if key != "reason" or value is not None
                }
                for check in self.checks
            ],
        }


def _reject_constant(_: str):
    raise ValueError("non-finite number")


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate key")
        result[key] = value
    return result


def _regular_parent_chain(path: Path) -> None:
    current = path.parent
    while current != current.parent:
        try:
            info = current.lstat()
        except FileNotFoundError as exc:
            raise VerdictError("missing_parent") from exc
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise VerdictError("unsafe_parent")
        current = current.parent


def _read_bounded_regular_file(path: Path) -> bytes:
    _regular_parent_chain(path)
    try:
        before = path.lstat()
    except FileNotFoundError as exc:
        raise VerdictError("missing_file") from exc
    if not stat.S_ISREG(before.st_mode) or stat.S_ISLNK(before.st_mode):
        raise VerdictError("not_regular_file")
    if before.st_nlink != 1:
        raise VerdictError("multiple_links")
    if before.st_size > MAX_VERDICT_BYTES:
        raise VerdictError("oversized_file")
    flags = os.O_RDONLY | getattr(os, "O_CLOEXEC", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(path, flags)
    except OSError as exc:
        raise VerdictError("unsafe_open") from exc
    try:
        opened = os.fstat(descriptor)
        if not stat.S_ISREG(opened.st_mode) or opened.st_nlink != 1:
            raise VerdictError("not_regular_file")
        if (opened.st_dev, opened.st_ino) != (before.st_dev, before.st_ino):
            raise VerdictError("file_changed")
        data = bytearray()
        while len(data) <= MAX_VERDICT_BYTES:
            chunk = os.read(descriptor, min(8192, MAX_VERDICT_BYTES + 1 - len(data)))
            if not chunk:
                break
            data.extend(chunk)
        after = os.fstat(descriptor)
        stable = (opened.st_dev, opened.st_ino, opened.st_size, opened.st_mtime_ns) == (
            after.st_dev,
            after.st_ino,
            after.st_size,
            after.st_mtime_ns,
        )
        if not stable:
            raise VerdictError("file_changed")
        if len(data) > MAX_VERDICT_BYTES:
            raise VerdictError("oversized_file")
        return bytes(data)
    finally:
        os.close(descriptor)


def _text(value, *, field: str, maximum: int = MAX_TEXT) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > maximum or "\x00" in value:
        raise VerdictError(f"invalid_{field}")
    return value


def _validate_evidence_path(value: str, evidence_roots: dict[str, Path]) -> None:
    mounted = Path(value)
    if not mounted.is_absolute() or ".." in mounted.parts:
        raise VerdictError("invalid_evidence_path")
    matches = [
        (prefix, root)
        for prefix, root in evidence_roots.items()
        if value == prefix or value.startswith(prefix.rstrip("/") + "/")
    ]
    if not matches:
        raise VerdictError("evidence_path_outside_review")
    prefix, root = max(matches, key=lambda item: len(item[0]))
    suffix = value[len(prefix) :].lstrip("/")
    target = root / suffix if suffix else root
    boundary = root if root.is_dir() else root.parent
    current = target.parent if suffix else boundary  # the root itself has no parent inside the review
    while True:
        try:
            info = current.lstat()
        except FileNotFoundError as exc:
            raise VerdictError("missing_evidence") from exc
        if not stat.S_ISDIR(info.st_mode) or stat.S_ISLNK(info.st_mode):
            raise VerdictError("unsafe_evidence_parent")
        if current == boundary:
            break
        if boundary not in current.parents:
            raise VerdictError("evidence_path_outside_review")
        current = current.parent
    try:
        info = target.lstat()
    except FileNotFoundError as exc:
        raise VerdictError("missing_evidence") from exc
    # An empty evidence root may be cited as a whole: it shows that nothing a rule forbids is there.
    # Anything else must be a regular file, so a pass always rests on content that can be read.
    if not suffix and stat.S_ISDIR(info.st_mode) and not stat.S_ISLNK(info.st_mode) and not any(target.iterdir()):
        return
    if not stat.S_ISREG(info.st_mode) or stat.S_ISLNK(info.st_mode) or info.st_nlink != 1:
        raise VerdictError("unsafe_evidence_file")
    if info.st_size > 1_000_000:
        raise VerdictError("oversized_evidence")


def read_verdict(
    path: Path,
    expected_candidate_hash: str,
    expected_contract_hash: str,
    required_criteria: tuple[str, ...],
    evidence_roots: dict[str, Path],
    check_policy=None,
) -> ValidityVerdict:
    raw = _read_bounded_regular_file(path)
    try:
        text = raw.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise VerdictError("invalid_utf8") from exc
    try:
        value = json.loads(
            text,
            object_pairs_hook=_unique_object,
            parse_constant=_reject_constant,
        )
    except json.JSONDecodeError as exc:
        raise VerdictError("malformed_json", f"{exc.msg} at line {exc.lineno} column {exc.colno}") from exc
    except ValueError as exc:
        raise VerdictError("malformed_json", str(exc)) from exc
    if not isinstance(value, dict) or set(value) != {
        "candidate_hash",
        "contract_hash",
        "verdict",
        "reasons",
        "checks",
    }:
        found = sorted(value) if isinstance(value, dict) else type(value).__name__
        raise VerdictError("invalid_schema", f"expected exactly candidate_hash, contract_hash, verdict, "
                                             f"reasons and checks; found {found}")
    candidate_hash = _text(value["candidate_hash"], field="candidate_hash", maximum=128)
    contract_hash = _text(value["contract_hash"], field="contract_hash", maximum=128)
    if candidate_hash != expected_candidate_hash:
        raise VerdictError("candidate_hash_mismatch")
    if contract_hash != expected_contract_hash:
        raise VerdictError("contract_hash_mismatch")
    verdict = value["verdict"]
    if verdict not in {"valid", "invalid"}:
        raise VerdictError("invalid_verdict")
    reasons = value["reasons"]
    if (
        not isinstance(reasons, list)
        or not 1 <= len(reasons) <= MAX_REASONS
        or any(
            not isinstance(reason, str) or not reason.strip() or len(reason) > MAX_TEXT for reason in reasons
        )
    ):
        raise VerdictError("invalid_reasons")
    checks = value["checks"]
    if not isinstance(checks, list) or not 1 <= len(checks) <= MAX_CHECKS:
        raise VerdictError("invalid_checks")
    parsed: list[ValidityCheck] = []
    seen = set()
    for check in checks:
        if not isinstance(check, dict) or not (
            {"criterion", "status", "evidence_paths"} <= set(check) <= {"criterion", "status", "evidence_paths", "reason"}
        ):
            found = sorted(check) if isinstance(check, dict) else type(check).__name__
            raise VerdictError("invalid_check_schema", f"a check has criterion, status, evidence_paths and "
                                                       f"reason; found {found}")
        criterion = _text(check["criterion"], field="criterion", maximum=128)
        if criterion in seen:
            raise VerdictError("duplicate_criterion", criterion)
        seen.add(criterion)
        if criterion not in required_criteria:
            raise VerdictError("unknown_criterion", criterion)
        status_value = check["status"]
        if status_value not in {"pass", "fail"}:
            raise VerdictError("invalid_check_status", f"{criterion}: status must be pass or fail")
        reason = _text(check["reason"], field="check_reason") if "reason" in check else None
        paths = check["evidence_paths"]
        if not isinstance(paths, list) or len(paths) > 32:
            raise VerdictError("invalid_evidence_paths", criterion)
        parsed_paths = tuple(_text(item, field="evidence_path") for item in paths)
        if status_value == "pass" and not parsed_paths:
            raise VerdictError("passing_check_without_evidence", criterion)
        for evidence_path in parsed_paths:
            try:
                _validate_evidence_path(evidence_path, evidence_roots)
            except VerdictError as exc:
                raise VerdictError(exc.code, f"{criterion} cites {evidence_path}") from exc
        if check_policy is not None:
            check_policy(criterion, status_value, parsed_paths, evidence_roots)
        parsed.append(ValidityCheck(criterion, status_value, parsed_paths, reason))
    if seen != set(required_criteria):
        raise VerdictError("missing_criterion", ", ".join(c for c in required_criteria if c not in seen))
    if verdict == "valid" and any(check.status != "pass" for check in parsed):
        raise VerdictError("valid_with_nonpassing_check")
    if verdict == "invalid" and not any(check.status == "fail" for check in parsed):
        raise VerdictError("invalid_without_failed_check")
    return ValidityVerdict(candidate_hash, contract_hash, verdict, tuple(reasons), tuple(parsed))


def invalid_record(candidate_hash: str, contract_hash: str, code: str) -> dict:
    return {
        "candidate_hash": candidate_hash,
        "contract_hash": contract_hash,
        "verdict": "invalid",
        "reasons": [f"Independent validity output could not be accepted ({code})."],
        "checks": [],
        "controller_error": code,
    }
