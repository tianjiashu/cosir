"""交互终端工具的输入契约（list / read / write）。

模型可见说明只来自字段描述：schema 生成边界会剥离模型级 description，类 docstring 只服务
源码阅读，不能当作投递渠道。
"""

import re

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.config.constant import Constant
from app.tools.terminal_runtime.inputs import TerminalWait

_SESSION_ID_DESCRIPTION = "Session ID from terminal_list."


# 会话归属取自执行上下文，不允许模型指定其他 Task。
class TerminalListArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")


class TerminalWriteArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    session_id: str = Field(min_length=1, max_length=128, description=_SESSION_ID_DESCRIPTION)
    command: str = Field(
        min_length=1,
        max_length=Constant.Terminal.MAX_AGENT_INPUT_BYTES,
        description=(
            "One command line to place at the prompt, without a line break or control "
            "characters. It is not run: the user decides whether to press Enter."
        ),
    )

    @field_validator("command")
    @classmethod
    def validate_command(cls, value: str) -> str:
        """填入命令的边界校验：只检查可打印与字节预算，不解析接收程序的语法。

        worker 编码时另行复查（``encode_input``）：这条规则同时保证文本里不可能出现 ``ESC``，括号
        粘贴的包裹不会被提前结束，填入也就不可能变成执行。
        """
        if any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in value):
            raise ValueError("command must not contain CR/LF or terminal control characters")
        if len(value.encode("utf-8")) > Constant.Terminal.MAX_AGENT_INPUT_BYTES:
            raise ValueError("terminal input exceeds 64 KiB")
        return value


# 按行读取；带 wait 时先按条件等待再读。
class TerminalReadArgs(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid")

    session_id: str = Field(min_length=1, max_length=128, description=_SESSION_ID_DESCRIPTION)
    start_line: int | None = Field(
        default=None,
        ge=1,
        description="Absolute line number to read forward from (with limit); null reads the tail.",
    )
    limit: int | None = Field(
        default=None,
        ge=1,
        le=Constant.Terminal.READ_MAX_LIMIT,
        description="Lines to read from start_line.",
    )
    tail: int | None = Field(
        default=None,
        ge=1,
        le=Constant.Terminal.READ_MAX_LIMIT,
        description=(
            f"Last N lines when start_line is null (default {Constant.Terminal.READ_DEFAULT_TAIL})."
        ),
    )
    pattern: str | None = Field(
        default=None,
        min_length=1,
        max_length=512,
        description=(
            "Python regex: return only matching lines, searching forward from start_line "
            "(default: the oldest kept line) to the end; limit caps the matches. Use it to find "
            "errors in long output instead of paging. Not combined with tail."
        ),
    )
    wait: TerminalWait | None = Field(
        default=None,
        description="Wait for this condition before reading; null reads now.",
    )

    @model_validator(mode="after")
    def validate_pattern(self) -> "TerminalReadArgs":
        if self.pattern is None:
            return self
        if self.tail is not None:
            raise ValueError("pattern searches forward from start_line; do not combine with tail")
        try:
            re.compile(self.pattern)
        except re.error as exc:
            raise ValueError(f"invalid pattern: {exc}") from exc
        return self
