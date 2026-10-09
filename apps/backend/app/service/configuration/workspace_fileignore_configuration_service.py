"""workspace ``.fileignore`` 配置 service。

本 service 只负责规则文件的安全读写和保存前校验；搜索运行时仍由
``load_search_ignore_rules`` 每次调用时解析，不在这里维护匹配器缓存。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.config.logging.logger import log
from app.core.tools.tool_handler.search.gitignore_rules import (
    build_gitwildmatch_spec,
    valid_rule_lines,
)
from app.core.tools.tool_handler.search.ignore_rules import (
    FILEIGNORE_MAX_FILE_BYTES,
    FILEIGNORE_MAX_RULES,
    ignore_file_path,
)
from app.service.configuration.file_store import ConfigurationFileStore


class WorkspaceFileIgnoreConfigurationError(ValueError):
    """workspace .fileignore 内容不满足配置中心写入契约。"""


@dataclass(frozen=True)
class WorkspaceFileIgnoreDocument:
    """workspace 忽略规则正文及校验元数据。"""

    content: str
    path: Path
    exists: bool
    rule_count: int
    max_rules: int


class WorkspaceFileIgnoreConfigurationService:
    """读取和更新 workspace ``.cosir/.fileignore``，不缓存搜索匹配器。"""

    def __init__(self, workspace_root: str | Path) -> None:
        """绑定 workspace 根目录和固定规则文件路径。"""

        self.root = Path(workspace_root)
        self.path = ignore_file_path(self.root)
        self.store = ConfigurationFileStore()

    def read(self) -> WorkspaceFileIgnoreDocument:
        """读取规则文件；缺失时返回空正文但不创建文件。"""

        if not self.path.exists():
            return self._document("", exists=False)
        content = self.store.read_text(self.path, root=self.root / ".cosir")
        return self._document(content, exists=True)

    def update(self, content: str) -> WorkspaceFileIgnoreDocument:
        """校验标准 gitignore 语法、大小和规则数后原子写入规则文件。"""

        self._validate(content)
        with self.store.locked(self.path):
            self.store.write_text_atomic(self.path, content, root=self.root / ".cosir")
        log.info(
            "configuration_workspace_fileignore_written",
            extra={
                "msg": "workspace .fileignore 配置已保存",
                "data": {
                    "workspace_root": str(self.root),
                    "rule_count": len(valid_rule_lines(content)),
                },
            },
        )
        return self._document(content, exists=True)

    def _document(self, content: str, *, exists: bool) -> WorkspaceFileIgnoreDocument:
        """把规则正文投影为配置文档，不改变正文。"""

        return WorkspaceFileIgnoreDocument(
            content=content,
            path=self.path,
            exists=exists,
            rule_count=len(valid_rule_lines(content)),
            max_rules=FILEIGNORE_MAX_RULES,
        )

    @staticmethod
    def _validate(content: str) -> None:
        """验证规则文件的大小、行数和 gitwildmatch 语法。"""

        if len(content) > FILEIGNORE_MAX_FILE_BYTES:
            raise WorkspaceFileIgnoreConfigurationError(
                f".fileignore 文件超过大小限制: {len(content)} > {FILEIGNORE_MAX_FILE_BYTES}"
            )
        lines = valid_rule_lines(content)
        if len(lines) > FILEIGNORE_MAX_RULES:
            raise WorkspaceFileIgnoreConfigurationError(
                f".fileignore 规则超过数量限制: {len(lines)} > {FILEIGNORE_MAX_RULES}"
            )
        try:
            build_gitwildmatch_spec(lines)
        except Exception as exc:
            raise WorkspaceFileIgnoreConfigurationError(
                f".fileignore 含无效 gitignore 规则: {type(exc).__name__}"
            ) from exc
