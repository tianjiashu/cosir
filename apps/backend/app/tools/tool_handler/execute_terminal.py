"""execute_terminal 工具实现。

本模块只承载 execute_terminal 这一个工具。危险命令裁决、后端选择、命令执行与
``ToolObservation`` 组装都在 ``ExecuteTerminalTool`` 内完成；不含 subprocess 细节
（下沉到 ``app.tools.tool_handler.terminal.local_backend``）。

前后台执行：命令包成 ``ThreadJob`` 交给 ``run_deferred``（默认前台，到上限自动转后台，模型可直接选
后台，用户可把前台等待转走）。所以本工具是 thread 工具：命令要能在本次调用返回后继续跑，作业必须
由后端进程持有，不能放进每次调用结束即被杀的工具子进程；超时与取消都由执行后端杀进程树。

平台 shell 契约（可下发哪些 shell、命令该用什么语法）的唯一事实源是参数模型
（``tool_models/execute_terminal_args.py``）：本模块的本机探测直接以该契约为候选清单，只负责
「怎么在宿主机上找到它」，并把实测可用的 shell 收窄进模型可见 schema，不另立一份 shell 清单。
"""

import os
import platform
import shutil
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import get_args

from app.config.constant import Constant
from app.config.logging.logger import log
from app.tools.display.terminal_display import build_terminal_display_data
from app.tools.policy.terminal_denylist import (
    DangerousCommandVerdict,
    detect_dangerous_command,
)
from app.tools.policy.terminal_denylist_configuration import (
    read_terminal_deny_patterns,
)
from app.core.tool.schemas import (
    TOOL_EXECUTE_TERMINAL,
    ToolDefinition,
    ToolDisplayHints,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.background.deferred_job import PlaceholderReason, ThreadJob
from app.core.tool.background.run_deferred import run_deferred
from app.core.tool.execute.tool_output_budget import ToolOutputBudget
from app.core.tool.schemas.model_tool_schema import strict_parameters_schema
from app.core.tool.results.tool_cancelled import tool_cancelled
from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.tool_grouping import TOOL_GROUP_TERMINAL
from app.core.workflow.cancel.conversation_run_cancellation_registry import cancellation_registry
from app.tools.display.terminal_display import build_terminal_background_display_data
from app.tools.tool_handler.terminal import ExecutionResult, LocalExecutionBackend
from app.tools.tool_handler.terminal.output_stream import TerminalOutputStream
from app.core.tool.tool_base import HandlerBase
from app.tools.tool_models.execute_terminal_args import (
    DEFAULT_COMMAND_TIMEOUT_S,
    EXECUTE_TERMINAL_DEFERRED,
    MAX_COMMAND_TIMEOUT_S,
    ExecuteTerminalShell,
    MacExecuteTerminalShell,
    WindowsExecuteTerminalShell,
    is_windows_host,
    resolve_execute_terminal_args_model,
)

# 平台无关的工具描述：只写选用与调用决策需要的执行语义（进程不保留状态、无 stdin、前后台与时长、
# 命令策略）；平台 shell、命令语法与 workdir 规则由参数模型各字段承载，不在此复述。不点名
# terminal_* 工具：子 Agent 与 Team 节点只有本工具，交互终端只开放给侧栏主 Agent。
_DESCRIPTION = (
    "Run one non-interactive command in a fresh shell process and return its exit code with "
    "merged stdout/stderr. Nothing carries over between calls (cd, exported variables, shell "
    "state), so chain dependent steps in one command. There is no stdin: prompts and pagers "
    "hang until the timeout. Use it for long builds, test suites, and other long tasks: set "
    "timeout to cover them and background=true to keep working meanwhile. Not suited to "
    "servers or watchers that never exit. Some destructive commands are blocked by policy."
)

# 取消后的模型文案：命令被强杀，结果未知且可能留下部分副作用。
_CANCELLED_REASON = (
    "the command was stopped before it finished (the user or the run cancelled it) and its "
    "process tree was killed. Partial side effects are possible; do not re-run it "
    "automatically."
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
    # thread 工具的 timeout_seconds 只是元数据（执行器不强杀）；命令级缺省与钳制见参数模型常量。
    timeout_seconds = MAX_COMMAND_TIMEOUT_S
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
            统一 strict schema 的投影（``shell`` 的 ``enum`` / ``description``
            已按本机可用值收敛）；参数模型未声明 ``shell`` 属性时原样返回并记 WARNING。

        异常:
            无。

        副作用:
            只修改本次调用新生成的 schema 字典，不写实例状态；``shell`` 属性缺失时写一条
            WARNING 日志（``execute_terminal_shell_schema_missing``）。
        """

        schema = strict_parameters_schema(self.args_model)
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
        background: bool | None = None,
        *,
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """在本机 shell 执行一条命令（前台或后台）并返回归一化观测。

        参数:
            command: 待执行命令。
            shell: shell 选择；``auto`` 保持宿主默认 shell，也可显式选择本机可用的
                cmd、PowerShell 或 POSIX shell。该值已由当前平台的参数模型
                （``WindowsExecuteTerminalArgs`` / ``MacExecuteTerminalArgs``）校验，此处
                再按本机实际检测结果复核。
            timeout: 命令级超时秒数；缺省 ``DEFAULT_COMMAND_TIMEOUT_S``，钳制到
                ``MAX_COMMAND_TIMEOUT_S``。
            workdir: 工作目录；缺省 ``execution_context.workspace_root``；相对路径相对
                该根解析；绝对路径直接使用。
            background: ``True`` 立即转后台；``None`` 取 ``EXECUTE_TERMINAL_DEFERRED`` 的默认（前台）。
            execution_context: 本次执行的运行时边界（任务 / 工作区 / 根路径）。命令的全部文件系统
                副作用由 workdir 边界与 deny-list 共同约束，而非仅靠 cwd 宣称。

        返回:
            ``ToolObservation``。灾难级命令/工作目录不存在/后端异常为
            ``status="error"``；命令正常执行（含非零退出码）为 ``status="success"``。
            退出码等结构性事实经 ``_render_content`` 并入 ``content`` 的 ``[exit_code=N]`` 标记，供
            模型消费。命令因超时强杀时返回 ``status="error"``，被取消时返回 ``cancelled``。转到后台
            时返回标 ``background`` 的占位（只有到达前台上限才挂起 Agent），结论稍后以
            ``background_result`` 回投。

        异常:
            不主动向上抛出；均转换为结构化 ``ToolObservation``。

        副作用:
            经后端派生子进程执行命令；前台期间输出经实时通道推给前端，转到后台后不再推送；以本次
            ``tool_call_id`` 在 ``deferred_registry`` 登记作业。
        """
        log.info(
            "terminal_command_started",
            extra={
                "msg": "终端命令开始执行",
                "data": {"command_preview": command[:200]},
            },
        )

        if shell not in self._available_shells:
            return tool_error(
                self.name,
                f"could not run the command: shell '{shell}' is not available on this host. "
                f"Choose one of the available shell values: {', '.join(self._available_shells)}",
            )

        try:
            verdict = detect_dangerous_command(
                command,
                read_terminal_deny_patterns(),
            )
        except (OSError, ValueError, TimeoutError) as exc:
            log.exception(
                "terminal_denylist_evaluation_failed",
                extra={
                    "msg": "终端 deny-list 无法可靠校验命令，已拒绝执行",
                    "data": {"error_type": type(exc).__name__},
                },
            )
            return tool_error(
                self.name,
                "command was not run because the terminal deny-list could not be evaluated. "
                "Repair the terminal deny-list configuration before retrying.",
            )
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

        effective_timeout = min(timeout or DEFAULT_COMMAND_TIMEOUT_S, MAX_COMMAND_TIMEOUT_S)
        tool_call_id = execution_context.tool_call_id
        channel = TerminalOutputStream.open(execution_context)
        # 作业线程跑完命令后在这里留下完整结果：前台渲染要完整输出（展示数据不截断），回投载荷
        # 则要过输出预算，两者不能共用作业结论。
        finished: list[ExecutionResult] = []

        def work(cancelled: Callable[[], bool]) -> dict[str, object]:
            result = LocalExecutionBackend().execute(
                command,
                str(cwd),
                effective_timeout,
                shell=shell,
                output_sink=channel.emit if channel is not None else None,
                is_cancelled=cancelled,
            )
            log.info(
                "terminal_command_finished",
                extra={
                    "msg": "终端命令执行完成",
                    "data": {
                        "tool_call_id": tool_call_id,
                        "exit_code": result.exit_code,
                        "timed_out": result.timed_out,
                        "cancelled": result.cancelled,
                    },
                },
            )
            finished.append(result)
            return self._background_payload(result, execution_context)

        outcome = run_deferred(
            # 撤销只置位标记，执行后端按时间片复查取消，不需要额外唤醒。
            ThreadJob(work, wake=lambda: None, name=f"execute-terminal-{tool_call_id[-8:]}"),
            task_id=execution_context.task_id,
            run_id=execution_context.run_id,
            tool_call_id=tool_call_id,
            kind="terminal_command",
            label=f"terminal_command:{command[:80]}",
            background=EXECUTE_TERMINAL_DEFERRED.resolve_background(background),
            foreground_limit_s=EXECUTE_TERMINAL_DEFERRED.foreground_limit_s,
            is_cancelled=lambda: cancellation_registry.is_cancelled(execution_context.task_id),
        )
        if channel is not None:
            # 前台结束或转到后台都关闭实时通道：后台占位已闭合，之后的增量没有可更新的运行中卡片。
            channel.finish()
        if outcome.placeholder is not None:
            return self._placeholder_observation(command, cwd, effective_timeout, outcome.placeholder)
        return self._result_observation(command, cwd, finished[0])

    def _result_observation(
        self, command: str, cwd: Path, result: ExecutionResult
    ) -> ToolObservation:
        """前台拿到结论时的观察：取消、超时、正常结束（含非零退出码）三种。"""
        if result.cancelled:
            return tool_cancelled(self.name, reason=_CANCELLED_REASON)
        content = self._render_content(result.output, result)
        # 超时强杀意味着命令未正常结束，模型无法从退出码判断成败，按瞬态故障
        # 返回 error 并建议加大超时或拆分命令，避免把超时命令的部分输出误判为成功结果。
        if result.timed_out:
            return tool_error(
                self.name,
                f"{content}. "
                f"Use a larger timeout (up to {MAX_COMMAND_TIMEOUT_S:g}s) or split "
                "the command into smaller steps.",
                status_hint="命令超时",
            )
        return tool_success(
            tool_name=self.name,
            content=content,
            display_data=build_terminal_display_data(
                command=command,
                workdir=cwd,
                output=result.output,
                exit_code=result.exit_code,
                timed_out=result.timed_out,
            ),
        )

    def _background_payload(
        self, result: ExecutionResult, execution_context: ToolExecutionContext
    ) -> dict[str, object]:
        """转到后台后回投给模型的结论；输出过统一的输出预算（超限保留头尾、完整原文落盘）。

        作业线程里调用：前台拿到结论时不使用它（见 ``finished``），只在转后台后经
        ``deferred_registry.complete`` 回投。
        """
        end_reason = (
            "cancelled" if result.cancelled else "timed_out" if result.timed_out else "exited"
        )
        budgeted = ToolOutputBudget().apply(
            tool_success(self.name, self._render_content(result.output, result)),
            execution_context,
        )
        return {"end_reason": end_reason, "exit_code": result.exit_code, "output": budgeted.content}

    def _placeholder_observation(
        self, command: str, cwd: Path, timeout_s: float, reason: PlaceholderReason
    ) -> ToolObservation:
        """命令转到后台时的占位（``background``）。

        只有到达前台上限时挂起：Agent 要的是这个结果，只是不该占着工具线程；Agent 主动选后台、或
        用户把等待转走时不挂起，Agent 先做别的、需要时用 wait_messages 等。
        """
        situation = {
            "background": f"the command is running in the background (timeout {timeout_s:g}s); "
            "keep working",
            "detached": "the user moved this command to the background; keep working",
            "limit": "the command is still running; it moved to the background and you are "
            "paused now",
        }[reason]
        follow_up = (
            " and resume you"
            if reason == "limit"
            else " (use wait_messages if you need to pause for it)"
        )
        observation = tool_success(
            tool_name=self.name,
            content=(
                f"{situation}. Its result will arrive as a <background_result> message for this "
                f"tool call{follow_up}."
            ),
            display_data=build_terminal_background_display_data(command=command, workdir=cwd),
            suspend=reason == "limit",
        )
        observation.deferred = True
        return observation

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
            handler=self.execute,
            args_model=self.args_model,
            timeout_seconds=self.timeout_seconds,
            resource_keys=("shell",),
            execution_mode="thread",  # 作业须由后端进程持有才能转到后台，见模块 docstring
            deferred=EXECUTE_TERMINAL_DEFERRED,
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
            ``status="error"`` 的 ``ToolObservation``：命令被策略确定性拒绝，原样重试必然
            再次失败，故只给出替代动作。

        异常:
            无。

        副作用:
            无（纯构造）。
        """
        return tool_error(
            self.name,
            f"command blocked by the safety policy: {verdict.description}. "
            "Use a dedicated file tool for filesystem changes or choose a "
            "non-destructive command.",
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
            出」与「命令失败但无 stderr」。超时与取消**不**并入 ``content``：前台由观察状态表达，
            后台由回投载荷的 ``end_reason`` 表达，本方法只负责退出码与输出。

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
            ``status="error"`` 的 ``ToolObservation``：三类原因都能通过换一个合法的 workspace
            内目录修正，故建议改用安全目录。

        异常:
            无。

        副作用:
            无（纯构造）。
        """
        return tool_error(
            self.name,
            f"{err}. "
            "Provide a safe, existing directory inside the workspace root.",
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
        ``ToolDefinition``，标成可为 None 会在 ``tool_registration`` 的注册调用点引入 mypy 类型错误。

    异常:
        无。

    副作用:
        创建 ``ExecuteTerminalTool`` 实例与定义对象，不执行命令。
    """
    return ExecuteTerminalTool().to_definition_if_avaliable()
