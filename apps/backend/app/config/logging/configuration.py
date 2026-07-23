"""配置后端文件日志。"""

import logging
from pathlib import Path
from queue import Queue

from app.config.logging.caller import CallerFilter
from app.config.logging.log_context import LogContextFilter
from app.config.logging.log_files_dir_service import current_log_file
from app.config.logging.process_bridge import (
    install_log_queue_bridge,
    install_queue_handler,
    stop_queue_listener,
)
from app.config.logging.save.jsonl import JsonlFormatter
from app.config.logging.save.sqlite_handler import SQLiteLogHandler
from app.storage.crud.log_crud import LogStore


def configure_logging(
    log_dir: Path,
    log_database_file: Path | None = None,
    sqlite_logging_enabled: bool = False,
    queue_size: int = 1000,
    batch_size: int = 50,
    flush_interval_ms: int = 1000,
) -> logging.Logger:
    """配置并返回后端应用日志器。

    参数:
        log_dir: 后端日志应写入的目录。
        log_database_file: 可选独立日志 SQLite 数据库路径。
        sqlite_logging_enabled: 是否启用 SQLite 日志落库。
        queue_size: SQLite 日志队列容量。
        batch_size: SQLite 日志批量写入大小。
        flush_interval_ms: SQLite 日志最大刷盘间隔毫秒数。

    返回:
        名为 ``coding_agent.backend`` 且已挂载处理器的日志器。

    异常:
        OSError: 当操作系统无法创建日志目录或日志文件时抛出。
        ValueError: 当 SQLite 日志参数非法时抛出。

    副作用:
        创建日志目录并配置文件日志与可选 SQLite 日志处理器。
    """

    log_dir.mkdir(parents=True, exist_ok=True)

    # 日志文件路径 logs-YYYY-MM-DD.log
    log_file = current_log_file(log_dir)
    # 返回一个以 name 为标识的 logger 对象。相同名字多次调用拿到的是同一个 logger 实例（单例）
    logger = logging.getLogger("coding_agent.backend")
    # 定一个最低记录级别（threshold）。logging.DEBUG 是 Python logging 级别体系里最低的一档
    logger.setLevel(logging.DEBUG)
    logger.propagate = False

    # 幂等性保护：保证无论 configure_logging 被调几次，logger 身上的 handler 都是"干净的一份"
    # configure_logging 这个函数可能被调用多次（比如开发期 uvicorn 开了 reload=True，代码改动后进程重载会重新执行；或者测试里反复 setup/teardown）
    existing_handlers = [
        handler
        for handler in logger.handlers
        if isinstance(handler, (logging.FileHandler, SQLiteLogHandler))
    ]
    for handler in existing_handlers:
        logger.removeHandler(handler)
        handler.close()

    context_filter = LogContextFilter()
    caller_filter = CallerFilter()
    file_handler = logging.FileHandler(log_file, encoding="utf-8")
    file_handler.setLevel(logging.DEBUG)
    file_handler.setFormatter(JsonlFormatter())
    file_handler.addFilter(context_filter)
    file_handler.addFilter(caller_filter)
    logger.addHandler(file_handler)

    if sqlite_logging_enabled and log_database_file is not None:
        _add_sqlite_handler_or_warn(
            logger=logger,
            file_handler=file_handler,
            context_filter=context_filter,
            caller_filter=caller_filter,
            log_database_file=log_database_file,
            queue_size=queue_size,
            batch_size=batch_size,
            flush_interval_ms=flush_interval_ms,
        )

    install_log_queue_bridge(logger)
    return logger


