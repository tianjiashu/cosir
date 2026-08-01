"""Langfuse 可观测性接入聚合包（Langfuse 三方依赖在后端代码的唯一收口）。

本包只承载 LLM / 工具调用 trace 的 Langfuse 接入逻辑，不掺入业务规则。所有 langfuse
import 均为惰性加载，未启用 / 缺密钥 / 未安装时运行时行为与集成前一致。
"""

from app.core.observability.langfuse_tool_trace_recorder import LangfuseToolTraceRecorder
from app.core.observability.langfuse_tracing import (
    TraceMetadata,
    TurnTraceResult,
    flush_langfuse,
    tracing_enabled,
    turn_trace,
)

__all__ = [
    "LangfuseToolTraceRecorder",
    "TraceMetadata",
    "TurnTraceResult",
    "flush_langfuse",
    "tracing_enabled",
    "turn_trace",
]
