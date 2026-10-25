"""Task 终端会话发现入口；不创建 worker，也不读取终端输出。"""

from typing import ClassVar

from app.tools.display.terminal_display import build_terminal_session_list_display_data
from app.core.tool.schemas import (
    TOOL_TERMINAL_LIST,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.tool_grouping import TOOL_GROUP_TERMINAL_SESSION
from app.tools.tool_handler.terminal_session.common import (
    agent_session_payload,
    cancelled,
    cancelled_observation,
    success_observation,
)
from app.core.tool.tool_base import HandlerBase
from app.tools.tool_models import TerminalListArgs
from app.service import depends as service_depends


class TerminalListTool(HandlerBase):
    """从运行时服务发现本 Task 跨 Run 保留的会话，避免依赖历史消息猜测存活状态。"""

    name = TOOL_TERMINAL_LIST
    description = (
        "List this task's live terminal sessions and recent ended sessions. Check this before "
        "starting a new shell or when resuming work in another run. Reuse a live session_id "
        "with terminal_read/write/signal/close. initial_cwd is the starting directory, not the "
        "current directory; running means the shell is alive, not that a command is finished. "
        "Sessions survive run completion and cancellation, but not backend restarts. "
        "Close sessions explicitly when no longer needed. Output history is bounded; use "
        "terminal_read to observe the current screen and bounded recent history."
    )
    args_model = TerminalListArgs
    timeout_seconds: ClassVar[float] = 10.0
    group = TOOL_GROUP_TERMINAL_SESSION

    def execute(self, execution_context: ToolExecutionContext) -> ToolObservation:
        """归属取自执行上下文；取消时不读取服务。"""

        if cancelled(execution_context):
            return cancelled_observation(self.name)
        sessions = service_depends.get_terminal_session_service().list_sessions(
            task_id=execution_context.task_id,
            workspace_id=execution_context.workspace_id,
        )
        return success_observation(
            self.name,
            {"sessions": [agent_session_payload(session) for session in sessions]},
            summary="Task terminal sessions.",
            display_payload=build_terminal_session_list_display_data(len(sessions)),
        )

    def to_definition(self) -> ToolDefinition:
        """复用交互终端工具组和 shell 权限，列表数量是本次调用的展示结果。"""

        return ToolDefinition(
            name=self.name,
            group=self.group,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            resource_keys=("shell",),
            execution_mode="thread",
            display=ToolDisplayHints(
                verb="列出终端",
                icon="terminal",
                variant="terminal-session-list",
                surface="standalone",
                expandable=False,
                expand_layout="none",
                show_result=False,
            ),
        )
