"""交互终端 read 的工具边界；shell 观察由 service 统一管理。"""

from typing import Any, ClassVar

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
    require_service,
    success_observation,
    with_terminal_errors,
)
from app.core.tools.tool_handler.terminal_session.descriptions import (
    build_terminal_session_parameters_schema,
    describe_terminal_tool,
)
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models import TerminalReadArgs


class TerminalReadTool(HandlerBase):
    name = TOOL_TERMINAL_READ
    description = (
        "Observe terminal output and shell events using next_cursor. Wait for "
        "activity, a named command_returned, or a new shell_prompt. Timeout and "
        "output do not mean completion. A command returning means the shell "
        "regained control, not that background or suspended processes ended. gap "
        "means retained output was lost; has_more means continue paging. "
        "Interactive program input does not create a shell command."
    )
    args_model = TerminalReadArgs
    timeout_seconds: ClassVar[float] = 35.0
    group = TOOL_GROUP_TERMINAL_SESSION

    def execute(
        self,
        session_id: str,
        cursor: str | None = None,
        wait: dict[str, Any] | None = None,
        execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """仅观察，不控制命令；分页游标不越过未交付的输出。"""
        if execution_context is None:
            raise ValueError("terminal_read requires execution context")
        if cancelled(execution_context):
            return cancelled_observation(self.name)

        def action() -> ToolObservation:
            service = require_service(execution_context)
            result = service.read(
                session_id,
                task_id=execution_context.task_id,
                workspace_id=execution_context.workspace_id,
                cursor=cursor,
                wait=wait,
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
