"""终端交互契约的公开出口。

输入只负责把一行文字或一个控制键投递进 PTY，不代表是 shell 命令。
工具 schema、tool model 与 service 统一从本包导入，不直接依赖子模块。
"""

from app.service.terminal.interaction.input import (
    InputKey,
    InputLine,
    TerminalInput,
    control_keys_for_platform,
)

__all__ = [
    "InputKey",
    "InputLine",
    "TerminalInput",
    "control_keys_for_platform",
]
