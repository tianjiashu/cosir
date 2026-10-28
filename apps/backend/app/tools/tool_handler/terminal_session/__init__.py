"""交互终端 Agent 工具（start / write / read / list / close）与注册定义构建入口。

所有 handler 复用进程内的终端 session service；用户经 HTTP / WS 使用同一批会话，双方以输入租约
互斥。``terminal_signal`` 已并入 ``terminal_write`` 的 ``signal`` 输入类型。

注意：子模块 ``list`` 导入后会遮蔽本包命名空间里的内置 ``list``，因此注解延迟求值。
"""

from __future__ import annotations

from app.core.tool.schemas import ToolDefinition
from app.tools.tool_handler.terminal_session.close import TerminalCloseTool
from app.tools.tool_handler.terminal_session.list import TerminalListTool
from app.tools.tool_handler.terminal_session.read import TerminalReadTool
from app.tools.tool_handler.terminal_session.start import TerminalStartTool
from app.tools.tool_handler.terminal_session.write import TerminalWriteTool


def build_terminal_definitions() -> list[ToolDefinition]:
    """按注册顺序构建 5 个终端工具定义；终端工具不分平台，全部注册。"""

    return [
        tool.to_definition()
        for tool in (
            TerminalListTool(),
            TerminalStartTool(),
            TerminalReadTool(),
            TerminalWriteTool(),
            TerminalCloseTool(),
        )
    ]


__all__ = [
    "TerminalCloseTool",
    "TerminalListTool",
    "TerminalReadTool",
    "TerminalStartTool",
    "TerminalWriteTool",
    "build_terminal_definitions",
]
