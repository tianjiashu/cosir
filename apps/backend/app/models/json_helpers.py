"""Strict JSON object serialization helpers for persistence records."""

from __future__ import annotations

import json
from typing import Any


def serialize_json_object(value: dict[str, Any], field_name: str) -> str:
    """Serialize a typed JSON object without silently dropping invalid values."""

    if not isinstance(value, dict):
        raise TypeError(f"{field_name} must be a JSON object")
    try:
        return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False)
    except (TypeError, ValueError) as exc:
        raise TypeError(f"{field_name} must contain only JSON values") from exc


def deserialize_json_object(raw: str, field_name: str) -> dict[str, Any]:
    """Deserialize and validate a persisted JSON object."""

    if not isinstance(raw, str):
        raise TypeError(f"{field_name} must be JSON text")
    value = json.loads(raw, parse_constant=_reject_non_json_constant)
    if not isinstance(value, dict):
        raise ValueError(f"{field_name} must contain a JSON object")
    if not all(isinstance(key, str) for key in value):
        raise ValueError(f"{field_name} must contain string keys")
    return value


def _reject_non_json_constant(value: str) -> None:
    """Reject Python's non-standard NaN/Infinity JSON extensions."""

    raise ValueError(f"invalid JSON constant: {value}")
