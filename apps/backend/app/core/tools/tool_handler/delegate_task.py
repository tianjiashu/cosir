"""delegate_task tool handler."""

from dataclasses import replace
from typing import ClassVar

from app.config.logging.logger import log
from app.config.settings import Settings
from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.core.tools.schemas import (
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_execute.tool_cancelled import tool_cancelled
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models.delegate_task_args import DelegateTaskArgs


def _contract_description() -> str:
    """返回面向模型的委派契约主描述（不含子 Agent 清单与并行引导）。

    本函数只描述「委派是什么、何时该用、代价与边界」，**不重复具体数值上限**
    （``PROMPT_MAX`` / ``TITLE_MAX`` 只留在参数字段描述里——工具描述与参数 schema 在同一份
    function 定义里同时下发给模型，同一数值说两遍纯属浪费 token）；这里只保留「超预算会被
    立刻拒绝」这一确定性后果。并发额度在**每次投影时**从 ``Settings`` 实时读取，避免把配置
    值写死在文案里后与运行时漂移（``DELEGATION_MAX_CONCURRENCY`` 的类属性声明是 4，
    ``Settings.load`` 会把生效值覆盖为 2）。

    参数:
        无。

    返回:
        面向模型的契约主描述文本。

    异常:
        无。

    副作用:
        无（纯函数，只读 ``Settings``）。
    """

    return (
        "Delegate one focused subtask to a single child agent and wait for its result. Use it "
        "when part of the work is separable from your own turn. The child runs its own agent "
        "loop with only your prompt as input — it cannot see this conversation — and returns "
        "only a final summary, so the prompt must be self-contained. A child cannot delegate "
        "further, and its tools are reduced by parent and child permissions. Delegation is "
        "synchronous: you wait until the child finishes. "
        f"At most {Settings.DELEGATION_MAX_CONCURRENCY} children run at once; further "
        "delegate_task calls in the same reply are rejected deterministically. A failed or "
        "rejected delegation is terminal: adjust the contract or ask the user instead of "
        "retrying identical arguments. "
        "CRITICAL BUDGET LIMIT: an over-budget call is rejected immediately and counts as a "
        "tool error, so trim or split the task instead of overshooting."
    )


_PARALLEL_HINT = (
    "To run several children in parallel, emit several delegate_task calls in the same reply; "
    "reusing the same child_agent_id is fine as long as each prompt is self-contained."
)


def _compose_description(agent_summary: str) -> str:
    """把契约主描述、子 Agent 清单与并行引导拼装为面向模型的完整工具描述。

    子 Agent 清单自带 ``Available child agents`` 标题（由
    ``AgentProfileRegistry.child_agent_summary`` 产出），本函数**不再重复加标题**——历史
    实现两处都加，模型实际看到的是同一个标题连写两遍。清单为空（注册表尚未注入）时只输出
    契约与并行引导，既不暴露模板占位符也不留误导性标题。

    参数:
        agent_summary: 已投影的子 Agent 能力摘要；空串表示当前取不到，退化为不含清单的
            通用描述。

    返回:
        完整的 delegate_task 工具描述文本。

    异常:
        无。

    副作用:
        无（纯函数）。
    """

    blocks = [_contract_description()]
    if agent_summary.strip():
        blocks.append(agent_summary.strip())
    blocks.append(_PARALLEL_HINT)
    return "\n\n".join(blocks)


class DelegateTaskTool(HandlerBase):
    """Validate a delegate_task request and route it to the injected runtime executor."""

    name: str = "delegate_task"
    # 类级描述只是「静态兜底」：真实下发文本由 build_delegate_task_definition 注册的
    # ``description_provider`` 在每次投影时重新拼装（含运行期子 Agent 清单与并发额度）。
    description: str = _compose_description("")
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

        self.description = description if description is not None else _compose_description("")

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
        if cancellation_registry.is_cancelled(execution_context.run_id):
            return tool_cancelled(
                tool_name=self.name,
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
            display=ToolDisplayHints(
                verb="委派任务",
                icon="users",
                surface="standalone",
                expandable=False,
                expand_layout="none",
                show_result=False,
            ),
        )


def _runtime_child_agent_summary() -> str:
    """投影期实时读取进程级 agent 注册表的子 Agent 能力摘要。

    委派目标清单只能在运行期（注册表注入之后）取得：启动装配期 agent 注册表尚未注入，
    任何注册期快照都会把空摘要永久固化（2026-09-17 委派全线失败的根因）。因此本函数在
    **每次向模型投影工具定义时**实时读取，并显式记录取数失败，便于排查装配顺序问题。

    参数:
        无。

    返回:
        形如 ``"Available child agents:\\n- agent_id ..."`` 的摘要文本；注册表未就绪时
        返回空串（描述退化为通用形态，但绝不暴露占位符）。

    异常:
        无（注册表不可用属预期降级路径，异常在本函数内收口后返回空串）。

    副作用:
        注册表不可用时写一条 WARNING 日志（``delegate_agent_catalog_unavailable``）。
    """

    try:
        from app.config.configuration import get_agent_registry

        return get_agent_registry().child_agent_summary()
    except Exception as exc:
        # 异常必须在本函数收口：本函数处于「每次下发模型」的投影热路径上，任何穿透
        # 都会炸穿整轮 run（比原缺陷的静默降级更严重）。
        log.warning(
            "delegate_agent_catalog_unavailable",
            extra={
                "msg": "委派子 Agent 能力摘要不可用，delegate_task 描述降级为通用形态",
                "data": {
                    "reason": "agent_registry_unavailable",
                    "error_type": type(exc).__name__,
                },
            },
        )
        return ""


def build_delegate_task_definition() -> ToolDefinition:
    """构建 delegate_task 工具定义（子 Agent 清单与候选集在投影期实时解析）。

    子 Agent 清单与 ``child_agent_id`` 候选集均依赖 agent 注册表，而注册表在启动序列中
    晚于工具系统装配（``build_agent_registry`` 又反向依赖 ``get_tool_registry``，二者构成
    循环依赖）。因此本函数把描述与参数 schema 注册为**运行期投影钩子**
    （``description_provider`` / ``schema_provider``）：每次下发模型时实时读取注册表，
    注册期空值不会被固化。静态 ``description`` 仅作钩子异常时的兜底值。

    参数:
        无。**不提供静态摘要注入口**：任何静态摘要都会遮蔽运行期清单，一旦被传入即退回
        「子 Agent 清单不可见」的旧缺陷形态（2026-09-17 委派全线失败），故从签名上消除该
        地雷，而非依赖调用方自律。

    返回:
        可直接注册到工具注册表的 delegate_task 工具定义（含运行期投影钩子）。

    异常:
        无。

    副作用:
        创建 DelegateTaskTool 实例并绑定投影钩子，但不会启动委派；钩子仅在每次投影时
        读取进程级注册表。
    """

    static_description = _compose_description("")

    def _description_provider() -> str:
        return _compose_description(_runtime_child_agent_summary())

    definition = DelegateTaskTool(description=static_description).to_definition()
    return replace(
        definition,
        description=static_description,
        description_provider=_description_provider,
        schema_provider=DelegateTaskArgs.model_json_schema,
    )
