"""后端应用启动入口。

通过 ``python -m app`` 从 ``apps/backend`` 目录启动本地开发服务器。
仅作为开发期启动约定；生产部署应使用外部进程管理器拉起 ``app.main:app``。

可通过环境变量覆盖运行参数：

- ``CODING_AGENT_HOST``：监听地址，默认 ``127.0.0.1``。
- ``CODING_AGENT_PORT``：监听端口，默认 ``8000``。
- ``CODING_AGENT_RELOAD``：是否开启热重载，默认 ``true``。
"""

import os
import traceback

import uvicorn

from app.bootstate import (
    BOOT_PHASE_BOOTING,
    BOOT_PHASE_FAILED,
    boot_state_file_from_env,
    write_bootstate,
)
from app.config.logging.configuration import install_logging_for_current_process
from app.config.settings import Settings
from app.service.depends import initialize_service_dependencies


def main() -> None:
    """解析配置、初始化存储与日志并启动 uvicorn 开发服务器。

    启动初期先写入 ``booting`` 启动状态，便于桌面端 supervisor 在进程崩溃瞬间
    拿到结构化失败原因；任何未捕获异常都会写入 ``failed`` 启动状态后再向上抛出，
    使进程以非零退出码结束。

    参数:
        无。

    返回:
        无。

    异常:
        RuntimeError: 当无法解析仓库根目录、日志目录不可写或存储初始化失败时。
        其余启动期异常：捕获后写入 ``failed`` 启动状态并原样向上抛出。

    副作用:
        初始化 SQLite 存储引擎（含日志库 schema）；向 ``logs/logs-YYYY-MM-DD.log``
        挂载文件日志处理器并向 SQLite 日志库挂载异步写入 handler；按需启动
        uvicorn 进程；按环境决定是否写入 ``storage/backend.bootstate.json``
        启动状态文件。
    """
    boot_state_file = boot_state_file_from_env()
    try:
        if boot_state_file is not None:
            write_bootstate(boot_state_file, BOOT_PHASE_BOOTING, step="start")

        # 存储引擎（含日志库 session 工厂）必须在日志配置之前初始化，
        # 否则 SQLiteLogHandler 内部构造 LogStore 时会因 log_session_factory()
        # 不可用而抛 RuntimeError，导致日志仅落文件、SQLite 库永远为空。
        Settings.load()
        initialize_service_dependencies()
        install_logging_for_current_process(
            log_dir=Settings.LOG_DIR,
            log_database_file=Settings.LOG_DATABASE_FILE,
            sqlite_logging_enabled=Settings.SQLITE_LOGGING_ENABLED,
            queue_size=Settings.LOG_QUEUE_SIZE,
            batch_size=Settings.LOG_BATCH_SIZE,
            flush_interval_ms=Settings.LOG_FLUSH_INTERVAL_MS,
        )

        host = os.environ.get("CODING_AGENT_HOST", "127.0.0.1")
        port = int(os.environ.get("CODING_AGENT_PORT", "8000"))
        reload_enabled = os.environ.get("CODING_AGENT_RELOAD", "true").lower() == "true"

        uvicorn.run(
            "app.main:app",
            host=host,
            port=port,
            reload=reload_enabled,
        )
    except Exception as exc:
        if boot_state_file is not None:
            write_bootstate(
                boot_state_file,
                BOOT_PHASE_FAILED,
                error_type=type(exc).__name__,
                error_message=str(exc),
                traceback_text=traceback.format_exc(),
            )
        # 保持非零退出码，使桌面端 supervisor 能通过进程退出感知失败。
        raise


if __name__ == "__main__":
    main()
