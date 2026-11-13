"""wait_messages 工具：Agent 主动挂起，等下游的结论以消息送达。

可等待的对象有三类：``execute_terminal`` 的后台命令（经端口 ``background_commands`` 问终端工具层）、
批准后运行中的 Agent Team，以及本 Task 仍在活动的派生 Task（委派的子 Agent、Agent fork；后两类见
``derived_report.open_derived_titles``）。都属于 Task，含更早 Run 开出的。

Agent 不必在答复前等：答复之后才到的结论会唤醒或开出新 Run，所以本工具只用于「需要结论才能继续」。
取代轮询式等待：调用当步闭合并带 ``suspend``，Run 随即挂起、不占执行者；每到一个结论（后台工作的
``<background_result>``、派生 Task 的 ``<agent_message report=...>``）或用户发来消息都会唤醒同一个
Run。是否继续等由模型决定（再次调用本工具）。没有任何可等待的对象时返回错误、不挂起：挂起会无人
唤醒，返回成功又会让模型以为自己在等。
"""

from typing import ClassVar

from app.core.tool.background_commands import background_commands
from app.core.tool.handler.wait.wait_messages_args import WaitMessagesArgs
from app.core.tool.multi_agent.derived_report import open_derived_titles
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
        "Pause yourself until a result arrives, instead of polling. Use it when you need a result "
        "before you can go on: a delegated child agent or fork that is still working, an approved "
        "agent team, or a background terminal command, including ones started earlier in this "
        "task. You do not have to wait before answering: anything that finishes after you answer "
        "resumes you with its result. You resume when a child or fork reports (<agent_message "
        "report=...>), background work finishes (<background_result>), another agent messages "
        "you, or an immediate input arrives; results arrive one at a time, so call this tool "
        "again if you still need to wait for others. If nothing is pending, it fails immediately "
        "without pausing."
    )
    args_model: type[WaitMessagesArgs] = WaitMessagesArgs
    timeout_seconds: ClassVar[float] = 10.0
    group = TOOL_GROUP_CHILD_AGENT

    def execute(self, execution_context: ToolExecutionContext) -> ToolObservation:
        # 标签由登记方或派生 Task 的标题给出，原样展示给模型。
        task_id = execution_context.task_id
        pending = [*background_commands().pending(task_id), *open_derived_titles(task_id)]
        if not pending:
            return tool_error(
                self.name,
                (
                    "nothing to wait for: no derived task or background work is still running, "
                    "so you were not paused. Results that already arrived "
                    "are in the conversation or will appear before your next step; do not call "
                    "wait_messages again unless you start new work to wait for."
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
