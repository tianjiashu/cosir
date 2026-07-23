"""后端应用的日志配置。

本包是日志能力的统一门面：具体实现分散在子模块中，这里集中重导出
对外公开的名称，供 ``app`` 其余模块直接通过 ``app.config.logging`` 引用。
"""

from app.config.logging.common import current_log_file
from app.config.logging.configuration import (
    configure_logging,
    install_logging_for_current_process,
    shutdown_logging,
)
from app.config.logging.context.log_context_store import (
    bind_log_context,
    current_log_context,
    merge_log_context,
    reset_log_context,
    set_log_context,
    trace_log_extra,
)
from app.config.logging.filter.caller_filter import CallerFilter, compute_caller
from app.config.logging.filter.log_context_filter import LogContextFilter
from app.config.logging.formatter.jsonl_formatter import JsonlFormatter
from app.config.logging.handler.sqlite_handler import (
    SQLiteLogHandler,
    entry_from_log_record,
)
from app.config.logging.logger import install_msg_relocation
from app.config.logging.process_bridge import (
    get_log_queue,
    stop_queue_listener,
)
from app.models.mapped_log_record import MAX_LOG_TEXT_LENGTH

# 全局允许 coding_agent.backend 及子进程 logger 使用规范约定的 extra["msg"] 键。
# setLoggerClass 对既有 logger（如已创建的 coding_agent.backend）与子进程 logger 不可靠，
# 因此改为在 makeRecord 阶段重定位，导入本包即生效（幂等）。
install_msg_relocation()

__all__ = [
    "MAX_LOG_TEXT_LENGTH",
    "CallerFilter",
    "JsonlFormatter",
    "LogContextFilter",
    "SQLiteLogHandler",
    "bind_log_context",
    "compute_caller",
    "configure_logging",
    "current_log_context",
    "current_log_file",
    "entry_from_log_record",
    "get_log_queue",
    "install_logging_for_current_process",
    "install_msg_relocation",
    "merge_log_context",
    "reset_log_context",
    "set_log_context",
    "shutdown_logging",
    "stop_queue_listener",
    "trace_log_extra",
]
