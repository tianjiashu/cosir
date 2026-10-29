"""单会话的 Agent 观察事实：逻辑行缓冲 + shell 集成语义（命令边界、退出结果、cwd）。

命令只由 shell 集成事件创建；输入接受、输出静默与提示符文字都不构成命令返回。命令输出区间按
「事件到达时光标所在逻辑行」记录：worker 保证语义事件前的输出与屏幕快照先到，因此事件时刻的
光标位置就是命令输出的边界。

线程模型：由所属会话锁串行访问；不做 IO、不写数据库。
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any

from app.tools.terminal_runtime.errors import TerminalSessionError
from app.tools.terminal_runtime.line_buffer import TerminalLineBuffer

# 在下一次读取输入（提示符已打印之后）才报告 returned 的 shell：结束行取光标行 - 1。
_RETURN_AFTER_PROMPT_SHELLS = frozenset({"powershell", "pwsh"})


@dataclass
class TerminalCommand:
    """一条由集成事件界定的命令；``status`` 为 executing / returned / tracking_ended。

    ``end_line`` 在命令结束（返回或收敛跟踪）时确定；``result`` 是集成报告的原样结果
    （含 ``code``），只在 returned 时有值；``reason`` 只在 tracking_ended 时有值。
    """

    number: int
    command: str
    start_line: int
    status: str = "executing"
    result: dict[str, Any] | None = None
    reason: str | None = None
    end_line: int | None = None

    @property
    def output_lines(self) -> tuple[int, int] | None:
        """已结束命令的输出行闭区间；末行早于起始行（命令无输出）或尚未结束时为 None。"""
        end = self.end_line
        return (self.start_line, end) if end is not None and end >= self.start_line else None


class TerminalObservation:
    """逻辑行 + shell 状态；``information_version`` 在任何 Agent 可见变化时推进。"""

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

    def shell_event(self, event: dict[str, Any]) -> str | None:
        """应用一条集成事件，返回用于日志的事件名；与当前状态矛盾的事件收敛跟踪而不报错。"""
        kind = event["kind"]
        if kind == "ready":
            self.integration_status = "ready"
        elif kind == "unavailable":
            self.end_tracking(event["reason"])
            self.integration_status = "unavailable"
        elif kind == "command_started":
            if self.active_command is not None:
                self.end_tracking("unexpected_command_boundary")
            self.command_counter += 1
            self.phase = "executing"
            self.latest_command = TerminalCommand(
                number=self.command_counter,
                command=event["command"][:1024],
                start_line=self.buffer.cursor_line,
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
                self.end_tracking("prompt_without_command_result")
            self.phase = "editing"
            self.cwd = event.get("cwd")
        else:
            raise TerminalSessionError(f"unknown shell observation: {kind}")
        self.information_version += 1
        return str(kind)

    def _end_line(self) -> int:
        """命令输出末行：光标在行首说明输出以换行结束，末行是上一行。"""
        cursor = self.buffer.cursor_line
        if self.shell_kind in _RETURN_AFTER_PROMPT_SHELLS or self.buffer.cursor_column == 0:
            return cursor - 1
        return cursor

    def end_tracking(self, reason: str) -> None:
        command = self.active_command
        if command is not None:
            command.status = "tracking_ended"
            command.reason = reason
            command.end_line = self._end_line()
            self.information_version += 1
        self.phase = "unknown"
