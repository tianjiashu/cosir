"""子 Agent 目录来源：本工作区可见的 CHILD Agent，供委派时挑选。

只在 Session 固化了委派工具时出现。条目键为 agent_id，文本是单行无序列表项 ``- id: 描述``；指纹取
这两个字段而不是文本，改写法不算配置变化。读取的是进程级 Agent 注册表。

不列可用工具与系统提示词正文：主 Agent 按描述挑选，能力边界（如只读）由描述写明；配置里的
``allowed_tools`` 也不是子 Session 实际拿到的工具（创建时还会按 Session 类型与派生深度过滤，见
``session_tool_scope``），照抄会说错，还让每个会话的提示词多出整串工具名。
"""

from __future__ import annotations

from collections.abc import Mapping

from app.core.agents.agent_profile import AgentProfileType
from app.core.context.prompt_sources.source import (
    ItemDiff,
    PromptSource,
    SourceContext,
    SourceItem,
    SourceItems,
    fingerprint,
)
from app.core.context.prompt_sources.sources.metadata_encoding import prompt_line
from app.core.tool.schemas.tool_names import TOOL_DELEGATE_TASK
from app.models.message_metadata import PromptSourceEntry


class ChildAgentsSource(PromptSource):
    key = "child_agents"

    def applies(self, context: SourceContext) -> bool:
        return TOOL_DELEGATE_TASK in context.tool_names

    def project(self, context: SourceContext) -> dict[str, SourceItem]:
        """异常:
        RuntimeError: 进程级 Agent 注册表尚未初始化（装配错误，不静默降级）。
        """

        # 函数内延迟导入：``app.config.configuration`` 模块级导入 ``app.tools``，而工具装配链会反向
        # 导入 ``app.core.context``，顶层导入会形成环（见 ``app/tools/__init__.py`` 的惰性导出说明）。
        from app.config.configuration import get_agent_registry

        items: dict[str, SourceItem] = {}
        for profile in get_agent_registry().list(context.workspace_root):
            if profile.agent_type is not AgentProfileType.CHILD:
                continue
            description = profile.description or ""
            text = f"- {prompt_line(profile.agent_id)}: {prompt_line(description)}"
            items[profile.agent_id] = SourceItem(fingerprint(profile.agent_id, description), text)
        return items

    def render(self, items: SourceItems) -> str:
        if not items:
            return ""
        return "Available child agents:\n" + "\n".join(item.text for item in items.values())

    def describe(
        self, diff: ItemDiff, items: SourceItems, previous: Mapping[str, PromptSourceEntry]
    ) -> str:
        """改了的条目给出整行（一行就是它的全部目录信息），删掉的只报 id。"""

        lines = ["- Child agents you can delegate to changed."]
        updated = [items[key].text for key in (*diff.added, *diff.changed)]
        if updated:
            lines.append("  Added or updated:\n" + "\n".join(f"  {text}" for text in updated))
        if diff.removed:
            lines.append(f"  No longer available: {', '.join(diff.removed)}.")
        return "\n".join(lines)
