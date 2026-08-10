"""delegate_task tool handler."""

from typing import ClassVar

from pydantic import ValidationError

from app.tools.schemas import ToolDefinition, ToolExecutionContext, ToolObservation
from app.tools.tool_execute.tool_error import tool_error
from app.tools.tool_handler.tool_base import HandlerBase
from app.tools.tool_models.delegate_task_args import DelegateTaskArgs


class DelegateTaskTool(HandlerBase):
    """Validate a delegate_task request and route it to the injected runtime executor."""

    name: str = "delegate_task"
    description: str = "Delegate a focused task to a child agent and return its result."
    permission: ClassVar[str] = "delegate_task"
    args_model: type[DelegateTaskArgs] = DelegateTaskArgs
    timeout_seconds: ClassVar[float] = 300.0
    risk_level: ClassVar[str] = "medium"

    def execute(
        self,
        child_agent_id: str,
        delegation_type: str,
        prompt: str,
        requested_tools: list[str],
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """通过执行上下文中的运行时执行器委派任务。

        参数:
            child_agent_id: 要运行的 child agent profile 标识。
            delegation_type: 请求的委派类别。
            prompt: 传递给 child agent 的聚焦指令。
            requested_tools: 为 child agent 请求的工具名称列表。
            execution_context: 包含运行时依赖的父工具执行边界。

        返回:
            注入执行器的归一化结果；当执行上下文、执行器或参数缺失或无效时，返回错误观察结果。

        异常:
            无。参数校验失败会返回错误观察结果；执行器异常由执行器自身负责处理。

        副作用:
            当请求有效时调用注入的运行时执行器。
        """

        if execution_context is None:
            return tool_error(
                self.name,
                "delegate_task requires an execution context.",
                reason="Provide the parent task execution context before delegating work.",
                permission=self.permission,
            )

        try:
            args = DelegateTaskArgs.model_validate(
                {
                    "child_agent_id": child_agent_id,
                    "delegation_type": delegation_type,
                    "prompt": prompt,
                    "requested_tools": requested_tools,
                }
            )
        except ValidationError:
            return tool_error(
                self.name,
                "delegate_task received invalid arguments.",
                reason="Correct the delegate_task arguments, then retry.",
                permission=self.permission,
            )

        executor = execution_context.runtime_dependencies.delegate_task_executor
        if executor is None:
            return tool_error(
                self.name,
                "delegate_task_executor is not configured.",
                reason="Configure a delegate_task runtime executor before delegating work.",
                permission=self.permission,
            )
        return executor.execute(args, execution_context)

    def to_definition(self) -> ToolDefinition:
        """构建 delegate_task 工具的注册定义。

        参数:
            无。

        返回:
            使用进程内线程执行的 delegate_task 工具定义。

        异常:
            无。

        副作用:
            无。
        """

        return ToolDefinition(
            name=self.name,
            description=self.description,
            permission=self.permission,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            risk_level=self.risk_level,
            execution_mode="thread",
        )


def build_delegate_task_definition() -> ToolDefinition:
    """构建 delegate_task 工具定义。

    参数:
        无。

    返回:
        可直接注册到工具注册表的 delegate_task 工具定义。

    异常:
        无。

    副作用:
        创建 DelegateTaskTool 实例，但不会启动委派。
    """

    return DelegateTaskTool().to_definition()
