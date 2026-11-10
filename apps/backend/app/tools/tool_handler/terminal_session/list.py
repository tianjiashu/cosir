"""``terminal_list``：用户为本 Task 打开的会话（含最近结束的）及其当前状态。"""

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
from app.core.tool.tool_grouping import TOOL_GROUP_TERMINAL_SESSION
from app.service import depends as service_depends
from app.tools.display.terminal_display import (
    build_terminal_session_list_display_data,
)
from app.tools.tool_handler.terminal_session.common import cancelled
from app.tools.tool_models import TerminalListArgs


class TerminalListTool(HandlerBase):
    """从运行时服务发现跨 Run 保留的会话，避免依赖历史消息猜测存活状态。

    结果是纯文本：每个会话一行，状态措辞与 read / write 的 header 同源
    （``render.session_state_parts``），只保留复用与观察决策要用的事实。
    """

    name = TOOL_TERMINAL_LIST
    # 中文译文：列出用户为本 Task 打开的终端，一行一个：当前在做什么（停在提示符、正在跑命令、
    # 全屏程序、用户正在输入、缺少 shell 集成、已结束）、cwd 与最近一次命令结果。只有用户能新建
    # 和关闭终端；读取或填入前先调它，接手新 Run 的工作时也先看一眼。
    description = (
        "List the terminals the user opened for this task, one line each: what it is doing now "
        "(at prompt, running a command, full-screen app, user typing, no shell integration, "
        "ended), its cwd and last command result. Only the user opens and closes terminals; "
        "call this before reading or filling a terminal, and when picking up work in a new run."
    )
    args_model = TerminalListArgs
    timeout_seconds: ClassVar[float] = 10.0
    group = TOOL_GROUP_TERMINAL_SESSION

    def execute(self, execution_context: ToolExecutionContext) -> ToolObservation:
        if cancelled(execution_context):
            return tool_cancelled(self.name)
        runtimes = service_depends.get_terminal_session_service().task_runtimes(
            execution_context.task_id
        )
        sessions = [runtime.snapshot().summary for runtime in runtimes]
        lines = sessions or ["no terminal sessions"]
        return tool_success(
            tool_name=self.name,
            content="\n".join(lines),
            display_data=build_terminal_session_list_display_data(len(sessions)),
        )

    def to_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            group=self.group,
            needs_user_surface=True,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            resource_keys=("shell",),
            execution_mode="thread",
        )

