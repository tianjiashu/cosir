"""Trace 基础设施原语。

本包只承载与业务模型无关、与编排服务无关的 trace 基础设施函数（ID 生成/校验、
事件名规范化、payload 脱敏）。它不依赖 ``app.models``，也不依赖 ``app.service``，
以避免循环依赖；服务层与模型层按需从这里导入。
"""

from app.utils.trace_infra.ids import (
    is_span_id,
    is_trace_id,
    new_event_id,
    new_span_id,
    new_trace_id,
)
from app.utils.trace_infra.redaction import redact_value

__all__ = [
    "is_span_id",
    "is_trace_id",
    "new_event_id",
    "new_span_id",
    "new_trace_id",
    "redact_value",
]
