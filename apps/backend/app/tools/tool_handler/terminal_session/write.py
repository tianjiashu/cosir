"""``terminal_write`` handler：整行、按键、信号或交接，按条件等待后返回新增行。"""

from typing import Any, ClassVar

from app.core.tool.results.tool_cancelled import tool_cancelled
from app.core.tool.schemas import (
    TOOL_TERMINAL_WRITE,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.tool_base import HandlerBase
from app.core.tool.tool_grouping import TOOL_GROUP_TERMINAL_SESSION
from app.service import depends as service_depends
from app.tools.terminal_runtime.terminal_input import TerminalWait
from app.tools.tool_handler.terminal_session.common import (
    cancelled,
    observation_result,
    with_terminal_errors,
)
from app.tools.tool_models import TerminalWriteArgs


class TerminalWriteTool(HandlerBase):
    name = TOOL_TERMINAL_WRITE
    description = (
        "Send input to a session and return the lines that appeared after it, in terminal_read's "
        "format. Submit one line per call; for multi-line scripts use execute_terminal. When the "
        "user must type instead (passwords, confirmations you should not answer), use "
        "terminal_handoff."
    )
    args_model = TerminalWriteArgs
    timeout_seconds: ClassVar[float] = 55.0
    group = TOOL_GROUP_TERMINAL_SESSION

    def execute(
        self,
        session_id: str,
        operation_id: str,
        input: dict[str, Any],
        wait: dict[str, Any] | None = None,
        *,
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """输入由参数边界校验；接受后的取消仍返回接受事实，避免模型重发。"""
        if cancelled(execution_context):
            return tool_cancelled(self.name)

        def action() -> ToolObservation:
            runtime = service_depends.get_terminal_session_service().require(
                session_id, execution_context.task_id
            )
            result = runtime.write(
                input,
                operation_id,
                TerminalWait.model_validate(wait) if wait is not None else None,
                run_id=execution_context.run_id,
                tool_call_id=execution_context.tool_call_id,
                is_cancelled=lambda: cancelled(execution_context),
            )
            return observation_result(self.name, result)

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
                verb="输入终端",
                icon="terminal",
                variant="terminal-session-write",
                surface="standalone",
                expandable=False,
                expand_layout="none",
                show_result=False,
            ),
        )

