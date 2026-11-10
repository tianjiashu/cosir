"""``terminal_write`` handler：把一行命令填进用户终端的空提示符，不执行、不等待。"""

from typing import ClassVar

from app.core.tool.results.tool_cancelled import tool_cancelled
from app.core.tool.schemas import (
    TOOL_TERMINAL_WRITE,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.tool_base import HandlerBase
from app.core.tool.tool_grouping import TOOL_GROUP_TERMINAL_SESSION
from app.service import depends as service_depends
from app.tools.tool_handler.terminal_session.common import (
    cancelled,
    observation_result,
    with_terminal_errors,
)
from app.tools.tool_models import TerminalWriteArgs


class TerminalWriteTool(HandlerBase):
    name = TOOL_TERMINAL_WRITE
    # 中文译文：把一行命令填进用户打开的终端提示符，但不执行：用户看过之后自己按回车，你也不会知道
    # 他们按没按（之后用 terminal_read 看发生了什么）。用户让你把命令放进他们的终端时用它。只有
    # shell 集成就绪、停在提示符且还没有任何输入时才可用；否则失败，你可以改为在回复里给出命令。
    # 只能一行：多步操作先写脚本文件，再填运行它的那一行命令。
    description = (
        "Fill one command line into the prompt of a terminal the user opened, without running "
        "it: the user reviews it and presses Enter themselves, and you are not told whether they "
        "did (read the terminal later to see what happened). Use it when the user asks you to put "
        "a command in their terminal. It works only while the shell is idle at its prompt with "
        "nothing typed yet (shell integration required); otherwise it fails and you can give the "
        "command in your reply instead. One line only: for several steps, write a script file "
        "first and fill in the command that runs it."
    )
    args_model = TerminalWriteArgs
    timeout_seconds: ClassVar[float] = 10.0
    group = TOOL_GROUP_TERMINAL_SESSION

    def execute(
        self,
        session_id: str,
        command: str,
        *,
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """命令由参数边界校验；填入立即返回，用户回车与之后输出由 terminal_read 观察。"""
        if cancelled(execution_context):
            return tool_cancelled(self.name)

        def action() -> ToolObservation:
            runtime = service_depends.get_terminal_session_service().require(
                session_id, execution_context.task_id
            )
            result = runtime.prefill(
                command,
                run_id=execution_context.run_id,
                tool_call_id=execution_context.tool_call_id,
            )
            return observation_result(self.name, result)

        return with_terminal_errors(self.name, action)

    def to_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            group=self.group,
            needs_user_surface=True,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            resource_keys=("shell",),
            execution_mode="thread",
        )
