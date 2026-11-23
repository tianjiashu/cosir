"""execute_terminal 工具实现。

本模块只承载 execute_terminal 这一个工具：参数复核、工作目录解析、前后台编排与
``ToolObservation`` 组装。命令怎样启动、怎样结束与清理在执行后端
（``app.tools.terminal_runtime.command.local_backend``，经 Terminal Worker 的执行模式）。

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
from collections.abc import Sequence
from dataclasses import replace
from pathlib import Path
from typing import Literal, cast

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.tool.execute.tool_output_budget import clip_head_tail
from app.core.tool.placeholder_settlement import USER_CANCELLED_HINT
from app.core.tool.results.tool_cancelled import tool_cancelled
from app.core.tool.results.tool_error import tool_error
from app.core.tool.results.tool_success import tool_success
from app.core.tool.schemas import (
    TOOL_EXECUTE_TERMINAL,
    RunInBackground,
    ToolDefinition,
    ToolExecutionContext,
    ToolObservation,
)
from app.core.tool.schemas.model_tool_schema import strict_parameters_schema
from app.core.tool.schemas.tool_grouping import TOOL_GROUP_TERMINAL
from app.core.tool.terminal_approval import yielding as terminal_approval_yielding
from app.core.tool.terminal_approval.registry import (
    CancelCause,
    PendingTerminalApproval,
    terminal_approvals,
)
from app.core.tool.tool_base import HandlerBase
from app.core.workflow.cancel.run_cancellation_registry import cancellation_registry
from app.core.workflow.cancel.tool_call_cancellation_registry import should_stop_call
from app.models.message_context.message_metadata.tool_message_metadata import (
    BackgroundWork,
)
from app.tools.display.terminal_display import (
    build_terminal_display_data,
    build_terminal_started_display_data,
)
from app.tools.file_system.facts.file_version import FileVersion
from app.tools.file_system.knowledge import FileKnowledge
from app.tools.file_system.write_attribution import write_attribution
from app.tools.shell.host_shells import available_shells
from app.tools.terminal_runtime.command.command_output import CommandOutput, CommandOutputSummary
from app.tools.terminal_runtime.command.execution_result import ExecutionResult
from app.tools.terminal_runtime.command.local_backend import (
    LocalExecutionBackend,
    shell_display_name,
)
from app.tools.terminal_runtime.command.running_command_registry import running_commands
from app.tools.terminal_runtime.permissions.policy import TerminalExecutionPolicy, worker_sandbox
from app.tools.tool_handler.terminal.command_job import CommandConclusion, CommandJob
from app.tools.tool_handler.terminal.command_results import command_results
from app.tools.tool_handler.terminal.live_output_channel import LiveOutputChannel
from app.tools.tool_models.call_intent import MAX_INTENT_LENGTH
from app.tools.tool_models.terminal.execute_terminal_args import (
    DEFAULT_COMMAND_TIMEOUT_S,
    MAX_COMMAND_TIMEOUT_S,
    ExecuteTerminalShell,
    resolve_execute_terminal_args_model,
)

# 平台无关的工具描述：只写选用与调用决策需要的执行语义（无状态、无 stdin / 终端、前台占步、
# 不能长驻）。其余各有唯一出处，不在此复述：平台 shell、命令语法、timeout、workdir 与后台回投在
# 参数模型各字段。不点名 terminal_write：它只给侧栏会话；terminal_read /
# terminal_stop 在所有内置 Agent 里都与本工具同时出现，可以点名。
# stdin 是 DEVNULL、POSIX 上命令在新会话里没有控制终端：读输入的程序立刻读到输入结束，要终端的
# 程序（密码提示、全屏程序、git rebase -i 之类的交互参数）打不开 /dev/tty，所以写「失败」而不是「挂起」。
# 结构与 Git 一节对照 Claude Code 的 Bash 工具描述（根目录 claude-code-system-prompt.md）：Git 守则
# 放在这里而不是系统提示词，因为只有能跑命令的 Agent 用得上。调用卡片只保留输出的最近一段
# （core/tool/terminal_output_window），所以要求模型在回复里写出要紧的结果。


# 中文译文：在一个全新的 shell 进程里执行一条非交互命令或脚本，返回退出码与合并后的
# stdout/stderr。
# - 每次调用都从头开始：cd、导出的变量等 shell 状态不会带到下一次。用 workdir 代替 cd，有依赖的
#   步骤写进同一条命令（&&、管道、多行脚本）。
# - 没有 stdin、也没有终端：要输入的程序会读到输入结束、通常直接失败，要终端的程序（密码提示、
#   全屏程序、git rebase -i / git add -i 这类交互参数）跑不起来。带上 --yes 之类的非交互参数。
# - 用户在调用卡片上只看到输出的最后一段；要紧的结果写进回复。
# - 用终端移动、删除文件（mv / rm，PowerShell 用 Move-Item / Remove-Item）。改动现有文件前先读，
#   覆盖前先读全文。
# - 前台调用会一直占着这一步，直到命令退出或超时。可能跑两分钟以上的（构建、测试套件）设
#   background=true，继续做别的事；不要反复 terminal_read 轮询，非等结果不可时用 wait_messages。
#   给出时长门槛，「长」才有可操作的判断标准（对照 Claude Code 的约两分钟）。
# - 命令用 & 或 nohup 留下的进程会在它结束时被结束（自行脱离会话的守护进程除外），改用
#   background=true。
# - 只在工作期间需要的服务 / watcher 用 background=true 跑：用 terminal_read（wait until=match）等就绪
#   行，不要 sleep 轮询；用完 terminal_stop 结束。它不会自己结束，不要用 wait_messages 等它。
# Git：GitHub 操作用 gh；PR、issue、评论里的 @名字 会通知真人，只对真要通知的账号写裸 @，角色、
# 占位符与代码放进反引号或去掉 @；用户要求才提交或推送，在默认分支上先开分支。
_DESCRIPTION = (
    "Run a non-interactive command or script in a fresh shell process and return its exit code "
    "with merged stdout/stderr.\n"
    "\n"
    "- Each call starts from scratch: cd, exported variables and other shell state do not carry "
    "over. Set workdir instead of cd, and chain dependent steps in one command (&&, pipes, a "
    "multi-line script).\n"
    "- There is no stdin and no terminal: a program that asks for input reads end-of-input and "
    "usually fails, and one that needs a terminal (password prompts, full-screen apps, "
    "interactive flags such as git rebase -i or git add -i) cannot run. Pass non-interactive "
    "flags such as --yes.\n"
    "- The user sees only the tail of the output on the call card; state the results that "
    "matter in your reply.\n"
    "- Output here never counts as having read a file: file tools only edit what you read with "
    "read_file. After a command changes a file, read it again with read_file before editing it. "
    "Check a file's current contents before you move, delete, or overwrite it here.\n"
    "- Move and delete files here (mv / rm, or Move-Item / Remove-Item in PowerShell).\n"
    "- A foreground call holds this step until the command exits or times out. For anything "
    "likely to run longer than about two minutes (builds, test suites), set background=true and "
    "keep working. Do not poll it with repeated terminal_read; if you cannot go on without its "
    "result, use wait_messages.\n"
    "- Processes the command leaves behind with & or nohup are stopped when it exits (daemons "
    "that detach into their own session are not); use background=true instead.\n"
    "- A server or watcher you only need while working can run with background=true: wait for "
    "its ready line with terminal_read (wait until=match) instead of sleep loops, and stop it "
    "with terminal_stop when done. It never ends on its own, so do not use wait_messages on it.\n"
    "\n"
    "# Git\n"
    "- Use the gh CLI for GitHub operations (PRs, issues, API).\n"
    "- GitHub notifies the owner of any @name written in a PR, issue, or comment. Write a bare "
    "@name only for a real login you mean to notify; put roles, placeholders and code "
    "(@reviewer, @types/node) in backticks or drop the @.\n"
    "- Commit or push only when the user asks. If on the default branch, branch first."
)

# 取消后的模型文案：命令被强杀，结果未知且可能留下部分副作用。
_CANCELLED_REASON = (
    "the command was stopped before it finished (the user or the run cancelled it), together "
    "with every process it started. Partial side effects are possible; do not re-run it "
    "automatically."
)

# 结局之后给模型的下一步提示：前台观察与后台结论共用，是模型正文的一部分（计入帧）。
_END_HINTS: dict[str, str] = {
    "timed_out": (
        f"Use a larger timeout (up to {MAX_COMMAND_TIMEOUT_S:g}s) "
        "or split the command into smaller steps."
    ),
    # worker 失联前命令可能已经执行。
    "worker_failed": (
        "The command may have run (fully or partly); check its effects before running it again."
    ),
}

# 模型正文 = 帧（结局标记、失败原因、结局提示）+ 命令输出。``MAX_OUTPUT_CHARS`` 是整条正文的上限，
# 命令输出的预算要先扣掉帧：帧在命令结束时才知道，落盘阈值在命令开始时就要定，所以按上界预留——
# 标记约 20 字、提示约 130 字，失败原因是内部生成的短文案。帧不超过预留时正文恒在上限内，执行器的
# 通用预算对本工具是空操作，不会把标记里的日志路径截掉（写法同 ``read_file`` 的 ``page_frame_reserve``）。
_RESULT_FRAME_RESERVE = 512

# 命令改动了本会话读过的文件时，结局标记后附一行提醒（终端对文件系统不透明，只核对读过的文件，
# 见 :meth:`ExecuteTerminalTool._read_files_changed`）。同样在命令开始时就从输出预算里预留。
_CHANGED_READS_NOTE_BUDGET = 800
_CHANGED_READS_LISTED = 8

# 前台等待定期检查 Run 取消与转后台信号；命令结论到达时立即唤醒。
_CANCEL_CHECK_SECONDS = 0.25
_PlaceholderReason = Literal["background", "detached"]

# 批准没等到决定时给模型的说明，按撤销原因区分：被新输入取代与被用户停止要的下一步不一样。
_APPROVAL_CANCELLED: dict[CancelCause, str] = {
    "user_message": "Terminal approval was superseded by new user input; the command was not started. "
    "Address the new input before taking further actions.",
    "stopped": "The user stopped this session before approving; the command was not started. "
    "Do not request it again unless the user asks.",
    "deleted": "Terminal approval was cancelled; the command was not started.",
    "call_cancelled": "Terminal approval was cancelled; the command was not started.",
    "shutdown": "Terminal approval was cancelled because the app is shutting down; the command was not started.",
}



class ExecuteTerminalTool(HandlerBase):
    """在本机 shell 中执行一条终端命令（前台等待或后台运行）的工具。

    本工具不持有任何工作根目录状态；命令执行时的允许作用域由运行时经
    ``execution_context.workspace_root`` 注入，而非构造参数。``args_model`` 按宿主平台装配
    （Windows / macOS 各一套），工具名始终为 ``execute_terminal``；构造时探测本机可用 shell（见
    ``__init__``），不执行命令。
    """

    name = TOOL_EXECUTE_TERMINAL
    description = _DESCRIPTION
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
        intent: str = "",
        shell: ExecuteTerminalShell = "auto",
        timeout: float | None = None,
        workdir: str | None = None,
        background: bool | None = None,
        escalate_sandbox: bool | None = None,
        *,
        execution_context: ToolExecutionContext,
    ) -> ToolObservation:
        """在本机 shell 执行一条命令（前台或后台）并返回归一化观测。

        参数:
            command: 待执行命令。
            intent: 模型写的调用意图，只用作后台看板与等待提示的标签，以及审批卡上的说明。
            shell: shell 选择；``auto`` 保持宿主默认 shell，也可显式选择本机可用的
                cmd、PowerShell 或 POSIX shell。该值已由当前平台的参数模型
                （``WindowsExecuteTerminalArgs`` / ``MacExecuteTerminalArgs``）校验，此处
                再按本机实际检测结果复核。
            timeout: 命令级超时秒数；显式值钳制到 ``MAX_COMMAND_TIMEOUT_S``。缺省时前台为
                ``DEFAULT_COMMAND_TIMEOUT_S``，后台不限时（开发服务器要常驻，后台工作属于 Session，
                由单停、上游收回、删除、``terminal_stop`` 或后端退出结束，用户停止 Session 不结束它）。
                前台转到后台时沿用启动时的时限。
            workdir: 工作目录；缺省 ``execution_context.writable_root``（未收窄的 Session 即工作区根）；
                相对路径相对工作区根解析，目录必须存在且满足本条命令的可读范围。
            background: ``True`` 立即转后台；``None`` 与 False 均在前台等待。
            escalate_sandbox: 本条命令升级沙箱（网络与工作区外写入，控制目录仍受保护），请求批准模式下由用户决定。
            execution_context: 本次执行的运行时边界（会话、Run、根路径和冻结权限模式）；
                Rust 对命令及其子进程执行 OS 隔离，后台观察沿用同一有效策略。

        返回:
            ``ToolObservation``，状态按结局（``CommandEnd``）一处映射，见 ``_result_observation``：
            命令自己结束（含非零退出、被信号结束）为 ``success``，超时、启动失败、worker 失联与工作
            目录不合法为 ``error``，被取消为 ``cancelled``。结局标记经 ``_render_content`` 并入
            ``content`` 供模型消费。转到后台时返回标 ``background`` 的占位（不挂起 Agent），结论稍后
            以 ``background_result`` 回投。

        异常:
            不主动向上抛出；均转换为结构化 ``ToolObservation``。

        副作用:
            经 Terminal Worker 在本条命令的有效策略（OS 沙箱）下执行；终端对文件系统不透明，不建立
            也不推进已读资格（见 ``app.tools.file_system``）。实时输出持续推给前端（含转后台后的占位
            卡片），作业按调用标识登记在 ``command_results``。
        """
        log.info(
            "terminal_command_requested",
            extra={
                "msg": "收到终端命令请求",
                "data": {"command_preview": command[:200]},
            },
        )

        if shell not in self._available_shells:
            return tool_error(
                self.name,
                f"could not run the command: shell '{shell}' is not available on this host. "
                f"Choose one of the available shell values: {', '.join(self._available_shells)}",
            )

        # 缺省在 Session 可写根执行；显式 workdir 只定起点，随后按有效策略检查读范围。
        if workdir:
            cwd, err = self._resolve_workdir(workdir, execution_context.workspace_root)
        else:
            cwd, err = execution_context.writable_root.resolve(), ""
        if err:
            return self._workdir_error_observation(err)

        try:
            escalated = bool(escalate_sandbox)
            policy = TerminalExecutionPolicy.for_command(
                mode=execution_context.terminal_permission_mode,
                workspace=execution_context.workspace_root,
                writable_root=execution_context.writable_root,
                scratch=execution_context.scratch_dir,
                escalated=escalated,
            )
            if not policy.can_read(cwd):
                raise ValueError("工作目录位于终端禁读区域")
            if not policy.full_access:
                capability = worker_sandbox()
                if not capability.supported:
                    raise ValueError(f"请求批准模式的沙箱不可用：{capability.reason}")
                if escalated:
                    approval = self._await_permission(
                        execution_context, command, shell, str(cwd), intent
                    )
                    if approval.decision == "cancelled":
                        return tool_cancelled(
                            self.name, reason=_APPROVAL_CANCELLED[approval.cancel_cause or "call_cancelled"]
                        )
                    if approval.decision == "reject":
                        return tool_error(
                            self.name,
                            "The user rejected escalating the sandbox for this command. "
                            "The command was not started; do not bypass this decision.",
                        )
            if should_stop_call(execution_context.session_id, execution_context.tool_call_id):
                return tool_cancelled(self.name, reason="The command was cancelled before launch.")
        except (ValueError, OSError) as exc:
            return tool_error(self.name, str(exc), status_hint="终端权限")
        log.info(
            "terminal_policy_resolved",
            extra={
                "msg": "终端单次权限已确定",
                "data": {
                    "session_id": execution_context.session_id,
                    "run_id": execution_context.run_id,
                    "tool_call_id": execution_context.tool_call_id,
                    "mode": execution_context.terminal_permission_mode,
                    "network": policy.network,
                    "writable_roots": policy.writable_roots,
                    "writable_exceptions": policy.writable_exceptions,
                },
            },
        )
        effective_timeout = (
            None
            if background and timeout is None
            else min(timeout or DEFAULT_COMMAND_TIMEOUT_S, MAX_COMMAND_TIMEOUT_S)
        )
        return self._execute_command(
            command,
            cwd,
            intent=intent,
            shell=shell,
            timeout_s=effective_timeout,
            background=bool(background),
            execution_context=execution_context,
            policy=policy,
        )

    def _await_permission(
        self,
        context: ToolExecutionContext,
        command: str,
        shell: str,
        cwd: str,
        intent: str,
    ) -> PendingTerminalApproval:
        """在原调用内等到决定（或让位、被撤销）并返回收尾后的请求；不重发命令。"""
        request = terminal_approval_yielding.admit(
            origin_session_id=context.session_id,
            run_id=context.run_id,
            tool_call_id=context.tool_call_id,
            command=command,
            shell=shell_display_name(shell),
            cwd=cwd,
            intent=intent,
        )
        try:
            while not request.ready.wait(0.1):
                if should_stop_call(context.session_id, context.tool_call_id):
                    terminal_approvals.cancel(request, "call_cancelled")
            return request
        finally:
            # 正常决定后这里是空操作；异常退出时不留下无人等待的批准卡。
            terminal_approvals.cancel(request, "call_cancelled")

    def _execute_command(
        self,
        command: str,
        cwd: Path,
        *,
        intent: str,
        shell: ExecuteTerminalShell,
        timeout_s: float | None,
        background: bool,
        execution_context: ToolExecutionContext,
        policy: TerminalExecutionPolicy,
    ) -> ToolObservation:
        """启动命令并返回前台结果或后台占位；等待与结果路由均按调用创建，不存入工具实例。

        仅前台工具线程等待命令，Run 取消时通知后端停止并等结果收尾；用户转后台与命令完成
        由登记表裁决先后，转后台时登记表接过结论订阅，交接期间到达的结论照样回投。超时与进程清理
        由执行后端负责。
        """
        tool_call_id = execution_context.tool_call_id
        channel = None
        running = None
        try:
            channel = LiveOutputChannel.open(execution_context)
            if channel is not None:
                # 先于命令拉起：卡片从启动起就有命令可看，输出块之后只往上追加。
                channel.start(build_terminal_started_display_data(command=command, workdir=cwd))
            # 运行中可读：先把命令登记进登记表，再让输出收集点同时喂它（整理后的行）。
            running = running_commands.start(
                session_id=execution_context.session_id,
                command_id=tool_call_id,
                command=command,
                shell_kind=shell_display_name(shell),
                cwd=str(cwd),
            )
            # 命令输出的唯一收集点：卡片要原始文本，模型与读取要整理后的行；全文只在超过预算时落盘。
            output = CommandOutput(
                workspace_root=Path(execution_context.workspace_root),
                model_budget_chars=self.model_output_budget(),
                on_text=channel.emit if channel is not None else None,
                on_lines=running.feed,
            )
            backend = LocalExecutionBackend()
        except BaseException:
            if channel is not None:
                channel.finish()
            if running is not None:
                running_commands.finish(execution_context.session_id, tool_call_id, None)
            raise

        admitted = threading.Event()

        def work() -> CommandConclusion:
            admitted.wait()
            result: ExecutionResult | None = None
            try:
                result = backend.execute(
                    command,
                    str(cwd),
                    timeout_s,
                    output,
                    shell=shell,
                    extra_env={
                        "COSIR_SCRATCH_DIR": str(execution_context.scratch_dir),
                        "TMPDIR": str(execution_context.scratch_dir),
                        "TMP": str(execution_context.scratch_dir),
                        "TEMP": str(execution_context.scratch_dir),
                    },
                    policy=policy,
                )
            except Exception:
                log.exception(
                    "terminal_command_execution_failed",
                    extra={"data": {"tool_call_id": tool_call_id}},
                )
                result = ExecutionResult(
                    end="worker_failed", failure="the command did not report its outcome"
                )
            finally:
                output.finish()
                if channel is not None:
                    channel.finish()
                running_commands.finish(execution_context.session_id, tool_call_id, result)
            summary = output.summary()
            # 输出日志写盘失败不影响结果（模型正文只是少了日志路径），但要能排查：此时升为 warning。
            assert result is not None
            log_finished = log.warning if summary.log_error else log.info
            log_finished(
                "terminal_command_finished",
                extra={
                    "msg": "终端命令执行完成",
                    "data": {
                        "session_id": execution_context.session_id,
                        "tool_call_id": tool_call_id,
                        "end": result.end,
                        "exit_code": result.exit_code,
                        "signal": result.signal,
                        "failure": result.failure,
                        "output_chars": summary.model_total_chars,
                        "output_lines": summary.model_total_lines,
                        "output_overflowed": summary.overflowed,
                        "output_log_path": summary.log_path,
                        "output_log_error": summary.log_error,
                        "display_omitted_lines": summary.display_omitted_lines,
                    },
                },
            )
            output_text = self._bounded_output(summary)
            changed = self._read_files_changed(execution_context)
            return self._conclusion(command, cwd, result, summary, output_text, changed)

        # 撤销经唤醒回调直接停下命令（``backend.stop``），结论仍由 ``work`` 带着已收集输出返回。
        try:
            job = CommandJob.start_in_thread(
                work, wake=backend.stop, name=f"execute-terminal-{tool_call_id[-8:]}"
            )
        except BaseException:
            backend.stop()
            admitted.set()
            output.finish()
            if channel is not None:
                channel.finish()
            running_commands.finish(execution_context.session_id, tool_call_id, None)
            raise
        detached = None if background else threading.Event()
        try:
            registered = command_results.register(
                tool_call_id,
                session_id=execution_context.session_id,
                run_id=execution_context.run_id,
                label=self._background_label(intent, command),
                job=job,
                detached=detached,
            )
        except Exception:
            job.cancel()
            raise
        finally:
            admitted.set()
        if registered is None:
            # 拒收（Session 正被收回或后端关闭）：作业已停下，按它的取消结论返回，不留后台占位。
            return self._result_observation(cast(CommandConclusion, job.wait(None)))
        if detached is None:
            return self._placeholder_observation(
                command, cwd, timeout_s, "background", registered, tool_call_id
            )
        while True:
            conclusion = job.wait(_CANCEL_CHECK_SECONDS)
            if conclusion is not None:
                break
            if detached.is_set():
                # 登记表在转后台时已接过结论订阅。
                return self._placeholder_observation(
                    command, cwd, timeout_s, "detached", registered, tool_call_id
                )
            if cancellation_registry.is_cancelled(execution_context.session_id):
                # Run 取消只停前台等待：与转后台在登记表的锁里裁决，已转后台的命令照常运行。
                if (
                    command_results.cancel_foreground(execution_context.session_id, tool_call_id)
                    == "detached"
                ):
                    return self._placeholder_observation(
                        command, cwd, timeout_s, "detached", registered, tool_call_id
                    )
                conclusion = cast(CommandConclusion, job.wait(None))
                break
        outcome = command_results.settle_foreground(execution_context.session_id, tool_call_id)
        if outcome == "detached" or (outcome == "gone" and detached.is_set()):
            # 转后台先于前台结算：结论已由登记表回投，即使随后被撤销，工具仍须返回后台占位。
            return self._placeholder_observation(
                command, cwd, timeout_s, "detached", registered, tool_call_id
            )
        return self._result_observation(conclusion)

    @staticmethod
    def _background_label(intent: str, command: str) -> str:
        """看板与 ``wait_messages`` 提示的标签：优先模型写的意图，漏写时退回命令首个非空行。

        截到意图的长度上限，免得多行脚本整段进入给模型的等待提示；侧栏另有截断。
        """
        if intent.strip():
            return intent.strip()
        first_line = next((line.strip() for line in command.splitlines() if line.strip()), "")
        return first_line[:MAX_INTENT_LENGTH]

    def _result_observation(self, conclusion: CommandConclusion) -> ToolObservation:
        """前台拿到结论时的观察：观察状态只按结局映射，这是结局到状态的唯一一处。

        正文与卡片展示都取自结论（:meth:`_conclusion` 在作业线程里拼好），前台与后台回投是同一份。
        """
        payload = conclusion.result
        end = payload["end_reason"]
        if end == "failed":
            return tool_error(self.name, f"Command execution failed: {payload['error']}")
        content = cast(str, payload["output"])
        if end == "cancelled":
            observation = tool_cancelled(self.name, reason=f"{_CANCELLED_REASON}\n{content}")
        elif end == "timed_out":
            observation = tool_error(self.name, content, status_hint="命令超时")
        elif end == "start_failed":
            observation = tool_error(self.name, content, status_hint="启动失败")
        elif end == "worker_failed":
            observation = tool_error(self.name, content, status_hint="结果未知")
        else:
            observation = tool_success(self.name, content)
        # 状态说明执行结局，展示数据保留执行器实际收集的输出；不从模型正文还原。
        observation.display_data = {
            **(conclusion.display_data or {}),
            **(observation.display_data or {}),
        }
        return observation

    def _conclusion(
        self,
        command: str,
        cwd: Path,
        result: ExecutionResult,
        summary: CommandOutputSummary,
        output_text: str,
        changed: list[tuple[str, FileVersion | None]],
    ) -> CommandConclusion:
        """命令的结论，作业线程里拼一次：前台据它组装观察，转到后台后经登记表原样回投。

        给模型的正文是 :meth:`_render_content`（已在预算内，后台结论通道没有执行器的通用预算，也不需要）；
        给卡片的终态展示是原始输出的最近一段（完整输出在日志文件里）。撤销的命令仍携带已收集输出，
        上游收回时只结算原占位，不回投模型消息。
        """
        payload: dict[str, object] = {
            "end_reason": result.end,
            "output": self._render_content(result, output_text, changed),
        }
        if result.exit_code is not None:
            payload["exit_code"] = result.exit_code
        if result.signal is not None:
            payload["signal"] = result.signal
        return CommandConclusion(
            payload,
            cancel_hint=USER_CANCELLED_HINT if result.end == "cancelled" else None,
            display_data=self._display_data(command, cwd, result, summary),
        )

    @staticmethod
    def _display_data(
        command: str, cwd: Path, result: ExecutionResult, summary: CommandOutputSummary
    ) -> dict[str, object]:
        return build_terminal_display_data(
            command=command,
            workdir=cwd,
            output=summary.display,
            end=result.end,
            exit_code=result.exit_code,
            signal=result.signal,
            omitted_lines=summary.display_omitted_lines,
            log_path=summary.log_path,
        )

    def _placeholder_observation(
        self,
        command: str,
        cwd: Path,
        timeout_s: float | None,
        reason: _PlaceholderReason,
        work: BackgroundWork,
        terminal_id: str,
    ) -> ToolObservation:
        """命令转到后台时的占位（``background``）：正文带 ``terminal_id``，让 Agent 能读、能停。

        不挂起：Agent 主动选后台、或用户把等待转走，都是让 Agent 先做别的；需要结果时它自己用
        wait_messages 等。
        """
        situation = {
            "background": "the command is running in the background "
            f"({'no timeout' if timeout_s is None else f'timeout {timeout_s:g}s'}); keep working",
            "detached": "the user moved this command to the background; keep working",
        }[reason]
        return tool_success(
            tool_name=self.name,
            content=(
                f"{situation}. terminal_id: {terminal_id}. Read its output with terminal_read "
                "and stop it with terminal_stop; its result will arrive as a "
                "<background_result> message for this tool call (use wait_messages if you need "
                "to pause for it)."
            ),
            display_data=build_terminal_started_display_data(command=command, workdir=cwd),
            waiting=RunInBackground(work),
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
            handler=self.execute,
            args_model=self.args_model,  # 作业须由后端进程持有才能转到后台，见模块 docstring
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
        if not resolved.is_dir():
            return Path(), f"workdir not found: {resolved}"
        return resolved, ""

    @staticmethod
    def model_output_budget() -> int:
        """命令输出在模型正文里能占的字符数：整条正文上限扣掉帧预留（见 ``_RESULT_FRAME_RESERVE``）。

        ``CommandOutput`` 按它决定何时落盘、头尾各留多少，:meth:`_render_content` 按它裁剪，两处同一个数。
        """
        return (
            Constant.Tools.MAX_OUTPUT_CHARS
            - _RESULT_FRAME_RESERVE
            - ExecuteTerminalTool._changed_reads_note_budget()
        )

    @staticmethod
    def _changed_reads_note_budget() -> int:
        # 正文上限调小时（测试、受限模型）按比例缩，不让预留吃掉命令输出。
        return min(_CHANGED_READS_NOTE_BUDGET, Constant.Tools.MAX_OUTPUT_CHARS // 5)

    @staticmethod
    def _read_files_changed(context: ToolExecutionContext) -> list[tuple[str, FileVersion | None]]:
        """命令结束时核对本会话读过的文件（不扫描工作区，每个读过的路径 stat 一次）：变了的当场告诉
        模型，并以「命令期间观察到」登记写入者，别的会话的读后变更通知据此写明修改者。

        比对起点是命令启动时的已读证据，命令期间别的进程的写入也会被算进来，所以登记为 ``by_command``
        （措辞是「很可能」）。终端不因此建立已读资格：模型要改这些文件仍得重新 ``read_file``。
        """
        changed = FileKnowledge.of(context).stale()
        if changed:
            write_attribution.record_command(context.session_id, changed)
        return changed

    @staticmethod
    def _changed_reads_note(changed: Sequence[tuple[str, FileVersion | None]]) -> str:
        paths = [repr(path) for path, _ in changed[:_CHANGED_READS_LISTED]]
        more = len(changed) - len(paths)
        listed = ", ".join(paths) + (f", and {more} more" if more else "")
        note = (
            f"This command changed or removed files you read earlier: {listed}. "
            "Read them again with read_file before editing them."
        )
        return note[: ExecuteTerminalTool._changed_reads_note_budget()]

    @staticmethod
    def _render_content(
        result: ExecutionResult,
        output: str,
        changed: Sequence[tuple[str, FileVersion | None]] = (),
    ) -> str:
        """拼出给模型的完整正文：结局标记、失败原因、整理后的命令输出、结局提示，前后台共用这一份。

        ``content`` 是模型唯一可消费文本通道（``data`` 会在序列化前被清除，仅供前端），所以结局
        必须并入 ``content``，否则模型无法区分「命令成功但无输出」与「命令失败但无 stderr」。标记与
        结局一一对应，由 :meth:`ExecutionResult.end_marker` 唯一提供；没启动或结局未知时紧跟一行
        原因，超时与结局未知时末尾再跟一行下一步提示（``_END_HINTS``）。前台的观察状态与后台载荷的
        ``end_reason`` 也表达结局，三者同源于 ``result.end``。

        正文在这里一次拼完、之后不再追加：命令输出按 :meth:`model_output_budget` 收集与裁剪，帧在预留
        之内，整条正文因此不超过 ``MAX_OUTPUT_CHARS``。溢出时用 ``CommandOutput`` 已落盘的头尾拼出
        「头 + 标记 + 尾」，标记指向完整日志；若在这之后再追加文字，执行器的通用预算会截第二次，正好切掉
        中段的这个标记。
        """
        lines = [result.end_marker()]
        if changed:
            lines.append(ExecuteTerminalTool._changed_reads_note(changed))
        if result.failure:
            lines.append(result.failure)
        content = "\n".join(lines)
        if output:
            content = f"{content}\n{output}"
        hint = _END_HINTS.get(result.end)
        if hint is not None:
            # 整理后的输出逐行带换行结尾，提示直接接在后面；没有输出时另起一行。
            content = f"{content}{hint}" if content.endswith("\n") else f"{content}\n{hint}"
        return content

    @staticmethod
    def _bounded_output(summary: CommandOutputSummary) -> str:
        """按预算拼出模型可见输出。"""
        if not summary.overflowed:
            return summary.model_head
        clipped = clip_head_tail(
            summary.model_head,
            summary.model_tail,
            total_chars=summary.model_total_chars,
            total_lines=summary.model_total_lines,
            artifact_path=summary.log_path,
            max_chars=ExecuteTerminalTool.model_output_budget(),
        )
        return clipped.text

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
            f"{err}. " "Provide a safe, existing directory inside the workspace root.",
        )


def build_execute_terminal_definition() -> ToolDefinition:
    """构造 execute_terminal 工具定义。

    命令执行时的允许作用域（工作区根）由运行时经 ``execution_context.workspace_root``
    注入，不在此处绑定到任何具体工作区根目录，因此该工厂无参数。
    """
    return ExecuteTerminalTool().to_definition()
