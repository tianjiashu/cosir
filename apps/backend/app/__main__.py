"""后端应用启动入口。

通过 ``python -m app`` 从 ``apps/backend`` 目录启动本地开发服务器。
仅作为开发期启动约定；生产部署应使用外部进程管理器拉起 ``app.main:app``。

可通过环境变量覆盖运行参数：

- ``CODING_AGENT_HOST``：监听地址，默认 ``127.0.0.1``。
- ``CODING_AGENT_PORT``：监听端口，默认 ``8000``。
- ``CODING_AGENT_RELOAD``：是否开启热重载，默认 ``true``。
"""

import os

from app.config.settings import default_settings
from app.logging.configuration import configure_logging


def main() -> None:
    """解析配置、挂载文件日志并启动 uvicorn 开发服务器。

    参数:
        无。

    返回:
        无。

    异常:
        RuntimeError: 当无法解析仓库根目录或日志目录不可写时。

    副作用:
        向 ``logs/app.log`` 挂载文件日志处理器，并按需启动 uvicorn 进程。
    """
    settings = default_settings()
    configure_logging(settings.log_file)

    import uvicorn

    host = os.environ.get("CODING_AGENT_HOST", "127.0.0.1")
    port = int(os.environ.get("CODING_AGENT_PORT", "8000"))
    reload_enabled = os.environ.get("CODING_AGENT_RELOAD", "true").lower() == "true"

    uvicorn.run(
        "app.main:app",
        host=host,
        port=port,
        reload=reload_enabled,
    )


if __name__ == "__main__":
    main()
