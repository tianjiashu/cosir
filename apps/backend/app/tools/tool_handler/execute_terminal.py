"""execute_terminal 工具实现。

本模块只承载 execute_terminal 这一个工具：参数复核、工作目录解析、前后台编排与
``ToolObservation`` 组装。命令怎样启动、怎样结束与清理在执行后端
（``app.tools.tool_handler.terminal.local_backend``，经 Terminal Worker 的执行模式）。

前后台执行由本工具编排：命令包成线程作业（默认前台、一直等到结束，模型可
直接选后台，用户可把前台等待转走；没有超时自动转后台，转到后台也从不挂起 Agent）。所以本工具是
thread 工具：命令要能在本次调用返回后继续跑，作业必须由后端进程持有，不能放进每次调用结束即
被杀的工具子进程；超时由执行后端落实，撤销（取消调用、Run 停止）经作业的唤醒回调立即停下命令。

平台 shell 契约（可下发哪些 shell、命令该用什么语法）的唯一事实源是参数模型
（``tool_models/execute_terminal_args.py``）：本模块的本机探测直接以该契约为候选清单，经
``app.tools.shell.host_shells`` 定位，把实测可用的 shell 收窄进模型可见 schema，不另立一份
shell 清单。
"""

import platform
import threading
from collections.abc import Callable
from dataclasses import replace
from pathlib import Path
from typing import Literal

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.tool.background.background_job import BackgroundConclusion, BackgroundJob
from app.core.tool.background.background_registry import background_registry
from app.core.tool.background.run import start_background_job
from app.core.tool.execute.tool_output_budget import ToolOutputBudget
from app.core.tool.results.tool_cancelled import tool_cancelled
from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    TOOL_EXECUTE_TERMINAL,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.schemas.model_tool_schema import strict_parameters_schema
from app.core.tool.tool_base import HandlerBase
from app.core.tool.tool_grouping import TOOL_GROUP_TERMINAL
from app.core.workflow.cancel.conversation_run_cancellation_registry import cancellation_registry
from app.models.message_metadata import BackgroundWork
from app.tools.display.terminal_display import (
    build_terminal_display_data,
    build_terminal_placeholder_display_data,
)
from app.tools.shell.host_shells import available_shells
from app.tools.tool_handler.terminal import ExecutionResult, LocalExecutionBackend
from app.tools.tool_handler.terminal.output_stream import TerminalOutputStream
from app.tools.tool_models.execute_terminal_args import (
    DEFAULT_COMMAND_TIMEOUT_S,
    MAX_COMMAND_TIMEOUT_S,
    ExecuteTerminalShell,
    resolve_execute_terminal_args_model,
)

# 平台无关的工具描述：只写选用与调用决策需要的执行语义（无状态、无 stdin / 终端、前台占步、
# 不能长驻）。其余各有唯一出处，不在此复述：平台 shell、命令语法、timeout、workdir 与后台回投在
# 参数模型各字段；「读文件别用 shell」在系统提示词，只在 Agent 同时有文件工具时出现（没有文件
# 工具的 Agent 只能用本工具读）。不点名 terminal_* 工具：子 Agent 与 Team 节点只有本工具，交互
# 终端只开放给侧栏主 Agent，由它们的描述指回本工具。
# stdin 是 DEVNULL、POSIX 上命令在新会话里没有控制终端：读输入的程序立刻读到输入结束，要终端的
# 程序（密码提示、全屏程序）打不开 /dev/tty，所以写「失败」而不是「挂起」。


