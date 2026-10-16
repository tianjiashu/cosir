"""Agent 观察终端的等待条件；只描述何时返回，不改变 shell 命令语义。"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field


class ActivityWait(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    until: Literal["activity"] = "activity"
    timeout_ms: int = Field(default=3000, ge=0, le=30000)


class CommandWait(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    until: Literal["command_returned"]
    command_id: str = Field(min_length=1, max_length=128)
    timeout_ms: int = Field(default=3000, ge=0, le=30000)


class PromptWait(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    until: Literal["shell_prompt"]
    timeout_ms: int = Field(default=3000, ge=0, le=30000)


TerminalWait = Annotated[ActivityWait | CommandWait | PromptWait, Field(discriminator="until")]
