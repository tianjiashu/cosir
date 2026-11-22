"""Agent 预设来源：当前 Agent 的系统预设正文（主 Agent 提示词或子 Agent 定义里的 ``system_prompt``）。

只有一个条目，键为 Agent id：手动分叉后换了 Agent，键随之变化，也算变化。正文由 Agent 注册表从配置
文件生成，本来源不读文件，只施加预算。
"""

from __future__ import annotations

from collections.abc import Mapping

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.context.prompt_sources.cache import projection_cache
from app.core.context.prompt_sources.source import (
    ItemDiff,
    PromptSource,
    SourceContext,
    SourceItem,
    SourceItems,
    fingerprint,
    text_change,
)
from app.models.message_context.message_metadata.system_message_metadata import PromptSourceEntry
from app.utils.token_estimator import TokenEstimator


class AgentPresetSource(PromptSource):
    key = "agent_preset"

    def applies(self, context: SourceContext) -> bool:
        return True

    def project(self, context: SourceContext) -> dict[str, SourceItem]:
        """预设为空时没有条目；超预算时截断并记 Agent id（不记正文）。

        每步都会对账，截断要反复估算 token，所以按「Agent + 原文」缓存：原文没变就不再截断，也不重复告警。
        """

        agent = context.agent
        raw = agent.system_prompt
        return projection_cache.get(
            self.key, agent.agent_id, raw, lambda: self._load(agent.agent_id, raw)
        )

    @staticmethod
    def _load(agent_id: str, raw: str | None) -> dict[str, SourceItem]:
        if raw is None or not raw.strip():
            return {}
        content = TokenEstimator.truncate(raw, Constant.SystemPrompt.AGENT_PERSONA_MAX_TOKENS)
        if content != raw:
            log.warning(
                "agent_system_prompt_truncated",
                extra={"msg": "Agent 预设超出预算已截断", "data": {"agent_id": agent_id}},
            )
        return {agent_id: SourceItem(fingerprint(content), content)}

    def render(self, items: SourceItems) -> str:
        return "\n".join(item.text for item in items.values())

    def describe(
        self, diff: ItemDiff, items: SourceItems, previous: Mapping[str, PromptSourceEntry]
    ) -> str:
        if not items:
            return (
                "- Your agent preset (<agent_layer>) was removed; follow only the remaining layers."
            )
        new = next(iter(items.values())).text
        old = next(iter(previous.values())).text if previous else ""
        return "- Your agent preset (<agent_layer>) changed. " + text_change(old, new)
