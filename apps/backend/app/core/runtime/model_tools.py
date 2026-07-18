"""Build model-facing tool definitions from executable tools."""

from typing import Iterable, List

from app.models.base import ModelToolDefinition
from app.tools.schemas import ToolDefinition


def build_model_tool_definitions(
    tools: Iterable[ToolDefinition],
) -> List[ModelToolDefinition]:
    """Convert executable tool definitions into model-facing definitions."""

    model_tools = [
        ModelToolDefinition(
            name=tool.name,
            description=tool.description,
            parameters_schema=tool.parameters_schema,
        )
        for tool in tools
    ]
    return sorted(model_tools, key=lambda tool: tool.name)