def install_logging_for_current_process(
    *,
    log_dir: Path | None = None,
    log_database_file: Path | None = None,
    sqlite_logging_enabled: bool = False,
    queue_size: int = 1000,
    batch_size: int = 50,
    flush_interval_ms: int = 1000,
    log_queue: "Queue | None" = None,
) -> logging.Logger:
    """按当前进程角色统一安装日志管线，调用方无需感知父子进程差异。

    参数:
        log_dir: 父进程日志目录；子进程路径忽略。
        log_database_file: 可选 SQLite 日志库路径；子进程路径忽略。
        sqlite_logging_enabled: 是否启用 SQLite 日志；子进程路径忽略。
        queue_size: SQLite 队列容量；子进程路径忽略。
        batch_size: SQLite 批量写入大小；子进程路径忽略。
        flush_interval_ms: SQLite 刷盘间隔毫秒；子进程路径忽略。
        log_queue: 父进程创建的跨进程日志队列。传入时表示当前为子进程，
            仅把日志导向队列；为 None 时表示当前为父进程，直接落盘并启动桥接。

    返回:
        名为 ``coding_agent.backend`` 的日志器。

    异常:
        ValueError: 当父进程路径缺少 log_dir 时抛出。
        OSError: 当父进程无法创建日志目录或文件时抛出。

    副作用:
        子进程：给 backend logger 挂 SubprocessQueueHandler 并关闭向上传播；
        父进程：配置文件/SQLite handler 并启动队列监听器。
    """
    if log_queue is not None:
        install_queue_handler(log_queue)
        return logging.getLogger("coding_agent.backend")
    if log_dir is None:
        raise ValueError("log_dir is required to configure parent process logging")
    return configure_logging(
        log_dir=log_dir,
        log_database_file=log_database_file,
        sqlite_logging_enabled=sqlite_logging_enabled,
        queue_size=queue_size,
        batch_size=batch_size,
        flush_interval_ms=flush_interval_ms,
    )


def _add_sqlite_handler_or_warn(
    logger: logging.Logger,
    file_handler: logging.Handler,
    context_filter: LogContextFilter,
    caller_filter: CallerFilter,
    log_database_file: Path,
    queue_size: int,
    batch_size: int,
    flush_interval_ms: int,
) -> None:
    """尝试启用 SQLite 日志副本，失败时降级为仅文件日志。

    参数:
        logger: 后端主日志器。
        file_handler: 已启用的 JSONL 文件 handler。
        context_filter: 需要挂载到 SQLite handler 的上下文过滤器。
        log_database_file: SQLite 日志数据库路径。
        queue_size: SQLite 日志队列容量。
        batch_size: SQLite 日志批量写入大小。
        flush_interval_ms: SQLite 日志最大刷盘间隔毫秒数。

    返回:
        无。

    异常:
        无。SQLite 初始化失败会写入 warning 并保留文件日志。

    副作用:
        成功时向 logger 增加 SQLiteLogHandler；失败时写一条文件日志。
    """

    try:
        sqlite_handler = SQLiteLogHandler(
            store=LogStore(),
            queue_size=queue_size,
            batch_size=batch_size,
            flush_interval_seconds=flush_interval_ms / 1000,
            fallback_handler=file_handler,
        )
    except Exception as exc:
        logger.warning(
            "sqlite_log_handler_unavailable",
            extra={
                "msg": "SQLite 日志 handler 初始化失败，日志仅落文件",
                "data": {"error": str(exc)},
            },
        )
        return
    sqlite_handler.setLevel(logging.DEBUG)
    sqlite_handler.addFilter(context_filter)
    sqlite_handler.addFilter(caller_filter)
    logger.addHandler(sqlite_handler)


def shutdown_logging(logger_name: str = "coding_agent.backend") -> None:
    """关闭后端日志 handler 并尽量 flush。

    参数:
        logger_name: 需要关闭的 logger 名称。

    返回:
        无。

    异常:
        无。关闭失败由 logging handler 自身降级处理。

    副作用:
        flush 并关闭 logger 上的文件与 SQLite handler。
    """

    logger = logging.getLogger(logger_name)
    for handler in list(logger.handlers):
        logger.removeHandler(handler)
        handler.close()
    stop_queue_listener()
