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
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.schemas.model_tool_schema import strict_parameters_schema
from app.core.tool.tool_base import HandlerBase
from app.core.tool.tool_grouping import TOOL_GROUP_TERMINAL_SESSION
from app.service import depends as service_depends
from app.tools.display.terminal_display import (
    build_terminal_session_display_data,
)
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
    # 中文译文：新建一个交互 shell 会话：一个持续存活的本地 shell，带 PTY，用户在终端面板里
    # 看到同一个终端并可以输入。与 execute_terminal 不同，cwd、环境变量与运行中的程序在多次
    # terminal_write / terminal_read 之间保持；用它运行 dev server、REPL、watch 任务和交互程序。
    # 返回 session_id（后续调用用它寻址）、shell、cwd 与集成状态。label 是显示在标签页和
    # terminal_list 里的用途简述；cwd 是起始目录（工作区内、必须存在）；integration=auto 在 shell
    # 支持时启用 shell 集成（命令边界、退出码、cwd 跟踪，terminal_write 的 command_returned 等待
    # 依赖它），off 则关闭。先 terminal_list 看能否复用已有会话；会话归 Task，跨 Run 保留。
    description = (
        "Start a new interactive shell session: a live local shell with a PTY that the user "
        "sees in the terminal panel and can type into. Unlike execute_terminal, the cwd, "
        "environment and running program persist across terminal_write / terminal_read calls; "
        "use it for dev servers, REPLs, watch tasks and interactive programs. Returns the "
        "session_id to address later calls, the shell, cwd and integration status. label is a "
        "short purpose shown on the tab and in terminal_list; cwd is the start directory "
        "(inside the workspace, must exist); integration=auto enables shell integration where "
        "the shell supports it (command boundaries, exit codes, cwd tracking, needed for "
        "terminal_write's command_returned wait), off disables it. Check terminal_list first to "
        "reuse an existing session; sessions belong to the task and survive across runs."
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
