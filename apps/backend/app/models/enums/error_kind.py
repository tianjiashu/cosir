"""Stable error classification values for logs and runtime events."""

from enum import Enum


class ErrorKind(str, Enum):
    """日志、运行时事件与回放分析共用的稳定错误分类。

    参数:
        无。

    返回:
        无。

    异常:
        无。

    副作用:
        无。
    """

    PARSE_INVALID = "parse_invalid"
    SCHEMA_INVALID = "schema_invalid"
    UNKNOWN_TOOL = "unknown_tool"
    RUNTIME_FAILED = "runtime_failed"
    PERMISSION_DENIED = "permission_denied"

    def __str__(self) -> str:
        """返回可写入日志与 JSON payload 的稳定字符串值。

        参数:
            无。

        返回:
            枚举成员的字符串值。

        异常:
            无。

        副作用:
            无。
        """

        return self.value
