"""system/workspace 作用域的子 Agent JSON 配置 service。

本 service 以 ``AgentProfileRegistry`` 作为运行时事实源，以作用域对应的 `.cosir/agents/*.json` 作为
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
)
from app.task_runtime.agent_catalog_change import (
    AgentCatalogChange,
    AgentCatalogChangeAction,
)
from app.task_runtime.broadcaster.agent_catalog_update_broadcaster import (
    broadcast_agent_catalog_change,
)
from app.utils.path.system_cosir import system_agent_config_dir
from app.utils.path.workspace_cosir import workspace_agent_config_dir


class AgentConfigurationError(ValueError):
    """Agent 文档输入或状态不满足配置中心契约。"""


_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class AgentConfigurationService:
    """管理指定作用域子 Agent 的持久化文件与进程内注册表同步。

    ``workspace_root`` 为空时管理 system 作用域；传入 workspace 根路径时只管理该 workspace
    的本地文件。system 内置 Agent 只允许读取，workspace 配置不会覆盖 system 作用域。
    """

    def __init__(self, *, workspace_root: str | Path | None = None) -> None:
        """绑定 system 或 workspace Agent 配置目录、注册表与文件存储。

        配置目录由固定路径函数和已验证的 workspace 根路径计算，不接受任意客户端路径；Registry
        固定取进程级单例，确保文件写入与运行时能力使用同一事实源。

        参数:
            workspace_root: workspace 根路径；为空时使用 system 作用域。

        返回:
            无。

        异常:
            RuntimeError: 进程级注册表尚未初始化（调用顺序错误，见 ``get_agent_registry``）。

        副作用:
            解析配置目录、取得注册表引用并构造 ``ConfigurationFileStore``，不读写文件系统。
        """

        self.workspace_root = (
            None
            if workspace_root is None
            else AgentProfileRegistry.normalize_workspace(workspace_root)
        )
        self.scope = (
            AgentProfileRegistry.SYSTEM_WORKSPACE
            if self.workspace_root is None
            else self.workspace_root
        )
        self.directory = (
            system_agent_config_dir()
            if self.workspace_root is None
            else workspace_agent_config_dir(self.workspace_root)
        )
        self.registry = get_agent_registry()
        self.store = ConfigurationFileStore()

    def list_documents(self) -> list[AgentConfigurationDocument]:
        """列出当前作用域的本地子 Agent，不直接扫描磁盘文件。"""

        profiles = (
            self.registry.list_system_sub_agents()
            if self.workspace_root is None
            else self.registry.list_workspace_sub_agents(self.workspace_root)
        )
        return [self._from_profile(profile) for profile in profiles]

    def create_document(self, document: AgentConfigurationDocument) -> AgentConfigurationDocument:
        """校验并创建当前作用域的 Agent JSON，并延迟通知目录消费者。

        Agent 文件与 Registry 均成功更新后才广播新增事件；通知失败只记录旁路日志，不回滚
        已提交的配置事实。
        """

        self._validate_agent_id(document.agent_id)
        self._ensure_not_builtin(document.agent_id)
        target = self._path_for(document.agent_id)
        with self.store.locked(target):
            if target.exists():
                raise FileExistsError(document.agent_id)
            profile = self._profile_from_document(document, target)
            if not self.registry.register(self.scope, profile):
                raise AgentConfigurationError(f"Agent 已注册，不允许重复创建: {document.agent_id}")
            try:
                self.store.write_text_atomic(
                    target,
                    self._encode(document.to_json_document()),
                    root=self.directory,
                )
            except Exception:
                self.registry.unregister(self.scope, profile.agent_id)
                raise
        saved = self._from_profile(profile, path=target)
        log.info(
            "configuration_agent_written",
            extra={
                "msg": "Agent 配置已保存",
                "data": {"agent_id": document.agent_id, "scope": str(self.scope)},
            },
        )
        self._notify_catalog_change(
            action="created",
            agent_id=document.agent_id,
            current_description=profile.description,
        )
        return saved

    def update_document(
            self,
            agent_id: str,
            document: AgentConfigurationDocument,
    ) -> AgentConfigurationDocument:
        """无损更新当前作用域的 JSON 文档；仅描述变化时通知目录消费者。"""

        self._validate_agent_id(agent_id)
        if document.agent_id != agent_id:
            raise AgentConfigurationError("Agent ID 不可变，请使用新建后删除完成迁移")
        target = self._path_for(agent_id)
        profile = self._profile_from_document(document, target)
        existing = self.registry.resolve_local(self.scope, agent_id)
        if existing is None or existing.agent_type is not AgentProfileType.CHILD:
            raise KeyError(agent_id)
        with self.store.locked(target):
            self.store.write_text_atomic(
                target,
                self._encode(document.to_json_document()),
                root=self.directory,
            )
            self.registry.replace(self.scope, profile)
        saved = self._from_profile(profile, path=target)
        log.info(
            "configuration_agent_written",
            extra={
                "msg": "Agent 配置已保存",
                "data": {"agent_id": document.agent_id, "scope": str(self.scope)},
            },
        )
        if existing.description != profile.description:
            self._notify_catalog_change(
                action="updated",
                agent_id=agent_id,
                previous_description=existing.description,
                current_description=profile.description,
            )
        return saved

    def delete_document(self, agent_id: str) -> None:
        """删除当前作用域的 JSON Agent，并延迟通知目录消费者。"""

        self._validate_agent_id(agent_id)
        self._ensure_not_builtin(agent_id)
        target = self._path_for(agent_id)
        if not target.exists():
            raise KeyError(agent_id)
        existing = self.registry.resolve_local(self.scope, agent_id)
        if existing is None:
            raise KeyError(agent_id)
        with self.store.locked(target):
            self.store.delete_file(target, root=self.directory)
            self.registry.unregister(self.scope, agent_id)
        log.info(
            "configuration_agent_deleted",
            extra={
                "msg": "Agent 配置已删除",
                "data": {"agent_id": agent_id, "scope": str(self.scope)},
            },
        )
        self._notify_catalog_change(
            action="deleted",
            agent_id=agent_id,
            previous_description=existing.description,
        )

    def _notify_catalog_change(
        self,
        *,
        action: AgentCatalogChangeAction,
        agent_id: str,
        previous_description: str | None = None,
        current_description: str | None = None,
    ) -> None:
        """在配置事实提交后投递目录通知；通知旁路失败不影响配置请求成功。

        该方法只接收目录可见的 Agent ID 与描述，不把完整 profile、系统提示词或模型配置
        传给通知层。system 作用域用 ``None`` 表示全局，workspace 作用域使用规范化根路径。
        """

        change = AgentCatalogChange(
            scope=None if self.workspace_root is None else str(self.scope),
            action=action,
            agent_id=agent_id,
            previous_description=previous_description,
            current_description=current_description,
        )
        try:
            broadcast_agent_catalog_change(change)
        except Exception:
            log.exception(
                "configuration_agent_catalog_notification_failed",
                extra={
                    "msg": "Agent 配置已提交，但目录延迟通知失败",
                    "data": {
                        "action": action,
                        "agent_id": agent_id,
                        "scope": change.scope,
                    },
                },
            )

    def _from_profile(
            self,
            profile: AgentProfile,
            *,
            path: Path | None = None,
    ) -> AgentConfigurationDocument:
        """将注册表 profile 与配置文件中的模型选择字段投影为配置中心文档。

        ``AgentProfile`` 同时保存模型选择 ID 和已物化的 ``ModelSettings``；配置中心直接
        投影选择 ID，避免重新读取配置文件。
        """

        is_builtin = self.workspace_root is None and self._is_builtin(profile.agent_id)
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
        return self.directory / f"{agent_id}.json"

    def _ensure_not_builtin(self, agent_id: str) -> None:
        """拒绝用系统内置 Agent ID 创建或操作用户文件。"""

        if self.workspace_root is None and self._is_builtin(agent_id):
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
