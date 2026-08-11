"""delegate_task tool handler."""

from typing import ClassVar

from pydantic import ValidationError

from app.tools.schemas import ToolDefinition, ToolExecutionContext, ToolObservation
from app.tools.tool_execute.tool_error import tool_error
from app.tools.tool_handler.tool_base import HandlerBase
from app.tools.tool_models.delegate_task_args import DelegateTaskArgs

_DEFAULT_DESCRIPTION = (
    "Delegate a focused subtask to one child agent profile and return the child result. "
    "Use this when the subtask is separable from the current turn, such as code review, "
    "codebase analysis, or scoped coding work. The child cannot recursively delegate, "
    "and requested tools are reduced by parent and child permissions."
)

_STRUCTURED_SCHEMA_HINT = (
    "Provide the task as a structured contract: objective (the single goal), rules "
    "(hard constraints), references (relevant paths or documents), expected_output "
    "(what the child returns), and an optional title. Budgets: objective and "
    "expected_output <= 2000 chars; title <= 200 chars; rules and references each "
    "<= 10 items, each item <= 500 chars, and each list total <= 2000 chars."
)


class DelegateTaskTool(HandlerBase):
    """Validate a delegate_task request and route it to the injected runtime executor."""

    name: str = "delegate_task"
    description: str = _DEFAULT_DESCRIPTION
    permission: ClassVar[str] = "delegate_task"
    args_model: type[DelegateTaskArgs] = DelegateTaskArgs
    timeout_seconds: ClassVar[float] = 300.0
    risk_level: ClassVar[str] = "medium"

    def __init__(self, description: str | None = None) -> None:
        """构造 delegate_task 工具实例，可选覆盖面向模型的描述。

        参数:
            description: 可选的实例级描述。传入时覆盖类属性 ``description``；
                为 ``None`` 时回退到类属性默认描述，兼容现有无参构造。

        返回:
            无（构造函数）。

        异常:
            无。

        副作用:
            在实例上绑定 ``description`` 属性（覆盖类属性）。
        """

        self.description = description if description is not None else _DEFAULT_DESCRIPTION

    def execute(
        self,
        child_agent_id: str,
        objective: str,
        rules: list[str],
        references: list[str],
        expected_output: str,
        title: str | None = None,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """通过执行上下文中的运行时执行器委派结构化任务。

        参数:
            child_agent_id: 要运行的 child agent profile 标识。
            objective: 子 Agent 需要达成的单一任务目标。
            rules: 子 Agent 必须遵守的约束规则列表。
            references: 子 Agent 应参考的背景/路径条目列表。
            expected_output: 子 Agent 完成时应返回的期望产出说明。
            title: 可选的任务标题。
            execution_context: 包含运行时依赖的父工具执行边界。

        返回:
            注入执行器的归一化结果；当执行上下文、执行器或参数缺失或无效时，
            返回错误观察结果。

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
                    "title": title,
                    "objective": objective,
                    "rules": rules,
                    "references": references,
                    "expected_output": expected_output,
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


def build_delegate_task_definition(agent_summary: str = "") -> ToolDefinition:
    """构建 delegate_task 工具定义，可选注入子 Agent 能力摘要。

    参数:
        agent_summary: 可选的可选子 Agent 能力摘要文本。非空时拼入完整描述，
            为空时降级为通用默认描述（仍经带 description 的构造路径注入）。

    返回:
        可直接注册到工具注册表的 delegate_task 工具定义。

    异常:
        无。

    副作用:
        创建 DelegateTaskTool 实例，但不会启动委派。
    """

    description = _DEFAULT_DESCRIPTION
    if agent_summary.strip():
        description = (
            f"{_DEFAULT_DESCRIPTION}\n\nAvailable child agents:\n{agent_summary}\n\n"
            f"{_STRUCTURED_SCHEMA_HINT}"
        )
    else:
        description = f"{_DEFAULT_DESCRIPTION}\n\n{_STRUCTURED_SCHEMA_HINT}"
    return DelegateTaskTool(description=description).to_definition()
