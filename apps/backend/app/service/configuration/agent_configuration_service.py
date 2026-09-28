"""系统级子 Agent JSON 配置 service。

本 service 以 ``AgentProfileRegistry`` 作为运行时事实源，以系统 `.cosir/agents/*.json` 作为
持久化载体。所有配置写入都复用 ``parse_agent_profile_document`` 完成校验，并在文件操作成功
后同步注册表；本 service 不实现第二套 profile 加载或状态缓存。对外收发的
``AgentConfigurationDocument`` 是 API 层传输结构（``app.api.schemas``），本模块只按它读写文件与
注册表，不定义文档字段语义。
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from app.api.schemas.AgentConfigurationDocument import AgentConfigurationDocument
from app.config.configuration import get_agent_registry
from app.config.logging.logger import log
from app.core.agents.agent_profile import (
    AgentProfile,
    AgentProfileType,
    parse_agent_profile_document,
)
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.core.agents.define_agents import general_child_agent, main_agent
from app.service.configuration.file_store import (
    ConfigurationFileStore,
    ConfigurationPathError,
)
from app.utils.cosir_paths import system_agent_config_dir


class AgentConfigurationError(ValueError):
    """系统 Agent 文档输入或状态不满足配置中心契约。"""


_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class AgentConfigurationService:
    """管理系统子 Agent 的持久化文件与进程内注册表同步。"""

    def __init__(self) -> None:
        """绑定系统 Agent 配置目录、进程级注册表与文件存储。

        目录在构造时解析一次（``system_agent_config_dir()``），不提供注入点：系统 Agent
        只能落在系统 ``.cosir/agents`` 下，避免调用方指定任意目录绕过配置中心的路径边界。
        注册表同样固定取进程级单例（``get_agent_registry()``），不提供注入点：系统 Agent 事实
        只应有一份，允许注入会让配置写入落到另一个注册表而与运行时不一致。测试通过 monkeypatch
        模块级 ``system_agent_config_dir`` 与 ``get_agent_registry`` 隔离这两项依赖。

        参数:
            无。

        返回:
            无。

        异常:
            RuntimeError: 进程级注册表尚未初始化（调用顺序错误，见 ``get_agent_registry``）。

        副作用:
            解析配置目录、取得注册表引用并构造 ``ConfigurationFileStore``，不读写文件系统。
        """

        self.directory = system_agent_config_dir()
        self.registry = get_agent_registry()
        self.store = ConfigurationFileStore()

    def list_documents(self) -> list[AgentConfigurationDocument]:
        """列出注册表中的系统子 Agent，不直接扫描磁盘文件。"""

        return [self._from_profile(profile) for profile in self.registry.list_system_sub_agents()]

    def create_document(self, document: AgentConfigurationDocument) -> AgentConfigurationDocument:
        """校验并创建系统 Agent JSON；Agent ID 与文件名由后端生成。"""

        self._validate_agent_id(document.agent_id)
        self._ensure_not_builtin(document.agent_id)
        target = self._path_for(document.agent_id)
        with self.store.locked(target):
            if target.exists() or target.is_symlink():
                raise FileExistsError(document.agent_id)
            profile = self._profile_from_document(document, target)
            if not self.registry.register(AgentProfileRegistry.SYSTEM_WORKSPACE, profile):
                raise AgentConfigurationError(f"Agent 已注册，不允许重复创建: {document.agent_id}")
            try:
                self.store.write_text_atomic(
                    target,
                    self._encode(document.to_json_document()),
                    root=self.directory,
                )
            except Exception:
                self.registry.unregister(AgentProfileRegistry.SYSTEM_WORKSPACE, profile.agent_id)
                raise
        saved = self._from_profile(profile, path=target)
        log.info(
            "configuration_agent_written",
            extra={"msg": "系统 Agent 配置已保存", "data": {"agent_id": document.agent_id}},
        )
        return saved

    def update_document(
            self,
            agent_id: str,
            document: AgentConfigurationDocument,
    ) -> AgentConfigurationDocument:
        """无损更新一个系统 JSON 文档；不支持原地修改 Agent ID。"""

        self._validate_agent_id(agent_id)
        if document.agent_id != agent_id:
            raise AgentConfigurationError("Agent ID 不可变，请使用新建后删除完成迁移")
        target = self._path_for(agent_id)
        profile = self._profile_from_document(document, target)
        existing = self.registry.resolve(AgentProfileRegistry.SYSTEM_WORKSPACE, agent_id)
        if existing is None or existing.agent_type is not AgentProfileType.CHILD:
            raise KeyError(agent_id)
        with self.store.locked(target):
            self.store.write_text_atomic(
                target,
                self._encode(document.to_json_document()),
                root=self.directory,
            )
            self.registry.replace(AgentProfileRegistry.SYSTEM_WORKSPACE, profile)
        saved = self._from_profile(profile, path=target)
        log.info(
            "configuration_agent_written",
            extra={"msg": "系统 Agent 配置已保存", "data": {"agent_id": document.agent_id}},
        )
        return saved

    def delete_document(self, agent_id: str) -> None:
        """删除系统 JSON Agent；内置 Agent 和符号链接均拒绝删除。"""

        self._validate_agent_id(agent_id)
        self._ensure_not_builtin(agent_id)
        target = self._path_for(agent_id)
        if not target.exists():
            raise KeyError(agent_id)
        if self.registry.resolve(AgentProfileRegistry.SYSTEM_WORKSPACE, agent_id) is None:
            raise KeyError(agent_id)
        with self.store.locked(target):
            if target.is_symlink() or not target.is_file():
                raise ConfigurationPathError(
                    f"configuration target must be a regular file: {target}"
                )
            self.store.delete_file(target, root=self.directory)
            self.registry.unregister(AgentProfileRegistry.SYSTEM_WORKSPACE, agent_id)
        log.info(
            "configuration_agent_deleted",
            extra={"msg": "系统 Agent 配置已删除", "data": {"agent_id": agent_id}},
        )

    def _from_profile(
            self,
            profile: AgentProfile,
            *,
            path: Path | None = None,
    ) -> AgentConfigurationDocument:
        """将注册表 profile 投影为配置中心文档，不重新读取磁盘。"""

        is_builtin = self._is_builtin(profile.agent_id)
        if path is None and not is_builtin:
            candidate = self._path_for(profile.agent_id)
            path = candidate if candidate.exists() else None
        return AgentConfigurationDocument(
            agent_id=profile.agent_id,
            role=profile.role,
            description=profile.description or "",
            system_prompt=profile.system_prompt,
            allowed_tools=list(profile.allowed_tools),
            max_steps=profile.max_steps,
            model_config_id=profile.model_config_id,
            model_settings=profile.model_settings.to_dict(),
            source="builtin" if is_builtin else "user_file",
            path=path,
            editable=not is_builtin,
            deletable=not is_builtin,
            validation_status="valid",
            validation_error=None,
            file_name=path.name if path else None,
        )

    def _profile_from_document(
            self,
            document: AgentConfigurationDocument,
            source: Path,
    ) -> AgentProfile:
        """将编辑器文档按统一 JSON 契约转换为可注册的 CHILD profile。"""

        try:
            return parse_agent_profile_document(
                document.to_json_document(),
                source,
                strict_model_config=True,
            )
        except Exception as exc:
            raise AgentConfigurationError(str(exc)) from exc

    def _path_for(self, agent_id: str) -> Path:
        return self.store.assert_safe_child(self.directory, self.directory / f"{agent_id}.json")

    @staticmethod
    def _ensure_not_builtin(agent_id: str) -> None:
        """拒绝用系统内置 Agent ID 创建或操作用户文件。"""

        if AgentConfigurationService._is_builtin(agent_id):
            raise AgentConfigurationError(f"Agent ID 属于内置 Agent，不允许写入: {agent_id}")

    @staticmethod
    def _is_builtin(agent_id: str) -> bool:
        """判断 Agent ID 是否属于代码内置的主 Agent 或通用子 Agent。"""

        return agent_id in {main_agent().agent_id, general_child_agent().agent_id}

    @staticmethod
    def _validate_agent_id(agent_id: str) -> None:
        if not isinstance(agent_id, str) or not _AGENT_ID_RE.fullmatch(agent_id):
            raise AgentConfigurationError(
                "agent_id 只能包含字母、数字、下划线和连字符，长度为 1-64"
            )

    @staticmethod
    def _encode(document: dict[str, Any]) -> str:
        return json.dumps(document, ensure_ascii=False, indent=2, sort_keys=False) + "\n"
