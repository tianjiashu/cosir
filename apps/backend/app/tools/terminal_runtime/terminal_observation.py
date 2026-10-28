"""单会话的 Agent 观察事实：逻辑行缓冲 + shell 集成语义（命令边界、退出结果、cwd）。

命令只由 shell 集成事件创建；输入接受、输出静默与提示符文字都不构成命令返回。命令输出区间按
「事件到达时光标所在逻辑行」记录：worker 保证语义事件前的输出与屏幕快照先到，因此事件时刻的
光标位置就是命令输出的边界。

线程模型：由所属会话锁串行访问；不做 IO、不写数据库。
"""

from __future__ import annotations

import time
from typing import Any

from app.tools.terminal_runtime.errors import TerminalSessionError
from app.tools.terminal_runtime.line_buffer import TerminalLineBuffer

# 在下一次读取输入（提示符已打印之后）才报告 returned 的 shell：结束行取光标行 - 1。
_RETURN_AFTER_PROMPT_SHELLS = frozenset({"powershell", "pwsh"})


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
        self.latest_command: dict[str, Any] | None = None
        self.integration_status = "starting" if integration == "auto" else "off"
        self.integration_reason: str | None = None

    @property
    def active_command(self) -> dict[str, Any] | None:
        command = self.latest_command
        return command if command is not None and command["status"] == "executing" else None

    def output_arrived(self) -> None:
        self.last_output_at = time.monotonic()

    def update_screen(self, screen: dict[str, Any], history: list[str]) -> bool:
        """应用屏幕事件；返回几何尺寸是否变化（供预览推送 geometry 帧）。"""
        resized = (self.buffer.rows, self.buffer.columns) != (screen["rows"], screen["columns"])
        self.buffer.apply(screen, history)
        self.information_version += 1
        return resized

    def shell_event(self, event: dict[str, Any]) -> str | None:
        """应用一条集成事件，返回用于日志的事件名；与当前状态矛盾的事件收敛跟踪而不报错。"""
        kind = event["kind"]
        if kind == "ready":
            self.integration_status = "ready"
            self.integration_reason = None
        elif kind == "unavailable":
            self.end_tracking(event["reason"])
            self.integration_status = "unavailable"
            self.integration_reason = event["reason"]
            self.phase = "unknown"
        elif kind == "command_started":
            if self.active_command is not None:
                self.end_tracking("unexpected_command_boundary")
            self.command_counter += 1
            text = event["command"]
            self.phase = "executing"
            self.latest_command = {
                "number": self.command_counter,
                "command": text[:1024],
                "status": "executing",
                "result": None,
                "reason": None,
                "start_line": self.buffer.cursor_line,
                "end_line": None,
            }
        elif kind == "command_returned":
            command = self.active_command
            if command is None:
                return None
            command.update(status="returned", result=event["result"], end_line=self._end_line())
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
            command.update(status="tracking_ended", reason=reason, end_line=self._end_line())
            self.information_version += 1
        self.phase = "unknown"

    def command_summary(self) -> dict[str, Any] | None:
        """最近命令的对外摘要；output_lines 为空区间时为 None（命令无输出）。"""
        command = self.latest_command
        if command is None:
            return None
        summary: dict[str, Any] = {
            "number": command["number"],
            "command": command["command"],
            "status": command["status"],
        }
        if command["status"] == "returned":
            summary["result"] = command["result"]
        if command["status"] == "tracking_ended":
            summary["reason"] = command["reason"]
        end = command["end_line"]
        if end is not None:
            start = command["start_line"]
            summary["output_lines"] = [start, end] if end >= start else None
        else:
            summary["output_from_line"] = command["start_line"]
        return summary
