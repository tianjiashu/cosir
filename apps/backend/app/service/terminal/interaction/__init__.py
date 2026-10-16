"""终端交互契约的公开出口。

输入（``input.py``）与等待条件（``wait.py``）是两个独立概念：输入只负责把一行文字或
一个控制键投递进 PTY，不代表是 shell 命令；等待只声明观察何时返回，不改变命令语义。
工具 schema、tool model 与 service 统一从本包导入，不直接依赖子模块。
"""

from app.service.terminal.interaction.input import (
    InputKey,
    InputLine,
    TerminalInput,
    control_keys_for_platform,
)
from app.service.terminal.interaction.wait import (
    ActivityWait,
    CommandWait,
    PromptWait,
    TerminalWait,
)

__all__ = [
    "ActivityWait",
    "CommandWait",
    "InputKey",
    "InputLine",
    "PromptWait",
    "TerminalInput",
    "TerminalWait",
    "control_keys_for_platform",
]
