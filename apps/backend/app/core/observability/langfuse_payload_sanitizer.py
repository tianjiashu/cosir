"""Sanitize payloads before they leave the process through Langfuse."""

import json
from collections.abc import Mapping, Sequence
from typing import Any

from app.utils.trace_infra.redaction import redact_terminal_output, redact_value

_MAX_LANGFUSE_TEXT_LENGTH = 8000


def sanitize_langfuse_payload(value: Any) -> Any:
    """Return a redacted, bounded value safe for Langfuse payload fields.

    参数:
        value: 待上报到 Langfuse 的任意 payload 值。

    返回:
        已递归脱敏并控制字符串长度的 JSON 友好值。

    异常:
        无。无法结构化处理的值会转为字符串后脱敏。

    副作用:
        无。
    """

    if isinstance(value, str):
        return _sanitize_text(value)
    if isinstance(value, Mapping):
        return redact_value(
            {str(key): sanitize_langfuse_payload(item) for key, item in value.items()},
            max_text_length=_MAX_LANGFUSE_TEXT_LENGTH,
        )
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray | str):
        return [sanitize_langfuse_payload(item) for item in value]
    if isinstance(value, int | float | bool) or value is None:
        return value
    return _sanitize_text(str(value))


def _sanitize_text(text: str) -> str:
    """Sanitize free-form text and JSON-encoded structures.

    参数:
        text: 待脱敏的字符串，可能是普通文本，也可能是 JSON 字符串。

    返回:
        已脱敏字符串；若输入是 JSON object/list，则返回脱敏后的 JSON 字符串。

    异常:
        无。JSON 解析失败时按普通文本处理。

    副作用:
        无。
    """

    parsed = _try_parse_json(text)
    if parsed is not None:
        return json.dumps(sanitize_langfuse_payload(parsed), ensure_ascii=False)
    redacted = redact_terminal_output(text)
    if len(redacted) > _MAX_LANGFUSE_TEXT_LENGTH:
        return f"{redacted[:_MAX_LANGFUSE_TEXT_LENGTH]}...[TRUNCATED:{len(redacted)}]"
    return redacted


def _try_parse_json(text: str) -> Any | None:
    """Best-effort parse a string as JSON object or array.

    参数:
        text: 待检测字符串。

    返回:
        解析出的 dict/list，非 JSON object/list 或解析失败时返回 ``None``。

    异常:
        无。解析错误被内部消化，以保持 sanitizer 不影响主流程。

    副作用:
        无。
    """

    stripped = text.strip()
    if not stripped or stripped[0] not in "[{":
        return None
    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict | list):
        return parsed
    return None
