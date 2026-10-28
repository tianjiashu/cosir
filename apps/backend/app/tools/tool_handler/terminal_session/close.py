"""``terminal_close`` handler：结束会话并回收 shell 进程树。"""

from typing import ClassVar

from app.core.tool.schemas import (
    TOOL_TERMINAL_CLOSE,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.tool_base import HandlerBase
from app.core.tool.tool_grouping import TOOL_GROUP_TERMINAL_SESSION
from app.service import depends as service_depends
from app.tools.display.terminal_display import build_terminal_session_display_data
from app.tools.tool_handler.terminal_session.common import (
    cancelled,
    cancelled_observation,
    metadata_observation,
    with_terminal_errors,
)
from app.tools.tool_models import TerminalCloseArgs


class TerminalCloseTool(HandlerBase):
    """关闭不可逆：会终止该会话中仍在运行的进程（包括用户在其中启动的）。"""

    name = TOOL_TERMINAL_CLOSE
    description = (
        "Close a terminal session and its shell process tree. This also ends anything the user "
        "is running in it; leave sessions the user opened alone unless asked."
    )
    args_model = TerminalCloseArgs
    timeout_seconds: ClassVar[float] = 15.0
    group = TOOL_GROUP_TERMINAL_SESSION

    def execute(self, session_id: str, execution_context: ToolExecutionContext) -> ToolObservation:
        if cancelled(execution_context):
            return cancelled_observation(self.name)

        def action() -> ToolObservation:
            payload = service_depends.get_terminal_session_service().close(
                session_id, task_id=execution_context.task_id
            )
            return metadata_observation(
                self.name,
                payload,
                summary="Terminal session closed.",
                display_payload=build_terminal_session_display_data(payload),
            )

        return with_terminal_errors(self.name, action)

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
                verb="关闭终端",
                icon="x",
                variant="terminal-session-close",
                surface="standalone",
                expandable=False,
                expand_layout="none",
                show_result=False,
            ),
        )
