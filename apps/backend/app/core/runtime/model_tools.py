"""从可执行工具构建面向模型的工具定义。"""

from typing import Iterable, List

from app.models.base import ModelToolDefinition
from app.tools.schemas import ToolDefinition


def build_model_tool_definitions(
    tools: Iterable[ToolDefinition],
) -> List[ModelToolDefinition]:
    """将可执行工具定义转换为面向模型的定义。

    参数:
        tools: 来自调度器边界的可执行工具定义。

    返回:
        按工具名排序的面向模型的工具定义。

    异常:
        无。

    副作用:
        无。
    """

    model_tools = [
        ModelToolDefinition(
            name=tool.name,
            description=tool.description,
            parameters_schema=tool.parameters_schema,
        )
        for tool in tools
    ]
    return sorted(model_tools, key=lambda tool: tool.name)