# 中文译文：在一个全新的 shell 进程里执行一条非交互命令或脚本，返回退出码与合并后的
# stdout/stderr。每次调用都从头开始：cd、导出的变量等 shell 状态不会带到下一次，有依赖的步骤
# 写进同一条命令（&&、管道、多行脚本）。没有 stdin、也没有终端：要输入的程序会读到输入结束、
# 通常直接失败，要终端的程序（密码提示、全屏程序）跑不起来，所以带上 --yes 之类的非交互参数。
# 前台调用会一直占着这一步，直到命令退出或超时；长构建、测试套件把 timeout 调大并设
# background=true，期间继续做别的事。命令用 & 或 nohup 留下的进程会在它结束时被结束（自行脱离会话
# 的守护进程除外），所以要后台就用 background=true。必须一直开着（服务、watcher）或要交互的程序不
# 适合本工具：到 timeout 会被结束。
_DESCRIPTION = (
    "Run a non-interactive command or script in a fresh shell process and return its exit code "
    "with merged stdout/stderr. Each call starts from scratch: cd, exported variables and other "
    "shell state do not carry over, so chain dependent steps in one command (&&, pipes, a "
    "multi-line script). There is no stdin and no terminal: a program that asks for input reads "
    "end-of-input and usually fails, and one that needs a terminal (password prompts, "
    "full-screen apps) cannot run, so pass non-interactive flags such as --yes. A foreground "
    "call holds this step until the command exits or times out; for long builds and test "
    "suites raise timeout and set background=true to keep working meanwhile. Processes the "
    "command leaves behind (& or nohup) are stopped when it exits (daemons that detach into "
    "their own session are not), so use background=true instead. Programs that must keep "
    "running (servers, watchers) or need interaction do not fit this tool: they are stopped at "
    "the timeout."
)

# 取消后的模型文案：命令被强杀，结果未知且可能留下部分副作用。
_CANCELLED_REASON = (
    "the command was stopped before it finished (the user or the run cancelled it), together "
    "with every process it started. Partial side effects are possible; do not re-run it "
    "automatically."
)

# 结局未知时给模型的提醒：worker 失联前命令可能已经执行。
_UNKNOWN_OUTCOME_HINT = (
    "The command may have run (fully or partly); check its effects before running it again."
)

