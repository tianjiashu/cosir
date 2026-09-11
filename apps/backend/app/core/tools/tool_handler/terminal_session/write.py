"""Hidden ``terminal_write`` handler."""

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
from app.core.tools.tool_models import TerminalWriteArgs


class TerminalWriteTool(HandlerBase):
    """向已存在的 session 写入 Agent input；当前未注册到 Agent。"""

    name = "terminal_write"
    description = (
        "Write UTF-8 input bytes to an existing local terminal session and "
        "optionally wait for output."
    )
    permission: ClassVar[str] = "shell"
    args_model = TerminalWriteArgs
    timeout_seconds: ClassVar[float] = 30.0
    risk_level: ClassVar[str] = "high"

    def execute(
        self,
        session_id: str,
        data: str,
        after_seq: int | None = None,
        wait_ms: int = 500,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """写入 UTF-8 input；重复请求可能重复输入，当前不提供幂等保证。"""

        if execution_context is None:
            raise ValueError("terminal_write requires a workspace execution context")
        if cancelled(execution_context):
            return cancelled_observation(self.name, self.permission)

        def action() -> ToolObservation:
            result = require_service(execution_context).write(
                session_id,
                task_id=execution_context.task_id,
                data=data.encode("utf-8"),
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
                summary="Terminal input accepted.",
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
                verb="输入终端",
                icon="terminal",
                surface="trace",
                expandable=True,
                expand_layout="terminal",
                show_result=False,
            ),
        )
