"""``terminal_close`` handler."""

from typing import ClassVar

from app.core.tools.schemas import (
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_handler.terminal_session.common import (
    build_session_display_payload,
    cancelled,
    cancelled_observation,
    require_service,
    success_observation,
    with_terminal_errors,
)
from app.core.tools.tool_handler.terminal_session.descriptions import (
    build_terminal_session_parameters_schema,
    describe_terminal_tool,
)
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models import TerminalCloseArgs


class TerminalCloseTool(HandlerBase):
    """关闭 Agent 创建的终端 session。"""

    name = "terminal_close"
    description = "Close an existing local terminal session and its shell process tree."
    permission: ClassVar[str] = "shell"
    args_model = TerminalCloseArgs
    timeout_seconds: ClassVar[float] = 10.0
    risk_level: ClassVar[str] = "high"

    def execute(
        self,
        session_id: str,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """显式关闭 session；预览 WebSocket 断开不会触发此操作。"""

        if execution_context is None:
            raise ValueError("terminal_close requires a workspace execution context")
        if cancelled(execution_context):
            return cancelled_observation(self.name, self.permission)

        def action() -> ToolObservation:
            payload = require_service(execution_context).close(
                session_id,
                task_id=execution_context.task_id,
                workspace_id=execution_context.workspace_id,
            )
            return success_observation(
                self.name,
                self.permission,
                payload,
                summary="Terminal session closed.",
                display_payload=build_session_display_payload(payload),
            )

        return with_terminal_errors(self.name, self.permission, action)

    def to_definition(self) -> ToolDefinition:
        """返回交互终端工具定义。"""

        return ToolDefinition(
            name=self.name,
            description=self.description,
            permission=self.permission,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            risk_level=self.risk_level,
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
            description_provider=lambda: describe_terminal_tool(self.name, self.description),
            schema_provider=lambda: build_terminal_session_parameters_schema(
                self.name, self.args_model
            ),
        )
