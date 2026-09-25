"""execute_terminal 工具实现。

本模块只承载 execute_terminal 这一个工具。危险命令裁决、后端选择、命令执行与
``ToolObservation`` 组装都在 ``ExecuteTerminalTool`` 内完成；不含 subprocess 细节
（下沉到 ``app.tools.tool_handler.terminal.local_backend``）。

平台 shell 契约（可下发哪些 shell、命令该用什么语法）的唯一事实源是参数模型
（``tool_models/execute_terminal_args.py``）：本模块的本机探测直接以该契约为候选清单，只负责
「怎么在宿主机上找到它」，并把实测可用的 shell 收窄进模型可见 schema，不另立一份 shell 清单。
"""

import os
import platform
import shutil
from dataclasses import replace
from pathlib import Path
from typing import get_args

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.tools.display.terminal_display import build_terminal_display_data
from app.core.tools.schemas import (
    TOOL_EXECUTE_TERMINAL,
    TOOL_GROUP_TERMINAL,
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
    ExecuteTerminalShell,
    MacExecuteTerminalShell,
    WindowsExecuteTerminalShell,
    is_windows_host,
    resolve_execute_terminal_args_model,
)

# 平台无关的工具描述：命令执行语义（前台、非交互、输出合并、退出码、工作目录边界、命令
# 策略）在此说明，平台 shell 细节一律由参数模型的 ``shell`` 描述承载，避免同一契约写两遍。
_DESCRIPTION = (
    "Execute one foreground, non-interactive command locally in the current workspace. There "
    "is no interactive stdin, so commands that wait for input or open a pager time out. stdout "
    "and stderr are merged; the result carries the exit code and bounded output. The working "
    "directory defaults to the workspace root and must stay inside it, and some destructive "
    "commands are blocked by the current command policy. Read the shell parameter description "
    "to pick a shell available on this host and match the command syntax to it."
)


