"""``terminal_list``：发现本 Task 的活跃会话（含用户新建的）与宿主可用 shell。"""

from typing import ClassVar

from app.core.tool.schemas import (
    TOOL_TERMINAL_LIST,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.tool_base import HandlerBase
from app.core.tool.tool_grouping import TOOL_GROUP_TERMINAL_SESSION
from app.service import depends as service_depends
from app.tools.display.terminal_display import build_terminal_session_list_display_data
from app.tools.terminal_runtime.shell_resolver import ShellResolver
from app.tools.tool_handler.terminal_session.common import (
    cancelled,
    cancelled_observation,
    metadata_observation,
)
from app.tools.tool_models import TerminalListArgs


class TerminalListTool(HandlerBase):
    """从运行时服务发现跨 Run 保留的会话，避免依赖历史消息猜测存活状态。"""

    name = TOOL_TERMINAL_LIST
    description = (
        "List this task's live terminal sessions (including ones the user opened) and the shells "
        "installed on this host, with whether each supports shell integration (needed for "
        "wait until=command_returned and command exit codes/output ranges). Check this before "
        "terminal_start and when resuming work in a new run. Sessions do not survive a backend "
        "restart. input_holder shows who is typing; handoff shows an active handoff to the user."
    )
    args_model = TerminalListArgs
    timeout_seconds: ClassVar[float] = 10.0
    group = TOOL_GROUP_TERMINAL_SESSION

    def execute(self, execution_context: ToolExecutionContext) -> ToolObservation:
        if cancelled(execution_context):
            return cancelled_observation(self.name)
        service = service_depends.get_terminal_session_service()
        sessions = service.list_sessions(task_id=execution_context.task_id)
        return metadata_observation(
            self.name,
            {"sessions": sessions, "available_shells": ShellResolver.available()},
            summary="Task terminal sessions.",
            display_payload=build_terminal_session_list_display_data(len(sessions)),
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
