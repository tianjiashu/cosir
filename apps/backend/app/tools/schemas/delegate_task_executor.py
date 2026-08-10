"""delegate_task runtime execution contract."""

from __future__ import annotations

from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from app.tools.schemas.tool_execution_context import ToolExecutionContext
    from app.tools.schemas.tool_observation import ToolObservation
    from app.tools.tool_models.delegate_task_args import DelegateTaskArgs


class DelegateTaskExecutor(Protocol):
    """Execute a validated delegate_task request through the runtime."""

    def execute(
        self,
        args: DelegateTaskArgs,
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """执行一次 child agent 委派请求。

        参数:
            args: 已校验的 delegate_task 请求参数。
            execution_context: 父工具执行边界及其运行时依赖。

        返回:
            child delegation 的归一化工具观察结果。

        异常:
            当运行时无法完成委派时，由具体实现抛出对应异常。

        副作用:
            具体实现可能创建并运行一次 child agent 委派。
        """
        ...