class ExecuteTerminalTool(HandlerBase):
    """在本机 shell 中同步执行一条终端命令的工具。

    严格对齐现状文件工具的既定范式：类属性契约 + ``execute`` 实例方法 +
    类内 ``to_definition()`` 构造 ``ToolDefinition`` + 模块级
    ``build_execute_terminal_definition`` 薄封装。

    本工具不持有任何工作根目录状态；命令执行时的允许作用域由运行时经
    ``execution_context.workspace_root`` 注入（``execute`` 的
    ``execution_context`` 关键字参数），而非构造参数。

    ``args_model`` 与 ``description`` 按宿主平台装配：按平台选定参数模型（Windows / macOS
    各一套），工具名始终为 ``execute_terminal``。

    返回:
        ``ExecuteTerminalTool`` 实例。

    异常:
        初始化阶段不主动抛出业务异常。

    副作用:
        无（实例构造不执行命令、不读取文件系统、不保存状态）。
    """

    name = TOOL_EXECUTE_TERMINAL
    description = _DESCRIPTION
    permission = "execute_terminal"
    timeout_seconds = 120.0  # 外层 ToolHandlerRunner 硬保险
    risk_level = "high"
    default_command_timeout = 60.0  # 内层命令级缺省
    max_command_timeout = 110.0  # 内层钳制上限（< 外层 120 留 10s 收尾）
    group = TOOL_GROUP_TERMINAL


    def __init__(self) -> None:
        """初始化 execute_terminal 工具实例。

        参数:
            无。平台和可用 shell 均由当前 backend 进程自动检测。

        返回:
            无。

        异常:
            无。

        副作用:
            读取当前平台与可执行文件搜索路径：按平台选定参数模型
            （``WindowsExecuteTerminalArgs`` / ``MacExecuteTerminalArgs``）并检测本机可用
            shell；不执行命令、不读取 workspace。
        """
        self._platform_name = (platform.system() or "").strip() or "unknown"
        self.args_model = resolve_execute_terminal_args_model(self._platform_name)
        self._available_shells = self._detect_available_shells(self._platform_name)

    @staticmethod
    def _detect_available_shells(platform_name: str) -> tuple[str, ...]:
        """按参数契约探测本机真正可用的 shell，只查可执行文件不启动 shell。

        候选值直接取自当前平台参数类的 ``shell`` Literal（``get_args``），顺序即契约声明顺序，
        因此「契约新增或移除某个 shell」会自动同步到本机探测与模型可见 ``enum``——本方法不维护
        第二份 shell 清单，只描述「怎么在宿主机上找到它」：Windows 按 ``<shell>.exe`` 查 PATH
        （``cmd`` 除 PATH 外只要 ``COMSPEC`` 已设置即视为可用，与 ``local_backend`` 用
        ``shell=True`` 交由 ``COMSPEC`` 解释的口径一致），POSIX 按名字查 PATH（``sh`` 是软链，
        按绝对路径探测以免依赖 PATH 是否包含 ``/bin``）。

        返回值的顺序即模型可见 ``enum`` 的顺序，``'auto'`` 恒为首位；平台名由 ``__init__`` 传入，
        且与参数模型选型共用 ``is_windows_host``，不在本方法重复探测 ``platform.system()``。

        参数:
            platform_name: ``platform.system()`` 的取值（``__init__`` 已去除首尾空白并兜底为
                ``"unknown"``）。

        返回:
            以 ``"auto"`` 开头、按契约声明顺序排列的可用 shell 元组；检测不到任何显式 shell 时
            只有 ``("auto",)``。

        异常:
            无。

        副作用:
            只读进程环境（``COMSPEC``）与 PATH（``shutil.which``）、探测 ``/bin/sh`` 是否存在；
            不启动任何进程。
        """

        windows = is_windows_host(platform_name)
        contract = WindowsExecuteTerminalShell if windows else MacExecuteTerminalShell
        shells: list[str] = ["auto"]
        for shell in get_args(contract):
            if shell == "auto":
                continue
            if windows:
                if shutil.which(f"{shell}.exe") or (shell == "cmd" and os.environ.get("COMSPEC")):
                    shells.append(shell)
            elif shell == "sh":
                if Path("/bin/sh").is_file() and os.access("/bin/sh", os.X_OK):
                    shells.append(shell)
            elif shutil.which(shell):
                shells.append(shell)
        return tuple(shells)

    def _build_parameters_schema(self) -> dict[str, object]:
        """构造模型可见参数 schema：平台契约取参数模型，本机收敛只做收窄。

        平台差异（``shell`` 的取值集合与语法说明）由 ``args_model`` 提供，本方法只在其上做本机
        实测收敛：把静态枚举收窄为本机真实可用的 shell，并把可用值追加到平台描述之后。这样
        「枚举取值」与「描述里的可用值」同源，不会各写一份而漂移。

        参数:
            无。使用初始化阶段选定的 ``args_model`` 与检测到的可用 shell。

        返回:
            ``args_model.model_json_schema()`` 的投影（``shell`` 的 ``enum`` / ``description``
            已按本机可用值收敛）；参数模型未声明 ``shell`` 属性时原样返回并记 WARNING。

        异常:
            无。

        副作用:
            只修改本次调用新生成的 schema 字典，不写实例状态；``shell`` 属性缺失时写一条
            WARNING 日志（``execute_terminal_shell_schema_missing``）。
        """

        schema = self.args_model.model_json_schema()
        properties = schema.get("properties")
        if not isinstance(properties, dict):
            # 参数模型必然有 properties；走到这里说明 pydantic 投影结构异常，原样返回即可，
            # 平台契约仍由 args_model 校验兜住。
            return schema
        shell_schema = properties.get("shell")
        if not isinstance(shell_schema, dict):
            # 平台参数模型必须声明 shell 字段：缺失意味着契约被改坏，模型会拿到未经本机收敛的
            # 描述，必须留下排查线索而不是静默降级。
            log.warning(
                "execute_terminal_shell_schema_missing",
                extra={
                    "msg": "execute_terminal 参数模型缺少 shell 属性，schema 未按本机收敛",
                    "data": {
                        "tool": self.name,
                        "args_model": self.args_model.__name__,
                    },
                },
            )
            return schema
        shell_schema["enum"] = list(self._available_shells)
        platform_description = shell_schema.get("description")
        shell_schema["description"] = self._append_available_shells(
            platform_description if isinstance(platform_description, str) else ""
        )
        return schema

    def _append_available_shells(self, platform_description: str) -> str:
        """把本机实测可用的 shell 追加到平台静态描述之后。

        参数:
            platform_description: 参数模型里该平台 ``shell`` 字段的静态描述。

        返回:
            ``platform_description`` 非空时返回 ``"<平台描述> Available on this host: ..."``；
            为空时只返回 ``"Available on this host: ..."`` 一句，不留前导空格。

        异常:
            无。

        副作用:
            无。
        """

        values = ", ".join(f"'{shell}'" for shell in self._available_shells)
        availability = f"Available on this host: {values}."
        if not platform_description:
            return availability
        return f"{platform_description} {availability}"

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
            command: 待执行命令。
            shell: shell 选择；``auto`` 保持宿主默认 shell，也可显式选择本机可用的
                cmd、PowerShell 或 POSIX shell。该值已由当前平台的参数模型
                （``WindowsExecuteTerminalArgs`` / ``MacExecuteTerminalArgs``）校验，此处
                再按本机实际检测结果复核。
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
            可直接注册到 ``ToolRegistry`` 的工具定义：``args_model`` 为当前宿主平台的参数模型，
            ``parameters_schema`` 为其按本机可用 shell 收敛后的模型可见投影。

        异常:
            无。

        副作用:
            无。
        """
        definition = ToolDefinition(
            name=self.name,
            group=self.group,
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
        if not Constant.Tools.WORKDIR_SAFE_RE.match(workdir):
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
        """把灾难级命令裁决结果转成面向模型的错误观测。

        参数:
            verdict: ``detect_dangerous_command`` 的裁决结果，提供命中原因 ``description``。

        返回:
            ``status="error"`` 且 ``retryable`` 缺省为 False 的 ``ToolObservation``：命令被策略
            确定性拒绝，原样重试必然再次失败，故不给重试提示，只给出替代动作。

        异常:
            无。

        副作用:
            无（纯构造）。
        """
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
            output: 命令输出的原始解码文本，保留 ANSI 控制序列和敏感文本。
            result: 后端归一化的执行结果（含退出码 / 超时）。

        返回:
            ``[exit_code=N]`` 标记与输出以换行拼接的文本；命令无输出时只有标记本身。
            ``content`` 是模型唯一可消费文本通道（``data`` 会在序列化前被清除，仅供前端），
            因此退出码这类结构性事实必须并入 ``content``，否则模型无法区分「命令成功但无输
            出」与「命令失败但无 stderr」。超时**不**并入 ``content``：``execute`` 在
            ``result.timed_out`` 为真时改走 ``tool_error`` 表达，本方法只负责退出码与输出。

        异常:
            无。

        副作用:
            无（纯字符串拼接）。
        """
        marker = f"[exit_code={result.exit_code}]"
        return f"{marker}\n{output}" if output else marker

    def _workdir_error_observation(self, err: str) -> ToolObservation:
        """把 workdir 解析错误转成面向模型的错误观测。

        参数:
            err: ``_resolve_workdir`` 给出的人读错误（非法字符 / 越界 / 目录不存在）。

        返回:
            ``status="error"`` 且 ``retryable=True`` 的 ``ToolObservation``：三类原因都能通过换一个
            合法的 workspace 内目录修正，故给出重试提示。

        异常:
            无。

        副作用:
            无（纯构造）。
        """
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
        ``ToolDefinition``，供 ``ToolRegistry`` 注册；返回值恒非 None——
        ``to_definition_if_avaliable`` 只在 ``avaliable()`` 为假时返回 None，而本工具未覆写
        ``avaliable()``（基类恒为 True）。保留 ``ToolDefinition`` 而非 ``ToolDefinition | None``
        标注与仓内其余 ``build_*_definition`` 一致：``ToolRegistry.register`` 的形参类型是
        ``ToolDefinition``，标成可为 None 会在 ``tool_system`` 的注册调用点引入 mypy 类型错误。

    异常:
        无。

    副作用:
        创建 ``ExecuteTerminalTool`` 实例与定义对象，不执行命令。
    """
    return ExecuteTerminalTool().to_definition_if_avaliable()
