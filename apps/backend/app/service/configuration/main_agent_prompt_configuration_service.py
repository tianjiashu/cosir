"""主 Agent 系统提示词的文件配置 service。

负责主 Agent 系统提示词的默认模板安装、预算校验和原子写入，不负责构造 Agent profile、
系统提示词分层或 Task context 刷新。运行时 profile 的更新由配置装配层在保存成功后完成。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.config.constant import Constant
from app.config.logging.logger import log
from app.service.configuration.file_store import ConfigurationFileError, ConfigurationFileStore
from app.utils.cosir_paths import system_cosir_dir, system_main_agent_prompt_file
from app.utils.file_utils import read_text_file
from app.utils.token_estimator import TokenEstimator


class MainAgentPromptConfigurationError(ValueError):
    """主 Agent 系统提示词不满足配置契约。"""


@dataclass(frozen=True)
class MainAgentPromptDocument:
    """主 Agent 系统提示词及其预算和来源元数据。"""

    content: str
    path: Path
    token_length: int
    max_tokens: int
    source: str


class MainAgentPromptConfigurationService:
    """读取和更新主 Agent 系统提示词文件。

    缺少或外部修改为无效内容时，读取会回退到随应用分发的默认模板；通过配置 API 写入时，
    空文本和超出预算的正文会被拒绝。该 service 不修改 Registry，也不改写已经运行的 Run。
    """

    def __init__(
        self,
        *,
        path: Path | None = None,
        default_path: Path | None = None,
        root: Path | None = None,
    ) -> None:
        """绑定用户配置文件和随应用分发的默认模板路径。

        参数：
            path: 用户配置文件路径；缺省为系统 ``.cosir`` 下的固定文件。
            default_path: 默认模板路径；缺省为后端随应用分发的 ``main_agent.md``。
            root: 配置文件的受信任根目录；缺省为系统 ``.cosir``，测试或隔离装配可显式传入。

        异常：
            无。路径只在读写时执行安全校验。

        副作用：
            无；构造阶段不访问文件系统。
        """

        self.path = path or system_main_agent_prompt_file()
        self.root = root or system_cosir_dir()
        self.default_path = default_path or (
            Path(__file__).resolve().parents[2]
            / "core"
            / "context"
            / "system_prompt"
            / "main_agent.md"
        )
        self.store = ConfigurationFileStore()

    def read(self) -> MainAgentPromptDocument:
        """读取当前有效 prompt，缺失或无效时安装并返回默认模板。

        返回：
            当前实际供主 Agent 使用的提示词及其来源元数据。

        异常：
            MainAgentPromptConfigurationError: 默认模板为空或超出预算。
            ConfigurationPathError / OSError: 用户配置路径或目录无法安全访问。

        副作用：
            用户配置缺失时原子创建配置文件；已有无效文件不会被覆盖，只记录日志并使用默认模板。
        """

        self.store.assert_safe_child(self.root, self.path)
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            content = self._read_default()
            self.store.write_text_atomic(self.path, content, root=self.root)
            return self._document(content, source="builtin_default", path=self.path)

        try:
            raw = self.store.read_text(self.path, root=self.root)
        except ConfigurationFileError as exc:
            log.error(
                "configuration_main_agent_prompt_fallback",
                extra={
                    "msg": "主 Agent prompt 配置读取失败，使用内置默认模板",
                    "data": {"path": str(self.path), "error_type": type(exc).__name__},
                },
            )
            return self._document(self._read_default(), source="builtin_fallback", path=self.path)

        try:
            return self._document(raw, source="user_file", path=self.path)
        except MainAgentPromptConfigurationError as exc:
            log.error(
                "configuration_main_agent_prompt_fallback",
                extra={
                    "msg": "主 Agent prompt 配置无效，使用内置默认模板",
                    "data": {
                        "path": str(self.path),
                        "error_type": type(exc).__name__,
                        "error": str(exc)[:200],
                    },
                },
            )
            return self._document(self._read_default(), source="builtin_fallback", path=self.path)

    def update(self, content: str) -> MainAgentPromptDocument:
        """校验并原子更新主 Agent prompt。

        参数：
            content: 新的非空 UTF-8 prompt 正文。

        返回：
            保存后的用户文件文档。

        异常：
            MainAgentPromptConfigurationError: 正文为空或超出 Agent prompt token 预算。
            OSError: 配置文件无法安全写入。

        副作用：
            原子替换系统 ``.cosir`` 下的主 Agent prompt 文件，并写入结构化配置日志。
        """

        document = self._document(content, source="user_file", path=self.path)
        with self.store.locked(self.path):
            self.store.write_text_atomic(self.path, content, root=self.root)
        log.info(
            "configuration_main_agent_prompt_written",
            extra={"msg": "主 Agent 系统提示词已保存", "data": {"path": str(self.path)}},
        )
        return document

    def _read_default(self) -> str:
        """读取并校验随应用分发的默认模板。"""

        try:
            content = read_text_file(self.default_path)
        except (OSError, UnicodeDecodeError) as exc:
            raise RuntimeError(f"主 Agent 默认系统提示词无法读取: {self.default_path}") from exc
        self._document(content, source="builtin_default", path=self.path)
        return content

    @staticmethod
    def _document(
        content: str,
        *,
        source: str,
        path: Path,
    ) -> MainAgentPromptDocument:
        """把正文转换为带预算元数据的文档，并拒绝不可运行的内容。"""

        if not content.strip():
            raise MainAgentPromptConfigurationError("主 Agent 系统提示词不能为空")
        token_length = TokenEstimator.estimate(content)
        max_tokens = Constant.SystemPrompt.AGENT_PERSONA_MAX_TOKENS
        if token_length > max_tokens:
            raise MainAgentPromptConfigurationError(
                f"主 Agent 系统提示词超出 token 预算: {token_length} > {max_tokens}"
            )
        return MainAgentPromptDocument(
            content=content,
            path=path,
            token_length=token_length,
            max_tokens=max_tokens,
            source=source,
        )
