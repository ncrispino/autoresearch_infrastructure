"""Validate native hook correlation and supported observation transformations."""
from __future__ import annotations

from .adapters import get_adapter


def native_request(value, harness):
    if (
        not isinstance(value, dict) or set(value) - {"stage", "id", "name", "args", "result", "error"}
        or value.get("stage") not in {"pre", "post", "failure"}
        or not isinstance(value.get("id"), str) or not 0 < len(value["id"]) <= 200
        or value.get("name") not in get_adapter(harness).native_tools
        or not isinstance(value.get("args"), dict)
        or (value.get("error") is not None and not isinstance(value["error"], str))
        or (value["stage"] == "pre" and set(value) != {"stage", "id", "name", "args"})
        or (value["stage"] == "post" and ("result" not in value or value.get("error") is not None))
        or (value["stage"] == "failure" and not isinstance(value.get("error"), str))
    ):
        raise ValueError("Invalid native hook event")
    return value


def validate_native_transform(harness, original, transformed):
    """Delegate verified native output semantics to the selected adapter."""
    get_adapter(harness).validate_native_transform(original, transformed)
