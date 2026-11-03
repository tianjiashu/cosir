"""send_message 工具：派生关系两端的 Agent 互相发消息（委派子 Agent、Agent fork 与它们的发起方）。

方向按双方关系定，模型只给对方的 Task id：

- **向下**（对方是自己派生的 Task）：对方还有活动 Run 时并入（运行中下一步读到、挂起时被唤醒），
  结论由已有的等待回投；已到终态时为它新开 Run，并以本次调用登记等待，结论按它回投
  （``child_run_job.run_child``）。对方正在等自己回复时，这条消息就是回复：直接交付给对方提问的
  那几次调用（以 ``background_result`` 回到对方），不另投 Agent 消息，免得同一句话送达两次。
- **向上**（对方是派生自己的那一方）：以 Agent 消息投递；对方挂起时被唤醒，空闲时按它当前 Run 的
  发起配置开新 Run（与「有消息就处理」一致）。``expect_reply`` 时以本次调用在自己的 Task 里登记一条
  ``agent_reply`` 等待，Agent 随后用 ``wait_messages`` 挂起，等对方回复交付它。

Team 节点不在派生关系里（``TaskRecord.deriver_id``），与主 Agent 之间不能互发。不会互等死锁：下级的
提问是 Agent 消息，会唤醒挂起中的上级；上级之后的发送就是回复，交付即唤醒下级。
"""

import json
from typing import ClassVar, cast

from app.config.logging.logger import log
from app.core.agents.agent_profile import AgentProfileConfigError, AgentProfileType
from app.core.tool.background.job import BackgroundConclusion
from app.core.tool.background.registry import background_registry
from app.core.tool.mult_agent.args.send_message_args import SendMessageArgs
from app.core.tool.mult_agent.child_agent.child_run_job import run_child
from app.core.tool.mult_agent.task_status import MessageDirection, TaskStatusTool
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
from app.core.workflow.message_queue.task_message import RunLaunch, TaskMessage
from app.models import ConversationRunRecord, TaskRecord
from app.models.conversation_input import MessageSource
from app.models.conversation_run_options import ConversationRunOptions
from app.models.enums.conversation_run_status import ACTIVE_RUN_STATUSES
from app.service.conversation_run.agent_message import prepare_agent_message
from app.service.depends import (
    get_conversation_run_ledger,
    get_task_service,
    get_workspace_service,
)
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces


