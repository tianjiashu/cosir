"""execute_terminal 工具实现。

本模块只承载 execute_terminal 这一个工具。危险命令裁决、后端选择、命令执行与
``ToolObservation`` 组装都在 ``ExecuteTerminalTool`` 内完成；不含 subprocess 细节
（下沉到 ``app.tools.tool_handler.terminal.local_backend``）。
"""

import os
import platform
import re
import shutil
from collections.abc import Iterable
from dataclasses import replace
from pathlib import Path

from app.config.logging.logger import log
from app.core.tools.display.terminal_display import build_terminal_display_data
from app.core.tools.schemas import (
    OutputSink,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tools.tool_execute.tool_error import tool_error
from app.core.tools.tool_execute.tool_success import tool_success
from app.core.tools.tool_handler.terminal import (
    DangerousCommandVerdict,
    create_backend,
    detect_dangerous_command,
)
from app.core.tools.tool_handler.terminal.execution_result import ExecutionResult
from app.core.tools.tool_handler.tool_base import HandlerBase
from app.core.tools.tool_models.execute_terminal_args import (
    ExecuteTerminalArgs,
    ExecuteTerminalShell,
)

# workdir 字符白名单：挡住命令注入式 workdir（含 ;|&$() 等注入字符直接拒绝）。
_WORKDIR_SAFE_RE = re.compile(r"^[A-Za-z0-9/\\:_\-.~ +=@,]+$")


class ExecuteTerminalTool(HandlerBase):
    """在本机 shell 中同步执行一条终端命令的工具。

    严格对齐现状文件工具的既定范式：类属性契约 + ``execute`` 实例方法 +
    类内 ``to_definition()`` 构造 ``ToolDefinition`` + 模块级
    ``build_execute_terminal_definition`` 薄封装。

    本工具不持有任何工作根目录状态；命令执行时的允许作用域由运行时经
    ``execution_context.workspace_root`` 注入（``execute`` 的
    ``execution_context`` 关键字参数），而非构造参数。

    返回:
        ``ExecuteTerminalTool`` 实例。

    异常:
        初始化阶段不主动抛出业务异常。

    副作用:
        无（实例构造不执行命令、不读取文件系统、不保存状态）。
    """

    name = "execute_terminal"
    description = "Execute one foreground, non-interactive command locally in the workspace."
    permission = "execute_terminal"
    args_model = ExecuteTerminalArgs
    timeout_seconds = 120.0  # 外层 ToolHandlerRunner 硬保险
    risk_level = "high"
    default_command_timeout = 60.0  # 内层命令级缺省
    max_command_timeout = 110.0  # 内层钳制上限（< 外层 120 留 10s 收尾）

    def __init__(self) -> None:
        """初始化 execute_terminal 工具实例。

        参数:
            无。平台和可用 shell 均由当前 backend 进程自动检测。

        返回:
            无。

        异常:
            无。

        副作用:
            读取当前平台和可执行文件搜索路径，把可用 shell 与对应模型描述保存到实例；
            不执行命令或读取 workspace。
        """
        self._platform_name = (platform.system() or "").strip() or "unknown"
        self._available_shells = self._detect_available_shells()
        if "auto" not in self._available_shells:
            self._available_shells = ("auto", *self._available_shells)
        self.description = self._build_description()

    @property
    def available_shells(self) -> tuple[str, ...]:
        """返回当前实例会暴露给模型且允许执行的 shell 名称。"""

        return self._available_shells

    @staticmethod
    def _detect_available_shells() -> tuple[str, ...]:
        """根据平台和 PATH 检测可用 shell；不启动 shell。"""

        platform_name = (platform.system() or "").strip() or "unknown"
        shells: list[str] = ["auto"]
        if platform_name == "Windows":
            if os.environ.get("COMSPEC") or shutil.which("cmd.exe"):
                shells.append("cmd")
            if shutil.which("powershell.exe"):
                shells.append("powershell")
            if shutil.which("pwsh.exe"):
                shells.append("pwsh")
            return tuple(shells)

        if Path("/bin/sh").is_file() and os.access("/bin/sh", os.X_OK):
            shells.append("sh")
        for shell in ("bash", "zsh", "fish", "pwsh"):
            if shutil.which(shell):
                shells.append(shell)
        return tuple(shells)

    def _build_description(self) -> str:
        """生成当前宿主平台的模型可见工具描述。

        参数:
            无。使用初始化阶段自动检测的平台和可用 shell。

        返回:
            包含本工具共同执行语义和平台 shell 语法说明的描述文本。

        异常:
            无。

        副作用:
            读取当前进程的平台名称；不启动 shell、不访问 workspace，也不修改状态。
        """
        current_platform = self._platform_name
        shells = self._available_shells
        shell_list = ", ".join(f"'{shell}'" for shell in shells)
        common = (
            "Execute one foreground, non-interactive command locally in the current workspace. "
            "This tool has no interactive stdin, so commands waiting for user input may time out. "
            "stdout and stderr are merged; the result includes the exit code and bounded output. "
            "The working directory defaults to the workspace root and must remain inside it. "
            "Some destructive commands may be blocked by the current command policy. "
        )
        if current_platform == "Windows":
            return common + (
                "On Windows, the command is interpreted by cmd.exe. Use cmd syntax such as "
                "dir, type, where, %VAR%, &&, and ||. PowerShell is not the default; to use it, "
                f"Available shell values are {shell_list}. Use 'powershell' (powershell.exe) "
                "or 'pwsh' (pwsh.exe) for PowerShell syntax when listed as available. Do not "
                "mix cmd and PowerShell syntax."
            )
        if current_platform == "Darwin":
            return common + (
                "On macOS, the command uses /bin/sh semantics rather than an interactive login "
                f"zsh. Available shell values are {shell_list}. Use 'zsh' (/bin/zsh) when "
                "zsh-specific behavior is required and listed as available."
            )
        if current_platform == "Linux":
            return common + (
                f"On Linux, the command uses /bin/sh semantics. Available shell values are "
                f"{shell_list}. Use 'bash' (/bin/bash) when Bash-specific behavior is required "
                "and listed as available."
            )
        return common + (
            f"On {current_platform}, the command uses the non-Windows POSIX /bin/sh execution "
            f"semantics. Available shell values are {shell_list}. Do not assume Windows cmd.exe "
            "or PowerShell syntax."
        )

    def _build_parameters_schema(self) -> dict[str, object]:
        """构造当前平台的模型可见参数 schema。"""

        schema = self.args_model.model_json_schema()
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            return schema
        shell_schema = properties.get("shell")
        if isinstance(shell_schema, dict):
            shell_schema["enum"] = list(self._available_shells)
            shell_schema["description"] = self._build_shell_description(self._available_shells)
        return schema

    @staticmethod
    def _build_shell_description(available_shells: Iterable[str]) -> str:
        """生成 shell 参数字段的可用值说明。"""

        values = ", ".join(f"'{shell}'" for shell in available_shells)
        return (
            "Shell used to interpret command. Choose one of the shells available on this host: "
            f"{values}. 'auto' preserves the host default; the command syntax must match the "
            "selected shell."
        )

    def execute(
        self,
        command: str,
        shell: ExecuteTerminalShell = "auto",
        timeout: float | None = None,
        workdir: str | None = None,
        execution_context: ToolExecutionContext | None = None,
        output_sink: OutputSink | None = None,
    ) -> ToolObservation:
        """在本机 shell 同步执行一条命令并返回归一化观测。

        参数:
            shell: shell 选择；``auto`` 保持宿主默认 shell，也可显式选择 cmd、PowerShell
                或 POSIX shell。该值已由 ``ExecuteTerminalArgs`` 校验。
            command: 待执行命令。
            timeout: 命令级超时秒数；缺省 ``default_command_timeout``，钳制到
                ``max_command_timeout``。
            workdir: 工作目录；缺省 ``execution_context.workspace_root``；相对路径相对
                该根解析；绝对路径直接使用。
            execution_context: 本次执行的运行时边界（任务 / 工作区 / 根路径）；由执行链
                在子进程内无条件注入的关键字参数，handler 契约必须接受此 kwarg 以匹配
                ``ToolHandlerRunner._execute_handler`` 调用约定；本工具为命令执行入口且用户已
                注入时作为 workdir 的解析边界。本工具对模型可见（已在 agent profile
                工具集中登记），命令的全部文件系统副作用由 workdir 边界与 deny-list
                共同约束，而非仅靠 cwd 宣称。
            output_sink: 可选实时输出回调；由 ``ToolHandlerRunner`` 在子进程内注入，
                透传给执行后端，使命令输出可在运行期回传父进程做实时展示。
                为 None 时只省略实时增量，不影响命令执行和终态结果。

        返回:
            ``ToolObservation``。灾难级命令/工作目录不存在/后端异常为
            ``status="error"``；命令正常执行（含非零退出码）为 ``status="success"``。
            退出码、超时等结构性事实经 ``_render_content`` 并入 ``content`` 的
            ``[exit_code=N]`` 标记，供模型消费（``data`` 通道仅供前端展示，会在
            序列化前被清除，不回传模型）。命令因超时强杀时
            返回 ``status="error"`` 且 ``retryable=True``，明确告知命令未正常结束。

        异常:
            不主动向上抛出；均转换为结构化 ``ToolObservation``。

        副作用:
            经后端派生子进程执行命令；命令输出以解码后的原文写入 ``content`` 和展示数据。
        """
        log.info(
            "terminal_command_started",
            extra={
                "msg": "终端命令开始执行",
                "data": {"command_preview": command[:200]},
            },
        )

        if execution_context is None:
            return tool_error(
                self.name,
                "could not run the command: execution context is missing",
                reason="continue without retrying until the runtime supplies an execution context.",
                retryable=False,
                permission=self.permission,
            )

        if shell not in self._available_shells:
            return tool_error(
                self.name,
                f"could not run the command: shell '{shell}' is not available on this host",
                reason=(
                    f"choose one of the available shell values: {', '.join(self._available_shells)}"
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
        result = create_backend("local").execute(
            command, str(cwd), effective, shell=shell, output_sink=output_sink
        )

        log.info(
            "terminal_command_finished",
            extra={
                "msg": "终端命令执行完成",
                "data": {
                    "exit_code": result.exit_code,
                    "timed_out": result.timed_out,
                },
            },
        )

        content = self._render_content(result.output, result)
        # 超时强杀意味着命令未正常结束，模型无法从退出码判断成败，按瞬态故障
        # 返回 error（retryable=True），避免把超时命令的部分输出误判为成功结果。
        if result.timed_out:
            return tool_error(
                self.name,
                content,
                reason=(
                    f"use a larger timeout (up to {self.max_command_timeout:.0f}s) or split "
                    "the command into smaller steps."
                ),
                retryable=True,
                permission=self.permission,
                status_hint="命令超时",
            )
        return tool_success(
            tool_name=self.name,
            content=content,
            permission=self.permission,
            display_data=build_terminal_display_data(
                command=command,
                workdir=cwd,
                output=result.output,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
            ),
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
        definition = ToolDefinition(
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
                surface="standalone",
                expandable=True,
                expand_layout="terminal",
                show_result=False,
            ),
        )
        return replace(definition, parameters_schema=self._build_parameters_schema())

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
            f"command blocked by the safety policy: {verdict.description}",
            reason=(
                "use a dedicated file tool for filesystem changes or choose a "
                "non-destructive command."
            ),
            permission=self.permission,
        )

    @staticmethod
    def _render_content(output: str, result: ExecutionResult) -> str:
        """把命令输出与机器可读的执行元数据拼成模型可见文本。

        参数:
            命令输出的原始解码文本，保留 ANSI 控制序列和敏感文本。
            result: 后端归一化的执行结果（含退出码 / 超时）。

        返回:
            前缀了 ``[exit_code=N]`` 等机器可读标记的文本。``content`` 是模型
            唯一可消费文本通道（``data`` 会在序列化前被清除，仅供前端），因此
            退出码、超时这些结构性事实必须并入 ``content``，否则模型无法
            区分「命令成功但无输出」与「命令失败但无 stderr」。

        异常:
            无。

        副作用:
            无（纯字符串拼接）。
        """
        markers = [f"[exit_code={result.exit_code}]"]
        prefix = "".join(markers)
        return f"{prefix}\n{output}" if output else prefix

    def _workdir_error_observation(self, err: str) -> ToolObservation:
        """构造工作目录错误的观测。"""
        return tool_error(
            self.name,
            err,
            reason="provide a safe, existing directory inside the workspace root.",
            retryable=True,
            permission=self.permission,
        )


def build_execute_terminal_definition() -> ToolDefinition:
    """构造 execute_terminal 工具定义。

    命令执行时的允许作用域（工作区根）由运行时经 ``execution_context.workspace_root``
    注入，不在此处绑定到任何具体工作区根目录，因此该工厂无参数。

    返回:
        ``ToolDefinition``，供 ``ToolRegistry`` 注册。

    异常:
        无。

    副作用:
        创建 ``ExecuteTerminalTool`` 实例与定义对象，不执行命令。
    """
    return ExecuteTerminalTool().to_definition()
