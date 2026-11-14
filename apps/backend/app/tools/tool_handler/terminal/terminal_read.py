"""``terminal_read`` handler：按绝对行号读取终端逻辑行，可先按条件等待。"""

from typing import Any, ClassVar

from app.core.tool.schemas import (
    TOOL_TERMINAL_READ,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.tool_base import HandlerBase
from app.core.tool.tool_grouping import TOOL_GROUP_TERMINAL
from app.core.workflow.cancel.tool_call_cancellation_registry import should_stop_call
from app.tools.terminal_runtime.observation.wait import TerminalWait
from app.tools.tool_handler.terminal.session_terminals import find_terminal
from app.tools.tool_handler.terminal.tool_observation import (
    observation_result,
    with_terminal_errors,
)
from app.tools.tool_models import TerminalReadArgs


class TerminalReadTool(HandlerBase):
    name = TOOL_TERMINAL_READ
    # 中文译文：像读文本文件一样读一个终端的输出：可以是用户打开的终端，也可以是你从 execute_terminal
    # 启动、仍在运行的那条命令（按它的 terminal_id）。逻辑行（已合并软换行）以「行号│文本」给出，
    # 行号是绝对编号、永不复用。「live from」之后的行属于当前屏幕，仍可能被重绘；之前的行已定稿。
    # 用户终端里开着全屏程序（vim、less、top）时，不带 start_line 的读取改为以「s<行>│文本」返回
    # 整屏。需要等条件成立时用 wait 阻塞，不要轮询；读运行中的命令时，until=command_returned 表示
    # 命令结束。
    description = (
        "Read a terminal's output like a text file: a terminal the user opened, or your command "
        "still running from execute_terminal (by its terminal_id). Logical lines (soft wraps "
        "merged) come as 'line_number│text', with absolute line numbers that are never reused. "
        "Lines from 'live from' onward may still be redrawn; earlier lines are final. While a "
        "full-screen app (vim, less, top) is open in a user terminal, a read without start_line "
        "returns its screen as 's<row>│text' rows instead. Use wait to block for a condition "
        "instead of polling; for your running command, until=command_returned means the command "
        "ended."
    )
    args_model = TerminalReadArgs
    timeout_seconds: ClassVar[float] = 40.0
    group = TOOL_GROUP_TERMINAL

    def execute(
        self,
        terminal_id: str,
        start_line: int | None = None,
        limit: int | None = None,
        tail: int | None = None,
        pattern: str | None = None,
        wait: dict[str, Any] | None = None,
        *,
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:

        def action() -> ToolObservation:
            terminal = find_terminal(execution_context.session_id, terminal_id)
            result = terminal.read(
                start_line=start_line,
                limit=limit,
                tail=tail,
                pattern=pattern,
                wait=TerminalWait.model_validate(wait) if wait is not None else None,
                is_cancelled=lambda: should_stop_call(
                    execution_context.session_id, execution_context.tool_call_id
                ),
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
        )
