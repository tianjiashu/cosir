"""后端应用启动入口。

开发环境通过 ``python -m app`` 从 ``apps/backend`` 目录启动；桌面发布版由 PyInstaller
冻结入口调用本模块，后端进程的创建、监控和清理仍由 Tauri Rust 宿主负责。

可通过环境变量覆盖运行参数：

- ``CODING_AGENT_PORT``：监听端口，默认 ``8000``。
- ``CODING_AGENT_RELOAD``：是否开启热重载，默认 ``true``。
- ``CODING_AGENT_LOG_LEVEL``：uvicorn 日志级别，默认 ``info``。
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
from app.utils import paths
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
        初始化主 SQLite 存储引擎；向 ``logs/backend-YYYY-MM-DD.log`` 挂载按日期和
        5MB 大小轮转的固定 JSONL 文件日志处理器；同步系统代理
        环境变量到当前进程（在 ``.env`` 未显式设置代理时启用）；按需启动
        uvicorn 进程；按环境决定是否写入 ``app_data_dir()/.cosir/runtime/backend.bootstate.json``
        启动状态文件。
    """
    boot_state_file = boot_state_file_from_env()
    try:
        if boot_state_file is not None:
            write_bootstate(boot_state_file, BOOT_PHASE_BOOTING, step="start")

        # 先按固定路径安装最小日志管线，覆盖 Settings.load 和依赖初始化失败窗口。
        install_logging_for_current_process(log_dir=paths.LOG_DIR)
        Settings.load()
        initialize_service_dependencies()
        install_logging_for_current_process(
            log_dir=paths.LOG_DIR,
            max_bytes=Settings.LOG_MAX_BYTES,
            backup_count=Settings.LOG_BACKUP_COUNT,
        )
        port = int(os.environ.get("CODING_AGENT_PORT", "8000"))
        reload_enabled = os.environ.get("CODING_AGENT_RELOAD", "true").lower() == "true"
        log_level = os.environ.get("CODING_AGENT_LOG_LEVEL", "info").lower()

        uvicorn.run(
            "app.app:app",
            host="127.0.0.1",
            port=port,
            reload=reload_enabled,
            log_level=log_level,
            log_config=None,
            access_log=False,
            ws_ping_interval=30,
            ws_ping_timeout=90,
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
