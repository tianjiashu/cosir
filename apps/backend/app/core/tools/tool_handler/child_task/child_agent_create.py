"""delegate_task 工具 handler：把一次委派请求落地为 child Task/Run 并启动 child workflow。

职责：解析 child Agent profile、建立 child Task 与 Run、经父 Run 的事件循环调度 child 执行器，
返回「已启动」观察（携带 child locator），并把子 Agent 的工具禁用清单（``CHILD_BANNED_TOOLS``）
交给执行器。

不负责：child workflow 的实际运行与完成观测（父侧用 ``child_agent_wait`` / ``child_agent_status``
查询）；参数校验与准入门禁（``ToolAccessGate``）；委派展示数据构造
（``build_delegation_display_data``）。
"""

import asyncio
import json
from typing import ClassVar

from app.config.logging.logger import log
from app.core.agents.agent_profile import AgentProfile
from app.core.runtime.conversation_run_cancellation_registry import cancellation_registry
from app.core.tools.display.delegation_display import build_delegation_display_data
from app.core.tools.schemas import (
    TOOL_CHILD_AGENT_SEND,
    TOOL_CHILD_AGENT_STATUS,
    TOOL_CHILD_AGENT_WAIT,
    TOOL_DELEGATE_TASK,
    TOOL_TERMINAL_CLOSE,
    TOOL_TERMINAL_READ,
    TOOL_TERMINAL_SIGNAL,
    TOOL_TERMINAL_START,
    TOOL_TERMINAL_WRITE,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_execute.tool_cancelled import tool_cancelled
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models import DelegateTaskArgs
from app.models import ConversationRunCommand
from app.service.depends import (
    get_conversation_run_executor,
    get_conversation_run_service,
    get_conversation_run_state_service,
    get_task_service,
)


def _contract_description() -> str:
    """返回面向模型的委派契约主描述（不含子 Agent 清单与并行引导）。

    本函数只描述「委派是什么、何时该用、代价与边界」，**不重复具体数值上限**
    （``MESSAGE_MAX`` / ``AGENT_NAME_MAX`` 只留在参数字段描述里——工具描述与参数 schema 在同一份
    function 定义里同时下发给模型，同一数值说两遍纯属浪费 token）；这里只保留「超预算会被
    立刻拒绝」这一确定性后果。**不下发任何 child 并发额度契约**：进程内没有 child 并发
    裁决点，文案里写「超出 N 个会被拒绝」等于向模型下发不存在的规则（2026-09-23 随无执行点
    的并发配置一并删除）。**不下发 child 的工具集收窄规则**：进程内不存在「父权限 ∩ 子权限」
    这一逻辑（child 工具集只由自身 profile 的 ``allowed_tools`` 减 ``CHILD_BANNED_TOOLS``
    决定），原先的 "reduced by parent and child permissions" 表述不准确，2026-09-24 删除。

    参数:
        无。

    返回:
        面向模型的契约主描述文本。

    异常:
        无。

    副作用:
        无（纯常量拼接，不读配置、不访问注册表）。
    """
    return (
        "Delegate one focused subtask to a single child agent. Use it "
        "when part of the work is separable from your own turn. The child runs its own agent "
        "loop with only your message as input — it cannot see this conversation — and returns "
        "only a final summary, so the message must be self-contained. A child cannot delegate "
        "further. Delegation is asynchronous: after creating the child you can wait for it with "
        "child_agent_wait, or work on other tasks that do not interfere with it. "
        "A failed delegation is terminal: adjust "
        "the contract or ask the user instead of retrying identical arguments. "
        "CRITICAL BUDGET LIMIT: an over-budget call is rejected immediately and counts as a "
        "tool error, so trim or split the task instead of overshooting."
    )

# 子 Agent 不得使用的工具：**委派与父子通信**（``tool_handler/child_task/`` 全部工具）与
# **可交互终端**（``tool_handler/terminal_session/`` 全部工具）。清单与这两个目录一一对应，
# 新增同目录工具时必须同步登记。``delegate_task`` 与 ``child_agent_send`` 在启动 child Run
# 时都把它作为 ``ban_tools`` 传给执行器，保证子 Agent 既不能再向下委派，也不能操作父级
# 的交互式终端会话。
CHILD_BANNED_TOOLS: tuple[str, ...] = (
    # child_task/：委派与父子通信
    TOOL_DELEGATE_TASK,
    TOOL_CHILD_AGENT_STATUS,
    TOOL_CHILD_AGENT_SEND,
    TOOL_CHILD_AGENT_WAIT,
    # terminal_session/：可交互终端
    TOOL_TERMINAL_START,
    TOOL_TERMINAL_WRITE,
    TOOL_TERMINAL_READ,
    TOOL_TERMINAL_SIGNAL,
    TOOL_TERMINAL_CLOSE,
)

# 只等 executor 完成「登记 + 建后台 task」这一段，不等子 Agent 跑完。
_START_ACK_TIMEOUT_SECONDS = 10.0


def _compose_description() -> str:
    """把契约主描述、子 Agent 清单与并行引导拼装为面向模型的完整工具描述。

    子 Agent 清单自带 ``Available child agents`` 标题（由
    ``AgentProfileRegistry.child_agent_summary`` 产出），本函数**不再重复加标题**——历史
    实现两处都加，模型实际看到的是同一个标题连写两遍。清单为空时只输出契约与并行引导，既不
    暴露模板占位符也不留误导性标题。

    参数:
        无。

    返回:
        完整的 delegate_task 工具描述文本（各块之间以空行分隔）。

    异常:
        RuntimeError: agent registry 尚未注入（``get_agent_registry`` 取不到运行期单例），
            由装配期调用方暴露为工具定义构建失败。

    副作用:
        读取进程内 agent registry 的子 Agent 清单；该导入延迟到调用时执行，避免配置层与工具
        handler 在模块初始化期形成循环依赖。
    """
    from app.config.configuration import get_agent_registry
    agent_summary = get_agent_registry().child_agent_summary()
    blocks = [_contract_description()]
    if agent_summary.strip():
        blocks.append(agent_summary.strip())
    blocks.append(
        "To run several children in parallel, emit several delegate_task calls in the same reply; "
        "reusing the same child_agent_id is fine as long as each message is self-contained."
    )
    return "\n\n".join(blocks)


class DelegateTaskTool(HandlerBase):
    """把一次 delegate_task 调用落地为已启动的 child Task/Run。

    职责：检查运行期依赖可用性、解析 child profile、创建 child Task/Run 并调度执行器，全程以
    ``ToolObservation`` 返回结果。不等待 child 执行结束——完成情况由 ``child_agent_wait`` /
    ``child_agent_status`` 查询。
    """

    name: str = TOOL_DELEGATE_TASK
    # HandlerBase 要求提供类级描述；真正下发给模型的完整描述由 to_definition 在
    # ToolSystem 装配时读取 Agent registry 后固化。
    description: str = ""
    permission: ClassVar[str] = "delegate_task"
    args_model: type[DelegateTaskArgs] = DelegateTaskArgs
    timeout_seconds: ClassVar[float] = 300.0
    risk_level: ClassVar[str] = "medium"

    def __init__(self) -> None:
        """构造 delegate_task handler，并绑定委派所需的运行期服务依赖。

        参数:
            无。

        返回:
            无。

        异常:
            无（依赖由工具系统装配层提供；服务未初始化时由依赖装配层抛出异常）。

        副作用:
            在实例上绑定 Task 服务、Run 创建服务、Run 状态服务与 Run 执行器；构造即解析依赖
            单例，因此必须晚于存储与运行期装配。
        """

        self._task_service = get_task_service()
        self.run_setvice = get_conversation_run_service()
        self.run_state_service = get_conversation_run_state_service()
        self.run_exector = get_conversation_run_executor()

    def execute(
            self,
            child_agent_id: str,
            agent_name: str,
            message: str,
            execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """委派一个自由文本子任务给指定 child Agent，并返回启动结果观察。

        参数:
            child_agent_id: 要运行的 child agent profile 标识。
            agent_name: Agent 名称，用作 child Task 标题（展示与可追溯）。
            message: 面向子 Agent 的自由文本任务契约，原样作为 child turn 的输入。
            execution_context: 父工具执行边界，提供运行期依赖与父 Run 事实。

        返回:
            child 启动成功时返回携带稳定 locator 的 success 观察；缺少执行上下文、缺少父
            profile、run 已取消、child 无法解析、父事件循环不可用或启动失败时返回确定性的
            error/cancelled 观察。

        异常:
            无。参数校验由 ``ToolAccessGate`` 在准入门禁层统一完成（单一收口），本方法信任已
            校验入参，不再二次校验；委派过程的异常全部在本方法内收口为 error 观察并写
            ``delegate_task_exception`` 日志，不向上抛异常。

        副作用:
            创建 child Task 与 pending Run、认领该 Run，并经父 Run 的事件循环调度 child 执行器
            （只等待启动登记，不等待 child 执行结束）；写委派相关日志。
        """

        if execution_context is None:
            return tool_error(
                self.name,
                "delegate_task requires an execution context.",
                reason="Provide the parent task execution context before delegating work.",
                permission=self.permission,
                retryable=False,
            )

        runtime_dependencies = execution_context.runtime_dependencies
        parent_profile = runtime_dependencies.parent_agent_profile
        if parent_profile is None:
            return tool_error(
                self.name,
                "delegate_task_runtime_unavailable",
                reason="Configure the parent agent profile error before delegating work.",
                permission=self.permission,
                retryable=False,
            )
        if cancellation_registry.is_cancelled(execution_context.run_id):
            return tool_cancelled(
                tool_name=self.name,
                permission=self.permission,
            )

        # 延迟导入，避免配置层与工具 handler 的模块初始化形成循环依赖。
        from app.config.configuration import get_agent_registry

        agent_registry = get_agent_registry()
        child_agent_profile: AgentProfile | None = agent_registry.resolve(child_agent_id)
        if child_agent_profile is None:
            log.warning(
                "delegate_child_resolve_failed",
                extra={
                    "msg": "委派目标 child Agent 无法解析，返回工具错误",
                    "data": {
                        "parent_run_id": execution_context.run_id,
                        "child_agent_id": child_agent_id,
                    },
                },
            )
            return tool_error(
                self.name,
                f"delegate_task child not found: {child_agent_id}",
                reason=(
                    f"the child agent '{child_agent_id}' is not registered; "
                    f"verify the child_agent_id against the available delegate_* agents "
                    f"before retrying."
                ),
                permission=self.permission,
                retryable=True,
            )

        try:
            parent_run = self.run_state_service.get_run(execution_context.run_id)

            child_task = self._task_service.get_or_create_task(
                workspace_id=execution_context.workspace_id,
                title=agent_name,
                task_type="delegate_task",
                parent_task_id=execution_context.task_id,
                parent_run_id=execution_context.run_id,
            )

            provider_id = (
                child_agent_profile.provider_id
                if child_agent_profile.provider_id is not None
                else parent_profile.provider_id
            )
            model_name = (
                child_agent_profile.model_name
                if child_agent_profile.model_name is not None
                else parent_profile.model_name
            )
            reasoning_effort = parent_run.reasoning_effort

            child_run = self.run_setvice.create_run(
                task_id=child_task.id,
                agent_id=child_agent_id,
                provider_id=provider_id,
                model_name=model_name,
                reasoning_effort=reasoning_effort,
                run_command=ConversationRunCommand(display_text=message),
            )

            child_run = self.run_state_service.claim_pending_run(child_run.id)

            # 执行器入口是协程：在父 Run 的事件循环上调度，并只等待登记完成。
            loop = execution_context.runtime_dependencies.runtime_event_loop
            if loop is None or loop.is_closed():
                return tool_error(
                    self.name,
                    "delegate_task_runtime_unavailable",
                    reason=(
                        "the parent runtime event loop is unavailable, so the child run "
                        "cannot be started; retry from a normal turn."
                    ),
                    permission=self.permission,
                    retryable=False,
                )
            future = asyncio.run_coroutine_threadsafe(
                self.run_exector.start(
                    child_run.id, start_mode="fresh", ban_tools=list(CHILD_BANNED_TOOLS)
                ),
                loop,
            )
            future.result(timeout=_START_ACK_TIMEOUT_SECONDS)

        except Exception as e:
            log.exception(
                "delegate_task_exception",
                extra={
                    "msg": "委托任务时发生异常",
                    "data": {
                        "parent_run_id": execution_context.run_id,
                        "child_agent_id": child_agent_id,
                        "error": str(e),
                    },
                },
            )
            return tool_error(
                tool_name=self.name,
                error="error",
                reason="An error occurred while delegating the task.",
                retryable=False,
            )
        return tool_success(
            tool_name=self.name,
            permission=self.permission,
            content=json.dumps(
                {
                    "status": "running",
                    "child_task_id": child_task.id,
                    "child_run_id": child_run.id,
                    "child_agent_id": child_agent_id,
                    "agent_name": agent_name,
                },
                separators=(",", ":"),
            ),
            display_data=build_delegation_display_data(
                title=agent_name,
                child_agent_id=child_agent_id,
                child_task_id=child_task.id,
                child_run_id=child_run.id,
                status="running",
                role=child_agent_profile.role,
            ),
        )

    def to_definition(self) -> ToolDefinition:
        """构建 delegate_task 工具的注册定义。

        delegate_task 声明为工具级 ``parallel``：当模型在同一回复里发起多个
        ``delegate_task`` 时，执行层把它们放进同一并行批次并发执行，使多个子 Agent
        真正并行。并发度只受执行层线程池额度（``MAX_PARALLEL_TOOL_CALLS``）约束，
        工具层**不声明** child 并发上限——不存在的契约不得下发给模型。

        参数:
            无。

        返回:
            使用进程内线程执行、同一回复内多个委派可工具级并行的 delegate_task 工具定义；
            工具观察仅确认 child Run 已注册，不等待 child workflow 完成。

        异常:
            RuntimeError: agent registry 尚未注入，无法生成子 Agent 清单与 child_agent_id
                候选集。

        副作用:
            读取进程内 agent registry 生成子 Agent 清单；并固化 ``DelegateTaskArgs`` 的 JSON
            schema 作为模型可见参数契约。
        """

        return ToolDefinition(
            name=self.name,
            description=_compose_description(),
            permission=self.permission,
            handler=self.execute,
            args_model=self.args_model,
            parameters_schema=DelegateTaskArgs.model_json_schema(),
            timeout_seconds=self.timeout_seconds,
            risk_level=self.risk_level,
            execution_mode="thread",
            parallel_mode="parallel",
            display=ToolDisplayHints(
                verb="委派任务",
                icon="users",
                surface="standalone",
                expandable=True,
                expand_layout="details",
                show_result=False,
            ),
        )


def build_delegate_task_definition() -> ToolDefinition | None:
    """构建在装配期固化模型契约的 delegate_task 工具定义。

    agent registry 必须先于 ToolSystem 装配完成：描述里的子 Agent 清单与参数 schema 都由
    :meth:`DelegateTaskTool.to_definition` 在注册时一次性生成并固化，运行期不再刷新。本函数
    经 ``HandlerBase.to_definition_if_avaliable`` 包装调用 ``to_definition``；``DelegateTaskTool``
    未覆写 ``avaliable``，因此当前恒为可用。

    参数:
        无。

    返回:
        可直接注册到工具注册表的 delegate_task 工具定义；``avaliable`` 为假时返回 None。

    异常:
        RuntimeError: agent registry 尚未由应用启动流程注入。

    副作用:
        创建 ``DelegateTaskTool`` 实例（解析 Task/Run 服务与 Run 执行器单例）并生成一次静态
        ``ToolDefinition``；不创建 child Task、不启动委派。
    """

    return DelegateTaskTool().to_definition_if_avaliable()
