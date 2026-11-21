"""延迟工具目录来源：这个 Session 现在可加载的延迟工具名字（只有名字，定义由 ``tool_search`` 按需取）。

只在 Session 固化了 ``tool_search`` 时出现。目录是 ``DeferredCatalog``：Session 工具面里延迟加载的内置工具
（固化、不变），加上外部工具端口给的工具（契约要求只读本机记录、不连接外部服务），这里每次模型请求前都会读
一次。条目键是工具名，指纹取模型可见定义（描述与参数）——定义变了也算变化，已加载过的要重新加载才拿到新
版本。外部目录读不出来时多一条「目录不可用」，内置工具照常列出；修好后它消失、外部工具名出现，同样算变化。

文案与本仓对齐的 ToolSearch 的系统通知一致（只换工具名）：首次列出与新增用同一句，移除用「no longer
available」。「available again」那一句要知道某个名字更早被告知过，基准只记最近一次，所以重新出现的工具按新增告知。
"""

from __future__ import annotations

import json
from collections.abc import Mapping

from app.config.logging.logger import log
from app.core.context.prompt_sources.source import (
    ItemDiff,
    PromptSource,
    SourceContext,
    SourceItem,
    SourceItems,
    fingerprint,
    with_ambient,
)
from app.core.tool.handler.deferred.deferred_catalog import DeferredCatalog
from app.core.tool.schemas.tool_names import TOOL_SEARCH
from app.models.message_metadata import PromptSourceEntry

# 外部目录读不出来时的唯一条目键：空串，工具名至少一个字符，不会撞上。
UNAVAILABLE_KEY = ""


class DeferredToolsSource(PromptSource):
    key = "deferred_tools"

    def applies(self, context: SourceContext) -> bool:
        return TOOL_SEARCH in context.tool_names

    def project(self, context: SourceContext) -> dict[str, SourceItem]:
        catalog = DeferredCatalog.read(context.deferred_tools, context.workspace_root)
        items = {
            entry.name: SourceItem(
                fingerprint(json.dumps(entry.function, sort_keys=True, ensure_ascii=False)), entry.name
            )
            for entry in catalog.entries
        }
        if catalog.unavailable is not None:
            log.warning(
                "system_prompt_deferred_catalog_unavailable",
                extra={"msg": "外部工具目录读取失败", "data": {"error": catalog.unavailable}},
            )
            items[UNAVAILABLE_KEY] = SourceItem(fingerprint(catalog.unavailable), catalog.unavailable)
        return items

    def render(self, items: SourceItems) -> str:
        names = [name for name in items if name != UNAVAILABLE_KEY]
        blocks = [self._available(names)] if names else []
        if UNAVAILABLE_KEY in items:
            blocks.append(items[UNAVAILABLE_KEY].text)
        return with_ambient("\n\n".join(blocks))

    def describe(
        self, diff: ItemDiff, items: SourceItems, previous: Mapping[str, PromptSourceEntry]
    ) -> str:
        return with_ambient(self._changes(diff, items, previous))

    @classmethod
    def _changes(
        cls, diff: ItemDiff, items: SourceItems, previous: Mapping[str, PromptSourceEntry]
    ) -> str:
        if UNAVAILABLE_KEY in items:
            # 外部工具读不出来只是暂时看不见，不按「不再可用」告知；内置工具不会变，不用重提。
            return items[UNAVAILABLE_KEY].text
        if UNAVAILABLE_KEY in previous:
            # 之前看到的是「目录不可用」：那时外部工具都不在基准里，现在出现的按新增告知；一个都没有也要说清。
            added = [name for name in diff.added if name != UNAVAILABLE_KEY]
            if not added:
                return "The deferred tool catalog is readable again; no new deferred tools are available."
            return cls._available(added)
        blocks = []
        if diff.added:
            blocks.append(cls._available(list(diff.added)))
        if diff.changed:
            blocks.append(
                "The following deferred tools changed their definitions. If you loaded them before, "
                f'load them again with {TOOL_SEARCH} before calling them:\n' + "\n".join(diff.changed)
            )
        if diff.removed:
            blocks.append(
                "The following deferred tools are no longer available. Do not search for them — "
                f"{TOOL_SEARCH} will return no match:\n" + "\n".join(diff.removed)
            )
        return "\n\n".join(blocks)


    @staticmethod
    def _available(names: list[str]) -> str:
        return (
            f"The following deferred tools are now available via {TOOL_SEARCH}. Their schemas are NOT "
            "loaded — calling them directly will fail with an error. Use "
            f'{TOOL_SEARCH} with query "select:<name>[,<name>...]" to load tool schemas before calling '
            "them:\n" + "\n".join(names)
        )
