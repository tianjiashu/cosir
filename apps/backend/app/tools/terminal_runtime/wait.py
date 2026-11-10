"""条件等待的判定：在会话锁内、每次 worker 事件唤醒时对当前观察求值。

只做纯判定，不等待、不加锁：等待循环由 runtime 在 ``threading.Condition`` 上进行，worker 事件
``notify_all`` 唤醒；短时间片只用于复查 Run 取消信号，不是轮询条件。返回的「下次到期时刻」让
``output_quiet`` 能在静默满额的那一刻醒来，而不必等下一个事件。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass

from app.tools.terminal_runtime.inputs import TerminalWait
from app.tools.terminal_runtime.observation import TerminalObservation


@dataclass(frozen=True)
class WaitBaseline:
    """等待起点：读取时刻的光标行、命令计数与正在执行的命令。

    ``line`` 是 ``match`` 条件的首个扫描行：读取时光标行可能已有尚未换行的输出（如
    ``Password:`` 提示），应当参与匹配。全屏程序（备用屏幕）期间不用它，``match`` 扫描整屏
    （见 :meth:`WaitEvaluator._match`）。
    """

    line: int
    counter: int
    active: int | None
    at: float

    @classmethod
    def of(cls, observation: TerminalObservation) -> WaitBaseline:
        active = observation.active_command
        return cls(
            line=observation.buffer.cursor_line,
            counter=observation.command_counter,
            active=active.number if active is not None else None,
            at=time.monotonic(),
        )


class WaitEvaluator:
    """单次等待的判定状态；``match`` 记住已扫描过的已封口行，避免重复扫描历史。"""

    def __init__(self, spec: TerminalWait, baseline: WaitBaseline) -> None:
        self.spec = spec
        self.baseline = baseline
        self.deadline = baseline.at + spec.effective_timeout_ms / 1000
        self.matched_line: int | None = None
        self.matched_screen_row: int | None = None
        self._pattern = re.compile(spec.pattern) if spec.until == "match" and spec.pattern else None
        self._scan_from = baseline.line

    def check(self, observation: TerminalObservation, now: float) -> tuple[str | None, float]:
        """返回 ``(结束原因或 None, 下次需要醒来的时刻)``；超时也在这里判定。"""
        until = self.spec.until
        if until == "command_returned" and self._command_returned(observation):
            return "command_returned", self.deadline
        if until == "match" and self._match(observation):
            return "matched", self.deadline
        due = self.deadline
        if until == "output_quiet":
            quiet_due = (
                max(observation.last_output_at, self.baseline.at)
                + self.spec.effective_quiet_ms / 1000
            )
            if now >= quiet_due:
                return "output_quiet", self.deadline
            due = min(due, quiet_due)
        if now >= self.deadline:
            return ("elapsed" if until == "elapsed" else "timeout"), self.deadline
        return None, due

    def _command_returned(self, observation: TerminalObservation) -> bool:
        command = observation.latest_command
        if command is None:
            return False
        if self.baseline.active is not None:
            return not (command.number == self.baseline.active and command.status == "executing")
        return command.number > self.baseline.counter and command.status != "executing"

    def _match(self, observation: TerminalObservation) -> bool:
        assert self._pattern is not None
        buffer = observation.buffer
        if buffer.alternate:
            # 全屏程序原地重绘：每次扫整屏，命中记屏幕行（主屏逻辑行此时冻结，不会出现新内容）。
            for row, text in buffer.screen_lines():
                if self._pattern.search(text):
                    self.matched_screen_row = row
                    return True
            return False
        start = max(self._scan_from, buffer.first_available_line)
        for number, text in buffer.get(start, buffer.last_line):
            if self._pattern.search(text):
                self.matched_line = number
                return True
        # 已封口行不再变化，下次只需从可变区开始扫描。
        self._scan_from = max(start, buffer.live_from_line)
        return False

    def result(self, end_reason: str) -> dict[str, object]:
        result: dict[str, object] = {"until": self.spec.until, "end_reason": end_reason}
        if self.matched_line is not None:
            result["matched_line"] = self.matched_line
        if self.matched_screen_row is not None:
            result["matched_screen_row"] = self.matched_screen_row
        return result
