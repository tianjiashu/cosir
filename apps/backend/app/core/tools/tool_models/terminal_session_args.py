"""交互终端工具的输入契约。"""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.config.constant import Constant


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
    data: str = Field(
        min_length=1,
        max_length=65536,
        description=(
            "Raw UTF-8 text sent exactly as provided. No implicit Enter or decoding of "
            "\\r, \\n, or &#13;; use submit=true to submit input."
        ),
    )
    submit: bool = Field(
        default=False,
        description=(
            "If true, append one real Enter key (CR, 0x0D) after data; use it to submit "
            "commands or answer prompts."
        ),
    )
    after_seq: int | None = Field(
        default=None,
        ge=0,
        description="Return only output frames whose seq is greater than this value. Pass the "
        "largest seq you have already processed; keep the same value when no new "
        "frame was returned. Use null or 0 on the first call.",
    )
    wait_ms: int = Field(
        default=500,
        ge=0,
        le=30000,
        description="Maximum time to wait for new output in milliseconds; 0 returns immediately. "
        "This does not wait for the shell to exit.",
    )

    @model_validator(mode="after")
    def validate_encoded_input_size(self) -> "TerminalWriteArgs":
        """按实际 UTF-8 字节校验输入；submit 额外占用一个 CR 字节。"""

        final_size = len(self.data.encode("utf-8")) + (1 if self.submit else 0)
        if final_size > 64 * 1024:
            raise ValueError("terminal input exceeds 64 KiB after UTF-8 encoding")
        return self


class TerminalReadArgs(BaseModel):
    """terminal_read 的严格参数模型。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    session_id: str = Field(
        min_length=1,
        max_length=128,
        description="Session ID from terminal_start or terminal_list; reusable across runs.",
    )
    after_seq: int | None = Field(
        default=None,
        ge=0,
        description="Return only output frames whose seq is greater than this value. Pass the "
        "largest seq you have already processed; keep the same value when no new "
        "frame was returned. Use null or 0 on the first call.",
    )
    wait_ms: int = Field(
        default=3000,
        ge=0,
        le=30000,
        description="Maximum time to wait for new output in milliseconds; 0 returns immediately. "
        "This does not wait for the shell to exit.",
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
