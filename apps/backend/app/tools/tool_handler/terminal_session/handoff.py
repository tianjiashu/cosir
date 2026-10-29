"""``terminal_handoff`` handler：把会话输入交给用户，等用户交还（HITL，``execute`` 型）。"""

from typing import ClassVar

from app.core.tool.results.tool_awaiting_decision import tool_awaiting_decision
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    TOOL_TERMINAL_HANDOFF,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.tool_base import HandlerBase
from app.core.tool.tool_grouping import TOOL_GROUP_TERMINAL_SESSION
from app.service import depends as service_depends
from app.tools.display.terminal_display import build_terminal_observation_display_data
from app.tools.terminal_runtime.errors import TerminalSessionNotFoundError
from app.tools.tool_handler.terminal_session.common import (
    observation_result,
    with_terminal_errors,
)
from app.tools.tool_models import TerminalHandoffArgs

_SESSION_GONE = (
    "the terminal session no longer exists (it ended earlier or the backend restarted), so the "
    "handoff is over. Call terminal_start if you still need a terminal."
)


class TerminalHandoffTool(HandlerBase):
    """交接复用 HITL：首次调用登记交接并返回等待占位（挂起 Agent），用户交还即对这次调用作出决定，
    决定到达后 ``prepare_input`` 绕过模型重放本调用，报告交接以来的输出。

    交接只是会话上的提示，不改变输入仲裁、不登记延迟结果。会话在交接中意外结束（shell 退出、用户
    关闭）时由终端运行时代为作出决定，Agent 不会一直挂着；后端关闭不代为落定，等待中的决定按 HITL
    契约保留，之后交还会报告会话已不在。
    """

    name = TOOL_TERMINAL_HANDOFF
    # 中文译文：当用户必须亲自动手时把会话的输入交给用户：口令、不该由你回答的确认，或应当由
    # 用户执行而不是由你执行的命令。这类命令先用 terminal_write 的 text 输入（不带回车）写进
    # 提示符，再交接，让用户核对后自己按回车。你会挂起，直到用户交还，然后拿到这期间出现的行。
    # 必须单独调用，不要和其他工具并行。
    description = (
        "Hand a session's input to the user when they must act themselves: passwords, "
        "confirmations you should not answer, or a command they should run instead of you. "
        "For such a command, type it in with terminal_write text (no Enter) first, then hand "
        "off so the user reviews it and presses Enter. You are paused until they hand it back, "
        "then you get the lines that appeared meanwhile. Call it on its own, not together with "
        "other tools."
    )
    args_model = TerminalHandoffArgs
    timeout_seconds: ClassVar[float] = 15.0
    group = TOOL_GROUP_TERMINAL_SESSION

    def execute(
        self, session_id: str, reason: str, *, execution_context: ToolExecutionContext
    ) -> ToolObservation:
        service = service_depends.get_terminal_session_service()
        task_id = execution_context.task_id
        tool_call_id = execution_context.tool_call_id
        if execution_context.user_decision is None:

            def begin() -> ToolObservation:
                result = service.require(session_id, task_id).begin_handoff(reason, tool_call_id)
                return tool_awaiting_decision(
                    self.name,
                    tool_call_id,
                    display_data=build_terminal_observation_display_data(result),
                )

            return with_terminal_errors(self.name, begin)

        def end() -> ToolObservation:
            try:
                result = service.require(session_id, task_id).end_handoff(tool_call_id)
            except TerminalSessionNotFoundError:
                return tool_success(self.name, _SESSION_GONE)
            return observation_result(self.name, result)

        return with_terminal_errors(self.name, end)

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
            user_decision="execute",
            display=ToolDisplayHints(
                verb="交给用户输入",
                icon="keyboard",
                variant="terminal-session-handoff",
                surface="standalone",
                expandable=False,
                expand_layout="none",
                show_result=False,
            ),
        )
