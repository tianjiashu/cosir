"""terminal_write 工具的参数契约。"""

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.config.constant import Constant


class TerminalWriteArgs(BaseModel):
    """把一行命令填进用户终端的提示符；不代按回车，也不代执行。"""

    model_config = ConfigDict(strict=True, extra="forbid")

    terminal_id: str = Field(
        min_length=1,
        max_length=128,
        description="Terminal ID of a terminal the user opened, from terminal_list.",
    )
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
