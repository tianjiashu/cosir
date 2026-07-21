"""Trace 与日志 payload 脱敏。"""

from collections.abc import Mapping, Sequence
from typing import Any

SENSITIVE_KEYWORDS = (
    "api_key",
    "apikey",
    "authorization",
    "cookie",
    "password",
    "secret",
    "token",
)


def redact_value(value: Any, max_text_length: int = 2000) -> Any:
    """递归脱敏并裁剪可序列化值。

    参数:
        value: 待写入 trace 或日志的任意值。
        max_text_length: 字符串最大保留长度。

    返回:
        已脱敏、可 JSON 序列化的值。

    异常:
        无。

    副作用:
        无。
    """

    if isinstance(value, Mapping):
        redacted = {}
        for key, item in value.items():
            key_text = str(key)
            if _is_sensitive_key(key_text):
                redacted[key_text] = "[REDACTED]"
            else:
                redacted[key_text] = redact_value(item, max_text_length=max_text_length)
        return redacted
    if isinstance(value, str):
        if len(value) > max_text_length:
            return f"{value[:max_text_length]}...[TRUNCATED:{len(value)}]"
        return value
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, str)):
        return [redact_value(item, max_text_length=max_text_length) for item in value]
    if isinstance(value, (int, float, bool)) or value is None:
        return value
    return str(value)


def _is_sensitive_key(key: str) -> bool:
    """判断字段名是否疑似敏感字段。

    参数:
        key: 字段名。

    返回:
        字段名包含敏感关键词时返回 True。

    异常:
        无。

    副作用:
        无。
    """

    lowered = key.lower()
    return any(keyword in lowered for keyword in SENSITIVE_KEYWORDS)
