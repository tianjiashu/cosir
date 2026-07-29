"""Trace 与日志 payload 脱敏。"""

import re
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
    if isinstance(value, Sequence) and not isinstance(value, bytes | bytearray | str):
        return [redact_value(item, max_text_length=max_text_length) for item in value]
    if isinstance(value, int | float | bool) or value is None:
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


# ---------------------------------------------------------------------------
# 自由文本凭据脱敏（与 redact_value 分工互补）
# ---------------------------------------------------------------------------
# redact_value 按 dict 的 key 名脱敏结构化字段；本函数处理任意自由文本（命令输出、
# 命令 preview），正则匹配 token/JWT/密码/.env 等明文凭据。二者不重复造轮子。

_ASSIGN_RE = re.compile(
    r"(?i)\b([\w-]*(?:api[_-]?key|apikey|token|secret|password|passwd|pwd|"
    r"access[_-]?token|auth[_-]?token|client[_-]?secret|private[_-]?key)[\w-]*)"
    r"\s*[:=]\s*['\"]?([^\s'\"]{3,})['\"]?"
)

_TOKEN_RES: tuple[re.Pattern[str], ...] = (
    re.compile(r"\b(eyJ[A-Za-z0-9_-]{8,}\.eyJ[A-Za-z0-9_-]{8,}\.[A-Za-z0-9_-]+)\b"),
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{36}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bAKIA[0-9A-Z]{16}\b"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}\b"),
    re.compile(r"\bya29\.[A-Za-z0-9_-]{30,}\b"),
)
