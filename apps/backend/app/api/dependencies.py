"""FastAPI 层的依赖构建器。"""

from app.config.settings import default_settings
from app.context.builder import TextContextBuilder
from app.logging.configuration import configure_logging
from app.models.factory import build_model_adapter
from app.runtime.runner import AgentRuntime
from app.storage.sqlite import SQLiteTaskStore
from app.tools.registry import ToolRegistry
from app.tools.safe_read import SafeReadTools
from app.tools.scheduler import ToolScheduler


def build_runtime() -> AgentRuntime:
    """构建默认的运行时依赖图。

    参数:
        无。

    返回:
        接入本地配置、文件日志、SQLite 存储、文本上下文构建、安全读取工具，
        以及所配置的流式模型适配器的 AgentRuntime。

    异常:
        OSError: 当日志器或 SQLite 存储无法创建本地文件时抛出。

    副作用:
        配置文件日志并初始化 SQLite schema。
    """

    settings = default_settings()
    logger = configure_logging(settings.log_file)
    safe_tools = SafeReadTools(settings.project_root)
    registry = ToolRegistry(safe_tools.definitions())
    return AgentRuntime(
        settings=settings,
        task_store=SQLiteTaskStore(settings.database_file),
        context_builder=TextContextBuilder(),
        model_adapter=build_model_adapter(settings),
        tool_scheduler=ToolScheduler(
            registry=registry,
            allowed_permissions=("safe_read",),
            logger=logger,
        ),
        logger=logger,
    )
