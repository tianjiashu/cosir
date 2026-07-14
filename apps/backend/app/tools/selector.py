"""模型单轮可见工具选择。"""

from dataclasses import dataclass
from typing import Iterable, Sequence

from app.tools.catalog.records import ToolCatalogRecord
from app.tools.types import ToolDefinition


@dataclass(frozen=True)
class ToolSelectionContext:
    """描述本轮模型调用的工具选择约束。

    参数:
        intent: 当前用户意图或任务阶段文本。
        allowed_permissions: Agent 当前允许暴露的权限集合。
        max_tools: 最多注入模型的工具数。

    返回:
        不可变选择上下文。

    异常:
        无。

    副作用:
        无。
    """

    intent: str
    allowed_permissions: Sequence[str]
    max_tools: int = 8


class ToolSelector:
    """将目录候选收敛为模型可见工具定义。"""

    def select(
        self,
        records: Iterable[ToolCatalogRecord],
        definitions: Iterable[ToolDefinition],
        context: ToolSelectionContext,
    ) -> list[ToolDefinition]:
        """在权限和数量限制内选择模型可见工具。

        参数:
            records: 已按检索相关性排序的目录记录。
            definitions: 已注册工具定义。
            context: 当前选择约束。

        返回:
            至多 ``max_tools`` 个匹配工具定义。

        异常:
            ValueError: 当 max_tools 小于 1 时抛出。

        副作用:
            无。
        """

        if context.max_tools < 1:
            raise ValueError("max_tools must be greater than zero")
        definitions_by_name = {definition.name: definition for definition in definitions}
        allowed = set(context.allowed_permissions)
        selected = []
        for record in records:
            definition = definitions_by_name.get(record.name)
            if definition is None or not record.visible_by_default or definition.permission not in allowed:
                continue
            selected.append(definition)
            if len(selected) == context.max_tools:
                break
        return selected
