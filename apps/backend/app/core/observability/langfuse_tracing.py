"""Langfuse Run Trace 的公开门面。

具体 client 生命周期、配置热更新和根/工具 Trace 原子装配统一由
``langfuse_runtime.LangfuseRuntimeManager`` 负责。本模块只保留稳定的调用门面，避免
workflow/runtime 直接依赖 Langfuse SDK。
"""

from app.core.observability.langfuse_runtime import (
    ConversationRunTraceResult,
    TraceMetadata,
    get_langfuse_runtime_manager,
    tracing_enabled,
)


def conversation_run_trace(metadata: TraceMetadata):
    """返回一个统一管理的 Conversation Run 异步 Trace 上下文。

    参数:
        metadata: 本次 Run 的 task、run 和 agent 标识。

    返回:
        异步上下文管理器；上下文值包含 callbacks、Langfuse trace_id 与工具 recorder。

    异常:
        Langfuse 初始化和关闭异常由运行时管理器捕获并降级，不向 Agent 主流程传播。

    副作用:
        可能创建根 observation、CallbackHandler 和工具 Trace Recorder；退出时释放 Run
        lease，并在配置重载后的最后一个 lease 释放时关闭旧 client。
    """

    return get_langfuse_runtime_manager().conversation_run_trace(metadata)


def flush_langfuse() -> None:
    """关闭应用时 flush 并 shutdown 所有已经创建过的 Langfuse client。"""

    get_langfuse_runtime_manager().shutdown()


__all__ = [
    "ConversationRunTraceResult",
    "TraceMetadata",
    "conversation_run_trace",
    "flush_langfuse",
    "tracing_enabled",
]
