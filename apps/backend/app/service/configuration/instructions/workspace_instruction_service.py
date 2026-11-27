"""workspace ``AGENTS.md`` 配置 service。

本 service 复用运行时的 workspace 指令定位规则，负责读取、预算校验和原子写入当前有效的
``AGENTS.md``。它不构建系统提示词，也不修改已运行 Run 的持久化 context。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.config.constant import Constant
from app.config.logging.logger import log
from app.service.configuration import file_store
from app.utils.token_estimator import TokenEstimator
from app.utils.workspace_instruction import find_workspace_instruction_file


class WorkspaceInstructionConfigurationError(ValueError):
    """workspace AGENTS.md 内容不满足配置中心写入契约。"""


@dataclass(frozen=True)
class WorkspaceInstructionDocument:
    """workspace 指令正文及其实际文件位置和预算元数据。"""

    content: str
    revision: str | None
    path: Path
    relative_path: str
    token_length: int
    max_tokens: int
    exists: bool


class WorkspaceInstructionConfigurationService:
    """读取和更新一个 workspace 当前有效的 ``AGENTS.md`` 文件。"""

    def __init__(self, workspace_root: str | Path) -> None:
        """绑定已校验的 workspace 根目录，不在构造阶段读写文件。"""

        self.root = Path(workspace_root)

    def read(self) -> WorkspaceInstructionDocument:
        """读取当前运行时会选中的指令文件，缺失时返回 workspace 根文件的空文档。

        读取不会为了展示配置而创建文件；只有显式保存空正文时才会创建或替换根目录下的
        ``AGENTS.md``。
        """

        path = self._effective_path()
        if not path.exists():
            return self._document("", revision=None, path=path, exists=False)
        text = file_store.read_revised_text(path, root=self.root)
        return self._document(text.content, revision=text.revision, path=path, exists=True)

    def update(self, content: str, *, base_revision: str | None) -> WorkspaceInstructionDocument:
        """校验 token 预算，以 ``base_revision`` 为前置条件原子更新当前有效指令文件。

        没有既有候选时写入 workspace 根目录 ``AGENTS.md``；已有嵌套候选时保持其路径不变，
        以保证配置页面与运行时的有效文件一致。有效文件在编辑期间换了位置（比如更浅处新出现一份）
        时，新位置的版本对不上基准，同样按冲突处理。已固化系统提示词的 Session 不受影响。

        异常:
            ConfigurationRevisionConflict: 有效文件在读取之后被别处改过或换了位置。
        """

        path = self._effective_path()
        draft = self._document(content, revision=None, path=path, exists=True)
        if draft.token_length > draft.max_tokens:
            raise WorkspaceInstructionConfigurationError(
                "workspace AGENTS.md 超出 token 预算: " f"{draft.token_length} > {draft.max_tokens}"
            )
        revision = file_store.write_text_if_revision(
            path, content, root=self.root, base_revision=base_revision
        )
        log.info(
            "configuration_workspace_instruction_written",
            extra={
                "msg": "workspace AGENTS.md 配置已保存",
                "data": {"workspace_root": str(self.root), "path": str(path)},
            },
        )
        return self._document(content, revision=revision, path=path, exists=True)

    def _effective_path(self) -> Path:
        """运行时会选中的指令文件；没有候选时为 workspace 根目录的 ``AGENTS.md``。"""

        found = find_workspace_instruction_file(self.root)
        return found[1] if found is not None else self.root / "AGENTS.md"

    def _document(
        self, content: str, *, revision: str | None, path: Path, exists: bool
    ) -> WorkspaceInstructionDocument:
        """把正文和路径投影为配置文档；不执行写入侧校验。"""

        relative_path = str(path.relative_to(self.root).as_posix())
        return WorkspaceInstructionDocument(
            content=content,
            revision=revision,
            path=path,
            relative_path=relative_path,
            token_length=TokenEstimator.estimate(content) if content else 0,
            max_tokens=Constant.SystemPrompt.WORKSPACE_INSTRUCTION_MAX_FILE_TOKENS,
            exists=exists,
        )
