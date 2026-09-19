"""Limit Langfuse payload size and normalize values to JSON-friendly data."""

from collections.abc import Mapping, Sequence
from typing import Any

_MAX_LANGFUSE_TEXT_LENGTH = 8000


def limit_langfuse_payload(value: Any) -> Any:
    """Recursively bound text in values sent to Langfuse.

    参数:
        value: 待上报到 Langfuse 的任意 payload 值。

    返回:
        字符串按字符预算限制、可 JSON 序列化的值。

    异常:
        无。

    副作用:
        无。
    """

    if isinstance(value, str):
        return _limit_text(value)
    if isinstance(value, Mapping):
        return {str(key): limit_langfuse_payload(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray | str):
        return [limit_langfuse_payload(item) for item in value]
    if isinstance(value, int | float | bool) or value is None:
        return value
    return _limit_text(str(value))


def _limit_text(text: str) -> str:
    """Truncate one text value and annotate its original length."""

    if len(text) <= _MAX_LANGFUSE_TEXT_LENGTH:
        return text
    return f"{text[:_MAX_LANGFUSE_TEXT_LENGTH]}...[TRUNCATED:{len(text)}]"
