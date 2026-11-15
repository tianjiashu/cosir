"""工作区指令来源：按目录层级择优选出的唯一 ``AGENTS.md``（定位规则见 ``workspace_instruction``）。

条目键是选中文件的工作区相对路径：换成了另一个文件（例如更浅处新建了 AGENTS.md）也算变化。
文本首行标明来源路径；无命中或读取失败时没有条目。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from app.config.constant import Constant
from app.config.logging.logger import log
from app.core.context.prompt_sources.cache import projection_cache, workspace_scope
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
from app.utils.file_signature import stat_signature
from app.utils.file_utils import read_text_file
from app.utils.token_estimator import TokenEstimator


class WorkspaceInstructionsSource(PromptSource):
    key = "workspace_instructions"

    def applies(self, context: SourceContext) -> bool:
        return True

    def project(self, context: SourceContext) -> dict[str, SourceItem]:
        """签名是「选中了哪个文件 + 它的修改时间与大小」；定位由调用方做好放在 ``context`` 里
        （多久定位一次见 ``SourceContext``）。换成另一个文件也算变化。
        """

        workspace_root = context.workspace_root
        found = context.workspace_instruction
        signature = None if found is None else (found[0], stat_signature([found[1]]))
        return projection_cache.get(
            self.key, workspace_scope(workspace_root), signature, lambda: self._load(found)
        )

    @staticmethod
    def _load(found: tuple[Path, Path] | None) -> dict[str, SourceItem]:
        if found is None:
            return {}
        relative, absolute = found
        try:
            raw = read_text_file(absolute)
        except (OSError, UnicodeDecodeError) as exc:
            log.warning(
                "workspace_instruction_read_failed",
                extra={
                    "msg": "工作区指令读取失败",
                    "data": {"path": str(absolute), "error": str(exc)},
                },
            )
            return {}
        content = TokenEstimator.truncate(
            raw, Constant.SystemPrompt.WORKSPACE_INSTRUCTION_MAX_FILE_TOKENS
        )
        key = relative.as_posix()
        text = f"{_HEADER}./{key}\n{content}"
        return {key: SourceItem(fingerprint(text), text)}

    def render(self, items: SourceItems) -> str:
        return "\n".join(item.text for item in items.values())

    def describe(
        self, diff: ItemDiff, items: SourceItems, previous: Mapping[str, PromptSourceEntry]
    ) -> str:
        """条目键是选中文件的路径：换了文件就说明从哪个换到哪个，内容仍只发 diff。"""

        if not items:
            return "- The workspace AGENTS.md (<workspace_layer>) is gone; drop its rules."
        (path, item), = items.items()
        new = _body(item.text)
        if not previous:
            return f"- The workspace AGENTS.md is now ./{path}. " + text_change("", new)
        (old_path, old_item), = previous.items()
        where = "" if old_path == path else f" It is now loaded from ./{path} (was ./{old_path})."
        return (
            f"- The workspace AGENTS.md (<workspace_layer>) changed.{where} "
            + text_change(_body(old_item.text), new)
        )


_HEADER = "# Loaded AGENTS.md from workspace path: "


def _body(text: str) -> str:
    """条目文本去掉首行来源标题：路径变化另外说明，diff 只比正文。"""

    return text.split("\n", 1)[1] if text.startswith(_HEADER) else text
