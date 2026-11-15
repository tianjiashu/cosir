"""全局指令来源：系统级 ``<system_cosir_dir>/AGENTS.md``，跨所有工作区生效。

来源唯一、路径固定（``system_instruction_file``），不参与目录层级择优。文件缺失、读取失败或内容为空时
没有条目；空文件由 lifespan 启动时建好供用户就地编辑，本来源只读。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

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
from app.models.message_metadata import PromptSourceEntry
from app.service.configuration import file_store
from app.utils.file_signature import stat_signature
from app.utils.file_utils import read_text_file
from app.utils.path.system_cosir import system_instruction_file
from app.utils.token_estimator import TokenEstimator

_ITEM_KEY = "AGENTS.md"


class GlobalInstructionsSource(PromptSource):
    key = "global_instructions"

    def applies(self, context: SourceContext) -> bool:
        return True

    def project(self, context: SourceContext) -> dict[str, SourceItem]:
        """系统级来源：作用域取文件路径本身，数据根切换后自然是另一份缓存。"""

        path = system_instruction_file()
        return projection_cache.get(
            self.key, str(path), stat_signature([path]), lambda: self._load(path)
        )

    @staticmethod
    def _load(path: Path) -> dict[str, SourceItem]:
        if not path.is_file():
            return {}
        try:
            # 拒绝指向系统 .cosir 之外的符号链接：全局指令只认这一个位置。
            file_store.assert_safe_child(path.parent, path)
            raw = read_text_file(path)
        except (OSError, ValueError, UnicodeDecodeError) as exc:
            log.warning(
                "global_instruction_read_failed",
                extra={"msg": "全局指令读取失败", "data": {"path": str(path), "error": str(exc)}},
            )
            return {}
        content = TokenEstimator.truncate(
            raw, Constant.SystemPrompt.GLOBAL_INSTRUCTION_MAX_FILE_TOKENS
        )
        if not content.strip():
            return {}
        return {_ITEM_KEY: SourceItem(fingerprint(content), content)}

    def render(self, items: SourceItems) -> str:
        return "\n".join(item.text for item in items.values())

    def describe(
        self, diff: ItemDiff, items: SourceItems, previous: Mapping[str, PromptSourceEntry]
    ) -> str:
        if not items:
            return "- The global AGENTS.md (<global_layer>) is now empty; drop its rules."
        new = next(iter(items.values())).text
        old = next(iter(previous.values())).text if previous else ""
        return "- The global AGENTS.md (<global_layer>) changed. " + text_change(old, new)
