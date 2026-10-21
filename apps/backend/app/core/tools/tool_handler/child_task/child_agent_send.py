"""child_agent_send 工具：给已有子任务追加输入并启动新的 Run。"""
import asyncio
import json
from typing import ClassVar

from app.config.logging.logger import log
from app.core.agents.agent_profile import AgentProfileConfigError, AgentProfileType
from app.core.tools.display.child_agent_display import build_child_agent_result_display_data
from app.core.tools.schemas import (
    TOOL_CHILD_AGENT_SEND,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_grouping import TOOL_GROUP_CHILD_AGENT
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models.child_task import ChildAgentSendArgs
from app.models import ConversationRunCommand
from app.service.conversation_run.workflow_session import SessionConflictError
from app.service.depends import (
    get_conversation_run_service,
    get_task_service,
    get_workflow_session,
    get_workspace_service,
)


class ChildAgentSendTool(HandlerBase):
    """给已有子任务追加一条输入，并经调度入口为它新开一个后台 Run。

    职责边界：本类只解析 child 运行参数，建 Run 与启动交给 ``WorkflowSession.open_run``——
    闸门内判定子 Task 是否仍有活动 Run（含正在收尾的执行），因此不会与上一轮或其交接并发。
    子 Agent 的运行终态由它自己的 workflow 落定。
    """

    name: str = TOOL_CHILD_AGENT_SEND
    description: str = (
        "Send a message to a child agent after confirming that it has already produced a "
        "conclusion in its child task, reusing that child task. Note: if the child agent has "
        "not reached a terminal conclusion yet, this tool fails."
    )
    args_model: type[ChildAgentSendArgs] = ChildAgentSendArgs
    timeout_seconds: ClassVar[float] = 30.0
    group = TOOL_GROUP_CHILD_AGENT

    def __init__(self) -> None:
        """装配任务、工作区与 Run 读取服务。"""

        self._task_service = get_task_service()
        self._workspace_service = get_workspace_service()
        self._run_service = get_conversation_run_service()

    def execute(
        self,
        child_task_id: int,
        message: str,
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """为指定子任务创建一个新 Run 并启动后台执行。

        参数:
            child_task_id: 目标子任务标识（``delegate_task`` 返回的 ``child_task_id``）。
            message: 追加给子 Agent 的自由文本输入；原样作为新 Run 的输入。
            execution_context: 父工具执行边界，提供事件循环与父 Run 事实。

        返回:
            成功时返回包含 ``child_task_id`` / ``child_run_id`` / ``status`` 的 JSON 文本；
            父事件循环不可用、子任务不存在、无法确定 child Agent、子 Agent
            仍未结束、建 Run 或启动失败时返回错误观察。

        异常:
            无。配置无效、profile 已不可用、调度冲突或启动异常均归一化为错误观察；profile
            校验失败时不创建 Run。

        副作用:
            在父 Run 的事件循环上经调度入口为子任务新开 running Run 并启动；不等待子 Agent
            执行结束。
        """

        try:
            # 存在性校验：子任务不存在时直接返回确定性错误，不再继续创建 Run。
            child_task = self._task_service.get_task(child_task_id)
        except KeyError:
            return tool_error(
                self.name,
                f"child task not found: {child_task_id}",
                reason=(
                    "verify child_task_id against the value returned by delegate_task; "
                    "this task does not exist."
                ),
                retryable=True,
            )
        if not child_task.is_child or child_task.parent_task_id != execution_context.task_id:
            return tool_error(
                tool_name=self.name,
                error="child_task_not_delegated_by_caller",
                reason=(
                    "child_task_id does not refer to a child task delegated by this task; "
                    "use the child_task_id returned by delegate_task, or "
                    "delegate the subtask first and retry with the corrected child_task_id."
                ),
                retryable=True,
            )

        current_run = self._run_service.get_run(child_task.current_run_id)

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
                "child_agent_send_profile_unavailable",
                reason="The child agent configuration for this workspace is invalid.",
                retryable=False,
            )
        if current_profile is None or current_profile.agent_type is not AgentProfileType.CHILD:
            return tool_error(
                self.name,
                "child_agent_send_profile_unavailable",
                reason="The child agent is no longer available in this workspace.",
                retryable=False,
            )

        loop = execution_context.event_loop
        if loop is None or loop.is_closed():
            return tool_error(
                self.name,
                "delegate_task_runtime_unavailable",
                reason=(
                    "the parent runtime event loop is unavailable, so the child run "
                    "cannot be started; retry from a normal turn."
                ),
                retryable=False,
            )
        reasoning_effort = (
            current_profile.model_settings.reasoning_effort
            if current_profile.model_settings is not None
            else None
        )
        try:
            # 经调度入口在 Task 闸门内开 Run：子 Run 仍在运行或收尾时由闸门拒绝，不会并发执行。
            child_run = asyncio.run_coroutine_threadsafe(
                get_workflow_session().open_run(
                    child_task_id,
                    ConversationRunCommand(display_text=message),
                    agent_id=current_run.agent_id or "",
                    model_config_id=current_profile.model_config_id or current_run.model_config_id,
                    reasoning_effort=reasoning_effort,
                ),
                loop,
            ).result()
        except SessionConflictError:
            return tool_error(
                tool_name=self.name,
                error="error",
                reason=(
                    "the child agent is still running; inspect it with child_agent_status or "
                    "wait for its conclusion with child_agent_wait, and call this tool again "
                    "only after it reaches a terminal state."
                ),
            )
        except Exception as exc:
            log.exception(
                "child_agent_send_failed",
                extra={
                    "msg": "为子任务创建 Run 或启动后台执行失败",
                    "data": {
                        "parent_run_id": execution_context.run_id,
                        "child_task_id": child_task_id,
                        "error_type": type(exc).__name__,
                    },
                },
            )
            return tool_error(
                self.name,
                "child_agent_send_failed",
                reason="the follow-up run could not be started; ",
                retryable=False,
            )

        return tool_success(
            tool_name=self.name,
            content=json.dumps(
                {
                    "child_task_id": child_task_id,
                    "child_run_id": child_run.id,
                    "child_agent_id": current_run.agent_id,
                    "agent_name": child_task.title,
                    "status": "running",
                },
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            display_data=build_child_agent_result_display_data(
                operation="send",
                child_task_id=child_task_id,
                child_run_id=child_run.id,
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
