"""``terminal_list``：本 Task 的会话（含用户新建的、最近结束的）及其当前状态，与宿主可用 shell。"""

from typing import ClassVar

from app.core.tool.results.tool_cancelled import tool_cancelled
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    TOOL_TERMINAL_LIST,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.tool_base import HandlerBase
from app.core.tool.tool_grouping import TOOL_GROUP_TERMINAL_SESSION
from app.service import depends as service_depends
from app.tools.display.terminal_display import build_terminal_session_list_display_data
from app.tools.terminal_runtime.shell_resolver import ShellResolver
from app.tools.tool_handler.terminal_session.common import cancelled
from app.tools.tool_models import TerminalListArgs


class TerminalListTool(HandlerBase):
    """从运行时服务发现跨 Run 保留的会话，避免依赖历史消息猜测存活状态。

    结果是纯文本而非 start / close 那样的 JSON 元数据：每个会话一行，状态措辞与 read / write
    的 header 同源（``render.session_state_parts``），只保留复用与输入决策要用的事实。
    """

    name = TOOL_TERMINAL_LIST
    # 中文译文：列出本 Task 的终端会话，一行一个：谁创建的、当前在做什么（停在提示符、正在
    # 跑命令、全屏程序、用户正在输入、已结束）、cwd 与最近一次命令结果。同时列出已安装的
    # shell，以及哪些缺少 shell 集成。新建会话前先调它复用，接手新 Run 的工作时也先看一眼。
    description = (
        "List this task's terminal sessions, one line each: who created it, what it is doing "
        "now (at prompt, running a command, full-screen app, user typing, ended), its cwd and "
        "last command result. Also lists the installed shells and which lack shell integration. "
        "Call it before terminal_start to reuse a session, and when picking up work in a new run."
    )
    args_model = TerminalListArgs
    timeout_seconds: ClassVar[float] = 10.0
    group = TOOL_GROUP_TERMINAL_SESSION

    def execute(self, execution_context: ToolExecutionContext) -> ToolObservation:
        if cancelled(execution_context):
            return tool_cancelled(self.name)
        runtimes = service_depends.get_terminal_session_service().task_runtimes(
            execution_context.task_id
        )
        sessions = [runtime.snapshot().summary for runtime in runtimes]
        lines = sessions or ["no terminal sessions"]
        return tool_success(
            tool_name=self.name,
            content="\n".join([*lines, _shells_line()]),
            display_data=build_terminal_session_list_display_data(len(sessions)),
        )

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
                verb="列出终端",
                icon="terminal",
                variant="terminal-session-list",
                surface="standalone",
                expandable=False,
                expand_layout="none",
                show_result=False,
            ),
        )


def _shells_line() -> str:
    """``shells: zsh (default), bash, fish (no integration)``：只标注默认项与缺集成的例外。"""

    entries = []
    for shell in ShellResolver.available():
        notes = [
            note
            for note, applies in (
                ("default", shell["isDefault"]),
                ("no integration", not shell["integrationSupported"]),
            )
            if applies
        ]
        entries.append(f"{shell['kind']} ({', '.join(notes)})" if notes else str(shell["kind"]))
    return "shells: " + (", ".join(entries) or "none detected")
