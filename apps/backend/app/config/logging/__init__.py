"""后端应用的日志配置。

本包是日志能力的统一门面：具体实现分散在子模块中，这里集中重导出
对外公开的名称，供 ``app`` 其余模块直接通过 ``app.config.logging`` 引用。
"""

import logging

from app.config.logging.caller import CallerFilter, compute_caller
from app.config.logging.logger import install_msg_relocation

# 全局允许 coding_agent.backend 及子进程 logger 使用规范约定的 extra["msg"] 键。
# setLoggerClass 对既有 logger（如已创建的 coding_agent.backend）与子进程 logger 不可靠，
# 因此改为在 makeRecord 阶段重定位，导入本包即生效（幂等）。
install_msg_relocation()
from app.config.logging.configuration import (
    configure_logging,
    install_logging_for_current_process,
    shutdown_logging,
)
from app.config.logging.log_context import (
    LogContext,
    LogContextFilter,
    TraceLogContextFilter,
    bind_log_context,
    current_log_context,
    merge_log_context,
    reset_log_context,
    set_log_context,
    trace_log_extra,
)
from app.config.logging.log_files_dir_service import (
    current_log_file,
    dated_log_file,
    list_log_files,
)
from app.config.logging.process_bridge import (
    get_log_queue,
    stop_queue_listener,
)
from app.config.logging.record_mapper import MAX_LOG_TEXT_LENGTH
from app.config.logging.save.jsonl import (
    JsonlFormatter,
    query_log_file,
    query_log_files,
)
from app.config.logging.save.sqlite_handler import (
    SQLiteLogHandler,
    entry_from_log_record,
)
from app.config.logging.text_renderer_service import render_log_entries

__all__ = [
    "configure_logging",
    "install_logging_for_current_process",
    "shutdown_logging",
    "CallerFilter",
    "compute_caller",
    "install_msg_relocation",
    "LogContext",
    "LogContextFilter",
    "TraceLogContextFilter",
    "bind_log_context",
    "current_log_context",
    "merge_log_context",
    "reset_log_context",
    "set_log_context",
    "trace_log_extra",
    "current_log_file",
    "dated_log_file",
    "list_log_files",
    "get_log_queue",
    "stop_queue_listener",
    "MAX_LOG_TEXT_LENGTH",
    "JsonlFormatter",
    "query_log_file",
    "query_log_files",
    "SQLiteLogHandler",
    "entry_from_log_record",
    "render_log_entries",
]
