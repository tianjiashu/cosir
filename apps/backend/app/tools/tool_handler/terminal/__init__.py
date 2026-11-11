"""终端 Agent 工具：一个工具一个入口文件，文件名即工具名。

``execute_terminal`` 跑一条命令（自带 ``build_execute_terminal_definition``，与其他独立工具同一范式）；
``terminal_list`` / ``terminal_read`` / ``terminal_stop`` 同时覆盖用户打开的交互会话与 Agent 自己运行中
的命令（两类终端的组合只在 ``task_terminals``）；``terminal_write`` 只把一行命令填进用户终端的空提示符
（不执行，回车只能由用户按）。终端本身（会话、命令、观察模型、worker）在 ``app.tools.terminal_runtime``，
本包只做参数复核、编排与 ``ToolObservation`` 组装。
"""

from __future__ import annotations

from app.core.tool.schemas import ToolDefinition
from app.tools.tool_handler.terminal.terminal_list import TerminalListTool
from app.tools.tool_handler.terminal.terminal_read import TerminalReadTool
from app.tools.tool_handler.terminal.terminal_stop import TerminalStopTool
from app.tools.tool_handler.terminal.terminal_write import TerminalWriteTool


def build_terminal_definitions() -> list[ToolDefinition]:
    """按注册顺序构建 list / read / write / stop 四个工具定义；不分平台，全部注册。"""

    return [
        tool.to_definition()
        for tool in (
            TerminalListTool(),
            TerminalReadTool(),
            TerminalWriteTool(),
            TerminalStopTool(),
        )
    ]

