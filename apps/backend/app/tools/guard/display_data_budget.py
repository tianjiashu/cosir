"""客户端展示数据通道的字符预算守卫。

``ToolObservation.display_data`` 会整体进入事件流与可观测性平台，若 handler 直接
放入整页正文（如 web_extract 的网页内容），该通道会绕过面向模型的
:class:`ToolOutputBudget` 无约束膨胀。本守卫统一对展示数据里的长文本字段做截断，
并附加原始长度与截断标记，供客户端自行决定如何呈现。

职责边界：
- 负责：展示数据通道的输出治理（截断 + 截断标记）。
- 不负责：任何渲染（摘要文本、列表条目、diff 条目一律由客户端生成）。
"""

from dataclasses import replace
from typing import Any

from app.tools.schemas import ToolObservation

# 展示通道中允许携带的单个文本字段最大字符数。
DEFAULT_DISPLAY_TEXT_MAX_CHARS = 2_000


class DisplayDataBudget:
    """对 ``display_data`` 中的长文本字段应用统一字符预算。"""

    def __init__(self, max_chars: int = DEFAULT_DISPLAY_TEXT_MAX_CHARS) -> None:
        """初始化展示数据预算。

        参数:
            max_chars: 展示数据中单个文本字段的最大字符数。

        返回:
            无。

        异常:
            ValueError: ``max_chars`` 小于 1 时抛出。

        副作用:
            无。
        """

        if max_chars < 1:
            raise ValueError("max_chars must be greater than zero")
        self._max_chars = max_chars

    def apply(self, observation: ToolObservation) -> ToolObservation:
        """截断展示数据中的超长文本字段。

        参数:
            observation: 已经过模型通道预算处理的工具观察。

        返回:
            展示数据未超限时返回原对象；超限时返回展示数据已截断的新对象。

        异常:
            无。

        副作用:
            无（不修改入参，返回新对象）。
        """

        display_data = observation.data
        if not display_data:
            return observation
        trimmed, changed = self._trim_value(display_data)
        if not changed:
            return observation
        return replace(observation, data=trimmed)

    def _trim_value(self, value: Any) -> tuple[Any, bool]:
        """递归截断任意展示数据结构中的长文本。

        参数:
            value: 展示数据中的任意节点（字典 / 列表 / 字符串 / 其他标量）。

        返回:
            ``(处理后的值, 是否发生截断)``。

        异常:
            无。

        副作用:
            无。
        """

        if isinstance(value, str):
            if len(value) <= self._max_chars:
                return value, False
            return value[: self._max_chars], True
        if isinstance(value, dict):
            return self._trim_mapping(value)
        if isinstance(value, list):
            results = [self._trim_value(item) for item in value]
            changed = any(item_changed for _, item_changed in results)
            return [item for item, _ in results], changed
        return value, False

    def _trim_mapping(self, mapping: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        """截断字典节点中的长文本，并为被截断的字段补充可观测标记。

        参数:
            mapping: 展示数据中的字典节点。

        返回:
            ``(处理后的字典, 是否发生截断)``；被截断的字符串字段会额外写入
            ``{key}_chars``（原始长度）与 ``{key}_truncated=True``。

        异常:
            无。

        副作用:
            无。
        """

        trimmed: dict[str, Any] = {}
        changed = False
        for key, item in mapping.items():
            new_item, item_changed = self._trim_value(item)
            trimmed[key] = new_item
            if not item_changed:
                continue
            changed = True
            if isinstance(item, str):
                trimmed[f"{key}_chars"] = len(item)
                trimmed[f"{key}_truncated"] = True
        return trimmed, changed
