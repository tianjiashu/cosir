"""用于查找可用工具的工具注册表。"""

from typing import Dict, Iterable, List

from app.tools.types import ToolDefinition


class ToolRegistry:
    """按名称存储工具定义。"""

    def __init__(self, tools: Iterable[ToolDefinition] = ()) -> None:
        """使用可选的工具定义初始化注册表。

        参数:
            tools: 需要立即注册的工具定义。

        返回:
            无。

        异常:
            ValueError: 如果提供了重复的工具名。

        副作用:
            在内存中保存工具定义。
        """

        self._tools: Dict[str, ToolDefinition] = {}
        for tool in tools:
            self.register(tool)

    def register(self, tool: ToolDefinition) -> None:
        """注册一个工具定义。

        参数:
            tool: 待注册的工具定义。

        返回:
            无。

        异常:
            ValueError: 如果同名工具已注册。

        副作用:
            将工具定义添加到注册表。
        """

        if tool.name in self._tools:
            raise ValueError(f"tool already registered: {tool.name}")
        self._tools[tool.name] = tool

    def get(self, tool_name: str) -> ToolDefinition:
        """按名称返回一个已注册的工具。

        参数:
            tool_name: 待查找的工具名。

        返回:
            匹配的工具定义。

        异常:
            KeyError: 如果工具未注册。

        副作用:
            无。
        """

        return self._tools[tool_name]

    def list_tools(self) -> List[ToolDefinition]:
        """以与插入顺序无关的名称顺序列出已注册工具。

        参数:
            无。

        返回:
            按名称排序的已注册工具。

        异常:
            无。

        副作用:
            无。
        """

        return [self._tools[name] for name in sorted(self._tools)]
