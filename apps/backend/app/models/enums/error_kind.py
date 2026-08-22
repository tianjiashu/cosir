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
    CANCELLED = "cancelled"
    MODEL_NOT_CONFIGURED = "model_not_configured"
    # 模型调用错误细分（设计文档阶段 4，由 ``model_error_mapper`` 从 litellm
    # 异常归一，供前端按 error_code 给出修复引导）。
    MODEL_AUTH_FAILED = "model_auth_failed"
    MODEL_RATE_LIMITED = "model_rate_limited"
    MODEL_CONTEXT_WINDOW_EXCEEDED = "model_context_window_exceeded"
    MODEL_INVALID_REQUEST = "model_invalid_request"
    MODEL_NETWORK_ERROR = "model_network_error"
    MODEL_NOT_FOUND = "model_not_found"
    MODEL_INSUFFICIENT_QUOTA = "model_insufficient_quota"
    MODEL_CONTENT_BLOCKED = "model_content_blocked"
    MODEL_UNKNOWN = "model_unknown"

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
