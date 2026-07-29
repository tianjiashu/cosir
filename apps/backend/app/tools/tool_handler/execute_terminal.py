"""execute_terminal 工具实现。

本模块只承载 execute_terminal 这一个工具。危险命令裁决、后端选择、命令执行与
``ToolObservation`` 组装都在 ``ExecuteTerminalTool`` 内完成；不含 subprocess 细节
（下沉到 ``app.tools.tool_handler.terminal.local_backend``）。
"""

import re
from pathlib import Path
from typing import Any

from app.config.logging.logger import log
from app.tools.schemas import (
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.tools.tool_execute.tool_error import tool_error
from app.tools.tool_execute.tool_success import tool_success
from app.tools.tool_handler.terminal import (
    DangerousCommandVerdict,
    create_backend,
    detect_dangerous_command,
)
from app.tools.tool_handler.tool_base import HandlerBase
from app.tools.tool_models.execute_terminal_args import ExecuteTerminalArgs
from app.trace_infra.redaction import redact_terminal_output

# workdir 字符白名单：挡住命令注入式 workdir（含 ;|&$() 等注入字符直接拒绝）。
_WORKDIR_SAFE_RE = re.compile(r"^[A-Za-z0-9/\\:_\-.~ +=@,]+$")

_EXECUTE_TERMINAL_DESCRIPTION = (
    "Execute a command in the local shell (foreground) and return its merged output "
    "and exit code. Shell semantics depend on the runtime OS (Windows=cmd.exe, "
    "POSIX=/bin/sh)."
)


class ExecuteTerminalTool(HandlerBase):
    """在本机 shell 中同步执行一条终端命令的工具。

    严格对齐现状 7 个文件工具的既定范式：类属性契约 + ``execute`` 实例方法 +
    类内 ``to_definition()`` 构造 ``ToolDefinition`` + 模块级
    ``build_execute_terminal_definition`` 薄封装。

    参数:
        project_root: 本工具允许执行命令的默认工作根目录（应为任务所属 workspace
            的 ``root_path``，由运行时按 task → workspace 解析后注入）。

    返回:
        ``ExecuteTerminalTool`` 实例。

    异常:
        初始化阶段不主动抛出业务异常。

    副作用:
        仅保存 ``project_root``；不执行命令、不读取文件系统。
    """

    name = "execute_terminal"
    description = _EXECUTE_TERMINAL_DESCRIPTION
    permission = "execute_terminal"
    args_model = ExecuteTerminalArgs
    timeout_seconds = 120.0  # 外层 ToolExecutor 硬保险
    risk_level = "high"
    default_command_timeout = 60.0  # 内层命令级缺省
    max_command_timeout = 110.0  # 内层钳制上限（< 外层 120 留 10s 收尾）

    def __init__(self) -> None:
        """初始化 execute_terminal 工具实例。

        参数:
            无。

        返回:
            无。

        异常:
            无。

        副作用:
            仅保存默认工作目录，不执行命令。
        """

    def execute(
            self,
            command: str,
            timeout: float | None = None,
            workdir: str | None = None,
            execution_context: ToolExecutionContext | None = None,
    ) -> ToolObservation:
        """在本机 shell 同步执行一条命令并返回归一化观测。

        参数:
            command: 待执行命令。
            timeout: 命令级超时秒数；缺省 ``default_command_timeout``，钳制到
                ``max_command_timeout``。
            workdir: 工作目录；缺省 ``project_root``；相对路径相对 ``project_root``
                解析；绝对路径直接使用。
            execution_context: 本次执行的运行时边界（任务 / 工作区 / 根路径）；由执行链
                在子进程内无条件注入的关键字参数，handler 契约必须接受此 kwarg 以匹配
                ``ToolExecutor._execute_handler`` 调用约定；本工具为命令执行入口且用户已
                注入时作为 workdir 的解析边界。此工具当前不对模型可见，且不依靠
                cwd/deny-list 宣称可以约束命令的全部文件系统副作用。

        返回:
            ``ToolObservation``。灾难级命令/工作目录不存在/后端异常为
            ``status="error"``；命令正常执行（含非零退出码）为 ``status="success"``，
            ``data`` 回传 ``exit_code`` / ``truncated`` / ``timed_out``。

        异常:
            不主动向上抛出；均转换为结构化 ``ToolObservation``。

        副作用:
            经后端派生子进程执行命令；命令输出经凭据脱敏后写入 ``content``。
        """
        log.info(
            "terminal_command_started",
            extra={
                "msg": "终端命令开始执行",
                "data": {"command_preview": redact_terminal_output(command[:200])},
            },
        )

        if execution_context is None:
            return tool_error(
                self.name,
                "could not run the command: execution context is missing",
                reason=(
                    "the runtime did not provide a workspace root for this command. This "
                    "is an internal execution wiring error; retry after the runtime injects "
                    "ToolExecutionContext."
                ),
                retryable=True,
                permission=self.permission,
            )

        verdict = detect_dangerous_command(command)
        if verdict.is_dangerous:
            log.warning(
                "terminal_command_blocked",
                extra={"msg": "灾难级命令被拒绝", "data": {"key": verdict.key}},
            )
            return self._blocked_observation(verdict)

        execution_root = execution_context.workspace_root
        cwd, err = self._resolve_workdir(workdir, execution_root)
        if err:
            return self._workdir_error_observation(err)

        effective = min(timeout or self.default_command_timeout, self.max_command_timeout)
        result = create_backend("local").execute(command, str(cwd), effective)

        log.info(
            "terminal_command_finished",
            extra={
                "msg": "终端命令执行完成",
                "data": {
                    "exit_code": result.exit_code,
                    "timed_out": result.timed_out,
                    "truncated": result.truncated,
                },
            },
        )

        return tool_success(
            tool_name=self.name,
            content=redact_terminal_output(result.output),
            permission=self.permission,
        )

    def to_definition(self) -> ToolDefinition:
        """把工具实例转换成当前注册系统使用的 ``ToolDefinition``。

        参数:
            无。

        返回:
            可直接注册到 ``ToolRegistry`` 的工具定义。

        异常:
            无。

        副作用:
            无。
        """
        return ToolDefinition(
            name=self.name,
            description=self.description,
            permission=self.permission,
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            risk_level=self.risk_level,
            resource_keys=("shell",),
            execution_mode="process",  # 跑任意 shell，需子进程隔离 + 树杀兜底
            display=ToolDisplayHints(
                verb="执行命令",
                icon="terminal",
                title_summary=self.render_request_summary,
                result_summary=self.render_result_summary,
                expandable=True,
                expand_layout="terminal",
            ),
        )

    def render_request_summary(self, arguments: dict[str, Any]) -> str:
        """
        返回 execute_terminal 请求摘要。TODO: 待实现
        """

    def render_result_summary(self, display_data: dict[str, Any]) -> str | None:
        """
        返回 execute_terminal 展开态摘要。TODO: 待实现
        """

    def _resolve_workdir(self, workdir: str | None, execution_root: str | Path) -> tuple[Path, str]:
        """解析工作目录并限制在执行根内。

        参数:
            workdir: 用户指定的工作目录，可能为 None。
            execution_root: 当前工具调用允许使用的工作目录根。

        返回:
            ``(resolved_path, error)``：成功时 ``error=""``；失败时 ``resolved_path``
            为占位 ``Path()`` 且 ``error`` 为人读错误。

        异常:
            无。

        副作用:
            无（只读文件系统查询 ``is_dir``）。
        """
        root = Path(execution_root).resolve()
        if workdir is None or workdir == "":
            return root, ""
        if not _WORKDIR_SAFE_RE.match(workdir):
            return Path(), f"invalid workdir (contains unsafe characters): {workdir}"
        candidate = Path(workdir)
        resolved = (candidate if candidate.is_absolute() else root / candidate).resolve()
        try:
            resolved.relative_to(root)
        except ValueError:
            return Path(), f"workdir escapes workspace root: {workdir}"
        if not resolved.is_dir():
            return Path(), f"workdir not found: {resolved}"
        return resolved, ""

    def _blocked_observation(self, verdict: DangerousCommandVerdict) -> ToolObservation:
        """构造灾难级命令被拒的错误观测。"""
        return tool_error(
            self.name,
            f"Command blocked: {verdict.description}. Dangerous commands are not allowed; "
            f"use file tools (read_file / write_file / patch / delete) for filesystem changes.",
            reason=(
                "the command matches the deny-list of destructive commands (rm / del / "
                "rd / Remove-Item / irreversible git operations such as reset --hard, "
                "push --force, clean -f, checkout --, branch -D, config --global, "
                "commit --amend / etc.) that could irreversibly damage the repository, "
                "filesystem, or system, so it is always rejected. Use the dedicated file "
                "tools (read_file / write_file / patch / delete) for filesystem changes "
                "and avoid destructive git commands; the same command will always be "
                "blocked."
            ),
            permission=self.permission,
        )

    def _workdir_error_observation(self, err: str) -> ToolObservation:
        """构造工作目录错误的观测。"""
        return tool_error(
            self.name,
            err,
            reason=(
                "the working directory could not be used. Common causes: it contains "
                "shell-injection characters (rejected for safety), it points outside the "
                "workspace root, or it does not exist. Provide a safe, in-workspace "
                "directory path; the same invalid workdir will always fail."
            ),
            permission=self.permission,
        )


def build_execute_terminal_definition() -> ToolDefinition:
    """构造绑定到指定工作区根目录（workspace.root_path）的 execute_terminal 定义。

    参数:
        project_root: 任务所属 workspace 的 ``root_path``（遵循「工作区即根目录」
            约束，非全局 ``settings.project_root``），由运行时按 task → workspace
            解析后传入。

    返回:
        ``ToolDefinition``，供 ``ToolRegistry`` 注册。

    异常:
        无。

    副作用:
        创建 ``ExecuteTerminalTool`` 实例与定义对象，不执行命令。
    """
    return ExecuteTerminalTool().to_definition()
