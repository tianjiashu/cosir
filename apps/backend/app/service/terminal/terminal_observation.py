"""shell 语义与最近滚动历史的有界快照；屏幕事实由 worker 唯一维护。

观察允许丢弃旧内容，不提供消费游标。命令只由 shell 事件创建，
输入接受、输出静默与重复读取错误均不构成命令返回。
"""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from app.service.terminal.errors import TerminalSessionError
from app.service.terminal.terminal_screen_projection import project_screen


def json_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


class TerminalObservation:
    """由 session lock 保护；历史有界，只保留最近命令，不写数据库。"""

    def __init__(self, generation: str, limit: int, integration: str) -> None:
        self.generation = generation
        self.limit = limit
        self.history_text = ""
        self.history_omitted = False
        self.information_version = 0
        self.screen: dict[str, Any] | None = None
        self.phase = "unknown"
        self.command_counter = 0
        self.cwd: str | None = None
        self.latest_command: dict[str, Any] | None = None
        self.integration: dict[str, Any] = {
            "status": "starting" if integration == "auto" else "off",
            "capabilities": [],
            "reason": None,
        }

    @property
    def active(self) -> str | None:
        command = self.latest_command
        return (
            command["command_id"]
            if command is not None and command["status"] == "executing"
            else None
        )

    def initialize_screen(self, geometry: dict[str, int]) -> None:
        self.screen = {
            "revision": 0,
            **geometry,
            "lines": [{"text": "", "wrapped": False} for _ in range(geometry["rows"])],
            "cursor": {"row": 0, "column": 0, "visible": True},
            "alternate": False,
        }

    def geometry(self) -> dict[str, int]:
        # 订阅发生于 start 成功后；尺寸由握手初始化，并贯穿终态保留。
        assert self.screen is not None
        return {key: self.screen[key] for key in ("rows", "columns")}

    def update_screen(self, screen: dict[str, Any], history: list[str]) -> None:
        """worker 独占屏幕事实；此层只替换快照并将真正滚出的文本加入历史。"""
        # 闪烁可见性不算新信息；真实文字、位置、备用屏幕及滚动历史仍算进展。
        if (
            self.screen is None
            or any(self.screen[key] != screen[key] for key in ("lines", "alternate"))
            or any(self.screen["cursor"][key] != screen["cursor"][key] for key in ("row", "column"))
        ):
            self.information_version += 1
        self.screen = screen
        for text in history:
            self.history(text)

    def history(self, text: str) -> None:
        if text:
            self.information_version += 1
            combined = self.history_text + text
            self.history_omitted |= len(combined) > self.limit
            self.history_text = combined[-self.limit :]

    def shell_event(self, event: dict[str, Any]) -> dict[str, Any] | None:
        kind = event["kind"]
        if kind == "ready":
            self.integration = {
                "status": "ready",
                "shell": event["shell"],
                "version": event["version"],
                "capabilities": ["command_boundaries", "command_success", "cwd"],
                "reason": None,
            }
            result = {"type": "integration_ready", **self.integration}
        elif kind == "unavailable":
            self.end_tracking(event["reason"])
            self.integration = {
                "status": "unavailable",
                "capabilities": [],
                "reason": event["reason"],
            }
            self.phase = "unknown"
            result = {"type": "integration_unavailable", **self.integration}
        elif kind == "command_started":
            if self.active is not None:
                self.end_tracking("unexpected_command_boundary")
            self.command_counter += 1
            command_id = f"{self.generation}/c{self.command_counter}"
            self.phase = "executing"
            text = event["command"]
            result = {
                "type": kind,
                "command_id": command_id,
                "command": text[:1024],
                "command_text_truncated": len(text) > 1024,
            }
            self.latest_command = {**result, "status": "executing"}
            del self.latest_command["type"]
        elif kind == "command_returned":
            if self.active is None:
                return None
            result = {"type": kind, "command_id": self.active, "result": event["result"]}
            assert self.latest_command is not None
            self.latest_command.update(status="returned", result=event["result"])
            self.phase = "unknown"  # 只有随后 prompt 标记才证明进入输入阶段。
        elif kind == "shell_prompt":
            if self.active is not None:
                self.end_tracking("prompt_without_command_result")
            self.phase = "editing"
            self.cwd = event.get("cwd")
            result = {"type": kind, "cwd": self.cwd}
        else:
            raise TerminalSessionError(f"unknown shell observation: {kind}")
        self.information_version += 1
        return result

    def end_tracking(self, reason: str) -> None:
        if self.active is not None:
            assert self.latest_command is not None
            self.latest_command.update(status="tracking_ended", reason=reason)
            self.information_version += 1
        self.phase = "unknown"

    def state(self, status: str) -> dict[str, Any]:
        return {
            "session_status": status,
            "shell_phase": self.phase,
            "active_command_id": self.active,
            "cwd": self.cwd,
        }

    def snapshot(self, envelope: dict[str, Any], max_chars: int) -> dict[str, Any]:
        """先保留状态和当前屏幕，再用剩余预算保留最近历史；不消费任何输出。"""
        result = {
            **deepcopy(envelope),
            "latest_command": deepcopy(self.latest_command),
            "history": "",
            "history_truncated": self.history_omitted or bool(self.history_text),
        }
        # 为 screen 的文本分配余量，元数据不足时显式失败，绝不截坏 JSON。
        result["screen"] = project_screen(self.screen, max_chars - json_size(result) - 16)
        if json_size(result) > max_chars:
            raise TerminalSessionError("observation budget cannot hold metadata")
        low, high = 0, len(self.history_text)
        while low < high:
            middle = (low + high + 1) // 2
            candidate = {
                **result,
                "history": self.history_text[-middle:],
                "history_truncated": self.history_omitted or middle < len(self.history_text),
            }
            if json_size(candidate) <= max_chars:
                low = middle
            else:
                high = middle - 1
        result["history"] = self.history_text[-low:] if low else ""
        result["history_truncated"] = self.history_omitted or low < len(self.history_text)
        return result
