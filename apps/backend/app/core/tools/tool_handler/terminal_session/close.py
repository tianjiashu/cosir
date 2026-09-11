"""Hidden ``terminal_close`` handler."""

from typing import ClassVar

from app.core.tools.schemas import (
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_handler.terminal_session.common import (
    cancelled,
    cancelled_observation,
    require_service,
    success_observation,
    with_terminal_errors,
)
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models import TerminalCloseArgs


class TerminalCloseTool(HandlerBase):
    """关闭 Agent 创建的终端 session；当前未注册到 Agent。"""

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
            )
            return success_observation(
                self.name,
                self.permission,
                payload,
                summary="Terminal session closed.",
            )

        return with_terminal_errors(self.name, self.permission, action)

    def to_definition(self) -> ToolDefinition:
        """返回 hidden tool definition；调用方当前不得自动注册到 Agent。"""

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
                surface="trace",
                expandable=False,
                expand_layout="none",
                show_result=False,
            ),
        )
