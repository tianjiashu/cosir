"""``terminal_read`` handler：按绝对行号读取逻辑行，可先按条件等待。"""

from typing import Any, ClassVar

from app.core.tool.results.tool_cancelled import tool_cancelled
from app.core.tool.schemas import (
    TOOL_TERMINAL_READ,
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
from app.tools.tool_models import TerminalReadArgs


class TerminalReadTool(HandlerBase):
    name = TOOL_TERMINAL_READ
    description = (
        "Read session output like a text file: logical lines (soft wraps merged) as "
        "'line_number│text', with absolute line numbers that are never reused. Lines from "
        "'live from' onward are the current screen and may still be redrawn; earlier lines are "
        "final. While a full-screen app (vim, less, top) is open, a read without start_line "
        "returns its screen as 's<row>│text' rows instead; line numbers still address the shell "
        "output. Use pattern to find lines (errors, URLs) in long output, and wait to block for "
        "a condition instead of polling."
    )
    args_model = TerminalReadArgs
    timeout_seconds: ClassVar[float] = 40.0
    group = TOOL_GROUP_TERMINAL_SESSION

    def execute(
        self,
        session_id: str,
        start_line: int | None = None,
        limit: int | None = None,
        tail: int | None = None,
        pattern: str | None = None,
        wait: dict[str, Any] | None = None,
        *,
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        if cancelled(execution_context):
            return tool_cancelled(self.name)

        def action() -> ToolObservation:
            runtime = service_depends.get_terminal_session_service().require(
                session_id, execution_context.task_id
            )
            result = runtime.read(
                start_line=start_line,
                limit=limit,
                tail=tail,
                pattern=pattern,
                wait=TerminalWait.model_validate(wait) if wait is not None else None,
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
                verb="读取终端",
                icon="terminal",
                variant="terminal-session-read",
                surface="standalone",
                expandable=False,
                expand_layout="none",
                show_result=False,
            ),
        )
