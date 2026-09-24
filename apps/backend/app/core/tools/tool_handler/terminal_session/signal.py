"""``terminal_signal`` handler."""

from typing import ClassVar

from app.core.tools.schemas import (
    TOOL_TERMINAL_SIGNAL,
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
from app.core.tools.tool_models import TerminalSignalArgs


class TerminalSignalTool(HandlerBase):
    """由 Agent 向 PTY 发送 interrupt/eof/suspend。"""

    name = TOOL_TERMINAL_SIGNAL
    description = "Send an interrupt, EOF, or suspend signal to a local terminal session."
    permission: ClassVar[str] = "shell"
    args_model = TerminalSignalArgs
    timeout_seconds: ClassVar[float] = 10.0
    risk_level: ClassVar[str] = "high"

    def execute(
        self,
        session_id: str,
        signal: str,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """调用 session service 发送平台无关 signal。"""

        if execution_context is None:
            raise ValueError("terminal_signal requires a workspace execution context")
        if cancelled(execution_context):
            return cancelled_observation(self.name, self.permission)

        def action() -> ToolObservation:
            payload = require_service(execution_context).signal(
                session_id,
                task_id=execution_context.task_id,
                workspace_id=execution_context.workspace_id,
                signal_name=signal,
            )
            return success_observation(
                self.name,
                self.permission,
                payload,
                summary="Terminal signal sent.",
                display_payload=build_session_display_payload(payload, extra={"signal": signal}),
            )

        return with_terminal_errors(self.name, self.permission, action)

    def to_definition(self) -> ToolDefinition:
        """返回交互终端工具定义。"""

        return ToolDefinition(
            name=self.name,
            description=describe_terminal_tool(self.name, self.description),
            permission=self.permission,
            handler=self.execute,
            args_model=self.args_model,
            parameters_schema=build_terminal_session_parameters_schema(
                self.name, self.args_model
            ),
            timeout_seconds=self.timeout_seconds,
            risk_level=self.risk_level,
            resource_keys=("shell",),
            execution_mode="thread",
            display=ToolDisplayHints(
                verb="控制终端",
                icon="square-terminal",
                variant="terminal-session-signal",
                surface="standalone",
                expandable=False,
                expand_layout="none",
                show_result=False,
            ),
        )
