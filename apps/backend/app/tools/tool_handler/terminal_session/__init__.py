"""交互终端 Agent 工具（list / read / write）与注册定义构建入口。

所有 handler 复用进程内的终端 session service；用户经 HTTP / WS 使用同一批会话。Agent 只能列出、
读取，以及把一行命令填进空提示符（不执行，回车只能由用户按）。

注意：子模块 ``list`` 导入后会遮蔽本包命名空间里的内置 ``list``，因此注解延迟求值。
"""

from __future__ import annotations

from app.core.tool.schemas import ToolDefinition
from app.tools.tool_handler.terminal_session.list import TerminalListTool
from app.tools.tool_handler.terminal_session.read import TerminalReadTool
from app.tools.tool_handler.terminal_session.write import TerminalWriteTool


def build_terminal_definitions() -> list[ToolDefinition]:
    """按注册顺序构建 3 个终端工具定义；终端工具不分平台，全部注册。"""

    return [
        tool.to_definition()
        for tool in (
            TerminalListTool(),
            TerminalReadTool(),
            TerminalWriteTool(),
        )
    ]


__all__ = [
    "TerminalListTool",
    "TerminalReadTool",
    "TerminalWriteTool",
    "build_terminal_definitions",
]
