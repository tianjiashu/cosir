"""Hidden ``terminal_start`` handler."""

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
from app.core.tools.tool_models import TerminalStartArgs


class TerminalStartTool(HandlerBase):
    """创建 Agent 驱动的 PTY session。

    当前类已经实现并可单元测试，但刻意未加入 ``ToolSystem``，因此当前 Agent 不可见。
    前端不会调用此 handler；前端只能 attach ``terminal_start`` 返回的 session 预览。
    """

    name = "terminal_start"
    description = (
        "Start a persistent local interactive shell session for agent-driven terminal work. "
        "The frontend only previews its output; use terminal_write/read/signal/close "
        "to operate the session."
    )
    permission: ClassVar[str] = "shell"
    args_model = TerminalStartArgs
    timeout_seconds: ClassVar[float] = 10.0
    risk_level: ClassVar[str] = "high"

    def execute(
        self,
        shell: str = "auto",
        cwd: str | None = None,
        cols: int = 120,
        rows: int = 32,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """创建 session 并返回 session 元数据；不向前端发送任何输入。"""

        if execution_context is None:
            raise ValueError("terminal_start requires a workspace execution context")
        if cancelled(execution_context):
            return cancelled_observation(self.name, self.permission)

        def action() -> ToolObservation:
            service = require_service(execution_context)
            payload = service.start(
                task_id=execution_context.task_id,
                workspace_id=execution_context.workspace_id,
                workspace_root=str(execution_context.workspace_root),
                shell=shell,
                cwd=cwd,
                cols=cols,
                rows=rows,
                created_by_run_id=execution_context.run_id or None,
            )
            return success_observation(
                self.name,
                self.permission,
                payload,
                summary="Terminal session started.",
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
                verb="启动终端",
                icon="terminal",
                surface="standalone",
                expandable=True,
                expand_layout="terminal",
                show_result=False,
            ),
        )
