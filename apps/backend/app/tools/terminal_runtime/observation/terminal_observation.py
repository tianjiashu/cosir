"""一个终端的 Agent 观察事实：逻辑行缓冲 + shell 集成语义（命令边界、退出结果、cwd）。

会话的命令事件来自 shell 集成；运行中的 ``execute_terminal`` 命令用同一词表合成（``RunningCommand``），
输出经 ``pipe_output`` 写入。命令只由这些事件创建；输入接受、输出静默与提示符文字都不构成命令返回。命令输出区间按
「事件到达时光标所在逻辑行」记录：worker 保证语义事件前的输出与屏幕快照先到，因此事件时刻的
光标位置就是命令输出的边界。

线程模型：由所属终端的锁串行访问；不做 IO、不写数据库。
"""

from __future__ import annotations

import time
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Any, Literal

from app.tools.terminal_runtime.errors import TerminalError
from app.tools.terminal_runtime.observation.line_buffer import TerminalLineBuffer

# 只有用户会发起命令：Agent 只把一行填进提示符，回车只能由用户按。
InputSource = Literal["user"]

# 一条命令的状态。结束有三种，区别在「命令是不是真的不在跑了」：
# - returned：shell 报告返回，带退出结果；
# - ended：确知已经结束、但没有退出结果（提示符回来了却没收到返回事件、又开了一条新命令、Agent 的
#   命令被停止或超时）；
# - tracking_ended：跟丢了，命令可能还在跑或下落不明（会话结束、shell 集成不可用、worker 失联）。
# ``until=command_returned`` 只认前两种：把跟丢当成返回，会让 Agent 以为被打断的命令已经跑完。
CommandStatus = Literal["executing", "returned", "ended", "tracking_ended"]
COMMAND_FINISHED: frozenset[CommandStatus] = frozenset({"returned", "ended"})

# 在下一次读取输入（提示符已打印之后）才报告 returned 的 shell：结束行取光标行 - 1。
_RETURN_AFTER_PROMPT_SHELLS = frozenset({"powershell", "pwsh"})


@dataclass
class TerminalCommand:
    """一条由集成事件界定的命令；``status`` 见 :data:`CommandStatus`。

    ``end_line`` 在命令离开 executing 时确定；``result`` 是集成报告的原样结果（含 ``code``），只在
    returned 时有值；``reason`` 只在 ended / tracking_ended 时有值。
    ``started_by`` 取命令开始时最近一次被接受输入的来源（会话启动脚本等无输入时为 None）：集成事件
    不带发起方，而回车一定先于 ``command_started`` 送达，所以「最近一次输入」就是发起命令的那次。
    """

    number: int
    command: str
    start_line: int
    started_by: InputSource | None
    status: CommandStatus = "executing"
    result: dict[str, Any] | None = None
    reason: str | None = None
    end_line: int | None = None

    @property
    def output_lines(self) -> tuple[int, int] | None:
        """已结束命令的输出行闭区间；末行早于起始行（命令无输出）或尚未结束时为 None。"""
        end = self.end_line
        return (self.start_line, end) if end is not None and end >= self.start_line else None


