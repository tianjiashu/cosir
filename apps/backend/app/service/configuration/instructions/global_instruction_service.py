"""系统级全局 ``AGENTS.md`` 配置 service。

负责该指令文件的读取、token 预算校验与原子写入。不负责系统提示词的分层构建与运行期截断（分别见
``SystemPromptBuilder`` 与 ``Constant.SystemPrompt``），也不改写已运行 Run 的上下文。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.config.constant import Constant
from app.config.logging.logger import log
from app.service.configuration import file_store
from app.utils.path.system_cosir import system_cosir_dir, system_instruction_file
from app.utils.token_estimator import TokenEstimator


class InstructionConfigurationError(ValueError):
    """全局指令内容不满足配置中心写入契约。"""


@dataclass(frozen=True)
class GlobalInstructionDocument:
    """全局指令文本及其预算元数据。"""

    content: str
    revision: str | None
    path: Path
    token_length: int
    max_tokens: int


class InstructionConfigurationService:
    """读取并原子更新系统级全局指令，不修改已运行 Run 的上下文。"""

    def __init__(self, *, path: Path | None = None) -> None:
        self.path = path or system_instruction_file()
        self.root = system_cosir_dir()

    def read(self) -> GlobalInstructionDocument:
        """读取缺失时视为空文本的全局指令。"""

        self.root.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            return self._document("", revision=None)
        text = file_store.read_revised_text(self.path, root=self.root)
        return self._document(text.content, revision=text.revision)

    def update(self, content: str, *, base_revision: str | None) -> GlobalInstructionDocument:
        """校验 token 预算后，以 ``base_revision`` 为前置条件原子写入；已固化系统提示词的 Session 不受影响。

        异常:
            ConfigurationRevisionConflict: 文件在读取之后被别处改过。
        """

        draft = self._document(content, revision=None)
        if draft.token_length > draft.max_tokens:
            raise InstructionConfigurationError(
                f"全局 AGENTS.md 超出 token 预算: {draft.token_length} > {draft.max_tokens}"
            )
        revision = file_store.write_text_if_revision(
            self.path, content, root=self.root, base_revision=base_revision
        )
        log.info(
            "configuration_global_instruction_written",
            extra={"msg": "系统全局指令已保存", "data": {"path": str(self.path)}},
        )
        return self._document(content, revision=revision)

    def _document(self, content: str, *, revision: str | None) -> GlobalInstructionDocument:
        """把正文投影为文档对象，并附上 token 预算元数据（预算值取自 ``Constant.SystemPrompt``）。

        参数:
            content: 全局指令正文，空串合法（``token_length`` 记 0）。
            revision: 正文的内容版本，文件不存在时为 ``None``。

        返回:
            含正文、路径、``token_length`` 与 ``max_tokens`` 的文档对象。

        异常:
            无。

        副作用:
            无（纯计算，不读写文件）。
        """

        return GlobalInstructionDocument(
            content=content,
            revision=revision,
            path=self.path,
            token_length=TokenEstimator.estimate(content) if content else 0,
            max_tokens=Constant.SystemPrompt.GLOBAL_INSTRUCTION_MAX_FILE_TOKENS,
        )
