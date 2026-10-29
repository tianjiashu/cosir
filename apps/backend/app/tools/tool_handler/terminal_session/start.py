"""``terminal_start`` handler：Agent 新建 Task 所有的交互终端。

PTY 创建、shell 解析、cwd 边界校验与容量判定在 ``TerminalSessionService``（与用户新建共用）；
本模块负责 Agent 专属的部分：新建后等 shell 集成就绪，再把元数据归一化为观察。
"""

import platform
from collections.abc import Mapping
from typing import Any, ClassVar

from app.core.tool.results.tool_cancelled import tool_cancelled
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    TOOL_TERMINAL_START,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.schemas.model_tool_schema import strict_parameters_schema
from app.core.tool.tool_base import HandlerBase
from app.core.tool.tool_grouping import TOOL_GROUP_TERMINAL_SESSION
from app.service import depends as service_depends
from app.tools.display.terminal_display import build_terminal_session_display_data
from app.tools.terminal_runtime.errors import ShellResolutionError
from app.tools.terminal_runtime.shell_resolver import ShellResolver
from app.tools.tool_handler.terminal_session.common import (
    cancelled,
    with_terminal_errors,
)
from app.tools.tool_models import TerminalStartArgs


class TerminalStartTool(HandlerBase):
    """新建持久 PTY 会话并返回元数据（geometry、集成状态、可用按键与信号）。

    会话一旦建立就是一个存活的本地 shell，用户在终端面板里看到同一个终端并可输入。
    """

    name = TOOL_TERMINAL_START
    # 中文译文：像读文本文件一样读会话输出：逻辑行（已合并软换行）以「<行号>│文本」给出，行号
    # 是绝对编号、永不复用。「live from」之后的行属于当前屏幕，仍可能被重绘；之前的行已定稿。
    # 全屏程序（vim、less、top）运行期间，不带 start_line 的读取改为以「s<行>│文本」返回
    # 整屏；行号仍指向 shell 输出。长输出用 pattern 搜索而不是翻页；需要等条件成立时用 wait
    # 阻塞，不要轮询。不带 wait 重复读取且内容未变时，只返回一行提示。
    description = (
        "Read session output like a text file: logical lines (soft wraps merged) as "
        "'<line>│text', with absolute line numbers that are never reused. Lines from "
        "'live from' onward are the current screen and may still be redrawn; earlier lines are "
        "final. While a full-screen app (vim, less, top) is open, a read without start_line "
        "returns its screen as 's<row>│text' rows instead; line numbers still address the shell "
        "output. Search long output with pattern instead of paging, and use wait to block for a "
        "condition instead of polling. An unchanged repeat read without wait returns a single "
        "hint line."
    )

    args_model = TerminalStartArgs
    timeout_seconds: ClassVar[float] = 15.0
    group = TOOL_GROUP_TERMINAL_SESSION

    def execute(
        self,
        shell: str = "auto",
        cwd: str | None = None,
        label: str | None = None,
        integration: str = "auto",
        *,
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """副作用：在本机创建 PTY 与 shell 子进程并登记到 session service。"""

        if cancelled(execution_context):
            return tool_cancelled(self.name)

        def action() -> ToolObservation:
            runtime = service_depends.get_terminal_session_service().start(
                task_id=execution_context.task_id,
                workspace_root=str(execution_context.workspace_root),
                shell=shell,
                integration=integration,
                cwd=cwd,
                label=label,
                created_by="agent",
                run_id=execution_context.run_id,
            )
            if integration == "auto":
                # Agent 需要立刻知道能否用 command_returned；用户新建不等，集成就绪后经快照通知。
                runtime.wait_for_integration()
            session = runtime.snapshot()
            return tool_success(
                tool_name=self.name,
                content=session.summary,
                display_data=build_terminal_session_display_data(
                    session, include_terminal_info=True
                ),
            )

        return with_terminal_errors(self.name, action)

    def to_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            group=self.group,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
            parameters_schema=_parameters_schema(self.args_model),
            timeout_seconds=self.timeout_seconds,
            resource_keys=("shell",),
            execution_mode="thread",
            display=ToolDisplayHints(
                verb="启动终端",
                icon="terminal",
                variant="terminal-session-start",
                surface="standalone",
                expandable=True,
                expand_layout="terminal",
                show_result=False,
            ),
        )


# 以下只生产模型可见文本（英文，属于模型契约）：按当前宿主补全 shell=auto 的解析结果。其他可选
# shell 及其集成支持以 terminal_list 的返回为唯一事实源，这里不复述。执行路径仍是能力检查的权威
# 来源，这里只改善引导，因此 shell 解析失败时安全回退。


def _parameters_schema(args_model: type[TerminalStartArgs]) -> Mapping[str, Any]:
    """在 SDK 生成的 strict schema 上补宿主说明：shell=auto 在本机的解析结果。"""

    schema = strict_parameters_schema(args_model)
    schema["properties"]["shell"]["description"] = _shell_parameter_description(
        platform.system(), _resolve_auto_shell_kind()
    )
    return schema


def _resolve_auto_shell_kind() -> str:
    """``shell=auto`` 在当前宿主上的解析结果；失败返回空串，由调用方降级为「宿主默认」措辞。"""

    try:
        return ShellResolver.resolve("auto").kind
    except ShellResolutionError:
        return ""


def _shell_parameter_description(system: str, resolved_kind: str) -> str:
    if system == "Windows":
        auto = f"resolves to {resolved_kind} here" if resolved_kind else "uses the host default"
    else:
        fallback = "zsh" if system == "Darwin" else "bash"
        here = f" (here: {resolved_kind})" if resolved_kind else ""
        auto = f"uses $SHELL, else {fallback}{here}"
    return (
        f"'auto' {auto}. terminal_list shows the other installed shells and which support "
        "shell integration."
    )
