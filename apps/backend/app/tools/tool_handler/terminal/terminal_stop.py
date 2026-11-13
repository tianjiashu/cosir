"""``terminal_stop`` handler：停止 Agent 自己仍在运行的 ``execute_terminal`` 命令。"""

from typing import ClassVar

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    TOOL_TERMINAL_STOP,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.tool_base import HandlerBase
from app.core.tool.tool_grouping import TOOL_GROUP_TERMINAL
from app.core.workflow.cancel.tool_call_cancellation_registry import should_stop_call
from app.tools.terminal_runtime.command.running_command import RunningCommand
from app.tools.terminal_runtime.errors import TerminalSessionStateError
from app.tools.tool_handler.terminal.command_results import command_results
from app.tools.tool_handler.terminal.session_terminals import find_terminal
from app.tools.tool_handler.terminal.tool_observation import with_terminal_errors
from app.tools.tool_models import TerminalStopArgs


class TerminalStopTool(HandlerBase):
    """只能停 Agent 自己运行中的命令；用户终端归用户，不给 Agent 停。

    停止走 ``command_results`` 的单项取消（与用户点卡片上的「停止」同一条路）：登记表选择停止
    对象，作业的撤销回调落到 ``LocalExecutionBackend.stop``；结论照常以 ``<background_result>``
    回投，工具这里只等进程收尾。
    """

    name = TOOL_TERMINAL_STOP
    # 中文译文：停止你从 execute_terminal 启动、仍在运行的那条命令（按它的 terminal_id），连同它
    # 启动的全部进程。用它结束用完的 dev server / watcher；最终输出仍以 <background_result> 消息
    # 到达。用户打开的终端不能停。
    description = (
        "Stop your command still running from execute_terminal (by its terminal_id), together "
        "with every process it started. Use it to end a dev server or watcher once you are done "
        "with it. Its final output still arrives as a <background_result> message. Terminals the "
        "user opened cannot be stopped."
    )
    args_model = TerminalStopArgs
    timeout_seconds: ClassVar[float] = Constant.Terminal.COMMAND_STOP_WAIT_SECONDS + 5
    group = TOOL_GROUP_TERMINAL

    def execute(
        self, terminal_id: str, *, execution_context: ToolExecutionContext
    ) -> ToolObservation:

        def action() -> ToolObservation:
            # 查找顺序与 terminal_read 同一份（find_terminal）；找到的是用户会话就拒绝。
            command = find_terminal(execution_context.session_id, terminal_id)
            if not isinstance(command, RunningCommand):
                raise TerminalSessionStateError(
                    f"terminal {terminal_id} is a terminal the user opened; you can stop only "
                    "your own commands still running from execute_terminal."
                )
            # driving=False：目标是另一次调用登记的后台条目，不是当前正在执行的工具调用。
            command_results.request_cancel(
                execution_context.session_id, terminal_id, driving=False
            )
            log.info(
                "terminal_command_stop_requested",
                extra={
                    "msg": "Agent 要求停止运行中的终端命令",
                    "data": {
                        "session_id": execution_context.session_id,
                        "terminal_id": terminal_id,
                    },
                },
            )
            stopped = command.wait_finished(
                Constant.Terminal.COMMAND_STOP_WAIT_SECONDS,
                lambda: should_stop_call(execution_context.session_id, execution_context.tool_call_id),
            )
            log.info(
                "terminal_command_stopped",
                extra={
                    "msg": "终端命令停止请求的收尾结果",
                    "data": {
                        "session_id": execution_context.session_id,
                        "terminal_id": terminal_id,
                        "stopped": stopped,
                    },
                },
            )
            if stopped:
                return tool_success(
                    self.name,
                    f"stopped command {terminal_id}. Its final output arrives as a "
                    "<background_result> message.",
                )
            return tool_error(
                self.name,
                f"asked command {terminal_id} to stop, but it has not exited after "
                f"{Constant.Terminal.COMMAND_STOP_WAIT_SECONDS:g}s; its result will still "
                "arrive as a <background_result> message.",
            )

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
