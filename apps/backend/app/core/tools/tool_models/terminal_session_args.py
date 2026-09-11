"""Interactive terminal tool argument models."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field


class TerminalStartArgs(BaseModel):
    """terminal_start 的严格参数模型。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    shell: str = Field(default="auto", min_length=1, max_length=256)
    cwd: str | None = Field(default=None, description="Workspace-relative initial directory.")
    cols: int = Field(default=120, ge=20, le=500)
    rows: int = Field(default=32, ge=5, le=200)


class TerminalWriteArgs(BaseModel):
    """terminal_write 的严格参数模型。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    session_id: str = Field(min_length=1, max_length=128)
    data: str = Field(min_length=1, max_length=65536)
    after_seq: int | None = Field(default=None, ge=0)
    wait_ms: int = Field(default=500, ge=0, le=30000)


class TerminalReadArgs(BaseModel):
    """terminal_read 的严格参数模型。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    session_id: str = Field(min_length=1, max_length=128)
    after_seq: int | None = Field(default=None, ge=0)
    wait_ms: int = Field(default=1000, ge=0, le=30000)


class TerminalSignalArgs(BaseModel):
    """terminal_signal 的严格参数模型。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    session_id: str = Field(min_length=1, max_length=128)
    signal: Literal["interrupt", "eof", "suspend"]


class TerminalCloseArgs(BaseModel):
    """terminal_close 的严格参数模型。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    session_id: str = Field(min_length=1, max_length=128)
