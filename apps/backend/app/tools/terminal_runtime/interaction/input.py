"""Agent 向终端投递输入的边界契约；输入操作不声明自己是 shell 命令。"""

import platform
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config.constant import Constant


def control_keys_for_platform(system: str) -> tuple[str, ...]:
    """参数校验与 schema 共用宿主键集合；会话实际能力另由 worker 握手确定。"""
    return ("ctrl_c", "ctrl_z") if system == "Windows" else ("ctrl_c", "ctrl_d")


def _control_key_schema(schema: dict[str, Any]) -> None:
    schema["enum"] = list(control_keys_for_platform(platform.system()))


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
    key: Literal["ctrl_c", "ctrl_d", "ctrl_z"] = Field(json_schema_extra=_control_key_schema)

    @field_validator("key")
    @classmethod
    def validate_host_key(cls, value: str) -> str:
        """宿主不支持的键在工具参数边界拒绝，不依赖会话存在或 worker 可用。"""
        system = platform.system()
        keys = control_keys_for_platform(system)
        if value not in keys:
            raise ValueError(
                f"control key {value!r} is not supported on {system}; use {', '.join(keys)}"
            )
        return value


TerminalInput = Annotated[InputLine | InputKey, Field(discriminator="type")]
