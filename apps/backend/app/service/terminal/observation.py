"""shell 观察的唯一进程内投影与有界日志。

预览继续使用原始 output seq；此处的游标绑定 generation，并支持文本内偏移。
命令只由 shell 事件创建，输入接受、输出静默与信号投递均不构成命令返回。
"""

from __future__ import annotations

import json
from collections import OrderedDict, deque
from copy import deepcopy
from typing import Any

from app.service.terminal.errors import TerminalSessionError


def json_size(value: object) -> int:
    return len(json.dumps(value, ensure_ascii=False, separators=(",", ":")))


class TerminalObservation:
    """由 session lock 保护；保留窗口与命令结果均有界，不写数据库。"""

    def __init__(self, generation: str, limit: int, integration: str) -> None:
        self.generation = generation
        self.limit = limit
        self.events: deque[tuple[int, dict[str, Any], int]] = deque()
        self.size = 0
        self.next_seq = 1
        self.phase = "unknown"
        self.active: str | None = None
        self.command_counter = 0
        self.prompt_seq = 0
        self.cwd: str | None = None
        self.commands: OrderedDict[str, dict[str, Any] | None] = OrderedDict()
        self.integration: dict[str, Any] = {
            "status": "starting" if integration == "auto" else "off",
            "capabilities": [],
            "reason": None,
        }

    def cursor(self, seq: int | None = None, offset: int = 0) -> str:
        return f"{self.generation}:{self.next_seq - 1 if seq is None else seq}:{offset}"

    def position(self, cursor: str | None) -> tuple[int, int]:
        if cursor is None:
            return (self.events[0][0] - 1 if self.events else self.next_seq - 1, 0)
        try:
            generation, seq_text, offset_text = cursor.split(":")
            seq, offset = int(seq_text), int(offset_text)
        except (ValueError, TypeError) as exc:
            raise TerminalSessionError("invalid observation cursor") from exc
        if generation != self.generation or seq < 0 or seq >= self.next_seq or offset < 0:
            raise TerminalSessionError(
                "observation cursor belongs to another generation or position"
            )
        if offset:
            entry = next((event for number, event, _ in self.events if number == seq), None)
            if entry is not None and (entry["type"] != "output" or offset >= len(entry["text"])):
                raise TerminalSessionError("invalid observation text offset")
        return seq, offset

    def append(self, event: dict[str, Any]) -> None:
        size = json_size(event)
        self.events.append((self.next_seq, event, size))
        self.next_seq += 1
        self.size += size
        while self.size > self.limit and self.events:
            _, _, removed = self.events.popleft()
            self.size -= removed

    def output(self, text: str) -> None:
        # 小块使单个输出不会一次淘汰整个保留窗口；分页仍可在字符边界切分。
        for start in range(0, len(text), 1024):
            self.append({"type": "output", "text": text[start : start + 1024]})

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
            self.active = f"{self.generation}/c{self.command_counter}"
            self.phase = "executing"
            self.commands[self.active] = None
            while len(self.commands) > 128:
                self.commands.popitem(last=False)
            text = event["command"]
            result = {
                "type": kind,
                "command_id": self.active,
                "command": text[:1024],
                "command_text_truncated": len(text) > 1024,
            }
        elif kind == "command_returned":
            if self.active is None:
                return None
            result = {"type": kind, "command_id": self.active, "result": event["result"]}
            self.commands[self.active] = result
            self.active = None
            self.phase = "unknown"  # 只有随后 prompt 标记才证明进入输入阶段。
        elif kind == "shell_prompt":
            if self.active is not None:
                self.end_tracking("prompt_without_command_result")
            self.phase = "editing"
            self.cwd = event.get("cwd")
            self.prompt_seq = self.next_seq
            result = {"type": kind, "cwd": self.cwd}
        else:
            raise TerminalSessionError(f"unknown shell observation: {kind}")
        self.append(result)
        return result

    def end_tracking(self, reason: str) -> None:
        if self.active is not None:
            event = {"type": "tracking_ended", "command_id": self.active, "reason": reason}
            self.commands[self.active] = event
            self.append(event)
            self.active = None
        self.phase = "unknown"

    def state(self, status: str) -> dict[str, Any]:
        return {
            "session_status": status,
            "shell_phase": self.phase,
            "active_command_id": self.active,
            "cwd": self.cwd,
            "observed_cursor": self.cursor(),
        }

    def page(self, cursor: str | None, envelope: dict[str, Any], max_chars: int) -> dict[str, Any]:
        """按完整 JSON 大小分页；游标只跨过实际交付的字符或事件。"""
        seq, offset = self.position(cursor)
        first = self.events[0][0] if self.events else self.next_seq
        gap = seq < first - 1 or (offset > 0 and seq < first)
        if gap:
            seq, offset = first - 1, 0
        result = {
            **deepcopy(envelope),
            "events": [],
            "next_cursor": self.cursor(seq, offset),
            "has_more": False,
            "gap": gap,
        }
        if json_size(result) > max_chars:
            raise TerminalSessionError("observation budget cannot hold metadata")
        for number, event, _ in self.events:
            if number < seq or (number == seq and not offset):
                continue
            start = offset if number == seq else 0
            if event["type"] == "output":
                text = event["text"][start:]
                low, high = 0, len(text)
                while low < high:
                    middle = (low + high + 1) // 2
                    if json_size(self._text_page(result, number, start, text, middle)) <= max_chars:
                        low = middle
                    else:
                        high = middle - 1
                if low == 0:
                    result["has_more"] = True
                    return result
                result = self._text_page(result, number, start, text, low)
                if low < len(text):
                    result["has_more"] = True
                    return result
            else:
                candidate_event = {
                    **result,
                    "events": [*result["events"], deepcopy(event)],
                    "next_cursor": self.cursor(number),
                    "has_more": False,
                }
                if json_size(candidate_event) > max_chars:
                    result["has_more"] = True
                    return result
                result = candidate_event
        result["has_more"] = False
        return result

    def _text_page(
        self, page: dict[str, Any], number: int, start: int, text: str, count: int
    ) -> dict[str, Any]:
        previous = page["events"]
        if previous and previous[-1]["type"] == "output":
            items = [
                *previous[:-1],
                {"type": "output", "text": previous[-1]["text"] + text[:count]},
            ]
        else:
            items = [*previous, {"type": "output", "text": text[:count]}]
        return {
            **page,
            "events": items,
            "next_cursor": self.cursor(number, start + count if count < len(text) else 0),
            "has_more": False,
        }
