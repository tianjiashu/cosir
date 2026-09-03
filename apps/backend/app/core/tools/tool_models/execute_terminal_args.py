"""Pydantic arguments for execute_terminal."""

from pydantic import BaseModel, ConfigDict, Field


class ExecuteTerminalArgs(BaseModel):
    """Validated arguments accepted by the execute_terminal tool."""

    model_config = ConfigDict(strict=True, extra="forbid")

    command: str = Field(min_length=1, description="要执行的 shell 命令")
    timeout: float | None = Field(
        default=None,
        gt=0,
        description="命令级超时秒数；缺省 60s，上限钳制 110s",
    )
    workdir: str | None = Field(
        default=None,
        description="工作目录；缺省 workspace 根；相对路径相对 project_root 解析",
    )
