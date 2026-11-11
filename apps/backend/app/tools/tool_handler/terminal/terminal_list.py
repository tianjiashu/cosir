"""``terminal_list``：本 Task 的终端——用户打开的会话与 Agent 自己运行中的命令。"""

from typing import ClassVar

from app.core.tool.results.tool_cancelled import tool_cancelled
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    TOOL_TERMINAL_LIST,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.tool_base import HandlerBase
from app.core.tool.tool_grouping import TOOL_GROUP_TERMINAL
from app.core.workflow.cancel.tool_call_cancellation_registry import should_stop_call
from app.tools.display.terminal_display import (
    build_terminal_list_display_data,
)
from app.tools.tool_handler.terminal.task_terminals import terminal_summaries
from app.tools.tool_models import TerminalListArgs


class TerminalListTool(HandlerBase):
    """从运行时现取两类终端（``task_terminals``），避免依赖历史消息猜测存活状态。

    结果是纯文本：用户会话在前、运行中的命令在后，每行一条，状态措辞与 read / write 的 header
    同源（``render.terminal_state_parts``），只保留复用与观察决策要用的事实。
    """

    name = TOOL_TERMINAL_LIST
    # 中文译文：列出本 Task 的终端，一行一个：先是用户打开的终端（当前在做什么：停在提示符、正在跑
    # 命令、全屏程序、用户正在输入、缺少 shell 集成、已结束；cwd 与最近一次命令结果），再是你从
    # execute_terminal 启动、仍在运行中的命令。只有用户能新建和关闭自己的终端。读取终端前先调它，
    # 接手新 Run 的工作时也先看一眼。
    description = (
        "List this task's terminals, one line each: the terminals the user opened (what each is "
        "doing now: at prompt, running a command, full-screen app, user typing, no shell "
        "integration, ended; its cwd and last command result), then your commands still running "
        "from execute_terminal. Only the user opens and closes their terminals. Call it before "
        "reading a terminal, and when picking up work in a new run."
    )
    args_model = TerminalListArgs
    timeout_seconds: ClassVar[float] = 10.0
    group = TOOL_GROUP_TERMINAL

    def execute(self, execution_context: ToolExecutionContext) -> ToolObservation:
        if should_stop_call(execution_context.task_id, execution_context.tool_call_id):
            return tool_cancelled(self.name)
        summaries = terminal_summaries(execution_context.task_id)
        return tool_success(
            tool_name=self.name,
            content="\n".join(summaries or ["no terminals"]),
            display_data=build_terminal_list_display_data(len(summaries)),
        )

    def to_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            group=self.group,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            resource_keys=("shell",),
            execution_mode="thread",
        )
