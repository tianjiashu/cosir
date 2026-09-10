"""delegate_task tool handler."""

from typing import ClassVar

from app.core.tools.schemas import ToolDefinition, ToolExecutionContext, ToolObservation
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models.delegate_task_args import PROMPT_MAX, TITLE_MAX, DelegateTaskArgs

_DEFAULT_DESCRIPTION = (
    "Delegate a focused subtask to one child agent profile and return the child result. "
    "Use this when the subtask is separable from the current turn, such as code review, "
    "codebase analysis, or scoped coding work. The child cannot recursively delegate, "
    "and requested tools are reduced by parent and child permissions. "
    "When the user's request decomposes into multiple independent subtasks (e.g. review "
    "and analysis, or two unrelated changes), emit multiple delegate_task calls in the "
    "same reply so the child agents run concurrently; each call targets one child profile "
    "with its own structured contract. "
    f"CRITICAL BUDGET LIMIT: prompt MUST NOT exceed {PROMPT_MAX} chars; if your task is "
    "larger, trim or split it. Exceeding this limit causes an immediate validation "
    "failure — the call will be rejected."
)

_STRUCTURED_SCHEMA_HINT = (
    "Provide the task as a single free-form prompt structured with markdown sections: "
    f"# Title (short, <= {TITLE_MAX} chars), ## Objective (the single goal the child must "
    "achieve), ## Rules (hard constraints, one per line with a leading dash), "
    "## References (relevant paths or documents, one per line with a leading dash), "
    "## Expected Output (what the child returns), and an optional ## Background "
    "(extra context, omit if empty). "
    f"Budgets: prompt total <= {PROMPT_MAX} chars; title <= {TITLE_MAX} chars. "
    "To run several child agents in parallel, emit several delegate_task calls in one "
    "reply, each with a distinct child_agent_id and a self-contained contract."
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
        title: str,
        prompt: str,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """通过执行上下文中的运行时执行器委派自由文本任务。

        参数:
            child_agent_id: 要运行的 child agent profile 标识。
            title: 任务标题，必填，用于展示与可追溯。
            prompt: 面向子 Agent 的自由文本任务契约，原样作为 child turn 的输入。
            execution_context: 包含运行时依赖的父工具执行边界。

        返回:
            注入执行器的归一化结果；当执行上下文或执行器缺失时，返回错误观察结果。

        异常:
            无。参数校验由 ``ToolAccessGate`` 在准入门禁层统一
            完成（单一收口），本方法信任已校验入参，不再二次校验；若上游契约被破坏，
            ``DelegateTaskArgs`` 构造会抛出 ``ValidationError`` 由 ``ToolHandlerRunner``
            归一化为错误观察。执行器异常由执行器自身负责处理。

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

        executor = execution_context.runtime_dependencies.delegate_task_executor
        if executor is None:
            return tool_error(
                self.name,
                "delegate_task_executor is not configured.",
                reason="Configure a delegate_task runtime executor before delegating work.",
                permission=self.permission,
            )
        return executor.execute(
            DelegateTaskArgs(
                child_agent_id=child_agent_id,
                title=title,
                prompt=prompt,
            ),
            execution_context,
        )

    def to_definition(self) -> ToolDefinition:
        """构建 delegate_task 工具的注册定义。

        delegate_task 声明为工具级 ``parallel``：当模型在同一回复里发起多个
        ``delegate_task`` 时，它们进入独立的 ``delegate_task_group`` 并行组并发执行，
        使多个子 Agent 真正并行。child 并发的最终裁决权仍在 delegation 业务层的
        并发额度（``DELEGATION_MAX_CONCURRENCY``）——超额的委派会在业务层被拒并回退为
        错误观察，执行层并行不绕过该约束。``parallel_group`` 固定为
        ``"delegate_task_group"``，不与外部工具共享分组，避免 delegate_task 与文件类
        工具被错误地并发调度。

        参数:
            无。

        返回:
            使用进程内线程执行、同一回复内多个委派可工具级并行的 delegate_task 工具定义。

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
            parallel_mode="parallel",
            parallel_group="delegate_task_group",
        )


def build_delegate_task_definition(agent_summary: str = "") -> ToolDefinition:
    """构建 delegate_task 工具定义，可选注入子 Agent 能力摘要。

    参数:
        agent_summary: 可选的子 Agent 能力摘要文本。非空时拼入完整描述，
            为空时降级为通用默认描述。

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
