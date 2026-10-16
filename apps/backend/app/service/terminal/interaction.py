"""终端输入与等待的领域契约；输入操作不声明自己是 shell 命令。"""

from typing import Annotated, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config.constant import Constant


class InputLine(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    type: Literal["line"]
    text: str = Field(
        description="One complete line, without CR/LF or control characters. "
        "Empty sends Enter; whitespace is preserved."
    )

    @field_validator("text")
    @classmethod
    def validate_line(cls, value: str) -> str:
        """仅在输入边界检查单行与字节预算；不解析接收程序的语法。"""
        if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
            raise ValueError("line must not contain CR/LF or terminal control characters")
        if len(value.encode("utf-8")) + 1 > Constant.Terminal.MAX_AGENT_INPUT_BYTES:
            raise ValueError("terminal input exceeds 64 KiB including Enter")
        return value


class InputKey(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")
    type: Literal["key"]
    key: Literal["ctrl_c", "ctrl_d", "ctrl_z"]


TerminalInput = Annotated[InputLine | InputKey, Field(discriminator="type")]


def control_keys_for_platform(system: str) -> tuple[str, ...]:
    """工具 schema 的宿主声明；实际投递以 worker 握手能力为准。"""
    return ("ctrl_c", "ctrl_z") if system == "Windows" else ("ctrl_c", "ctrl_d")


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
