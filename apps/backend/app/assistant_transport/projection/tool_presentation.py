"""从当前版本工具定义解析呈现类别，历史不冻结旧样式。"""

from collections.abc import Mapping

from app.core.tool.presentation import ToolPresentation
from app.core.tool.tool_registry import ToolRegistry


def presentation_catalog(registry: ToolRegistry) -> dict[str, ToolPresentation]:
    """只读定义；不重新判断历史工具的准入或执行结果。"""
    return {item.name: item.conversation_presentation for item in registry.get_all_definitions()}


def tool_presentation(name: str, catalog: Mapping[str, ToolPresentation]) -> ToolPresentation:
    """已移除工具保留可见诊断记录。"""
    return catalog.get(name, "activity")
