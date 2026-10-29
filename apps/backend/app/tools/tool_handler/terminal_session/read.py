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
from app.tools.terminal_runtime.inputs import TerminalWait
from app.tools.tool_handler.terminal_session.common import (
    cancelled,
    observation_result,
    with_terminal_errors,
)
from app.tools.tool_models import TerminalReadArgs


class TerminalReadTool(HandlerBase):
    name = TOOL_TERMINAL_READ
    # 中文译文：像读文本文件一样读会话输出：逻辑行（已合并软换行）以「行号│文本」给出，行号
    # 是绝对编号、永不复用。「live from」之后的行属于当前屏幕，仍可能被重绘；之前的行已定稿。
    # 全屏程序（vim、less、top）运行期间，不带 start_line 的读取改为以「s<行>│文本」返回
    # 整屏；行号仍指向 shell 输出。长输出里用 pattern 找目标行（报错、URL），需要等条件成立
    # 时用 wait 阻塞，不要轮询。
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
