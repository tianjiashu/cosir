"""Agent 读取终端时的等待条件契约（``terminal_read`` 的 ``wait``）。"""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config.constant import Constant

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