class SendMessageTool(HandlerBase):
    """在派生关系两端之间投递一条消息；向下的新 Run 与向上的提问在后台等结论，不阻塞工具线程。"""

    name: str = TOOL_SEND_MESSAGE
    description: str = (
        "Send a message to a task you started (a delegated child agent or a fork) or to the "
        "agent that started you. To a task you started: if it is still running or waiting, the "
        "message is merged into its current run (status \"merged\"); if it has finished, a new "
        "run starts and its conclusion comes back to you (status \"submitted\"); if it asked you "
        "something and is waiting, this message is your answer (status \"replied\"). To the "
        "agent that started you: use it for something it needs to know before you finish, or "
        "set expect_reply when you need its decision to go on, then call wait_messages; its "
        "answer arrives as a <background_result> for this call. Conclusions and replies arrive "
        "as <background_result> messages."
    )
    args_model: type[SendMessageArgs] = SendMessageArgs
    timeout_seconds: ClassVar[float] = 30.0
    group = TOOL_GROUP_CHILD_AGENT

    def __init__(self) -> None:
        self._task_service = get_task_service()
        self._workspace_service = get_workspace_service()
        self._ledger = get_conversation_run_ledger()

    def execute(
            self,
            task_id: int,
            message: str,
            execution_context: ToolExecutionContext,
            expect_reply: bool | None = None,
    ) -> ToolObservation:
        """按双方关系投递消息；对方不存在、不在派生关系里或运行时不可用时返回错误观察，不向上抛。"""
        try:
            target = self._task_service.get_task(task_id)
        except KeyError:
            return tool_error(
                self.name,
                f"task not found: {task_id}. Use a child_task_id / fork_task_id you received, or "
                "the source_task_id of an <agent_message> sent to you.",
            )
        loop = execution_context.event_loop
        if loop is None or loop.is_closed():
            return tool_error(
                self.name,
                "The parent runtime event loop is unavailable, so the message cannot be "
                "delivered; retry from a normal turn.",
            )
        sender = self._task_service.get_task(execution_context.task_id)
        try:
            if target.derived_by(sender.id):
                return self._send_down(sender, target, message, execution_context)
            if sender.deriver_id() == target.id:
                return self._send_up(sender, target, message, bool(expect_reply), execution_context)
        except Exception as exc:
            log.exception(
                "send_message_failed",
                extra={
                    "msg": "投递 Agent 消息失败",
                    "data": {
                        "run_id": execution_context.run_id,
                        "target_task_id": task_id,
                        "error_type": type(exc).__name__,
                    },
                },
            )
            return tool_error(self.name, "The message could not be delivered.")
        return tool_error(
            self.name,
            "task_id is neither a task you started nor the agent that started you; you can only "
            "message the child_task_id / fork_task_id you received or the source_task_id of an "
            "<agent_message> sent to you.",
        )

    def _send_down(
            self,
            sender: TaskRecord,
            target: TaskRecord,
            message: str,
            execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """发给自己派生的 Task：先当作对它提问的回复，否则并入活动 Run 或新开 Run 并登记等待。"""
        reply = BackgroundConclusion(
            {"from_task_id": sender.id, "from": sender.title, "reply": message}
        )
        answered = [
            tool_call_id
            for tool_call_id in background_registry.reply_waits(target.id, awaiting=sender.id)
            if background_registry.complete(tool_call_id, reply)
        ]
        if answered:
            log.info(
                "send_message_replied",
                extra={
                    "msg": "回复派生 Task 的提问",
                    "data": {"task_id": sender.id, "target_task_id": target.id, "calls": answered},
                },
            )
            return self._observation("replied", "down", target)

        current_run = self._ledger.get_run(cast(int, target.current_run_id))
        launch = (
            self._fork_launch(current_run)
            if target.task_type == "fork"
            else self._child_profile_launch(target, current_run, execution_context)
        )
        if isinstance(launch, ToolObservation):
            return launch
        # Agent 消息一律 immediate：目标 Run 活动（运行中或挂起）时并入或唤醒它，已终态时按这份发起
        # 配置新开 Run；并入活动 Run 时不看配置（与用户发送同一条「入队不校验配置」）。
        agent_message = self._agent_message(message, sender, launch)

        def enqueue() -> None:
            task_runtime_spaces.enqueue(target.id, agent_message)

        if current_run.status in ACTIVE_RUN_STATUSES:
            # 并入活动 Run 不另登记：已有的等待要等对方无事可做，结论落在它读到的最新 Run 上。
            enqueue()
            return self._observation("merged", "down", target)
        return run_child(
            execution_context,
            target.id,
            label=target.title,
            enqueue=enqueue,
            placeholder=lambda: self._observation("submitted", "down", target),
        )

    def _send_up(
            self,
            sender: TaskRecord,
            target: TaskRecord,
            message: str,
            expect_reply: bool,
            execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """发给派生自己的那一方；``expect_reply`` 时先登记等回复，再投递（回复可能很快就到）。"""
        launch = RunLaunch.from_run(self._ledger.get_run(cast(int, target.current_run_id)))
        if launch is None:
            return tool_error(
                self.name,
                "The agent that started you can no longer run (its model configuration is gone); "
                "put this in your final report instead.",
            )
        agent_message = self._agent_message(message, sender, launch)
        if not expect_reply:
            task_runtime_spaces.enqueue(target.id, agent_message)
            return self._observation("delivered", "up", target)

        tool_call_id = execution_context.tool_call_id
        # 等的是对方的一句回复，没有要停的工作：撤销（本 Task 停止）只是不再等。
        work = background_registry.register(
            tool_call_id,
            task_id=sender.id,
            run_id=execution_context.run_id,
            kind="agent_reply",
            label=target.title,
            on_cancel=lambda: None,
            target_task_id=target.id,
        )
        try:
            task_runtime_spaces.enqueue(target.id, agent_message)
        except Exception:
            background_registry.discard(tool_call_id)
            raise
        observation = self._observation("awaiting_reply", "up", target)
        observation.background = work
        return observation

    @staticmethod
    def _agent_message(message: str, sender: TaskRecord, launch: RunLaunch) -> TaskMessage:
        return prepare_agent_message(
            message,
            source=MessageSource(task_id=sender.id, name=sender.title),
            agent_id=launch.agent_id,
            model_config_id=launch.model_config_id,
            reasoning_effort=launch.options.reasoning_effort,
            disabled_tool_groups=launch.options.disabled_tool_groups,
        )

    def _observation(
            self, status: str, direction: MessageDirection, target: TaskRecord
    ) -> ToolObservation:
        return tool_success(
            tool_name=self.name,
            content=json.dumps(
                {"task_id": target.id, "name": target.title, "status": status},
                ensure_ascii=False,
                separators=(",", ":"),
            ),
            display_data=TaskStatusTool._agent_task_display_data(
                operation="send",
                direction=direction,
                task_id=target.id,
                agent_name=target.title,
                final_output=None,
                end_reason=None,
            ),
        )

    def _fork_launch(self, current_run: ConversationRunRecord) -> RunLaunch | ToolObservation:
        """分叉不换身份：新 Run 沿用它当前 Run 冻结的发起配置（最初来自发起分叉的那个 Run）。"""
        launch = RunLaunch.from_run(current_run)
        if launch is None:
            return tool_error(
                self.name,
                "The forked conversation's model configuration no longer exists, so it cannot "
                "start a new run; do the work yourself instead.",
            )
        return launch

    def _child_profile_launch(
            self,
            child_task: TaskRecord,
            current_run: ConversationRunRecord,
            execution_context: ToolExecutionContext,
    ) -> RunLaunch | ToolObservation:
        """委派子 Agent 的发起配置每次按当前 profile 重算：profile 改了，下一个新 Run 就生效。"""
        try:
            workspace = self._workspace_service.get_workspace(child_task.workspace_id)
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
                        "child_task_id": child_task.id,
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
                            or cast(int, current_run.model_config_id),
            options=ConversationRunOptions(reasoning_effort=reasoning_effort),
        )

    def to_definition(self) -> ToolDefinition:
        """线程直跑：工具观察只确认已投递或已登记等待，不等对方执行结束。"""
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
