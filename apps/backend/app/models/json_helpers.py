"""Strict JSON helpers and the persisted conversation JSON contracts."""

from __future__ import annotations

import json
import math
from typing import Any, Literal, NotRequired, TypeAlias, TypedDict, cast

JSONValue: TypeAlias = None | bool | int | float | str | list["JSONValue"] | dict[str, "JSONValue"]


class TransportTextPart(TypedDict):
    """Text or reasoning part stored in context transport metadata."""

    type: Literal["text", "reasoning"]
    text: str
    status: NotRequired[Literal["running", "completed"]]


class TransportToolCallPart(TypedDict):
    """Tool-call part stored in context transport metadata."""

    type: Literal["tool-call"]
    toolCallId: str
    toolName: str
    status: Literal["pending", "running", "completed", "failed", "cancelled"]
    args: NotRequired[dict[str, JSONValue]]
    error: NotRequired[str | None]
    errorCode: NotRequired[str | None]
    presentation: NotRequired[dict[str, JSONValue]]
    display_data: NotRequired[dict[str, JSONValue] | None]
    isError: NotRequired[bool]
    approvalRequestId: NotRequired[None]


TransportPart: TypeAlias = TransportTextPart | TransportToolCallPart


class TransportMetadata(TypedDict):
    """Persisted metadata contract for one context message."""

    schema_version: int
    parts: list[TransportPart]
    tool_result: dict[str, JSONValue] | None


class ConversationRunError(TypedDict):
    """Controlled, user-safe error contract persisted for a failed run."""

    code: str
    message: str
    retryable: bool


_TRANSPORT_METADATA_KEYS = {"schema_version", "parts", "tool_result"}
_TEXT_PART_KEYS = {"type", "text", "status"}
_TOOL_PART_KEYS = {
    "type",
    "toolCallId",
    "toolName",
    "status",
    "args",
    "error",
    "errorCode",
    "presentation",
    "display_data",
    "isError",
    "approvalRequestId",
}
_TOOL_STATUSES = {"pending", "running", "completed", "failed", "cancelled"}


def serialize_json_object(value: dict[str, Any], field_name: str) -> str:
    """Serialize a JSON object after recursively validating its JSON value shape."""

    if not isinstance(value, dict):
        raise TypeError(f"{field_name} must be a JSON object")
    _validate_json_value(value, field_name)
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
    _validate_json_value(value, field_name)
    return cast(dict[str, Any], value)


def serialize_transport_metadata(value: TransportMetadata) -> str:
    """Serialize the exact context transport metadata contract."""

    return serialize_json_object(
        cast(dict[str, Any], _validate_transport_metadata(value)), "transport_metadata"
    )


def deserialize_transport_metadata(raw: str) -> TransportMetadata:
    """Deserialize and validate context transport metadata."""

    return _validate_transport_metadata(deserialize_json_object(raw, "transport_metadata"))


def serialize_run_error(value: ConversationRunError) -> str:
    """Serialize only the controlled run error fields."""

    return serialize_json_object(cast(dict[str, Any], _validate_run_error(value)), "error")


def deserialize_run_error(raw: str) -> ConversationRunError:
    """Deserialize and validate a controlled persisted run error."""

    return _validate_run_error(deserialize_json_object(raw, "error"))


def empty_transport_metadata() -> TransportMetadata:
    """Return the valid empty metadata baseline for a new context row."""

    return {"schema_version": 1, "parts": [], "tool_result": None}


def _validate_transport_metadata(value: object) -> TransportMetadata:
    if not isinstance(value, dict) or set(value) != _TRANSPORT_METADATA_KEYS:
        raise ValueError(
            "transport_metadata must contain exactly schema_version, parts, and tool_result"
        )
    schema_version = value["schema_version"]
    if (
        not isinstance(schema_version, int)
        or isinstance(schema_version, bool)
        or schema_version < 1
    ):
        raise ValueError("transport_metadata.schema_version must be a positive integer")
    parts = value["parts"]
    if not isinstance(parts, list):
        raise ValueError("transport_metadata.parts must be an array")
    for part in parts:
        _validate_transport_part(part)
    tool_result = value["tool_result"]
    if tool_result is not None:
        if not isinstance(tool_result, dict):
            raise ValueError("transport_metadata.tool_result must be an object or null")
        _validate_json_value(tool_result, "transport_metadata.tool_result")
    return cast(TransportMetadata, value)


def _validate_transport_part(part: object) -> None:
    if not isinstance(part, dict):
        raise ValueError("transport_metadata part must be an object")
    part_type = part.get("type")
    if part_type in {"text", "reasoning"}:
        if set(part) - _TEXT_PART_KEYS:
            raise ValueError("transport_metadata text part contains unknown fields")
        if not isinstance(part.get("text"), str):
            raise ValueError("transport_metadata text part text must be a string")
        if part.get("status") not in {None, "running", "completed"}:
            raise ValueError("transport_metadata text part status is invalid")
        return
    if part_type == "tool-call":
        if set(part) - _TOOL_PART_KEYS:
            raise ValueError("transport_metadata tool part contains unknown fields")
        if not isinstance(part.get("toolCallId"), str) or not isinstance(
            part.get("toolName"), str
        ):
            raise ValueError("transport_metadata tool identity is malformed")
        if part.get("status") not in _TOOL_STATUSES:
            raise ValueError("transport_metadata tool status is invalid")
        if part.get("args") is not None and not isinstance(part.get("args"), dict):
            raise ValueError("transport_metadata tool args must be an object")
        if part.get("error") is not None and not isinstance(part.get("error"), str):
            raise ValueError("transport_metadata tool error must be a string or null")
        if part.get("errorCode") is not None and not isinstance(part.get("errorCode"), str):
            raise ValueError("transport_metadata tool errorCode must be a string or null")
        if part.get("presentation") is not None and not isinstance(part.get("presentation"), dict):
            raise ValueError("transport_metadata tool presentation must be an object")
        if part.get("display_data") is not None and not isinstance(
            part.get("display_data"), dict
        ):
            raise ValueError("transport_metadata tool display_data must be an object or null")
        if part.get("isError") is not None and not isinstance(part.get("isError"), bool):
            raise ValueError("transport_metadata tool isError must be a boolean")
        if part.get("approvalRequestId") is not None:
            raise ValueError("transport_metadata approval requests are not implemented")
        _validate_json_value(part, "transport_metadata part")
        return
    raise ValueError("transport_metadata contains an unknown part type")


def _validate_run_error(value: object) -> ConversationRunError:
    if (
        not isinstance(value, dict)
        or set(value) != {"code", "message", "retryable"}
        or not isinstance(value.get("code"), str)
        or not isinstance(value.get("message"), str)
        or not isinstance(value.get("retryable"), bool)
    ):
        raise ValueError("error must contain only code, message, and retryable")
    return cast(ConversationRunError, value)


def _validate_json_value(value: object, field_name: str) -> None:
    if value is None or isinstance(value, bool | int | str):
        return
    if isinstance(value, float):
        if not math.isfinite(value):
            raise ValueError(f"{field_name} must not contain NaN or Infinity")
        return
    if isinstance(value, list):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{field_name}[{index}]")
        return
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise ValueError(f"{field_name} must contain string keys")
            _validate_json_value(item, f"{field_name}.{key}")
        return
    raise TypeError(f"{field_name} must contain only JSON values")


def _reject_non_json_constant(value: str) -> None:
    """Reject Python's non-standard NaN/Infinity JSON extensions."""

    raise ValueError(f"invalid JSON constant: {value}")
