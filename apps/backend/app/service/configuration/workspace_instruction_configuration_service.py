"""workspace ``AGENTS.md`` 配置 service。

本 service 复用运行时的 workspace 指令定位规则，负责读取、预算校验和原子写入当前有效的
``AGENTS.md``。它不构建系统提示词，也不修改已运行 Run 的持久化 context。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.config.constant import Constant
from app.config.logging.logger import log
from app.service.configuration.file_store import ConfigurationFileStore
from app.utils.token_estimator import TokenEstimator
from app.utils.workspace_instruction import find_workspace_instruction_file


class WorkspaceInstructionConfigurationError(ValueError):
    """workspace AGENTS.md 内容不满足配置中心写入契约。"""


@dataclass(frozen=True)
class WorkspaceInstructionDocument:
    """workspace 指令正文及其实际文件位置和预算元数据。"""

    content: str
    path: Path
    relative_path: str
    token_length: int
    max_tokens: int
    exists: bool


class WorkspaceInstructionConfigurationService:
    """读取和更新一个 workspace 当前有效的 ``AGENTS.md`` 文件。"""

    def __init__(self, workspace_root: str | Path) -> None:
        """绑定已校验的 workspace 根目录，不在构造阶段读写文件。"""

        self.root = Path(workspace_root).resolve()
        self.store = ConfigurationFileStore()

    def read(self) -> WorkspaceInstructionDocument:
        """读取当前运行时会选中的指令文件，缺失时返回 workspace 根文件的空文档。

        读取不会为了展示配置而创建文件；只有显式保存空正文时才会创建或替换根目录下的
        ``AGENTS.md``。
        """

        found = find_workspace_instruction_file(self.root)
        path = found[1] if found is not None else self.root / "AGENTS.md"
        if not path.exists():
            return self._document("", path=path, exists=False)
        raw = self.store.read_text(path, root=self.root)
        return self._document(raw, path=path, exists=True)

    def update(self, content: str) -> WorkspaceInstructionDocument:
        """校验 token 预算并原子更新当前有效指令文件。

        没有既有候选时写入 workspace 根目录 ``AGENTS.md``；已有嵌套候选时保持其路径不变，
        以保证配置页面与运行时的有效文件一致。
        """

        document = self.read()
        next_document = self._document(content, path=document.path, exists=True)
        if next_document.token_length > next_document.max_tokens:
            raise WorkspaceInstructionConfigurationError(
                "workspace AGENTS.md 超出 token 预算: "
                f"{next_document.token_length} > {next_document.max_tokens}"
            )
        with self.store.locked(document.path):
            self.store.write_text_atomic(document.path, content, root=self.root)
        log.info(
            "configuration_workspace_instruction_written",
            extra={
                "msg": "workspace AGENTS.md 配置已保存",
                "data": {"workspace_root": str(self.root), "path": str(document.path)},
            },
        )
        return next_document

    def _document(self, content: str, *, path: Path, exists: bool) -> WorkspaceInstructionDocument:
        """把正文和路径投影为配置文档；不执行写入侧校验。"""

        relative_path = str(path.relative_to(self.root).as_posix())
        return WorkspaceInstructionDocument(
            content=content,
            path=path,
            relative_path=relative_path,
            token_length=TokenEstimator.estimate(content) if content else 0,
            max_tokens=Constant.SystemPrompt.WORKSPACE_INSTRUCTION_MAX_FILE_TOKENS,
            exists=exists,
        )
