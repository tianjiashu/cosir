"""交互终端 Agent 工具 handler 与可注册定义构建入口。

所有 handler 复用进程内既有的终端 session service。浏览器只 attach 只读预览端点，既不拥有
PTY，也不向它写入输入；会话输入只经 Agent 工具发生。
"""

from app.core.tool.schemas import ToolDefinition
from app.tools.tool_handler.terminal_session.close import TerminalCloseTool
from app.tools.tool_handler.terminal_session.list import TerminalListTool
from app.tools.tool_handler.terminal_session.read import TerminalReadTool
from app.tools.tool_handler.terminal_session.signal import TerminalSignalTool
from app.tools.tool_handler.terminal_session.start import TerminalStartTool
from app.tools.tool_handler.terminal_session.write import TerminalWriteTool


def build_terminal_list_definition() -> ToolDefinition:
    """注册 Task 会话发现工具。"""

    return TerminalListTool().to_definition()


def build_terminal_start_definition() -> ToolDefinition | None:
    """构建 ``terminal_start`` 的注册定义。

    返回:
        ``ToolDefinition``；该工具在当前平台不可用时返回 ``None``（见
        ``HandlerBase.avaliable()``）。
    """

    return TerminalStartTool().to_definition_if_avaliable()


def build_terminal_read_definition() -> ToolDefinition | None:
    """构建 ``terminal_read`` 的注册定义。

    返回:
        ``ToolDefinition``；该工具在当前平台不可用时返回 ``None``（见
        ``HandlerBase.avaliable()``）。
    """

    return TerminalReadTool().to_definition_if_avaliable()


def build_terminal_write_definition() -> ToolDefinition | None:
    """构建 ``terminal_write`` 的注册定义。

    返回:
        ``ToolDefinition``；该工具在当前平台不可用时返回 ``None``（见
        ``HandlerBase.avaliable()``）。
    """

    return TerminalWriteTool().to_definition_if_avaliable()


def build_terminal_signal_definition() -> ToolDefinition | None:
    """构建 ``terminal_signal`` 的注册定义。

    返回:
        当前平台均注册该工具；Windows worker 仅支持 interrupt，Unix 还支持 suspend
        和有条件的 canonical EOF。具体会话以 worker 握手能力为准。
    """

    return TerminalSignalTool().to_definition_if_avaliable()


def build_terminal_close_definition() -> ToolDefinition | None:
    """构建 ``terminal_close`` 的注册定义。

    返回:
        ``ToolDefinition``；该工具在当前平台不可用时返回 ``None``（见
        ``HandlerBase.avaliable()``）。
    """

    return TerminalCloseTool().to_definition_if_avaliable()


__all__ = [
    "TerminalCloseTool",
    "TerminalListTool",
    "TerminalReadTool",
    "TerminalSignalTool",
    "TerminalStartTool",
    "TerminalWriteTool",
    "build_terminal_close_definition",
    "build_terminal_list_definition",
    "build_terminal_read_definition",
    "build_terminal_signal_definition",
    "build_terminal_start_definition",
    "build_terminal_write_definition",
]
