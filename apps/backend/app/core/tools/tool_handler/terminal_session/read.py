"""交互终端 read 的工具边界；shell 观察由 service 统一管理。"""

from typing import ClassVar

from app.core.tools.display.terminal_display import build_terminal_observation_display_data
from app.core.tools.schemas import (
    TOOL_TERMINAL_READ,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_grouping import TOOL_GROUP_TERMINAL_SESSION
from app.core.tools.tool_handler.terminal_session.common import (
    cancelled,
    cancelled_observation,
    success_observation,
    with_terminal_errors,
)
from app.core.tools.tool_handler.terminal_session.descriptions import (
    build_terminal_session_parameters_schema,
    describe_terminal_tool,
)
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models import TerminalReadArgs
from app.service import depends as service_depends


class TerminalReadTool(HandlerBase):
    name = TOOL_TERMINAL_READ
    description = (
        "Wait at least 3 seconds, then observe the current terminal screen, bounded recent history "
        "and latest_command state. No cursor or pagination; snapshots may overlap and old output "
        "may be lost. For complete output, redirect the command to a file. Screen text replaces "
        "the previous view; wrapped_rows uses original zero-based rows, row_offset and "
        "first_row_omitted_chars mark omitted prefixes. screen.truncated/history_truncated "
        "explicitly mark loss. The first read establishes a baseline without counting a repeat. "
        "New information resets the repeat count and produces no warning. Consecutive reads "
        "with no new information warn on counts 1 through 4 and return a recoverable read error "
        "from count 5 onward. Warnings show the count and suggest a longer wait when no input "
        "is needed; new output or state changes restore reads. Inspect prompts before waiting "
        "longer. A longer wait does not reset the counter or guarantee recovery. "
        "Only shell integration determines command return; executing can mean waiting "
        "for input, and returned does not mean background processes ended. A read error does "
        "not fail or stop the command. Cancellation or session end may return before the wait ends."
    )
    args_model = TerminalReadArgs
    timeout_seconds: ClassVar[float] = 35.0
    group = TOOL_GROUP_TERMINAL_SESSION

    def execute(
        self,
        session_id: str,
        wait_ms: int = 3000,
        *,
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """只读有界当前快照；重复观察错误不改变会话或命令状态。"""
        if cancelled(execution_context):
            return cancelled_observation(self.name)

        def action() -> ToolObservation:
            service = service_depends.get_terminal_session_service()
            result = service.read(
                session_id,
                task_id=execution_context.task_id,
                workspace_id=execution_context.workspace_id,
                run_id=execution_context.run_id,
                wait_ms=wait_ms,
                is_cancelled=lambda: cancelled(execution_context),
            )
            return success_observation(
                self.name,
                result,
                summary="Terminal observation.",
                display_payload=build_terminal_observation_display_data(result),
            )

        return with_terminal_errors(self.name, action)

    def to_definition(self) -> ToolDefinition:
        """返回可注册到工具注册表的 ``terminal_read`` 定义。

        返回:
            ``ToolDefinition``：模型可见描述由 ``descriptions.py`` 按宿主平台补全，权限
            ``shell``、``execution_mode="thread"``。

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
                verb="读取终端",
                icon="terminal",
                variant="terminal-session-read",
                surface="standalone",
                expandable=False,
                expand_layout="none",
                show_result=False,
            ),
        )
