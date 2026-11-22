"""外部服务说明来源：MCP 服务在握手时写给模型的使用说明（工作流程、工具先后顺序等），每个服务一段。

只在 Session 固化了 ``tool_search`` 时出现：外部工具只能经它加载，没有它说明就无处可用。说明经外部工具端口读
（``ExternalTools.instructions``，与延迟工具目录同一份本机记录、同样不连接服务），已按长度上限裁过。

说明是服务写的外部内容：渲染与通知里的原文一律进 ``ExternalContent`` 的 untrusted 块，块外只写本仓确定的
事实（服务名来自用户的配置），收尾用 ``close_usage``（说明本身就是用法，不说成「不是指令」）；
规则定义在系统提示词的外部内容段落，它与本来源同样以 ``tool_search`` 为条件。

不与 ``DeferredToolsSource`` 合并：那边条目是工具名、只报增删改的名字；这里条目是文档，变化要发 diff、
要包 untrusted，合在一个来源里渲染与通知都得按条目种类分支。

外部目录读不出来时投影一条标记：提示词里什么也不写（「目录不可用」由延迟工具目录说），通知只说明说明暂时
看不到，不把之前给过的说明当作删除；恢复后基准里只有标记，现有说明按新增重新给出全文。
"""

from __future__ import annotations

from collections.abc import Mapping

from app.core.context.prompt_sources.source import (
    ItemDiff,
    PromptSource,
    SourceContext,
    SourceItem,
    SourceItems,
    fingerprint,
    text_change,
    with_ambient,
)
from app.core.tool.external_tools import ExternalCatalogUnavailable, external_tools
from app.core.tool.results.external_content import ExternalContent
from app.core.tool.schemas.tool_names import TOOL_SEARCH
from app.models.message_context.message_metadata.system_message_metadata import PromptSourceEntry

# 外部目录读不出来时的唯一条目键：空串，服务名至少一个字符，不会撞上。
UNAVAILABLE_KEY = ""
_TAG = "mcp_server_instructions"
_HEADER = (
    "The following MCP servers have provided instructions for how to use their tools (load the "
    f"tools with {TOOL_SEARCH} before calling them):"
)
_UNAVAILABLE_TEXT = (
    "MCP server instructions cannot be read right now; the ones you saw earlier may be out of date."
)


class ServerInstructionsSource(PromptSource):
    key = "server_instructions"

    def applies(self, context: SourceContext) -> bool:
        return TOOL_SEARCH in context.tool_names

    def project(self, context: SourceContext) -> dict[str, SourceItem]:
        """条目键与文本：服务名 → 说明原文（未包装，基准存原文，通知据它算 diff）。"""

        try:
            instructions = external_tools().instructions(context.workspace_root)
        except ExternalCatalogUnavailable as exc:
            # 日志由延迟工具目录在同一次投影里记过，这里不重复。
            return {UNAVAILABLE_KEY: SourceItem(fingerprint(str(exc)), "")}
        return {item.source: SourceItem(fingerprint(item.text), item.text) for item in instructions}

    def render(self, items: SourceItems) -> str:
        servers = {name: item.text for name, item in items.items() if name != UNAVAILABLE_KEY}
        if not servers:
            return ""
        external = ExternalContent()
        blocks = [external.block(_TAG, text, name) for name, text in servers.items()]
        return external.close_usage("\n".join([_HEADER, *blocks]))

    def describe(
        self, diff: ItemDiff, items: SourceItems, previous: Mapping[str, PromptSourceEntry]
    ) -> str:
        """变化是背景信息：末尾带「不要主动讲给用户」一句（提示词里的说明本身不带，那是要照着用的）。"""

        return with_ambient(self._changes(diff, items, previous))

    @staticmethod
    def _changes(
        diff: ItemDiff, items: SourceItems, previous: Mapping[str, PromptSourceEntry]
    ) -> str:
        if UNAVAILABLE_KEY in items:
            return f"- {_UNAVAILABLE_TEXT}"
        external = ExternalContent()
        lines = ["- MCP server instructions changed."]
        added = [name for name in diff.added if name != UNAVAILABLE_KEY]
        if added:
            lines.append("  New instructions:")
            lines.extend(external.block(_TAG, items[name].text, name) for name in added)
        if diff.changed:
            lines.append("  Updated instructions:")
            lines.extend(
                external.block(_TAG, text_change(previous[name].text, items[name].text), name)
                for name in diff.changed
            )
        removed = [name for name in diff.removed if name != UNAVAILABLE_KEY]
        if removed:
            lines.append(
                f"  No longer in effect (server removed or gave none): {', '.join(removed)}."
            )
        if len(lines) == 1:
            # 只有标记消失：目录恢复了，但没有任何服务给说明。
            return "- MCP server instructions are readable again; no server provides any."
        return external.close_usage("\n".join(lines))
