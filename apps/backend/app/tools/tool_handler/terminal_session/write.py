"""交互终端 write 的工具边界；shell 观察由 service 统一管理。"""

from typing import ClassVar

from app.tools.display.terminal_display import build_terminal_observation_display_data
from app.core.tool.schemas import (
    TOOL_TERMINAL_WRITE,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.tool_grouping import TOOL_GROUP_TERMINAL_SESSION
from app.tools.tool_handler.terminal_session.common import (
    cancelled,
    cancelled_observation,
    success_observation,
    with_terminal_errors,
)
from app.tools.tool_handler.terminal_session.descriptions import (
    build_terminal_session_parameters_schema,
    describe_terminal_tool,
)
from app.core.tool.tool_base import HandlerBase
from app.tools.tool_models import TerminalWriteArgs
from app.service import depends as service_depends


class TerminalWriteTool(HandlerBase):
    name = TOOL_TERMINAL_WRITE
    description = (
        "Submit one complete line with Enter, or one supported control key. A line may be a "
        "shell command, a multiline continuation, or an answer to a program. Input "
        "acceptance is not execution. Reuse operation_id only with identical "
        "input. Cancellation stops waiting, not accepted input. Use returned "
        "terminal_read for later observations. After the bounded wait, returns the current "
        "screen, recent history and latest_command state; these snapshots may overlap. "
        "Old output can be lost; no cursor or pagination is provided. screen.truncated and "
        "history_truncated mark loss. Screen text replaces the previous view; row_offset and "
        "first_row_omitted_chars mark omitted prefixes, wrapped_rows uses original row indices. "
        "latest_command may predate this input; compare command_id instead of treating an old "
        "result as this input's result. "
        "Only shell integration proves command return; prompts, timeouts and accepted input do not."
    )
    args_model = TerminalWriteArgs
    timeout_seconds: ClassVar[float] = 35.0
    group = TOOL_GROUP_TERMINAL_SESSION

    def execute(
        self,
        session_id: str,
        operation_id: str,
        input: dict[str, str],
        wait_ms: int = 500,
        *,
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """输入由参数边界校验；接受后的取消仍返回接受事实，避免模型重发。"""
        if cancelled(execution_context):
            return cancelled_observation(self.name)

        def action() -> ToolObservation:
            service = service_depends.get_terminal_session_service()
            result = service.write(
                session_id,
                task_id=execution_context.task_id,
                workspace_id=execution_context.workspace_id,
                operation_id=operation_id,
                input=input,
                wait_ms=wait_ms,
                is_cancelled=lambda: cancelled(execution_context),
            )
            return success_observation(
                self.name,
                result,
                summary="Terminal input accepted.",
                display_payload=build_terminal_observation_display_data(result),
            )

        return with_terminal_errors(self.name, action)

    def to_definition(self) -> ToolDefinition:
        """返回可注册到工具注册表的 ``terminal_write`` 定义。

        返回:
            ``ToolDefinition``：模型可见描述由 ``descriptions.py`` 按宿主平台补全，
            ``parameters_schema`` 用宿主专属 shell 说明覆盖，权限 ``shell``、
            ``execution_mode="thread"``。

        异常:
            无。

        副作用:
            无；每次调用重新构造定义，不写注册表、不读会话状态。
        """

        return ToolDefinition(
            name=self.name,
            group=self.group,
            description=describe_terminal_tool(self.name, self.description),
            handler=self.execute,
            args_model=self.args_model,
            parameters_schema=build_terminal_session_parameters_schema(self.name, self.args_model),
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