class TerminalObservation:
    """逻辑行 + shell 状态；``information_version`` 在任何 Agent 可见变化时推进。

    ``prompt_has_input`` 是「这个提示符出现之后，用户或 Agent 是否已经往这一行写过东西」：Agent
    填入的准入依据，也替代了旧的幂等缓存（同一个提示符至多填一次）。它只在 ``shell_prompt`` 到达时
    复位，与屏幕内容无关。
    """

    def __init__(self, shell_kind: str, integration: str) -> None:
        self.shell_kind = shell_kind
        self.buffer = TerminalLineBuffer()
        self.information_version = 0
        self.last_output_at = time.monotonic()
        self.phase = "unknown"
        self.cwd: str | None = None
        self.command_counter = 0
        self.latest_command: TerminalCommand | None = None
        self.integration_status = "starting" if integration == "auto" else "off"
        self.prompt_has_input = False

    @property
    def active_command(self) -> TerminalCommand | None:
        command = self.latest_command
        return command if command is not None and command.status == "executing" else None

    def output_arrived(self) -> None:
        self.last_output_at = time.monotonic()

    def update_screen(self, screen: dict[str, Any], history: list[str]) -> bool:
        """应用屏幕事件；返回几何尺寸是否变化（供交互通道推送 geometry 帧）。"""
        resized = (self.buffer.rows, self.buffer.columns) != (screen["rows"], screen["columns"])
        self.buffer.apply(screen, history)
        self.information_version += 1
        return resized

    def pipe_output(self, lines: Sequence[str], pending: str) -> None:
        """管道来源的输出：写进行缓冲、推进信息版本、记下最近输出时刻（output_quiet 等待用）。"""
        self.buffer.append_lines(lines, pending)
        self.output_arrived()
        self.information_version += 1

    def shell_event(
        self, event: dict[str, Any], *, last_input_by: InputSource | None = None
    ) -> str | None:
        """应用一条集成事件，返回用于日志的事件名；与当前状态矛盾的事件收敛跟踪而不报错。

        ``last_input_by`` 由会话 runtime 给出，只在 ``command_started`` 时记为命令发起方。
        执行模式没有 shell：运行中命令的开始与结束也按同一词表喂进来，由 ``RunningCommand`` 负责。
        """
        kind = event["kind"]
        if kind == "ready":
            self.integration_status = "ready"
        elif kind == "unavailable":
            self.end_tracking(event["reason"])
            self.integration_status = "unavailable"
        elif kind == "command_started":
            if self.active_command is not None:
                # 新命令开始了，上一条一定已经不在跑，只是没收到它的返回事件。
                self.end_command("unexpected_command_boundary")
            self.command_counter += 1
            self.phase = "executing"
            self.latest_command = TerminalCommand(
                number=self.command_counter,
                command=event["command"][:1024],
                start_line=self.buffer.cursor_line,
                started_by=last_input_by,
            )
        elif kind == "command_returned":
            command = self.active_command
            if command is None:
                return None
            command.status = "returned"
            command.result = event["result"]
            command.end_line = self._end_line()
            self.phase = "unknown"  # 只有随后的 prompt 标记才证明回到输入阶段。
        elif kind == "shell_prompt":
            if self.active_command is not None:
                # 提示符回来了：命令已经结束，只是没收到返回事件。
                self.end_command("prompt_without_command_result")
            self.phase = "editing"
            self.cwd = event.get("cwd")
            self.prompt_has_input = False
        else:
            raise TerminalError(f"unknown shell observation: {kind}")
        self.information_version += 1
        return str(kind)

    def _end_line(self) -> int:
        """命令输出末行：光标在行首说明输出以换行结束，末行是上一行。"""
        cursor = self.buffer.cursor_line
        if self.shell_kind in _RETURN_AFTER_PROMPT_SHELLS or self.buffer.cursor_column == 0:
            return cursor - 1
        return cursor

    def note_input(self) -> None:
        """记下「这个提示符上已经有输入」；输入入队成功之后由会话运行时调用（之后 worker 才写进 PTY）。

        Agent 填入与用户按键都算。提示符上已有输入后，Agent 填入被拒（见会话运行时的填入准入），
        直到下一个 ``shell_prompt`` 把它复位。
        """
        self.prompt_has_input = True

    def end_command(self, reason: str) -> None:
        """正在执行的命令确知已经结束、但没有退出结果（见 :data:`CommandStatus` 的 ``ended``）。"""
        self._close_active("ended", reason)

    def end_tracking(self, reason: str) -> None:
        """跟丢了正在执行的命令：它可能还在跑，结局未知（见 :data:`CommandStatus` 的 ``tracking_ended``）。"""
        self._close_active("tracking_ended", reason)

    def _close_active(self, status: CommandStatus, reason: str) -> None:
        command = self.active_command
        if command is not None:
            command.status = status
            command.reason = reason
            command.end_line = self._end_line()
            self.information_version += 1
        self.phase = "unknown"
