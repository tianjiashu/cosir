"""条件等待：Agent 读取终端时的等待条件契约（``terminal_read`` 的 ``wait``）与它的判定。

判定只做纯求值，不等待、不加锁：等待循环由 ``ObservedTerminal`` 在 ``threading.Condition`` 上进行，
来源写入 ``notify_all`` 唤醒；短时间片只用于复查取消信号，不是轮询条件。返回的「下次到期时刻」让
``output_quiet`` 能在静默满额的那一刻醒来，而不必等下一个事件。
"""

from __future__ import annotations

import re
import time
from dataclasses import dataclass
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config.constant import Constant
from app.tools.terminal_runtime.observation.terminal_observation import (
    COMMAND_FINISHED,
    TerminalObservation,
)

WaitUntil = Literal["command_returned", "output_quiet", "match", "elapsed"]


# 结束条件 + 总超时。等待只在前台、占着工具线程进行，所以总超时有上限；更长的工作交给
# execute_terminal 的后台执行。模型可见说明只在字段描述里，类 docstring 只服务源码阅读。
class TerminalWait(BaseModel):
    """结束条件与总超时的组合；跨字段约束在 ``validate_condition``。"""

    model_config = ConfigDict(strict=True, extra="forbid")
    until: WaitUntil = Field(
        description="command_returned: the shell reports the command returned (needs shell "
        "integration). output_quiet: no output for quiet_ms. match: pattern matches a line from "
        "the cursor line on, or the redrawn screen in a full-screen app. elapsed: wait "
        "timeout_ms."
    )
    pattern: str | None = Field(
        default=None,
        max_length=512,
        description="Python regex for until=match; null otherwise.",
    )
    quiet_ms: int | None = Field(
        default=None,
        ge=100,
        le=60_000,
        description="Silence for until=output_quiet "
        f"(default {Constant.Terminal.WAIT_DEFAULT_QUIET_MS}); null otherwise.",
    )
    timeout_ms: int | None = Field(
        default=None,
        ge=0,
        le=Constant.Terminal.WAIT_MAX_TIMEOUT_MS,
        description=f"Overall limit (default {Constant.Terminal.WAIT_DEFAULT_TIMEOUT_MS}). For "
        "long builds or test suites, use execute_terminal with background=true instead.",
    )

    @model_validator(mode="after")
    def validate_condition(self) -> TerminalWait:
        if self.until == "match":
            if not self.pattern:
                raise ValueError("until=match requires pattern")
            try:
                re.compile(self.pattern)
            except re.error as exc:
                raise ValueError(f"invalid pattern: {exc}") from exc
        return self

    @property
    def effective_timeout_ms(self) -> int:
        if self.timeout_ms is None:
            return Constant.Terminal.WAIT_DEFAULT_TIMEOUT_MS
        return self.timeout_ms

    @property
    def effective_quiet_ms(self) -> int:
        return self.quiet_ms or Constant.Terminal.WAIT_DEFAULT_QUIET_MS


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
        # 只认确知结束的命令；跟丢（会话结束、集成不可用、worker 失联）不算返回。
        if self.baseline.active is not None:
            if command.number != self.baseline.active:
                # 等的那条之后又开过新命令，说明它已经结束。
                return True
            return command.status in COMMAND_FINISHED
        return command.number > self.baseline.counter and command.status in COMMAND_FINISHED

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
