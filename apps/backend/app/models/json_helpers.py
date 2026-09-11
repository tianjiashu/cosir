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
    args: dict[str, JSONValue]
    error: NotRequired[str | None]
    errorCode: NotRequired[str | None]
    presentation: dict[str, JSONValue]
    display_data: NotRequired[dict[str, JSONValue] | None]
    isError: bool
    approvalRequestId: NotRequired[None]


TransportPart: TypeAlias = TransportTextPart | TransportToolCallPart


class TransportToolResult(TypedDict):
    """Structured tool result stored alongside a context message."""

    status: Literal["success", "error", "cancelled"]
    display_data: dict[str, JSONValue] | None
    status_hint: str | None
    error: str | None
    errorCode: NotRequired[str | None]
    isError: NotRequired[bool]


class TransportMetadata(TypedDict):
    """Persisted metadata contract for one context message."""

    schema_version: int
    parts: list[TransportPart]
    tool_result: TransportToolResult | None


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
_TOOL_RESULT_KEYS = {"status", "display_data", "status_hint", "error"}
_OPTIONAL_TOOL_RESULT_KEYS = {"errorCode", "isError"}
_TOOL_RESULT_STATUSES = {"success", "error", "cancelled"}
_REQUIRED_TOOL_PART_KEYS = {
    "type",
    "toolCallId",
    "toolName",
    "status",
    "args",
    "presentation",
    "isError",
}
_MAX_RUN_ERROR_MESSAGE_LENGTH = 256


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


def serialize_transport_metadata(value: object) -> str:
    """Normalize wire metadata, then serialize the exact persisted contract.

    Canonical context writers must use this API (or call
    :func:`normalize_transport_metadata` explicitly) before persistence. It fills only
    omitted wire tool-call fields; explicit ``None`` and wrong types remain errors.
    """

    return serialize_json_object(
        cast(dict[str, Any], normalize_transport_metadata(value)), "transport_metadata"
    )


def deserialize_transport_metadata(raw: str) -> TransportMetadata:
    """Deserialize and validate context transport metadata."""

    return _validate_transport_metadata(deserialize_json_object(raw, "transport_metadata"))


def validate_transport_metadata(value: object) -> TransportMetadata:
    """Validate already-materialized metadata without applying wire defaults."""

    return _validate_transport_metadata(value)


def serialize_run_error(value: ConversationRunError) -> str:
    """Serialize only the controlled run error fields."""

    return serialize_json_object(cast(dict[str, Any], _validate_run_error(value)), "error")


def deserialize_run_error(raw: str) -> ConversationRunError:
    """Deserialize and validate a controlled persisted run error."""

    return _validate_run_error(deserialize_json_object(raw, "error"))


def empty_transport_metadata() -> TransportMetadata:
    """Return the valid empty metadata baseline for a new context row."""

    return {"schema_version": 1, "parts": [], "tool_result": None}


