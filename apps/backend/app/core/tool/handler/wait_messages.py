"""wait_messages 工具：Agent 主动挂起，等委派的子 Agent 或后台任务的结论以消息送达。

取代轮询式等待：调用当步闭合并带 ``suspend``，Run 随即挂起、不占执行者；子 Agent 每次 Run 到
终态、后台任务结束、用户发来消息时都会唤醒同一个 Run，结论以 ``<agent_result>`` /
``<background_result>`` 消息并入。是否继续等由模型决定（再次调用本工具）。
"""

from typing import ClassVar

from app.core.tool.schemas import (
    TOOL_WAIT_MESSAGES,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.results.tool_success import tool_success
from app.core.tool.tool_grouping import TOOL_GROUP_CHILD_AGENT
from app.core.tool.tool_base import HandlerBase
from app.core.tool.handler.wait.wait_messages_args import WaitMessagesArgs
from app.models.enums.agent_team_run_status import AgentTeamRunStatus
from app.models.enums.conversation_run_status import ACTIVE_RUN_STATUSES
from app.service.depends import get_task_service
from app.storage.crud.agent_team_run_crud import AgentTeamRunCrud


class WaitMessagesTool(HandlerBase):
    """挂起当前 Agent 直到有消息唤醒；没有任何未完成工作时不挂起，避免无人唤醒的永久挂起。"""

    name: str = TOOL_WAIT_MESSAGES
    description: str = (
        "Pause yourself until a result arrives, instead of polling. Use it after delegating to "
        "child agents or after an Agent Team was approved: each time a child agent finishes a "
        "run, a background task ends, or the user sends a message, you resume and the result "
        "arrives as an <agent_result> or <background_result> message. Results arrive one at a "
        "time; call this tool again if you still need to wait for others. If nothing is "
        "pending, it returns immediately without pausing."
    )
    args_model: type[WaitMessagesArgs] = WaitMessagesArgs
    timeout_seconds: ClassVar[float] = 10.0
    group = TOOL_GROUP_CHILD_AGENT

    def execute(self, execution_context: ToolExecutionContext) -> ToolObservation:
        pending = self._pending_work(execution_context.task_id)
        if not pending:
            return tool_success(
                tool_name=self.name,
                content=(
                    "Nothing is pending: no child agent or background task is still running, so "
                    "you were not paused. Results that already arrived are in the conversation."
                ),
            )
        return tool_success(
            tool_name=self.name,
            content=f"Paused until a result arrives. Still running: {', '.join(pending)}.",
            suspend=True,
        )

    @staticmethod
    def _pending_work(task_id: int) -> list[str]:
        """仍可能回投结论的工作：活动中的委派子 Task 与本 Task 发起的待确认 / 运行中 TeamRun。"""
        tasks = get_task_service()
        pending = [
            f"child_task:{child.id}"
            for child in tasks.list_child_tasks(task_id)
            if (latest := tasks.get_latest_run(child.id)) is not None
            and latest.status in ACTIVE_RUN_STATUSES
        ]
        team_runs = AgentTeamRunCrud()
        pending.extend(
            f"agent_team_run:{row.id}"
            for row in (*team_runs.list_pending_confirmations(), *team_runs.list_active())
            if row.parent_task_id == task_id
            and row.status in {AgentTeamRunStatus.PENDING.value, AgentTeamRunStatus.RUNNING.value}
        )
        return pending

    def to_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            group=self.group,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            execution_mode="thread",
            parallel_mode="serial",
            display=ToolDisplayHints(
                verb="等待结果",
                icon="clock",
                surface="standalone",
                expandable=False,
                expand_layout="none",
                show_result=False,
            ),
        )


def build_wait_messages_definition() -> ToolDefinition:
    return WaitMessagesTool().to_definition()