# 前台等待定期检查 Run 取消与转后台信号；命令结论到达时立即唤醒。
_CANCEL_CHECK_SECONDS = 0.25
_PlaceholderReason = Literal["background", "detached"]


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
        """探测本机真正可用的 shell，只查可执行文件不启动 shell。

        候选清单与定位都来自 shell 目录（``host_shells.available_shells``），参数模型的 ``Literal``
        也由同一份声明派生，所以本机探测、模型可见 ``enum`` 与参数校验始终一致，顺序即目录声明
        顺序，``'auto'`` 恒为首位。平台名由 ``__init__`` 传入，与参数模型选型共用 ``is_windows``。

        参数:
            platform_name: ``platform.system()`` 的取值（``__init__`` 已去除首尾空白并兜底为
                ``"unknown"``）。

        返回:
            以 ``"auto"`` 开头、按契约声明顺序排列的可用 shell 元组；检测不到任何显式 shell 时
            只有 ``("auto",)``。

        异常:
            无。

        副作用:
            只读进程环境与文件系统（见 ``find_shell``），不启动任何进程。
        """

        return ("auto", *available_shells(system=platform_name))

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
            background: ``True`` 立即转后台；``None`` 与 False 均在前台等待。
            execution_context: 本次执行的运行时边界（任务 / 工作区 / 根路径）。workdir 只是命令的
                起点：命令自己可以 cd 或用绝对路径，文件系统副作用不受工作区约束
                （命令级隔离待沙箱接管）。

        返回:
            ``ToolObservation``，状态按结局（``CommandEnd``）一处映射，见 ``_result_observation``：
            命令自己结束（含非零退出、被信号结束）为 ``success``，超时、启动失败、worker 失联与工作
            目录不合法为 ``error``，被取消为 ``cancelled``。结局标记经 ``_render_content`` 并入
            ``content`` 供模型消费。转到后台时返回标 ``background`` 的占位（不挂起 Agent），结论稍后
            以 ``background_result`` 回投。

        异常:
            不主动向上抛出；均转换为结构化 ``ToolObservation``。

        副作用:
            经 Terminal Worker 执行命令；输出持续经实时通道推给前端（含转后台后的占位卡片）；以本次
            ``tool_call_id`` 在 ``background_registry`` 登记作业。
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

        execution_root = execution_context.workspace_root
        cwd, err = self._resolve_workdir(workdir, execution_root)
        if err:
            return self._workdir_error_observation(err)

        effective_timeout = min(timeout or DEFAULT_COMMAND_TIMEOUT_S, MAX_COMMAND_TIMEOUT_S)
        return self._execute_command(
            command, cwd, shell=shell, timeout_s=effective_timeout,
            background=bool(background), execution_context=execution_context,
        )

    def _execute_command(
        self, command: str, cwd: Path, *, shell: ExecuteTerminalShell, timeout_s: float,
        background: bool, execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """启动命令并返回前台结果或后台占位；等待与结果路由均按调用创建，不存入工具实例。

        仅前台工具线程等待命令，Run 取消时通知后端停止并等结果收尾；用户转后台与命令完成
        由登记表裁决先后，路由保证交接期间到达的结论仍能回投。超时与进程清理由执行后端负责。
        """
        tool_call_id = execution_context.tool_call_id
        channel = TerminalOutputStream.open(execution_context)
        # 作业线程跑完命令后在这里留下完整结果：前台拿到结论时按它渲染观察（模型正文另过预算）。
        finished: list[ExecutionResult] = []

        backend = LocalExecutionBackend()

        def work(_cancelled: Callable[[], bool]) -> BackgroundConclusion:
            # 撤销不靠轮询这个标记：作业的唤醒回调直接停下命令（``backend.stop``）。
            # 实时通道随命令而不是随工具调用结束：转到后台后命令还在跑，输出照常推给占位卡片。
            try:
                result = backend.execute(
                    command,
                    str(cwd),
                    timeout_s,
                    shell=shell,
                    output_sink=channel.emit if channel is not None else None,
                )
            finally:
                if channel is not None:
                    channel.finish()
            log.info(
                "terminal_command_finished",
                extra={
                    "msg": "终端命令执行完成",
                    "data": {
                        "tool_call_id": tool_call_id,
                        "end": result.end,
                        "exit_code": result.exit_code,
                        "signal": result.signal,
                        "failure": result.failure,
                    },
                },
            )
            finished.append(result)
            return self._background_conclusion(command, cwd, result, execution_context)

        job = BackgroundJob.start_in_thread(
            work, wake=backend.stop, name=f"execute-terminal-{tool_call_id[-8:]}"
        )
        if background:
            registered = start_background_job(
                job, task_id=execution_context.task_id, run_id=execution_context.run_id,
                tool_call_id=tool_call_id, kind="terminal_command",
                label=execution_context.call_intent,
                collect_cancelled=job.wait_result, reports_user_stop=True,
            )
            return self._placeholder_observation(
                command, cwd, timeout_s, "background", registered
            )

        detached = threading.Event()
        registered = background_registry.register(
            tool_call_id,
            task_id=execution_context.task_id,
            run_id=execution_context.run_id,
            kind="terminal_command",
            label=execution_context.call_intent,
            mode="foreground",
            on_cancel=lambda _reason: job.cancel(),
            detached=detached,
            collect_cancelled=job.wait_result, reports_user_stop=True,
        )
        wait = _ForegroundWait(execution_context.task_id, tool_call_id)
        job.on_done(wait.on_result)
        while True:
            conclusion = wait.wait_result(_CANCEL_CHECK_SECONDS)
            if conclusion is not None:
                break
            if cancellation_registry.is_cancelled(execution_context.task_id):
                # Run 取消：撤销作业并等它带取消结论收尾，前台照常返回结果。
                job.cancel()
                conclusion = wait.wait_result(None)
                break
            if detached.is_set():
                wait.detach()
                return self._placeholder_observation(
                    command, cwd, timeout_s, "detached", registered
                )
        assert conclusion is not None  # 取消路径必由作业的取消结论收尾
        outcome = background_registry.settle_foreground(execution_context.task_id, tool_call_id)
        if outcome == "detached" or (outcome == "gone" and detached.is_set()):
            # 转后台先于前台结算：即使随后被撤销，工具仍须返回后台占位。
            background_registry.complete(execution_context.task_id, tool_call_id, conclusion)
            return self._placeholder_observation(
                command, cwd, timeout_s, "detached", registered
            )
        if conclusion.result.get("end_reason") == "failed":
            return tool_error(self.name, f"Command execution failed: {conclusion.result['error']}")
        return self._result_observation(command, cwd, finished[0])

    def _result_observation(
        self, command: str, cwd: Path, result: ExecutionResult
    ) -> ToolObservation:
        """前台拿到结论时的观察：观察状态只按结局映射，这是结局到状态的唯一一处。"""
        content = self._render_content(result)
        if result.end == "cancelled":
            observation = tool_cancelled(self.name, reason=_CANCELLED_REASON)
        elif result.end == "timed_out":
            observation = tool_error(
                self.name,
                f"{content}\nUse a larger timeout (up to {MAX_COMMAND_TIMEOUT_S:g}s) "
                "or split the command into smaller steps.",
                status_hint="命令超时",
            )
        elif result.end == "start_failed":
            observation = tool_error(self.name, content, status_hint="启动失败")
        elif result.end == "worker_failed":
            observation = tool_error(
                self.name, f"{content}\n{_UNKNOWN_OUTCOME_HINT}", status_hint="结果未知"
            )
        else:
            observation = tool_success(self.name, content)
        # 状态说明执行结局，展示数据保留执行器实际收集的输出；不从模型正文还原。
        observation.display_data = {
            **self._display_data(command, cwd, result),
            **(observation.display_data or {}),
        }
        return observation

    def _background_conclusion(
        self,
        command: str,
        cwd: Path,
        result: ExecutionResult,
        execution_context: ToolExecutionContext,
    ) -> BackgroundConclusion:
        """转到后台后的结论：给模型的载荷过统一输出预算（超限保留头尾、完整原文落盘）；给占位卡片
        的终态展示是完整输出（展示契约不截断），并入时写回卡片。

        作业线程里调用：前台拿到结论时不使用它（见 ``finished``），只在转后台后经
        ``background_registry.complete`` 回投。撤销的命令仍携带已收集输出，
        父 Task 停止时只结算原占位，不回投模型消息。
        """
        budgeted = ToolOutputBudget().apply(
            tool_success(self.name, self._render_content(result)),
            execution_context,
        )
        payload: dict[str, object] = {"end_reason": result.end, "output": budgeted.content}
        if result.exit_code is not None:
            payload["exit_code"] = result.exit_code
        if result.signal is not None:
            payload["signal"] = result.signal
        return BackgroundConclusion(
            payload,
            cancel_hint="已取消" if result.end == "cancelled" else None,
            display_data=self._display_data(command, cwd, result),
        )

    @staticmethod
    def _display_data(command: str, cwd: Path, result: ExecutionResult) -> dict[str, object]:
        return build_terminal_display_data(
            command=command,
            workdir=cwd,
            output=result.output,
            end=result.end,
            exit_code=result.exit_code,
            signal=result.signal,
        )

    def _placeholder_observation(
        self, command: str, cwd: Path, timeout_s: float, reason: _PlaceholderReason,
        work: BackgroundWork,
    ) -> ToolObservation:
        """命令转到后台时的占位（``background``）。

        不挂起：Agent 主动选后台、或用户把等待转走，都是让 Agent 先做别的；需要结果时它自己用
        wait_messages 等。
        """
        situation = {
            "background": f"the command is running in the background (timeout {timeout_s:g}s); "
            "keep working",
            "detached": "the user moved this command to the background; keep working",
        }[reason]
        observation = tool_success(
            tool_name=self.name,
            content=(
                f"{situation}. Its result will arrive as a <background_result> message for this "
                "tool call (use wait_messages if you need to pause for it)."
            ),
            display_data=build_terminal_placeholder_display_data(command=command, workdir=cwd),
        )
        observation.background = work
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

    @staticmethod
    def _render_content(result: ExecutionResult) -> str:
        """把结局标记、失败原因与命令输出拼成模型可见文本。

        ``content`` 是模型唯一可消费文本通道（``data`` 会在序列化前被清除，仅供前端），所以结局
        必须并入 ``content``，否则模型无法区分「命令成功但无输出」与「命令失败但无 stderr」。标记与
        结局一一对应：``[exit_code=N]``、``[signal=N]``（命令被信号结束）、``[timed_out]``、
        ``[cancelled]``、``[start_failed]``、``[outcome_unknown]``；没启动或结局未知时紧跟一行原因。
        前台的观察状态与后台载荷的 ``end_reason`` 也表达结局，三者同源于 ``result.end``。
        """
        marker = {
            "exited": f"[exit_code={result.exit_code}]",
            "signaled": f"[signal={result.signal}]",
            "timed_out": "[timed_out]",
            "cancelled": "[cancelled]",
            "start_failed": "[start_failed]",
            "worker_failed": "[outcome_unknown]",
        }[result.end]
        lines = [marker]
        if result.failure:
            lines.append(result.failure)
        if result.output:
            lines.append(result.output)
        return "\n".join(lines)

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


class _ForegroundWait:
    """终端前台等待：作业结论的中转，交给工具线程或按转后台回投。

    前台等待只有本工具使用（后台命令经 ``start_background_job`` 直接回投，不经过这里）。
    中转必须活到工具调用结束：登记条目可能在结论到达前就被撤销（转后台、Run 停止），那时工具
    线程仍要拿到结论才能返回结果。转后台与结论到达在同一把锁里裁决，避免已到的结论无人投递。
    """

    def __init__(self, task_id: int, tool_call_id: str) -> None:
        self._task_id = task_id
        self._tool_call_id = tool_call_id
        self._lock = threading.Lock()
        self._arrived = threading.Event()
        self._background = False
        self._conclusion: BackgroundConclusion | None = None

    def on_result(self, conclusion: BackgroundConclusion) -> None:
        """作业线程交付结论：存下来；已经判给后台时直接回投。"""
        with self._lock:
            self._conclusion = conclusion
            background = self._background
            self._arrived.set()
        if background:
            background_registry.complete(self._task_id, self._tool_call_id, conclusion)

    def detach(self) -> None:
        """切到后台；结论已先到时由这里补投，否则留给 :meth:`on_result`。"""
        with self._lock:
            self._background = True
            pending = self._conclusion if self._arrived.is_set() else None
        if pending is not None:
            background_registry.complete(self._task_id, self._tool_call_id, pending)

    def wait_result(self, timeout: float | None) -> BackgroundConclusion | None:
        """等结论：拿到返回它，超时返回 ``None``（``timeout=None`` 时一直等）。"""
        if not self._arrived.wait(timeout):
            return None
        with self._lock:
            return self._conclusion


def build_execute_terminal_definition() -> ToolDefinition:
    """构造 execute_terminal 工具定义。

    命令执行时的允许作用域（工作区根）由运行时经 ``execution_context.workspace_root``
    注入，不在此处绑定到任何具体工作区根目录，因此该工厂无参数。

    返回:
        ``ToolDefinition``，供 ``ToolRegistry`` 注册；返回值恒非 None——
        ``to_definition_if_avaliable`` 只在 ``avaliable()`` 为假时返回 None，而本工具未覆写
        ``avaliable()``（基类恒为 True）。保留 ``ToolDefinition`` 而非 ``ToolDefinition | None``
        标注与仓内其余 ``build_*_definition`` 一致：``ToolRegistry.register`` 的形参类型是
        ``ToolDefinition``，标成可为 None 会在 ``tool_registration`` 的注册调用点
        引入 mypy 类型错误。

    异常:
        无。

    副作用:
        创建 ``ExecuteTerminalTool`` 实例与定义对象，不执行命令。
    """
    return ExecuteTerminalTool().to_definition_if_avaliable()
