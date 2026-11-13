"""send_message 只负责向同工作区的另一 Session 投递 Agent 消息；调度由收件方的 actor 决定。"""

import json
from typing import ClassVar, cast

from app.config.logging.logger import log
from app.core.agents.agent_profile import AgentProfileConfigError, AgentProfileType
from app.core.tool.multi_agent.agent_message import prepare_agent_message
from app.core.tool.multi_agent.args.send_message_args import SendMessageArgs
from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    TOOL_SEND_MESSAGE,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.tool_base import HandlerBase
from app.core.tool.tool_grouping import TOOL_GROUP_CHILD_AGENT
from app.core.workflow.message_queue.dispatch_policy import next_input
from app.core.workflow.message_queue.session_message import LaunchInputBody, RunLaunch
from app.models import RunRecord, SessionRecord
from app.models.conversation_input import MessageSource
from app.models.run_options import RunOptions
from app.models.errors.input_errors import UserInputRejectedError
from app.models.session_kind import SessionType
from app.service.depends import (
    get_session_message_queue,
    get_session_service,
    get_workspace_service,
)
from app.session_runtime.session_runtime_space_registry import session_runtime_spaces


class SendMessageTool(HandlerBase):
    """投递成功只表示消息入队；回信须另发一条消息。"""

    name: str = TOOL_SEND_MESSAGE
    description: str = (
        "Send a message to another session in this workspace using its session_id. The message is "
        "delivered once and sending does not wait. A session you derived (a delegated child agent or "
        "a fork) reports back to you automatically when it finishes the run your message starts; "
        "other sessions do not, so reply explicitly with send_message to the source_session_id of an "
        "<agent_message> that is not a report."
    )
    args_model: type[SendMessageArgs] = SendMessageArgs
    timeout_seconds: ClassVar[float] = 30.0
    group = TOOL_GROUP_CHILD_AGENT

    def __init__(self) -> None:
        self._session_service = get_session_service()
        self._workspace_service = get_workspace_service()

    def execute(
        self,
        session_id: int,
        message: str,
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """投递一条 immediate 消息，运行中、挂起与空闲的去向统一由收件方决定。"""
        sender = self._session_service.get_session(execution_context.session_id)
        try:
            target = self._session_service.get_session(session_id)
            if target.id == sender.id or target.workspace_id != sender.workspace_id:
                return tool_error(self.name, "Choose another session in this workspace.")
            with session_runtime_spaces.get_or_create(session_id).operation():
                launch = self._target_launch(target, execution_context)
                if isinstance(launch, ToolObservation):
                    return launch
                session_runtime_spaces.enqueue(
                    session_id,
                    prepare_agent_message(
                        message,
                        source=MessageSource(session_id=sender.id, name=sender.title),
                        agent_id=launch.agent_id,
                        model_config_id=launch.model_config_id,
                        reasoning_effort=launch.options.reasoning_effort,
                        disabled_tool_groups=launch.options.disabled_tool_groups,
                    ),
                )
        except KeyError:
            return tool_error(self.name, f"Target session {session_id} is unavailable.")
        except UserInputRejectedError as exc:
            return tool_error(self.name, exc.message)
        return tool_success(
            tool_name=self.name,
            content=json.dumps({"session_id": session_id, "status": "delivered"}, separators=(",", ":")),
            display_data={
                "kind": "agent-session-result",
                "operation": "send",
                "session_id": session_id,
                "agent_name": target.title,
            },
        )

    def _target_launch(
        self, target: SessionRecord, execution_context: ToolExecutionContext
    ) -> RunLaunch | ToolObservation:
        """只读收件方配置。首 Run 尚未建立时，继承 actor 将选中的启动输入。"""
        current = self._session_service.require_current_run(target.id)
        if current is None:
            first = next_input(get_session_message_queue().pending_nowait(target.id))
            if first is not None:
                return cast(LaunchInputBody, first.body).launch
            initial = RunLaunch.from_fork(target)
            if initial is not None:
                return initial
            return tool_error(self.name, "The target session has no launch configuration yet.")
        if target.session_type == SessionType.DELEGATED:
            return self._child_profile_launch(target, current, execution_context)
        launch = RunLaunch.from_run(current)
        if launch is None:
            return tool_error(self.name, "The target session's model configuration is unavailable.")
        return launch

    def _child_profile_launch(
        self,
        child_session: SessionRecord,
        current_run: RunRecord,
        execution_context: ToolExecutionContext,
    ) -> RunLaunch | ToolObservation:
        """委派子 Agent 的发起配置每次按当前 profile 重算：profile 改了，下一个新 Run 就生效。"""
        try:
            workspace = self._workspace_service.get_workspace(child_session.workspace_id)
            from app.config.configuration import get_agent_registry

            current_profile = get_agent_registry().resolve(
                workspace.root_path,
                current_run.agent_id or "",
            )
        except (AgentProfileConfigError, KeyError, RuntimeError) as exc:
            log.warning(
                "send_message_profile_unavailable",
                extra={
                    "msg": "workspace 子 Agent 配置无效，拒绝创建 follow-up Run",
                    "data": {
                        "parent_run_id": execution_context.run_id,
                        "child_session_id": child_session.id,
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
        reasoning_effort = (
            current_profile.model_settings.reasoning_effort
            if current_profile.model_settings is not None
            else None
        )
        return RunLaunch(
            agent_id=current_run.agent_id or "",
            model_config_id=current_profile.model_config_id
            or cast(str, current_run.model_config_id),
            options=RunOptions(reasoning_effort=reasoning_effort),
        )

    def to_definition(self) -> ToolDefinition:
        """线程直跑：工具观察只确认消息已入队。"""
        return ToolDefinition(
            name=self.name,
            group=self.group,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            execution_mode="thread",
        )


def build_send_message_definition() -> ToolDefinition:
    """供工具注册表登记的 send_message 定义。"""
    return SendMessageTool().to_definition()
