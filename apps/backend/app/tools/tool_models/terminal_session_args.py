"""交互终端工具的输入契约（5 个工具：start / write / read / list / close）。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.config.constant import Constant
from app.tools.terminal_runtime.terminal_input import (
    InputHandoff,
    InputKeys,
    InputLine,
    InputSignal,
    TerminalWait,
)

_SESSION_ID_DESCRIPTION = "Session ID from terminal_start or terminal_list; reusable across runs."


class TerminalListArgs(BaseModel):
    """会话归属取自执行上下文，不允许模型指定其他 Task。"""

    model_config = ConfigDict(strict=True, extra="forbid")


class TerminalStartArgs(BaseModel):
    """terminal_start 的严格参数模型。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    shell: str = Field(
        default="auto",
        min_length=1,
        max_length=256,
        description="auto uses the host default shell; see terminal_list for the shells "
        "installed on this host and which support shell integration.",
    )
    label: str | None = Field(
        default=None,
        max_length=64,
        description="Purpose label shown on the terminal tab and in terminal_list, "
        "e.g. 'dev server'.",
    )
    cwd: str | None = Field(
        default=None,
        description="Initial directory in host path syntax, relative to the workspace; defaults to "
        "the workspace root and must resolve to an existing directory inside it.",
    )
    integration: Literal["auto", "off"] = Field(
        default="auto",
        description="auto enables shell integration (command boundaries, exit codes, cwd) when "
        "the shell supports it; off disables it.",
    )


class TerminalWriteArgs(BaseModel):
    """terminal_write 的严格参数模型。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    session_id: str = Field(min_length=1, max_length=128, description=_SESSION_ID_DESCRIPTION)
    operation_id: str = Field(
        min_length=1,
        max_length=128,
        description=(
            "Idempotency key for this write. Reuse only with identical input; use a new key for "
            "each distinct write. Deduplication covers the session's most recent "
            f"{Constant.Terminal.MAX_WRITE_OPERATION_CACHE} accepted writes."
        ),
    )
    # 普通 union 导出 strict 支持的 anyOf，不把判别联合的 discriminator/oneOf 泄漏到模型 schema。
    input: InputLine | InputKeys | InputSignal | InputHandoff = Field(
        description=(
            "line: one line plus Enter. keys: a key sequence. signal: interrupt/suspend/eof. "
            "handoff: give keyboard control to the user (for passwords, confirmations) and pause "
            "until they hand it back."
        ),
    )
    wait: TerminalWait | None = Field(
        default=None,
        description=(
            "When to return after the input; null means until=output_quiet, quiet_ms=500, "
            "timeout_ms=3000. Ignored for handoff."
        ),
    )


class TerminalReadArgs(BaseModel):
    """按行读取；带 wait 时先按条件等待再读。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    session_id: str = Field(min_length=1, max_length=128, description=_SESSION_ID_DESCRIPTION)
    start_line: int | None = Field(
        default=None,
        ge=1,
        description="Read forward from this absolute line number (with limit). Null reads the tail.",
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        le=Constant.Terminal.READ_MAX_LIMIT,
        description=f"Lines to read from start_line (max {Constant.Terminal.READ_MAX_LIMIT}).",
    )
    tail: int | None = Field(
        default=None,
        ge=1,
        le=Constant.Terminal.READ_MAX_LIMIT,
        description=(
            f"Read the last N lines (default {Constant.Terminal.READ_DEFAULT_TAIL}) when "
            "start_line is null."
        ),
    )
    wait: TerminalWait | None = Field(
        default=None,
        description="Block until this condition before reading; null returns immediately.",
    )


class TerminalCloseArgs(BaseModel):
    """terminal_close 的严格参数模型。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    session_id: str = Field(min_length=1, max_length=128, description=_SESSION_ID_DESCRIPTION)
