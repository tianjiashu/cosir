"""``terminal_read`` handler."""

from typing import ClassVar

from app.core.tools.schemas import (
    TOOL_TERMINAL_READ,
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
from app.core.tools.tool_models import TerminalReadArgs


class TerminalReadTool(HandlerBase):
    """按 cursor 非破坏性读取 session 输出。"""

    name = TOOL_TERMINAL_READ
    description = "Read new output from a local terminal session using an output sequence cursor."
    permission: ClassVar[str] = "shell"
    args_model = TerminalReadArgs
    timeout_seconds: ClassVar[float] = 30.0
    risk_level: ClassVar[str] = "medium"

    def execute(
        self,
        session_id: str,
        after_seq: int | None = None,
        wait_ms: int = 1000,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """读取 output；不会消费其他 subscriber 或前端预览的数据。"""

        if execution_context is None:
            raise ValueError("terminal_read requires a workspace execution context")
        if cancelled(execution_context):
            return cancelled_observation(self.name, self.permission)

        def action() -> ToolObservation:
            result = require_service(execution_context).read(
                session_id,
                task_id=execution_context.task_id,
                workspace_id=execution_context.workspace_id,
                after_seq=after_seq,
                wait_ms=wait_ms,
                is_cancelled=lambda: cancelled(execution_context),
            )
            if cancelled(execution_context):
                return cancelled_observation(self.name, self.permission)
            return success_observation(
                self.name,
                self.permission,
                {"session_id": session_id, **result.to_dict()},
                summary="Terminal output read.",
                display_payload=build_session_display_payload(
                    {"session_id": session_id, **result.to_dict()},
                ),
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
                verb="读取终端",
                icon="terminal",
                variant="terminal-session-read",
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
