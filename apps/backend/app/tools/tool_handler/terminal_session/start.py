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
    # 描述只写选用边界与会话的生命周期；label / cwd 的含义只在参数字段里写一次。
    # 选用规则与 execute_terminal 互为两面：命令会自己结束、只要结果就用它，所以这里点名它；
    # 反过来 execute_terminal 的描述不点名本工具（子 Agent 没有交互终端）。
    # 中文译文：新建一个交互 shell 会话：带真实终端（PTY）的本地 shell，用户在终端面板里看得到、
    # 也能输入。适用于必须一直开着的（dev server、watcher、tail -f）、要来回交互的（REPL、TUI、
    # 会提问的程序），或需要用户看到、参与的事；命令会自己结束、只要它的结果时用 execute_terminal。
    # cwd、环境变量与运行中的程序在多次 terminal_write / terminal_read 之间保持；会话归 Task，之后的
    # Run 也能复用，所以先用 terminal_list 看看。返回后续调用用的 session_id，以及 shell、cwd 与
    # 集成状态。
    description = (
        "Start an interactive shell session: a local shell with a real terminal (PTY) that the "
        "user sees in the terminal panel and can type into. Use it for what must keep running "
        "(dev servers, watchers, tail -f), needs back-and-forth input (REPLs, TUIs, programs "
        "that ask questions), or the user should watch or join; for a command that finishes on "
        "its own and whose result is all you need, use execute_terminal. The cwd, environment "
        "and running program persist across terminal_write / terminal_read calls, and the "
        "session belongs to the task, so later runs can reuse it: check terminal_list first. "
        "Returns the session_id for later calls, plus the shell, cwd and integration status."
    )

    args_model = TerminalStartArgs
    timeout_seconds: ClassVar[float] = 15.0
    group = TOOL_GROUP_TERMINAL_SESSION

    def execute(
        self,
        shell: str = "auto",
        cwd: str | None = None,
        label: str | None = None,
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
                cwd=cwd,
                label=label,
                created_by="agent",
                run_id=execution_context.run_id,
            )
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
            needs_user_surface=True,
            description=self.description,
            handler=self.execute,
            args_model=self.args_model,
            parameters_schema=_parameters_schema(self.args_model),
            timeout_seconds=self.timeout_seconds,
            resource_keys=("shell",),
            execution_mode="thread",
        )


# 以下只生产模型可见文本（英文，属于模型契约）：按当前宿主补全 shell 的候选清单与 auto 的解析
# 结果。两者与执行路径同源（``ShellResolver``），因此探测失败时安全回退为只讲 auto，不列出清单。


def _parameters_schema(args_model: type[TerminalStartArgs]) -> Mapping[str, Any]:
    """在 SDK 生成的 strict schema 上补宿主说明：shell=auto 的解析结果与本机已安装清单。"""

    schema = strict_parameters_schema(args_model)
    schema["properties"]["shell"]["description"] = _shell_parameter_description(
        platform.system(), _resolve_auto_shell_kind(), _installed_shell_kinds()
    )
    return schema


def _resolve_auto_shell_kind() -> str:
    """``shell=auto`` 在当前宿主上的解析结果；失败返回空串，由调用方降级为「宿主默认」措辞。"""

    try:
        return ShellResolver.resolve("auto").kind
    except ShellResolutionError:
        return ""


def _installed_shell_kinds() -> list[str]:
    """本机已安装、可按名字请求的 shell；探测失败时返回空列表，描述因此省略清单。"""

    return [str(shell["kind"]) for shell in ShellResolver.available()]


def _shell_parameter_description(
    system: str, resolved_kind: str, installed: list[str]
) -> str:
    if system == "Windows":
        auto = f"resolves to {resolved_kind} here" if resolved_kind else "uses the host default"
    else:
        fallback = "zsh" if system == "Darwin" else "bash"
        here = f" (here: {resolved_kind})" if resolved_kind else ""
        auto = f"uses $SHELL, else {fallback}{here}"
    parts = [f"'auto' {auto}."]
    if installed:
        parts.append(f"Installed here: {', '.join(installed)}.")
    parts.append("Pass one of these names, or a path to a shell executable.")
    return " ".join(parts)
