"""配置后端固定格式文件日志。"""

from __future__ import annotations

import logging
from pathlib import Path

from app.config.constant import Constant
from app.config.logging.common import current_log_file
from app.config.logging.filter.caller_filter import CallerFilter
from app.config.logging.filter.log_context_filter import LogContextFilter
from app.config.logging.formatter.jsonl_formatter import JsonlFormatter
from app.config.logging.handler.date_size_rotating import DateSizeRotatingFileHandler

_BACKEND_LOGGER_NAME = "coding_agent.backend"
_UVICORN_ERROR_LOGGER_NAME = "uvicorn.error"


def configure_logging(
    log_dir: Path,
    *,
    max_bytes: int = Constant.Logging.MAX_BYTES,
    backup_count: int = Constant.Logging.BACKUP_COUNT,
) -> logging.Logger:
    """安装后端固定 JSONL 文件日志；可重复调用，只替换本配置创建的 handler。

    参数:
        log_dir: 后端日志目录。
        max_bytes: 单个日期日志分片的最大字节数。
        backup_count: 同一日期保留的历史大小分片数量。

    返回:
        已配置的 ``coding_agent.backend`` logger。

    异常:
        OSError: 日志目录或文件无法创建时抛出。
        ValueError: 轮转参数非法时抛出。

    副作用:
        创建日志目录和文件 handler。
        日志只写本地 JSONL 文件，不写数据库。
    """

    log_dir.mkdir(parents=True, exist_ok=True)
    logger = logging.getLogger(_BACKEND_LOGGER_NAME)
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    # lifespan 和 __main__ 都可能安装日志；只清理由本配置创建的文件 handler，
    # 不碰测试或调用方临时挂载的其他 handler。
    for handler in list(logger.handlers):
        if getattr(handler, "_coding_agent_managed", False):
            logger.removeHandler(handler)
            handler.close()

    uvicorn_logger = logging.getLogger(_UVICORN_ERROR_LOGGER_NAME)
    for handler in list(uvicorn_logger.handlers):
        if getattr(handler, "_coding_agent_managed", False):
            uvicorn_logger.removeHandler(handler)

    context_filter = LogContextFilter()
    caller_filter = CallerFilter()
    file_handler = DateSizeRotatingFileHandler(
        current_log_file(log_dir),
        maxBytes=max_bytes,
        backupCount=backup_count,
        encoding="utf-8",
    )
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(JsonlFormatter())
    file_handler.addFilter(context_filter)
    file_handler.addFilter(caller_filter)
    file_handler._coding_agent_managed = True  # type: ignore[attr-defined]
    logger.addHandler(file_handler)

    # Uvicorn access logs are disabled because the HTTP middleware already writes a
    # sanitized request summary. Uvicorn lifecycle/error records still belong in the
    # same fixed-format backend file.
    uvicorn_logger.setLevel(logging.INFO)
    uvicorn_logger.propagate = False
    uvicorn_logger.addHandler(file_handler)

    return logger


def shutdown_logging(logger_name: str = _BACKEND_LOGGER_NAME) -> None:
    """关闭指定 logger 的文件 handler。"""

    logger = logging.getLogger(logger_name)
    managed_handlers = [
        handler for handler in logger.handlers if getattr(handler, "_coding_agent_managed", False)
    ]
    uvicorn_logger = logging.getLogger(_UVICORN_ERROR_LOGGER_NAME)
    for handler in managed_handlers:
        logger.removeHandler(handler)
        uvicorn_logger.removeHandler(handler)
        handler.close()
