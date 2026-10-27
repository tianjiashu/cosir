"""wait_messages 工具：Agent 主动挂起，等委派的子 Agent 的结论以消息送达。

Agent Team 不在等待范围内：``agent_team`` 工具自己阻塞到 Team 落定，结论就是它的工具结果。

取代轮询式等待：调用当步闭合并带 ``suspend``，Run 随即挂起、不占执行者；子 Agent 每次 Run 到
终态、用户发来消息时都会唤醒同一个 Run，结论统一以 ``<background_result>``
消息并入（``tool_name`` 标明来源）。是否继续等由模型决定（再次调用本工具）。
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
from app.models.enums.conversation_run_status import ACTIVE_RUN_STATUSES
from app.service.depends import get_task_service


class WaitMessagesTool(HandlerBase):
    """挂起当前 Agent 直到有消息唤醒；没有任何未完成工作时不挂起，避免无人唤醒的永久挂起。"""

    name: str = TOOL_WAIT_MESSAGES
    description: str = (
        "Pause yourself until a result arrives, instead of polling. Use it after delegating to "
        "child agents: each time a child agent finishes a "
        "run or the user sends a message, you resume and the result "
        "arrives as a <background_result> message. Results arrive one at a "
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
                    "Nothing is pending: no child agent is still running, so "
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
        """仍可能回投结论的工作：活动中的委派子 Task。"""
        tasks = get_task_service()
        return [
            f"child_task:{child.id}"
            for child in tasks.list_child_tasks(task_id)
            if (latest := tasks.require_current_run(child.id)) is not None
               and latest.status in ACTIVE_RUN_STATUSES
        ]

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
