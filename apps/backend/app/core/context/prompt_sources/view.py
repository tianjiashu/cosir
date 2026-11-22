"""一次投影的结果：本 Session 适用的各来源及其条目，供提示词渲染、记录基准与对账。"""

from __future__ import annotations

from dataclasses import dataclass

from app.core.context.prompt_sources.registry import PROMPT_SOURCES
from app.core.context.prompt_sources.source import (
    ItemDiff,
    PromptSource,
    SourceContext,
    SourceItem,
)
from app.models.message_context.message_metadata.system_message_metadata import (
    PromptSourceBaseline,
    PromptSourceEntry,
)


@dataclass(frozen=True)
class PromptSourceView:
    """``projections`` 只含适用于这个 Session 的来源；不适用的来源不出现，也不进基准。"""

    projections: dict[str, tuple[PromptSource, dict[str, SourceItem]]]

    @classmethod
    def collect(cls, context: SourceContext) -> PromptSourceView:
        """按登记顺序投影全部适用的来源；读配置，不写文件。"""

        return cls(
            {
                source.key: (source, source.project(context))
                for source in PROMPT_SOURCES
                if source.applies(context)
            }
        )

    def render(self, source: PromptSource) -> str:
        """渲染某个来源的提示词正文；不适用于这个 Session 时返回 ``""``。"""

        projection = self.projections.get(source.key)
        if projection is None:
            return ""
        return source.render(projection[1])

    def baseline(self) -> PromptSourceBaseline:
        return {
            key: {
                item_key: PromptSourceEntry(fingerprint=item.fingerprint, text=item.text)
                for item_key, item in items.items()
            }
            for key, (_, items) in self.projections.items()
        }

    def describe_changes(self, baseline: PromptSourceBaseline) -> str:
        """对照 Agent 已看到的基准，说明净差异；没有变化时返回 ``""``。

        基准里没有的来源按「之前什么都没看到」比较（新登记的来源会把现有内容告知一次）；基准里有、
        这个 Session 已不适用的来源不比较。各来源按登记顺序、条目按字典序输出，同一份变化写出的
        文本相同。
        """

        blocks = []
        for key, (source, items) in self.projections.items():
            previous = baseline.get(key, {})
            diff = ItemDiff.between(previous, items)
            if not diff.empty:
                blocks.append(source.describe(diff, items, previous))
        if not blocks:
            return ""
        return "\n".join([_CHANGE_HEADER, *blocks])


_CHANGE_HEADER = "Configuration changed since you last saw it; apply these changes from now on:"
