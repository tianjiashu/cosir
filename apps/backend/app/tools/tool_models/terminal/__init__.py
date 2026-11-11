"""终端工具的输入契约。

模型可见说明只来自字段描述：schema 生成边界会剥离模型级 description，类 docstring 只服务
源码阅读，不能当作投递渠道。会话归属取自执行上下文，模型不能指定别的 Task。

一个工具一个模块：``execute_terminal`` 的平台差异见 :mod:`.execute_terminal_args`，
会话类工具各占 ``terminal_{list,read,write,stop}_args``。
"""

from app.tools.tool_models.terminal.execute_terminal_args import (
    ExecuteTerminalShell,
    MacExecuteTerminalArgs,
    WindowsExecuteTerminalArgs,
    resolve_execute_terminal_args_model,
)
from app.tools.tool_models.terminal.terminal_list_args import TerminalListArgs
from app.tools.tool_models.terminal.terminal_read_args import TerminalReadArgs
from app.tools.tool_models.terminal.terminal_stop_args import TerminalStopArgs
from app.tools.tool_models.terminal.terminal_write_args import TerminalWriteArgs

__all__ = [
    "ExecuteTerminalShell",
    "MacExecuteTerminalArgs",
    "TerminalListArgs",
    "TerminalReadArgs",
    "TerminalStopArgs",
    "TerminalWriteArgs",
    "WindowsExecuteTerminalArgs",
    "resolve_execute_terminal_args_model",
]
