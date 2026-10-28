"""delegate_task 工具 handler：把一次委派请求落地为 child Task/Run 并启动 child workflow。

职责：解析 child Agent profile、建立 child Task，经 ``task_runtime_spaces.enqueue`` 投递首条输入（由
child Task 的调度 actor 新开并启动 child Run），返回「已提交」观察（携带 child locator）。

不负责：child workflow 的实际运行与完成观测（结论由调度层以消息回投，父侧用 ``wait_messages``
挂起等待，或 ``child_agent_status`` 查询）；参数校验与准入门禁（``ToolAccessGate``）；委派展示数据构造
（``build_delegation_display_data``）。
"""
import copy
import json
from typing import ClassVar

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.agents.agent_profile import (
    AgentProfile,
    AgentProfileType,
)
from app.core.tool.handler.child_agent.delegation_display import build_delegation_display_data
from app.core.tool.schemas import (
    TOOL_DELEGATE_TASK,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.results.tool_cancelled import tool_cancelled
from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.tool_grouping import TOOL_GROUP_CHILD_AGENT, TOOL_GROUP_TERMINAL_SESSION
from app.core.tool.tool_base import HandlerBase
from app.core.tool.handler.child_agent.delegate_task_args import DelegateTaskArgs
from app.core.workflow.cancel.conversation_run_cancellation_registry import cancellation_registry
from app.models import ConversationRunCommand
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces
from app.service.depends import (
    get_conversation_run_ledger,
    get_user_input_service,
    get_task_service,
)


def strip_child_disallowed_tools(definitions: list[ToolDefinition]) -> list[ToolDefinition]:
    """剥掉子 Agent 不得使用的工具：委派、建 Team、提案配置，以及整个交互终端工具组。

    剥离发生在子 Task 固化 schema 之前，模型看不到也调用不了。终端按工具组剥离：该组新增工具
    无需同步禁用清单。
    """

    return [
        tool
        for tool in definitions
        if tool.name not in Constant.Tools.CHILD_DISALLOWED_TOOLS
        and tool.group != TOOL_GROUP_TERMINAL_SESSION
    ]


class DelegateTaskTool(HandlerBase):
    """把一次 delegate_task 调用落地为已启动的 child Task/Run。

    职责：解析 child profile、创建 child Task 并经调度入口启动 Run，全程以
    ``ToolObservation`` 返回结果。不等待 child 执行结束——子 Task 的每次 Run 到终态，由它的 actor 以
    ``background_result`` 消息回投父 Task，父侧用 ``wait_messages`` 挂起等待或 ``child_agent_status`` 查询。
    """

    name: str = TOOL_DELEGATE_TASK
    # HandlerBase 要求提供类级描述；描述与工具集、workspace 候选无关（子 Agent 目录由系统提示词
    # 的工具能力目录层下发），进程级基础定义即为模型可见终稿。
    description: str = (
        "Delegate a focused subtask to a child agent. It runs its own loop with only your "
        "message as input — it cannot see this conversation and cannot delegate further — so "
        "the message must be a self-contained task contract, and the child returns only a "
        "final summary. Delegation is asynchronous: after creating the child, wait for it "
        "with wait_messages or continue other non-interfering work. A failed delegation "
        "is terminal — adjust the contract or ask the user rather than retrying identical "
        "arguments. Keep inputs within the stated length limits; an oversized call is "
        "rejected as a tool error, so trim or split the task."
    )
    args_model: type[DelegateTaskArgs] = DelegateTaskArgs
    timeout_seconds: ClassVar[float] = 300.0
    group = TOOL_GROUP_CHILD_AGENT

    def __init__(self) -> None:
        """构造 delegate_task handler，并绑定委派所需的运行期服务依赖。

        参数:
            无。

        返回:
            无。

        异常:
            无（依赖由工具系统装配层提供；服务未初始化时由依赖装配层抛出异常）。

        副作用:
            在实例上绑定 Task 服务与 Run 读取服务；构造即解析依赖单例，因此必须晚于存储与运行期
            装配。
        """

        self._task_service = get_task_service()
        self._ledger = get_conversation_run_ledger()

    def execute(
            self,
            child_agent_id: str,
            agent_name: str,
            message: str,
            execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """委派一个自由文本子任务给指定 child Agent，并返回启动结果观察。

        参数:
            child_agent_id: 要运行的 child agent profile 标识。
            agent_name: Agent 名称，用作 child Task 标题（展示与可追溯）。
            message: 面向子 Agent 的自由文本任务契约，原样作为 child turn 的输入。
            execution_context: 父工具执行边界，提供工作区根与父事件循环。

        返回:
            child 启动成功时返回携带稳定 locator 的 success 观察；run 已取消、child 无法解析、
            父事件循环不可用或启动失败时返回确定性的
            error/cancelled 观察。

        异常:
            无。参数校验由 ``ToolAccessGate`` 在准入门禁层统一完成（单一收口），本方法信任已
            校验入参，不再二次校验；委派过程的异常全部在本方法内收口为 error 观察并写
            ``delegate_task_exception`` 日志，不向上抛异常。

        副作用:
            创建 child Task，并在父 Run 的事件循环上经调度入口新开 running Run 并启动（不等待
            child 执行结束）；写日志。
        """

        if cancellation_registry.is_cancelled(execution_context.task_id):
            return tool_cancelled(
                tool_name=self.name,
            )

        from app.config.configuration import get_agent_registry

        # ``resolve`` 的契约是不抛配置异常：坏配置由装载来源记日志后按「无配置」降级
        # （见 ``AgentProfileScopeSource``），因此这里只有「解析不到」这一条失败路径。
        child_agent_profile: AgentProfile | None = get_agent_registry().resolve(
            execution_context.workspace_root,
            child_agent_id,
        )
        if (
                child_agent_profile is None
                or child_agent_profile.agent_type is not AgentProfileType.CHILD
        ):
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
                f"delegate_task child not found: {child_agent_id}. "
                f"The child agent '{child_agent_id}' is not available in this workspace; "
                f"verify the child_agent_id against the available child agents "
                f"before retrying.",
            )

        loop = execution_context.event_loop
        if loop is None or loop.is_closed():
            return tool_error(
                self.name,
                "The parent runtime event loop is unavailable, so the child run "
                "cannot be started; retry from a normal turn.",
            )

        try:
            from app.config.configuration import get_tool_registry

            parent_run = self._ledger.get_run(execution_context.run_id)
            child_task_tools = child_agent_profile.select_tools(
                get_tool_registry().get_all_definitions()
            )
            child_final_task_tools = strip_child_disallowed_tools(child_task_tools)

            child_task = self._task_service.get_or_create_task(
                workspace_id=execution_context.workspace_id,
                title=agent_name,
                task_type="delegate_task",
                parent_task_id=execution_context.task_id,
                tool_schemas=[
                    copy.deepcopy(definition.to_model_tool_definition())
                    for definition in child_final_task_tools
                ]
            )

            reasoning_effort = (child_agent_profile.model_settings.reasoning_effort
                                if child_agent_profile.model_settings is not None
                                else None)
            child_agent_model_config_id = child_agent_profile.model_config_id
            model_config_id = child_agent_model_config_id or parent_run.model_config_id
            # 只入队，由子 Task 的 actor 新开 Run，不等子 Run 结束。
            task_runtime_spaces.enqueue(
                child_task.id,
                get_user_input_service().prepare(
                    child_task.id,
                    ConversationRunCommand(display_text=message),
                    model_config_id,
                    reasoning_effort,
                    child_agent_id,
                ),
            )

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
                content="An error occurred while delegating the task.",
            )
        return tool_success(
            tool_name=self.name,
            content=json.dumps(
                {
                    "status": "submitted",
                    "child_task_id": child_task.id,
                    "child_agent_id": child_agent_id,
                    "agent_name": agent_name,
                },
                separators=(",", ":"),
            ),
            display_data=build_delegation_display_data(
                title=agent_name,
                child_agent_id=child_agent_id,
                child_task_id=child_task.id,
                status="running",
                role=child_agent_profile.role,
            ),
        )

    def to_definition(self) -> ToolDefinition:
        """构建 delegate_task 工具的注册定义。

        delegate_task 声明为工具级 ``parallel``：当模型在同一回复里发起多个
        ``delegate_task`` 时，执行层把它们放进同一并行批次并发执行，使多个子 Agent
        真正并行。并发度只受执行层线程池额度（``Constant.Tools.MAX_PARALLEL_CALLS``）
        约束，工具层**不声明** child 并发上限——不存在的契约不得下发给模型。

        参数:
            无。

        返回:
            使用进程内线程执行、同一回复内多个委派可工具级并行的 delegate_task 工具定义；
            工具观察仅确认 child Run 已注册，不等待 child workflow 完成。

        异常:
            无。

        副作用:
            生成结构 schema 基线；不读取 agent registry 或固化 workspace 候选。
        """

        return ToolDefinition(
            name=self.name,
            group=self.group,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            execution_mode="thread",
            parallel_mode="parallel",
            display=ToolDisplayHints(
                verb="委派任务",
                icon="users",
                surface="standalone",
                expandable=False,
                expand_layout="details",
                show_result=False,
            ),
        )


def build_delegate_task_definition() -> ToolDefinition | None:
    """构建供工具注册表登记的 delegate_task 基础定义。

    CHILD 候选不在工具定义里固化（既无 ``enum`` 也不列举 ID 清单）：合法候选由系统提示词的
    工具能力目录层下发，目标是否可委派由 ``DelegateTaskTool.execute`` 在执行期按 workspace
    作用域解析内存 Registry 裁决。本函数经 `HandlerBase.to_definition_if_avaliable` 包装调用
    `to_definition`。

    参数:
        无。

    返回:
        可直接注册到工具注册表的 delegate_task 工具定义；``avaliable`` 为假时返回 None。

    异常:
        无。

    副作用:
        创建 `DelegateTaskTool` 实例（解析 Task/Run 服务单例）并生成基础
        `ToolDefinition`；不创建 child Task、不启动委派。
    """

    return DelegateTaskTool().to_definition_if_avaliable()
