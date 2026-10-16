"""交互终端工具的输入契约。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

from app.config.constant import Constant
from app.service.terminal.interaction import ActivityWait, TerminalInput, TerminalWait


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
        description="auto uses PowerShell on Windows and $SHELL on macOS/Linux, falling "
        "back to zsh/bash; explicit values may be cmd, powershell, pwsh, bash, zsh, fish, "
        "or a resolvable executable.",
    )
    label: str | None = Field(
        default=None,
        max_length=64,
        description="Purpose label shown in terminal_list, e.g. dev server.",
    )
    cwd: str | None = Field(
        default=None,
        description="Initial directory in host path syntax, relative to the workspace; defaults to "
        "the workspace root and must resolve to an existing directory inside it.",
    )

    integration: Literal["auto", "off"] = Field(
        default="auto",
        description=(
            "Enable shell observation when supported, or disable it explicitly. Read "
            "returned capabilities; worker startup is not integration readiness."
        ),
    )


class TerminalWriteArgs(BaseModel):
    """terminal_write 的严格参数模型。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    session_id: str = Field(
        min_length=1,
        max_length=128,
        description="Session ID from terminal_start or terminal_list; reusable across runs.",
    )
    operation_id: str = Field(
        min_length=1,
        max_length=128,
        description=(
            "Idempotency key for this session write. Reuse only with identical input; "
            "use a new key for each distinct write. Deduplication covers the session's "
            f"most recent {Constant.Terminal.MAX_WRITE_OPERATION_CACHE} accepted writes."
        ),
    )
    input: TerminalInput = Field(
        description=(
            "Submit one line with Enter, or send one platform-supported control key. "
            "No input arrays, embedded newlines, or text/key mixing. A line may be "
            "a command, a continuation, or a program answer; acceptance is not execution."
        ),
    )
    wait_ms: int = Field(
        default=500,
        ge=0,
        le=30000,
        description=(
            "Bounded activity collection after input acceptance; does not wait for "
            "command return."
        ),
    )


class TerminalReadArgs(BaseModel):
    """观察游标与预览 output seq 分域；等待不改变命令生命周期。"""

    model_config = ConfigDict(strict=True, extra="forbid")
    session_id: str = Field(
        min_length=1, max_length=128, description="Task-owned terminal session ID."
    )
    cursor: str | None = Field(
        default=None,
        max_length=256,
        description=(
            "Opaque next_cursor from a previous observation; null reads the retained " "window."
        ),
    )
    wait: TerminalWait = Field(
        default_factory=ActivityWait,
        description=(
            "Wait for activity, a named command to return, or a new top-level prompt. "
            "Timeout never terminates the program."
        ),
    )


class TerminalSignalArgs(BaseModel):
    """terminal_signal 的严格参数模型。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    session_id: str = Field(
        min_length=1,
        max_length=128,
        description="Session ID from terminal_start or terminal_list; reusable across runs.",
    )
    signal: Literal["interrupt", "eof", "suspend"] = Field(
        description=(
            "Signal to send: interrupt is Ctrl-C-like, eof sends canonical EOF, and "
            "suspend is Ctrl-Z-like. Availability depends on the host worker."
        )
    )


class TerminalCloseArgs(BaseModel):
    """terminal_close 的严格参数模型。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    session_id: str = Field(
        min_length=1,
        max_length=128,
        description="Session ID from terminal_start or terminal_list; reusable across runs.",
    )
