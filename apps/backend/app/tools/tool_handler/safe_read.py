"""安全只读工具集合。"""

from typing import List

from app.tools.schemas.tool_definition import ToolDefinition
from app.tools.tool_handler.definitions.read_file import build_read_file_definition


class SafeReadTools:
    """内置只读工具集合，按权限 ``safe_read`` 归类。"""

    def __init__(self, project_root: str) -> None:
        """初始化只读工具集合。

        参数:
            project_root: 项目根目录，注入到每个工具的 handler。

        返回:
            无。

        异常:
            无。

        副作用:
            无。
        """
        self.project_root = project_root

    def definitions(self) -> List[ToolDefinition]:
        """返回本组工具的 ToolDefinition 列表。

        参数:
            无。

        返回:
            ToolDefinition 列表（当前含 read_file）。

        异常:
            无。

        副作用:
            无。
        """
        return [
            build_read_file_definition(self.project_root),
        ]