def normalize_transport_metadata(value: object) -> TransportMetadata:
    """Convert optional wire tool-call fields to explicit persisted defaults.

    ``args``, ``presentation`` and ``isError`` may be omitted by the Assistant
    Transport wire event. Persistence requires them, so omitted values become ``{}``,
    ``{}`` and ``False`` respectively. The input is copied; explicit nulls, wrong
    types, unknown fields and all other structural errors are rejected by the strict
    validator instead of being silently repaired.
    """

    if not isinstance(value, dict):
        raise TypeError("transport_metadata must be a JSON object")
    normalized = dict(value)
    parts = value.get("parts")
    if isinstance(parts, list):
        normalized_parts: list[object] = []
        for part in parts:
            if isinstance(part, dict) and part.get("type") == "tool-call":
                normalized_part = dict(part)
                normalized_part.setdefault("args", {})
                normalized_part.setdefault("presentation", {})
                normalized_part.setdefault("isError", False)
                normalized_parts.append(normalized_part)
            else:
                normalized_parts.append(part)
        normalized["parts"] = normalized_parts
    return _validate_transport_metadata(normalized)


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
        _validate_tool_result(tool_result)
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
        if "status" in part and part["status"] not in {"running", "completed"}:
            raise ValueError("transport_metadata text part status is invalid")
        return
    if part_type == "tool-call":
        if set(part) - _TOOL_PART_KEYS:
            raise ValueError("transport_metadata tool part contains unknown fields")
        if set(part) < _REQUIRED_TOOL_PART_KEYS:
            raise ValueError("transport_metadata tool part is missing required fields")
        if not isinstance(part.get("toolCallId"), str) or not isinstance(
            part.get("toolName"), str
        ):
            raise ValueError("transport_metadata tool identity is malformed")
        if part.get("status") not in _TOOL_STATUSES:
            raise ValueError("transport_metadata tool status is invalid")
        if not isinstance(part["args"], dict):
            raise ValueError("transport_metadata tool args must be an object")
        if part.get("error") is not None and not isinstance(part.get("error"), str):
            raise ValueError("transport_metadata tool error must be a string or null")
        if part.get("errorCode") is not None and not isinstance(part.get("errorCode"), str):
            raise ValueError("transport_metadata tool errorCode must be a string or null")
        if not isinstance(part["presentation"], dict):
            raise ValueError("transport_metadata tool presentation must be an object")
        if part.get("display_data") is not None and not isinstance(
            part.get("display_data"), dict
        ):
            raise ValueError("transport_metadata tool display_data must be an object or null")
        if not isinstance(part["isError"], bool):
            raise ValueError("transport_metadata tool isError must be a boolean")
        if part.get("approvalRequestId") is not None:
            raise ValueError("transport_metadata approval requests are not implemented")
        _validate_json_value(part, "transport_metadata part")
        return
    raise ValueError("transport_metadata contains an unknown part type")


def _validate_tool_result(value: object) -> TransportToolResult:
    if not isinstance(value, dict) or not _TOOL_RESULT_KEYS.issubset(value) or set(value) - (
        _TOOL_RESULT_KEYS | _OPTIONAL_TOOL_RESULT_KEYS
    ):
        raise ValueError(
            "transport_metadata.tool_result must contain status, display_data, status_hint, "
            "and error; optional errorCode and isError are allowed"
        )
    if value["status"] not in _TOOL_RESULT_STATUSES:
        raise ValueError("transport_metadata.tool_result status is invalid")
    if value["display_data"] is not None and not isinstance(value["display_data"], dict):
        raise ValueError("transport_metadata.tool_result display_data must be an object or null")
    if value["status_hint"] is not None and not isinstance(value["status_hint"], str):
        raise ValueError("transport_metadata.tool_result status_hint must be a string or null")
    if value["error"] is not None and not isinstance(value["error"], str):
        raise ValueError("transport_metadata.tool_result error must be a string or null")
    if "errorCode" in value and value["errorCode"] is not None and not isinstance(
        value["errorCode"], str
    ):
        raise ValueError("transport_metadata.tool_result errorCode must be a string or null")
    if "isError" in value and not isinstance(value["isError"], bool):
        raise ValueError("transport_metadata.tool_result isError must be a boolean")
    _validate_json_value(value, "transport_metadata.tool_result")
    return cast(TransportToolResult, value)


def _validate_run_error(value: object) -> ConversationRunError:
    if (
        not isinstance(value, dict)
        or set(value) != {"code", "message", "retryable"}
        or not isinstance(value.get("code"), str)
        or not _is_short_error_message(value.get("message"))
        or not isinstance(value.get("retryable"), bool)
    ):
        raise ValueError("error must contain only code, message, and retryable")
    return cast(ConversationRunError, value)


def _is_short_error_message(value: object) -> bool:
    return (
        isinstance(value, str)
        and bool(value)
        and len(value) <= _MAX_RUN_ERROR_MESSAGE_LENGTH
        and value == value.strip()
        and value.isprintable()
    )


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
