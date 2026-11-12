"""Explicit parsing helpers for untrusted model output; no authorization policy."""
from __future__ import annotations

import json
import math
from typing import Any


def _parse_json(text: str, *, max_bytes: int) -> Any:
    """Shared bounded decoder for model output and worker RPC."""
    if type(max_bytes) is not int or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    if not isinstance(text, str) or len(text.encode("utf-8")) > max_bytes:
        raise ValueError("JSON response exceeds its text limit")

    def unique(pairs):
        result = {}
        for key, value in pairs:
            if key in result:
                raise ValueError("Duplicate JSON field")
            result[key] = value
        return result

    def reject_constant(value):
        raise ValueError("Non-finite JSON number: " + value)

    def finite_float(value):
        number = float(value)
        if not math.isfinite(number):
            raise ValueError("Non-finite JSON number: " + value)
        return number

    try:
        return json.loads(text, object_pairs_hook=unique,
                          parse_constant=reject_constant, parse_float=finite_float)
    except RecursionError as exc:
        raise ValueError("JSON response is too deeply nested") from exc


def parse_json_object(
    text: str, *, required_keys: set[str], max_bytes: int = 20_000,
) -> dict[str, Any]:
    """Parse one bounded JSON object with exactly the required keys.

    Reject duplicate keys at every depth, non-finite numbers (including overflow),
    and extra prose. Callers still validate field types and authorization meaning.
    Malformed or excessively nested input raises ValueError.
    """
    value = _parse_json(text, max_bytes=max_bytes)
    if not isinstance(value, dict) or set(value) != required_keys:
        raise ValueError("JSON response has unexpected fields")
    return value
