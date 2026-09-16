"""搜索 handler 的同进程取消检查适配。"""

from collections.abc import Callable
from functools import partial

from app.core.tools.schemas import ToolExecutionContext


def cancellation_callback(context: ToolExecutionContext) -> Callable[[], bool] | None:
    """从 thread 工具上下文提取本次执行专属的取消回调。

    返回的回调绑定本次 ``run_id`` 并每次调用重新读取进程内取消注册表，因此搜索引擎在
    分批扫描间隙能观察到执行途中发生的取消。

    参数:
        context: 本次工具调用的执行上下文，提供注入的取消查询与 run 标识。

    返回:
        绑定 ``run_id`` 的 ``() -> bool`` 取消查询回调；未注入取消查询或 ``run_id <= 0``
        （无 run 绑定的直接调用）时返回 None，调用方视为「不可取消」。

    异常:
        无。

    副作用:
        无；返回的回调被调用时只读注册表。
    """

    should_cancel = context.runtime_dependencies.is_run_cancelled
    if should_cancel is None or context.run_id <= 0:
        return None
    return partial(should_cancel, context.run_id)
