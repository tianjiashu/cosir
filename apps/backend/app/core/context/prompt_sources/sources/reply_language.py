"""回复语言来源：``Settings.DEFAULT_LANGUAGE``（BCP-47 代码，模型按代码选语言）。

渲染成输出契约层里的一行；来源是系统 ``.env``，经 ``Settings.load`` 进入进程。
"""

from __future__ import annotations

from collections.abc import Mapping

from app.config.settings import Settings
from app.core.context.prompt_sources.source import (
    ItemDiff,
    PromptSource,
    SourceContext,
    SourceItem,
    SourceItems,
    fingerprint,
)
from app.models.message_metadata import PromptSourceEntry

_ITEM_KEY = "language"


class ReplyLanguageSource(PromptSource):
    key = "reply_language"

    def applies(self, context: SourceContext) -> bool:
        return True

    def project(self, context: SourceContext) -> dict[str, SourceItem]:
        language = Settings.DEFAULT_LANGUAGE
        text = (
            f"- User language: {language}. Reply in that language in a friendly tone; do "
            "not use emoji unless the user asks."
        )
        return {_ITEM_KEY: SourceItem(fingerprint(language), text)}

    def render(self, items: SourceItems) -> str:
        return "\n".join(item.text for item in items.values())

    def describe(
        self, diff: ItemDiff, items: SourceItems, previous: Mapping[str, PromptSourceEntry]
    ) -> str:
        return "- The user's language setting changed:\n" + self.render(items)
