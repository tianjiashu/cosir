"""``terminal_start`` handler：Agent 新建 Task 所有的交互终端。

PTY 创建、shell 解析、cwd 边界校验与容量判定都在 ``TerminalSessionService``；本模块只做参数
透传、取消前置检查与结果归一化。
"""

import platform
from collections.abc import Mapping
from typing import Any, ClassVar

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
from app.tools.terminal_runtime.shell_resolver import ShellResolutionError, ShellResolver
from app.tools.tool_handler.terminal_session.common import (
    cancelled,
    cancelled_observation,
    metadata_observation,
    with_terminal_errors,
)
from app.tools.tool_models import TerminalStartArgs


class TerminalStartTool(HandlerBase):
    """新建持久 PTY 会话并返回元数据（geometry、集成状态、可用按键与信号）。

    会话一旦建立就是一个存活的本地 shell，用户在终端面板里看到同一个终端并可输入。
    """

    name = TOOL_TERMINAL_START
    description = (
        "Start a task-owned interactive terminal. Call terminal_list first to reuse an existing "
        "session (including ones the user opened) and to see which shells are installed. The "
        "user sees the same terminal in their terminal panel and can type into it. Sessions "
        "survive completed or cancelled runs until terminal_close, shell exit, task deletion, or "
        "backend restart. For one-off non-interactive commands or multi-line scripts, prefer "
        "execute_terminal."
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
            return cancelled_observation(self.name)

        def action() -> ToolObservation:
            payload = service_depends.get_terminal_session_service().start(
                task_id=execution_context.task_id,
                workspace_root=str(execution_context.workspace_root),
                shell=shell,
                integration=integration,
                cwd=cwd,
                label=label,
                created_by="agent",
                run_id=execution_context.run_id,
            )
            return metadata_observation(
                self.name,
                payload,
                summary="Terminal session started.",
                display_payload=build_terminal_session_display_data(
                    payload, include_terminal_info=True
                ),
            )

        return with_terminal_errors(self.name, action)

    def to_definition(self) -> ToolDefinition:
        return ToolDefinition(
            name=self.name,
            group=self.group,
            description=_host_description(self.description),
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


# 以下只生产模型可见文本（英文，属于模型契约）：按当前宿主补全 auto shell 的解析结果与 cwd 路径语法。
# 执行路径仍是能力检查的权威来源，这里只改善引导，因此 shell 解析失败时安全回退。


def _host_description(base: str) -> str:
    resolved = _resolve_auto_shell_kind()
    host = f" On this host shell=auto resolves to {resolved}." if resolved else ""
    return f"{base}{host}"


def _parameters_schema(args_model: type[TerminalStartArgs]) -> Mapping[str, Any]:
    """在 SDK 生成的 strict schema 上补宿主说明：shell 的解析结果与 cwd 的路径语法。"""

    schema = strict_parameters_schema(args_model)
    properties = schema["properties"]
    system = platform.system()
    properties["shell"]["description"] = _shell_parameter_description(
        system, _resolve_auto_shell_kind()
    )
    syntax = "Windows path syntax" if system == "Windows" else "POSIX path syntax"
    properties["cwd"]["description"] = (
        f"Initial directory using {syntax}, relative to the workspace; defaults to the "
        "workspace root and must resolve to an existing directory inside it."
    )
    return schema


def _resolve_auto_shell_kind() -> str:
    """``shell=auto`` 在当前宿主上的解析结果；失败返回空串，由调用方降级为「宿主默认」措辞。"""

    try:
        return ShellResolver.resolve("auto").kind
    except ShellResolutionError:
        return ""


def _shell_parameter_description(system: str, resolved_kind: str) -> str:
    resolved = resolved_kind or "the host default"
    if system == "Windows":
        return (
            f"'auto' currently resolves to {resolved}; use 'powershell', 'pwsh' or 'cmd' when "
            "installed (cmd has no shell integration). Use Windows command syntax."
        )
    fallback = "zsh" if system == "Darwin" else "bash"
    return (
        f"'auto' uses $SHELL and falls back to {fallback} (currently {resolved}); 'zsh' and "
        "'bash' support shell integration, 'fish' and custom executables do not. Use POSIX "
        "shell syntax."
    )
