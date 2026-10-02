"""Langfuse 运行时生命周期管理（公开门面）。

本模块是 Langfuse client、根 Trace、CallbackHandler 和工具 Trace Recorder 的进程级装配入口，
并持有唯一的 ``LangfuseRuntimeManager`` 单例。具体实现拆分在 ``langfuse_config``、
``langfuse_sdk`` 与 ``langfuse_runtime_manager`` 中，本模块只暴露稳定的访问函数与值对象，
保持向后兼容的导入路径。

它不负责业务 Run 状态，也不负责 Transport 事件；只负责把可观测性作为可降级旁路安全地接入
运行期，并保证同一个 Agent Run 的观测对象使用同一份配置和同一个 client。
"""

from __future__ import annotations

from app.core.observability.langfuse_config import LangfuseConfig, TraceMetadata
from app.core.observability.langfuse_runtime_manager import (
    ConversationRunTraceResult,
    LangfuseRuntimeManager,
)

_LANGFUSE_RUNTIME = LangfuseRuntimeManager()


def get_langfuse_runtime_manager() -> LangfuseRuntimeManager:
    """返回进程级 Langfuse 生命周期管理器。"""

    return _LANGFUSE_RUNTIME


def reload_langfuse_from_settings() -> None:
    """按当前 ``Settings`` 重载进程级 Langfuse client。"""

    _LANGFUSE_RUNTIME.reload_from_settings()


def tracing_enabled() -> bool:
    """返回当前 Langfuse 配置是否可用。"""

    return _LANGFUSE_RUNTIME.tracing_enabled()


def flush_langfuse() -> None:
    """在应用关闭时 flush 并关闭所有已创建的 Langfuse client。"""

    _LANGFUSE_RUNTIME.shutdown()


__all__ = [
    "ConversationRunTraceResult",
    "LangfuseConfig",
    "LangfuseRuntimeManager",
    "TraceMetadata",
    "flush_langfuse",
    "get_langfuse_runtime_manager",
    "reload_langfuse_from_settings",
    "tracing_enabled",
]
