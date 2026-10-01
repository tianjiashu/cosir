"""主 Agent 系统提示词的文件配置 service。

负责主 Agent 系统提示词用户文件的创建、读取、预算校验和原子写入，不负责构造 Agent profile、
系统提示词分层或 Task context 刷新。运行时 profile 的更新由配置装配层在保存成功后完成。

提示词事实源唯一：``<system_cosir_dir>/main_agent_system_prompt.md``。该文件不存在时读取会创建
空白文件，不安装任何随应用分发的内置模板；用户尚未配置时主 Agent 没有系统预设，
``SystemPromptBuilder`` 不生成 ``<agent_layer>``，主 Agent 的基础身份与运行期事实由动态变量层
（``<runtime_context>``）提供。
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from app.config.constant import Constant
from app.config.logging.logger import log
from app.service.configuration.file_store import ConfigurationFileError, ConfigurationFileStore
from app.utils.path.system_cosir import system_cosir_dir, system_main_agent_prompt_file
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
    """读取和更新主 Agent 系统提示词用户文件。

    文件不存在时读取会创建空白文件并返回空正文；读取失败时记录日志并降级为空正文，不覆盖
    用户文件、不阻断启动。通过配置 API 写入时，空白文本和超出预算的正文会被拒绝。该 service
    不修改 Registry，也不改写已经运行的 Run。
    """

    def __init__(
        self,
        *,
        path: Path | None = None,
        root: Path | None = None,
    ) -> None:
        """绑定用户配置文件路径。

        参数：
            path: 用户配置文件路径；缺省为系统 ``.cosir`` 下的固定文件。
            root: 配置文件的受信任根目录；缺省为系统 ``.cosir``，测试或隔离装配可显式传入。

        异常：
            无。路径只在读写时执行安全校验。

        副作用：
            无；构造阶段不访问文件系统。
        """

        self.path = path or system_main_agent_prompt_file()
        self.root = root or system_cosir_dir()
        self.store = ConfigurationFileStore()

    def read(self) -> MainAgentPromptDocument:
        """读取当前有效 prompt；文件不存在时创建空白文件。

        返回：
            当前实际供主 Agent 使用的提示词及其来源元数据；用户尚未配置时 ``content`` 为空串，
            ``token_length`` 为 0。读取侧不做空白与 token 预算校验：空白是合法的「未配置」状态，
            超预算正文由 ``SystemPromptBuilder`` 注入 ``<agent_layer>`` 时按同一预算截断。

        异常：
            ConfigurationPathError: 用户配置路径不在受信任根目录内，或路径命中符号链接边界。
            OSError: 系统 ``.cosir`` 目录无法创建。

        副作用：
            用户配置缺失时原子创建空白文件；创建失败、读取失败或编码无效时记录日志并降级为
            空正文，不修改已有文件内容。
        """

        self.store.assert_safe_child(self.root, self.path)
        self.root.mkdir(parents=True, exist_ok=True)
        if not self.path.exists():
            try:
                self.store.write_text_atomic(self.path, "", root=self.root)
            except ConfigurationFileError as exc:
                log.error(
                    "configuration_main_agent_prompt_create_failed",
                    extra={
                        "msg": "主 Agent prompt 空白文件创建失败，本次使用空正文",
                        "data": {"path": str(self.path), "error_type": type(exc).__name__},
                    },
                )
            return self._document("", source="user_file", path=self.path)

        try:
            raw = self.store.read_text(self.path, root=self.root)
        except ConfigurationFileError as exc:
            log.error(
                "configuration_main_agent_prompt_read_failed",
                extra={
                    "msg": "主 Agent prompt 配置读取失败，本次使用空正文",
                    "data": {"path": str(self.path), "error_type": type(exc).__name__},
                },
            )
            return self._document("", source="user_file", path=self.path)

        return self._document(raw, source="user_file", path=self.path)

    def update(self, content: str) -> MainAgentPromptDocument:
        """校验并原子更新主 Agent prompt。

        参数：
            content: 新的非空 UTF-8 prompt 正文；空白文本不被接受。

        返回：
            保存后的用户文件文档。

        异常：
            MainAgentPromptConfigurationError: 正文为空白或超出 Agent prompt token 预算。
            OSError: 配置文件无法安全写入。

        副作用：
            原子替换系统 ``.cosir`` 下的主 Agent prompt 文件，并写入结构化配置日志。
        """

        self._assert_writable(content)
        document = self._document(content, source="user_file", path=self.path)
        with self.store.locked(self.path):
            self.store.write_text_atomic(self.path, content, root=self.root)
        log.info(
            "configuration_main_agent_prompt_written",
            extra={"msg": "主 Agent 系统提示词已保存", "data": {"path": str(self.path)}},
        )
        return document

    @staticmethod
    def _assert_writable(content: str) -> None:
        """拒绝空白正文和超出 Agent prompt token 预算的正文（仅写入路径）。

        参数：
            content: 准备落盘的主 Agent prompt 正文。

        异常：
            MainAgentPromptConfigurationError: 正文为空白或估算 token 超出
                ``Constant.SystemPrompt.AGENT_PERSONA_MAX_TOKENS``。
        """

        if not content.strip():
            raise MainAgentPromptConfigurationError("主 Agent 系统提示词不能为空")
        token_length = TokenEstimator.estimate(content)
        max_tokens = Constant.SystemPrompt.AGENT_PERSONA_MAX_TOKENS
        if token_length > max_tokens:
            raise MainAgentPromptConfigurationError(
                f"主 Agent 系统提示词超出 token 预算: {token_length} > {max_tokens}"
            )

    @staticmethod
    def _document(
        content: str,
        *,
        source: str,
        path: Path,
    ) -> MainAgentPromptDocument:
        """把正文转换为带预算元数据的文档（不执行空白或预算校验）。

        校验职责分离：读取侧接受空白与超预算正文（空白表示未配置，超预算由系统提示词构建层
        注入时截断），写入侧由 :meth:`_assert_writable` 先行拒绝不可运行的内容。
        """

        return MainAgentPromptDocument(
            content=content,
            path=path,
            token_length=TokenEstimator.estimate(content),
            max_tokens=Constant.SystemPrompt.AGENT_PERSONA_MAX_TOKENS,
            source=source,
        )
