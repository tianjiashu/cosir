"""稳定的错误分类词表：日志、运行时事件与 Run 失败文案（``core/workflow/run_failure``）共用。"""

from enum import Enum


class ErrorKind(str, Enum):
    """成员值是稳定字符串，直接写进日志与 JSON payload；``model_*`` 也用作 Run 失败 code。"""

    PARSE_INVALID = "parse_invalid"
    SCHEMA_INVALID = "schema_invalid"
    UNKNOWN_TOOL = "unknown_tool"
    RUNTIME_FAILED = "runtime_failed"
    PERMISSION_DENIED = "permission_denied"
    CANCELLED = "cancelled"
    MODEL_NOT_CONFIGURED = "model_not_configured"
    # 模型调用错误细分：由 ``core/llm_provider/model_failure`` 从 provider 异常归一，前端按 code 给出修复引导。
    MODEL_AUTH_FAILED = "model_auth_failed"
    MODEL_RATE_LIMITED = "model_rate_limited"
    MODEL_TIMEOUT = "model_timeout"
    MODEL_CONTEXT_WINDOW_EXCEEDED = "model_context_window_exceeded"
    MODEL_INVALID_REQUEST = "model_invalid_request"
    MODEL_NETWORK_ERROR = "model_network_error"
    MODEL_SERVICE_ERROR = "model_service_error"
    MODEL_NOT_FOUND = "model_not_found"
    MODEL_INSUFFICIENT_QUOTA = "model_insufficient_quota"
    MODEL_CONTENT_BLOCKED = "model_content_blocked"
    MODEL_UNKNOWN = "model_unknown"

    def __str__(self) -> str:
        return self.value
