"""后端应用的日志配置。

本包是日志能力的统一门面：具体实现分散在子模块中，这里集中重导出
对外公开的名称，供 ``app`` 其余模块直接通过 ``app.config.logging`` 引用。
"""

import importlib

from app.config.logging.common import current_log_file
from app.config.logging.context.log_context_store import (
    current_log_context,
    merge_log_context,
    reset_log_context,
)
from app.config.logging.filter.caller_filter import CallerFilter, compute_caller
from app.config.logging.filter.log_context_filter import LogContextFilter
from app.config.logging.formatter.jsonl_formatter import JsonlFormatter
from app.config.logging.logger import install_msg_relocation
from app.models.mapped_log_record import MAX_LOG_TEXT_LENGTH

# 全局允许 coding_agent.backend 及子进程 logger 使用规范约定的 extra["msg"] 键。
# setLoggerClass 对既有 logger（如已创建的 coding_agent.backend）与子进程 logger 不可靠，
# 因此改为在 makeRecord 阶段重定位，导入本包即生效（幂等）。
install_msg_relocation()


def __getattr__(name: str) -> object:
    """按需加载日志配置中的重依赖对象。

    参数:
        name: 访问的导出名称。

    返回:
        对应的日志配置对象、handler 类型或进程桥函数。

    异常:
        AttributeError: 名称不是本包公开 API 时抛出。

    副作用:
        首次访问配置/handler 名称时导入对应子模块。
    """

    if name in {"configure_logging", "install_logging_for_current_process", "shutdown_logging"}:
        configuration = importlib.import_module("app.config.logging.configuration")
        return getattr(configuration, name)
    if name in {"get_log_queue", "stop_queue_listener"}:
        process_bridge = importlib.import_module("app.config.logging.process_bridge")
        return getattr(process_bridge, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    "MAX_LOG_TEXT_LENGTH",
    "CallerFilter",
    "JsonlFormatter",
    "LogContextFilter",
    "compute_caller",
    "configure_logging",
    "current_log_context",
    "current_log_file",
    "get_log_queue",
    "install_logging_for_current_process",
    "install_msg_relocation",
    "merge_log_context",
    "reset_log_context",
    "shutdown_logging",
    "stop_queue_listener",
]
