"""system/workspace 作用域的子 Agent JSON 配置 service。

本 service 以 ``AgentProfileRegistry`` 作为运行时事实源，以作用域对应的 `.cosir/agents/*.json` 作为
持久化载体。所有配置写入都复用 ``parse_agent_profile_document`` 完成校验，并在文件操作成功
后同步注册表；本 service 不实现第二套 profile 加载或状态缓存。写入收 ``AgentConfiguration``、
读取给 ``AgentConfigurationEntry``，界面需要的展示字段由 API 层按条目投影。
"""

from __future__ import annotations

import re
from pathlib import Path

from app.config.configuration import get_agent_registry
from app.config.logging.logger import log
from app.core.agents.agent_profile import (
    AgentProfile,
    AgentProfileType,
    parse_agent_profile_document,
)
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.service.configuration import file_store
from app.service.configuration.agents.builtin_agents import BUILTIN_CHILD_AGENT_IDS
from app.service.configuration.agents.configuration import (
    AgentConfiguration,
    AgentConfigurationEntry,
)


class AgentConfigurationError(ValueError):
    """Agent 文档输入或状态不满足配置中心契约。"""


_AGENT_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,63}$")


class AgentConfigurationService:
    """管理指定作用域子 Agent 的持久化文件与进程内注册表同步。

    ``workspace_root`` 为空时管理 system 作用域；传入 workspace 根路径时只管理该 workspace
    的本地文件。系统内置子 Agent 可以修改、不能删除，workspace 配置不会覆盖 system 作用域。
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
            取得注册表引用、由其配置来源解析出该作用域的配置目录，不读写文件系统。
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
        self.registry = get_agent_registry()
        # 目录直接取自注册表的配置来源：写入目录与装载目录必须是同一映射，否则保存后的配置
        # 可能落在读取方看不到的位置（「保存成功但列表为空」）。
        self.directory = self.registry.source.directory(self.scope)

    def list_entries(self) -> list[AgentConfigurationEntry]:
        """列出当前作用域的本地子 Agent，不直接扫描磁盘文件。"""

        profiles = (
            self.registry.list_system_sub_agents()
            if self.workspace_root is None
            else self.registry.list_workspace_sub_agents(self.workspace_root)
        )
        return [self._entry(profile) for profile in profiles]

    def create(self, document: AgentConfiguration) -> AgentConfigurationEntry:
        """校验并创建当前作用域的 Agent JSON。

        落盘前先确保该作用域已装载，避免「先注册、后首次读取」时新文件被读回并与内存项撞成重复，
        产生与实际不符的告警。
        """

        self._validate_agent_id(document.agent_id)
        self.registry.ensure_scope_loaded(self.scope)
        target = self._path_for(document.agent_id)
        with file_store.locked(target):
            if target.exists():
                raise FileExistsError(document.agent_id)
            profile = self._profile_from_document(document, target)
            if not self.registry.register(self.scope, profile):
                raise AgentConfigurationError(f"Agent 已注册，不允许重复创建: {document.agent_id}")
            try:
                file_store.write_json(target, document.to_json_document(), root=self.directory)
            except Exception:
                self.registry.unregister(self.scope, profile.agent_id)
                raise
        return self._saved(profile, target)

    def update(self, agent_id: str, document: AgentConfiguration) -> AgentConfigurationEntry:
        """无损更新当前作用域的 JSON 文档。"""

        self._validate_agent_id(agent_id)
        if document.agent_id != agent_id:
            raise AgentConfigurationError("Agent ID 不可变，请使用新建后删除完成迁移")
        target = self._path_for(agent_id)
        profile = self._profile_from_document(document, target)
        existing = self.registry.resolve_local(self.scope, agent_id)
        if existing is None or existing.agent_type is not AgentProfileType.CHILD:
            raise KeyError(agent_id)
        with file_store.locked(target):
            file_store.write_json(target, document.to_json_document(), root=self.directory)
            self.registry.replace(self.scope, profile)
        return self._saved(profile, target)

    def delete(self, agent_id: str) -> None:
        """删除当前作用域的 JSON Agent。"""

        self._validate_agent_id(agent_id)
        if self._is_builtin(agent_id):
            raise AgentConfigurationError(f"内置子 Agent 不能删除，可以修改: {agent_id}")
        target = self._path_for(agent_id)
        if not target.exists():
            raise KeyError(agent_id)
        existing = self.registry.resolve_local(self.scope, agent_id)
        if existing is None:
            raise KeyError(agent_id)
        with file_store.locked(target):
            file_store.delete_file(target, root=self.directory)
            self.registry.unregister(self.scope, agent_id)
        log.info(
            "configuration_agent_deleted",
            extra={
                "msg": "Agent 配置已删除",
                "data": {"agent_id": agent_id, "scope": str(self.scope)},
            },
        )

    def _saved(self, profile: AgentProfile, target: Path) -> AgentConfigurationEntry:
        """写入成功后的收尾：记日志并返回带文件位置的条目（新建与更新共用）。"""

        log.info(
            "configuration_agent_written",
            extra={
                "msg": "Agent 配置已保存",
                "data": {"agent_id": profile.agent_id, "scope": str(self.scope)},
            },
        )
        return self._entry(profile, path=target)

    def _entry(
        self, profile: AgentProfile, *, path: Path | None = None
    ) -> AgentConfigurationEntry:
        """注册表 profile 加上它的文件位置。"""

        if path is None:
            candidate = self._path_for(profile.agent_id)
            path = candidate if candidate.exists() else None
        return AgentConfigurationEntry(
            configuration=AgentConfiguration.from_profile(profile),
            path=path,
            builtin=self._is_builtin(profile.agent_id),
        )

    def _profile_from_document(self, document: AgentConfiguration, source: Path) -> AgentProfile:
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

    def _is_builtin(self, agent_id: str) -> bool:
        """内置子 Agent 只在系统作用域：可以修改，不能删除（清单见 ``builtin_agents``）。"""

        return self.workspace_root is None and agent_id in BUILTIN_CHILD_AGENT_IDS

    @staticmethod
    def _validate_agent_id(agent_id: str) -> None:
        if not _AGENT_ID_RE.fullmatch(agent_id):
            raise AgentConfigurationError(
                "agent_id 只能包含字母、数字、下划线和连字符，长度为 1-64"
            )
