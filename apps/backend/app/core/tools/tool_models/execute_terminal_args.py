"""Pydantic arguments for execute_terminal."""

from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

ExecuteTerminalShell = Literal[
    "auto",
    "cmd",
    "powershell",
    "pwsh",
    "sh",
    "bash",
    "zsh",
    "fish",
]


class ExecuteTerminalArgs(BaseModel):
    """Validated arguments accepted by the execute_terminal tool.

    ``shell`` is explicit so the model does not need to encode PowerShell inside a
    cmd command on Windows. ``auto`` preserves the existing host defaults.
    """

    model_config = ConfigDict(strict=True, extra="forbid")

    shell: ExecuteTerminalShell = Field(
        default="auto",
        description=(
            "Shell used to interpret command. 'auto' uses cmd.exe on Windows and /bin/sh "
            "on macOS/Linux; use 'powershell' or 'pwsh' for PowerShell syntax and keep "
            "the command syntax consistent with the selected shell."
        ),
    )
    command: str = Field(
        min_length=1,
        description="Command string interpreted by the selected shell.",
    )
    timeout: float | None = Field(
        default=None,
        gt=0,
        description="命令级超时秒数；缺省 60s，上限钳制 110s",
    )
    workdir: str | None = Field(
        default=None,
        description="工作目录；缺省 workspace 根；相对路径相对 project_root 解析",
    )
