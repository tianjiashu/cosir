"""session_send：向同工作区的另一 Session 投递 Agent 消息；调度由收件方的 actor 决定。

消息体的构造（``prepare_agent_message``）与委派、派生汇报共用，留在 ``core.tool.multi_agent``。
"""

import json
from typing import cast

from app.config.logging.logger import log
from app.core.agents.agent_profile import AgentProfileConfigError, AgentProfileType
from app.core.tool.multi_agent.agent_message import prepare_agent_message
from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    TOOL_SESSION_SEND,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.schemas.tool_grouping import TOOL_GROUP_CHILD_AGENT
from app.core.tool.session_management.addressing import find_recipient
from app.core.tool.session_management.args.session_send_args import SessionSendArgs
from app.core.tool.tool_base import HandlerBase
from app.core.workflow.message_queue.pending_messages import PendingMessages
from app.core.workflow.message_queue.session_message import LaunchInputBody, RunLaunch
from app.models.errors.input_errors import UserInputRejectedError
from app.models.message_context.conversation_input import MessageSource
from app.models.run.run_record import RunRecord
from app.models.run.run_status import RunStatus
from app.models.session.session_kind import SessionType
from app.models.session.session_record import SessionRecord
from app.service.depends import (
    get_session_message_queue,
    get_session_service,
    get_workspace_service,
)
from app.session_runtime.session_runtime_space_registry import session_runtime_spaces


class SessionSendTool(HandlerBase):
    """投递成功只表示消息入队；回信须另发一条消息。"""

    name: str = TOOL_SESSION_SEND
    # 对照 Claude Code 的 SendMessage 补了三点：收件方看不到你的对话；别追问「做完没」；别让别的会话
    # 替你做自己被拒的事——可写根收窄后，另一个会话的可写区可能更大，转托就绕过了限制。
    description: str = (
        "Send a message to another session in this workspace using its session_id. The message is "
        "delivered once and sending does not wait; delivery means it reached that session, not "
        "that it was read or agreed to. The result says what the message does there: read before "
        "its next step if it is running, wake it if it is paused, or start a new run if it is idle.\n"
        "- The recipient does not see your conversation, and your plain replies never reach it: "
        "make the message self-contained, and point at files by path instead of pasting them.\n"
        "- A session you derived (a delegated child agent or a fork) reports back to you "
        "automatically when it finishes the run your message starts; other sessions do not, so "
        "reply explicitly with session_send to the source_session_id of an <agent_message> that "
        "is not a report.\n"
        "- Do not send 'are you done?' messages: wait for the report with wait_messages, or check "
        "a session you derived with session_read.\n"
        "- Never ask another session to do what your own tools refused, such as writing outside "
        "your writable root; tell the user instead."
    )
    args_model: type[SessionSendArgs] = SessionSendArgs
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
        """投递一条 immediate 消息，运行中、挂起与空闲的去向统一由收件方决定；结果按投递那一刻的 Run 状态
        告诉发送方消息的去向（对照 Claude Code 的 delivered / queued：只说「送达」时，发送方分不清对方是
        顺手看到、被唤醒还是为它开了新 Run）。

        去向在 Session 闸门内判定、与入队同一时刻：之后对方怎么推进归它的 actor（``Action.decide``）。
        少见的例外不在这里兜：对方正在被停止时，消息会在停止落定后开新 Run；发起配置的模型已被删时，消息
        留在队列里等用户换模型（见 ``SessionActor``）。
        """
        target = find_recipient(self.name, session_id, execution_context)
        if isinstance(target, ToolObservation):
            return target
        sender = self._session_service.get_session(execution_context.session_id)
        if self._outranks_sender(target, execution_context):
            return tool_error(
                self.name,
                f"Session {session_id} has broader terminal permissions than yours, so it cannot take "
                "messages from you. Ask the user if that session should do this.",
            )
        try:
            with session_runtime_spaces.get_or_create(session_id).operation():
                current = self._session_service.require_current_run(target.id)
                launch = self._target_launch(target, current, execution_context)
                if isinstance(launch, ToolObservation):
                    return launch
                session_runtime_spaces.enqueue(
                    session_id,
                    prepare_agent_message(
                        message,
                        source=MessageSource(
                            session_id=sender.id,
                            name=sender.title,
                            from_deriver=target.derived_by(sender.id),
                        ),
                        launch=launch,
                    ),
                )
        except KeyError:
            # 寻址之后、闸门之内对方被删除。
            return tool_error(self.name, f"Target session {session_id} is unavailable.")
        except UserInputRejectedError as exc:
            return tool_error(self.name, exc.message)
        return tool_success(
            tool_name=self.name,
            content=json.dumps(
                {"session_id": session_id, "status": "delivered", **self._effect(current)},
                separators=(",", ":"),
            ),
            display_data={
                "kind": "agent-session-result",
                "operation": "send",
                "session_id": session_id,
                "agent_name": target.title,
            },
        )

    def _outranks_sender(self, target: SessionRecord, context: ToolExecutionContext) -> bool:
        """收件方的终端权限比发送方宽，且不在同一棵所有权树里：拒绝，免得借它之手越过自己的权限。

        同一棵树里（派生方与它的子 Agent）不受限：子 Agent 的有效权限从不超过祖先，用户对整棵树给的是
        根会话的权限；派生汇报本来也不走这里。
        """
        sessions = self._session_service
        if sessions.ownership_root_id(target.id) == sessions.ownership_root_id(context.session_id):
            return False
        return (
            context.terminal_permission_mode == "request_approval"
            and sessions.effective_terminal_mode(target.id) == "full_access"
        )

    @staticmethod
    def _effect(current: RunRecord | None) -> dict[str, str]:
        """消息在收件方那里的去向：immediate 的 Agent 消息并入运行中的驱动、唤醒挂起的 Run，空闲时开新 Run。"""
        if current is not None and current.status == RunStatus.RUNNING.value:
            return {"recipient": "running", "effect": "it reads your message before its next step"}
        if current is not None and current.status == RunStatus.SUSPENDED.value:
            return {"recipient": "paused", "effect": "your message wakes it"}
        return {"recipient": "idle", "effect": "your message starts a new run"}

    def _target_launch(
        self,
        target: SessionRecord,
        current: RunRecord | None,
        execution_context: ToolExecutionContext,
    ) -> RunLaunch | ToolObservation:
        """只读收件方配置。首 Run 尚未建立时，继承 actor 将选中的启动输入。"""
        if current is None:
            first = PendingMessages.of(get_session_message_queue().pending_nowait(target.id)).next_input()
            if first is not None:
                launch = cast(LaunchInputBody, first.body).launch
                return launch
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
                "session_send_profile_unavailable",
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
            reasoning_effort=reasoning_effort,
        )

    def to_definition(self) -> ToolDefinition:
        """线程直跑：工具观察只确认消息已入队。"""
        return ToolDefinition(
            name=self.name,
            group=self.group,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
            # 同工作区 Session 间的消息是少数协作场景才用，延迟加载。
            deferred=True,
        )


def build_session_send_definition() -> ToolDefinition:
    """供工具注册表登记的 session_send 定义。"""
    return SessionSendTool().to_definition()
