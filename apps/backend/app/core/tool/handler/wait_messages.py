"""wait_messages 工具：Agent 主动挂起，等后台工作的结论以消息送达。

可等待的工作只以后台结果登记表 ``background_registry`` 为准（委派子 Agent、``execute_terminal`` 的
后台命令、批准后执行的 Agent Team）。

取代轮询式等待：调用当步闭合并带 ``suspend``，Run 随即挂起、不占执行者；后台工作每到一个结论、
用户发来消息时都会唤醒同一个 Run，结论统一以 ``<background_result>`` 消息并入（``tool_name``
标明来源）。是否继续等由模型决定（再次调用本工具）。没有任何未完成工作时返回错误、不挂起：挂起会无人唤醒，
返回成功又会让模型以为自己在等。
"""

from typing import ClassVar

from app.core.tool.background.backgraound_registry import background_registry
from app.core.tool.handler.wait.wait_messages_args import WaitMessagesArgs
from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    TOOL_WAIT_MESSAGES,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.tool_base import HandlerBase
from app.core.tool.tool_grouping import TOOL_GROUP_CHILD_AGENT


class WaitMessagesTool(HandlerBase):
    """挂起当前 Agent 直到有消息唤醒；没有任何未完成工作时报错、不挂起，避免无人唤醒的永久挂起。"""

    name: str = TOOL_WAIT_MESSAGES
    description: str = (
        "Pause yourself until a result arrives, instead of polling. Use it while background "
        "work is pending: delegated child agents, forks, approved agent teams or background terminal commands. "
        "When such work finishes, "
        "another agent messages you, or an immediate input arrives, you resume; results arrive as "
        "<background_result> messages, one at a time, so call this tool again if you still need "
        "to wait for others. If nothing is pending, it fails immediately without pausing."
    )
    args_model: type[WaitMessagesArgs] = WaitMessagesArgs
    timeout_seconds: ClassVar[float] = 10.0
    group = TOOL_GROUP_CHILD_AGENT

    def execute(self, execution_context: ToolExecutionContext) -> ToolObservation:
        # 标签由登记方给出，原样展示给模型。
        pending = background_registry.pending(execution_context.task_id, execution_context.run_id)
        if not pending:
            return tool_error(
                self.name,
                (
                    "nothing to wait for: no background work is still running, "
                    "so you were not paused. Results that already arrived "
                    "are in the conversation or will appear before your next step; do not call "
                    "wait_messages again unless you start new background work."
                ),
                tool_call_id=execution_context.tool_call_id,
                status_hint="无可等待",
            )
        return tool_success(
            tool_name=self.name,
            content=f"Paused until a result arrives. Still running: {', '.join(pending)}.",
            suspend=True,
        )


    def to_definition(self) -> ToolDefinition:
        return ToolDefinition(
            conversation_presentation="quiet",
            name=self.name,
            group=self.group,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            execution_mode="thread",
            parallel_mode="serial",
        )


def build_wait_messages_definition() -> ToolDefinition:
    return WaitMessagesTool().to_definition()
