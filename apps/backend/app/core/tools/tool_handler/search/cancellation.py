"""搜索 handler 的同进程取消检查适配。"""

from collections.abc import Callable

from app.core.tools.schemas import ToolExecutionContext


def cancellation_callback(context: ToolExecutionContext) -> Callable[[], bool] | None:
    """从 thread 工具上下文提取 Run 取消回调。"""

    callback = context.runtime_dependencies.is_run_cancelled
    if callback is None or context.run_id <= 0:
        return None
    return lambda: callback(context.run_id)
