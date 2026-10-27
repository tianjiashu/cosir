"""child_agent_send 工具：给已有子任务追加输入并启动新的 Run。"""
import json
from typing import ClassVar

from app.config.logging.logger import log
from app.core.agents.agent_profile import AgentProfileConfigError, AgentProfileType
from app.core.tool.handler.child_agent.child_agent_display import build_child_agent_result_display_data
from app.core.tool.schemas import (
    TOOL_CHILD_AGENT_SEND,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.tool_grouping import TOOL_GROUP_CHILD_AGENT
from app.core.tool.tool_base import HandlerBase
from app.core.tool.handler.child_agent.child_agent_send_args import ChildAgentSendArgs
from app.models import ConversationRunCommand
from app.models.conversation_run_options import ConversationRunOptions
from app.core.workflow.inbox.task_message import RunLaunch
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces
from app.core.workflow.inbox.dispatch_policy import matches_run
from app.service.depends import (
    get_conversation_run_ledger,
    get_user_input_service,
    get_task_service,
    get_workspace_service,
)


class ChildAgentSendTool(HandlerBase):
    """给已有子任务追加一条输入：子 Agent 仍在运行时并入它的当前 Run，否则为它新开后台 Run。

    职责边界：本类只解析 child 运行参数，消息经 ``task_runtime_spaces.enqueue`` 投递（与用户发送同一
    入队语义，由子 Task 的 actor 串行判定活动 Run，不会并发执行）。子 Agent 的运行终态由它自己的
    workflow 落定。
    """

    name: str = TOOL_CHILD_AGENT_SEND
    description: str = (
        "Send a follow-up message to a child agent, reusing its child task. If the child agent "
        "is still running, the message is merged into its current run and read at its next "
        "step; otherwise a new run starts for the child task. Its conclusion arrives as a "
        "<background_result> message; use wait_messages to pause until it does, or "
        "child_agent_status to check on it."
    )
    args_model: type[ChildAgentSendArgs] = ChildAgentSendArgs
    timeout_seconds: ClassVar[float] = 30.0
    group = TOOL_GROUP_CHILD_AGENT

    def __init__(self) -> None:
        """装配任务、工作区与 Run 读取服务。"""

        self._task_service = get_task_service()
        self._workspace_service = get_workspace_service()
        self._ledger = get_conversation_run_ledger()

    def execute(
        self,
        child_task_id: int,
        message: str,
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """把消息交给子任务：并入其活动 Run，或新开 Run 并启动后台执行。

        参数:
            child_task_id: 目标子任务标识（``delegate_task`` 返回的 ``child_task_id``）。
            message: 追加给子 Agent 的自由文本输入；原样作为新 Run 的输入。
            execution_context: 父工具执行边界，提供事件循环与父 Run 事实。

        返回:
            成功时返回包含 ``child_task_id`` 与 ``status=submitted`` 的 JSON 文本（只确认消息已
            投递给子任务；并入哪条 Run 由子 Task 的调度 actor 决定，不回传 run_id）；父事件循环
            不可用、子任务不存在、无法确定 child Agent 或输入解析失败时返回错误观察。

        异常:
            无。配置无效、profile 已不可用、调度冲突或启动异常均归一化为错误观察；profile
            校验失败时不创建 Run。

        副作用:
            在父 Run 的事件循环上经调度入口接收消息（并入活动 Run 或新开 running Run 并启动）；
            不等待子 Agent 执行结束。
        """

        try:
            # 存在性校验：子任务不存在时直接返回确定性错误，不再继续创建 Run。
            child_task = self._task_service.get_task(child_task_id)
        except KeyError:
            return tool_error(
                self.name,
                f"child task not found: {child_task_id}. "
                "Verify child_task_id against the value returned by delegate_task; "
                "this task does not exist.",
            )
        if not child_task.is_child or child_task.parent_task_id != execution_context.task_id:
            return tool_error(
                tool_name=self.name,
                content=(
                    "child_task_id does not refer to a child task delegated by this task; "
                    "use the child_task_id returned by delegate_task, or "
                    "delegate the subtask first and retry with the corrected child_task_id."
                ),
            )

        current_run = self._ledger.get_run(child_task.current_run_id)

        try:
            workspace = self._workspace_service.get_workspace(child_task.workspace_id)
            from app.config.configuration import get_agent_registry


            current_profile = get_agent_registry().resolve(
                workspace.root_path,
                current_run.agent_id or "",
            )
        except (AgentProfileConfigError, KeyError, RuntimeError) as exc:
            log.warning(
                "child_agent_send_profile_unavailable",
                extra={
                    "msg": "workspace 子 Agent 配置无效，拒绝创建 follow-up Run",
                    "data": {
                        "parent_run_id": execution_context.run_id,
                        "child_task_id": child_task_id,
                        "error_type": type(exc).__name__,
                    },
                },
            )
            return tool_error(
                self.name,
                "The child agent configuration for this workspace is invalid.",
            )
        if current_profile is None or current_profile.agent_type is not AgentProfileType.CHILD:
            return tool_error(
                self.name,
                "The child agent is no longer available in this workspace.",
            )

        loop = execution_context.event_loop
        if loop is None or loop.is_closed():
            return tool_error(
                self.name,
                "The parent runtime event loop is unavailable, so the child run "
                "cannot be started; retry from a normal turn.",
            )
        reasoning_effort = (
            current_profile.model_settings.reasoning_effort
            if current_profile.model_settings is not None
            else None
        )
        try:
            launch = RunLaunch(
                agent_id=current_run.agent_id or "",
                model_config_id=current_profile.model_config_id or current_run.model_config_id,
                options=ConversationRunOptions(reasoning_effort=reasoning_effort),
            )
            # 与用户发送同一语义：子 Run 仍活动时消息并入它，已终态时新开 Run，不会并发执行。
            # 子 Agent 配置在子 Run 运行期间变了时，消息只能交给下一个 Run（运行期配置锁定）。
            # 只入队，由子 Task 的 actor 推进（新开 Run 或并入），不等子 Run 结束。
            task_runtime_spaces.enqueue(
                child_task_id,
                get_user_input_service().prepare(
                    child_task_id,
                    ConversationRunCommand(display_text=message),
                    launch.model_config_id,
                    reasoning_effort,
                    launch.agent_id,
                    delivery="immediate" if matches_run(launch, current_run) else "next_run",
                ),
            )
        except Exception as exc:
            log.exception(
                "child_agent_send_failed",
                extra={
                    "msg": "向子任务投递追加输入失败",
                    "data": {
                        "parent_run_id": execution_context.run_id,
                        "child_task_id": child_task_id,
                        "error_type": type(exc).__name__,
                    },
                },
            )
            return tool_error(
                self.name,
                "The follow-up message could not be delivered to the child agent.",
            )

        return tool_success(
            tool_name=self.name,
            content=json.dumps(
                {
                    "child_task_id": child_task_id,
                    "child_agent_id": current_run.agent_id,
                    "agent_name": child_task.title,
                    "status": "submitted",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            display_data=build_child_agent_result_display_data(
                operation="send",
                child_task_id=child_task_id,
                status="running",
                agent_id=current_run.agent_id,
                agent_name=child_task.title,
                final_output=None,
                end_reason=None,
            ),
        )

    def to_definition(self) -> ToolDefinition:
        """构建 child_agent_send 工具的注册定义。

        参数:
            无。

        返回:
            线程直跑的 child_agent_send 工具定义；工具观察只确认新 Run 已登记，
            不等待子 Agent 执行结束。

        异常:
            无。

        副作用:
            无。
        """

        return ToolDefinition(
            name=self.name,
            group=self.group,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            execution_mode="thread",
            display=ToolDisplayHints(
                verb="发送给子 Agent",
                icon="send",
                surface="standalone",
                expandable=False,
                expand_layout="details",
                show_result=False,
            ),
        )


def build_child_agent_send_definition() -> ToolDefinition:
    """构建 child_agent_send 工具定义。

    参数:
        无。

    返回:
        可直接注册到工具注册表的 child_agent_send 工具定义。

    异常:
        无。

    副作用:
        创建一个 ``ChildAgentSendTool`` 实例（构造期取得四个 service 单例）。
    """

    return ChildAgentSendTool().to_definition()
