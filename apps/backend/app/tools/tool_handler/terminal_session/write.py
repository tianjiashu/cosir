"""``terminal_write`` handler：整行、按键、信号或交接，按条件等待后返回新增行。"""

import platform
from typing import Any, ClassVar

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
from app.tools.terminal_runtime.terminal_input import (
    TerminalWait,
    keys_for_platform,
    signals_for_platform,
)
from app.tools.tool_handler.terminal_session.common import (
    cancelled,
    cancelled_observation,
    observation_result,
    with_terminal_errors,
)
from app.tools.tool_models import TerminalWriteArgs


class TerminalWriteTool(HandlerBase):
    name = TOOL_TERMINAL_WRITE
    description = (
        "Send input to a terminal and return the lines that appeared after it, as "
        "'line_number│text'. input is one of: line (one line plus Enter), keys (a key sequence "
        "such as [\"down\",\"enter\"] or [\"tab\"]), signal (interrupt/suspend/eof), or handoff "
        "(give keyboard control to the user, e.g. for a password or a confirmation you should "
        "not answer; you are paused until the user hands it back and a <background_result> from "
        "terminal_handoff resumes you). wait controls when it returns: command_returned (needs "
        "shell integration), output_quiet, match (regex on new lines, e.g. 'password:' or "
        "'Listening on'), or elapsed. Waits up to 30s run inline; longer timeout_ms (e.g. builds "
        "and test suites) pause you and a <background_result> from terminal_wait resumes you "
        "when the condition is met or times out, so do not poll. Long output keeps the first "
        "20 and last 60 lines and marks the omitted range; read it with terminal_read "
        "start_line. If the user is typing, the write waits up to 10s and then fails with a "
        "retryable TERMINAL_INPUT_HELD_BY_USER. Reuse operation_id only with identical input. "
        "For non-interactive multi-line scripts prefer execute_terminal; here submit one line "
        "at a time."
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
            return cancelled_observation(self.name)

        def action() -> ToolObservation:
            result = service_depends.get_terminal_session_service().write(
                session_id,
                task_id=execution_context.task_id,
                operation_id=operation_id,
                input=input,
                wait=TerminalWait.model_validate(wait) if wait is not None else None,
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
            description=_host_description(self.description),
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


def _host_description(base: str) -> str:
    """在基准描述后追加当前宿主可用的按键与信号集合（模型契约，英文）。"""

    system = platform.system()
    keys = ", ".join(keys_for_platform(system))
    signals = ", ".join(signals_for_platform(system))
    return (
        f"{base} Keys on this host: {keys}. Signals on this host: {signals}. A line keeps "
        "whitespace and adds exactly one Enter; empty text just presses Enter. Keys never "
        "add Enter unless 'enter' is listed."
    )
