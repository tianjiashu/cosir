"""Agent Team 配置的应用服务。

本模块负责 system/workspace 配置的文件加载、保存和作用域校验，并把已校验配置同步到
``app.agent_team.registry``。静态字段与图结构由 ``app.agent_team.configuration`` 领域模型
负责；本模块不负责 TeamRun 生命周期、节点执行或主 Agent workflow。
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from app.agent_team.configuration.agent_team_configuration import (
    AgentTeamConfiguration,
    TeamScope,
    configuration_document,
)
from app.agent_team.registry import AgentTeamConfigurationRegistry
from app.config.logging.logger import log
from app.core.agents.agent_profile import AgentProfileType
from app.service.configuration.file_store import ConfigurationFileStore
from app.utils.path import system_cosir
from app.utils.path.workspace_cosir import workspace_agent_team_config_dir


class AgentTeamConfigurationService:
    """管理 system/workspace Team 配置并同步 Agent Team 注册表。

    本服务把配置领域对象与 JSON 文件、作用域和 Agent profile 引用校验连接起来。它不负责
    TeamRun 数据库状态迁移，也不启动节点执行。配置文件的实际安全读写委托给
    :class:`ConfigurationFileStore`，从而把路径安全、锁和原子替换等底层细节留在通用文件设施中。
    """

    def __init__(self, registry: AgentTeamConfigurationRegistry | None = None) -> None:
        """创建配置文件仓储，并绑定一个 Agent Team 注册表。

        返回:
            无。

        副作用:
            只创建注册表引用和文件仓储对象；不会读取目录或修改文件。
        """

        self._registry = registry or AgentTeamConfigurationRegistry()

    def load_directory(self, scope: str, directory: str | Path) -> None:
        """加载一个作用域目录，并原子替换注册表中的该作用域索引。

        参数:
            scope: ``system`` 或 workspace 根路径。
            directory: 该作用域对应的配置目录。

        异常:
            OSError: 目录创建或基础文件系统操作失败时抛出。

        副作用:
            读取目录中的 JSON 文件并更新进程内索引；无效文件由仓储记录日志并跳过。
        """

        loaded = self._load_directory_documents(directory)
        self._registry.replace_scope(scope, loaded)

    def save_confirmed(
        self,
        document: Mapping[str, Any],
        *,
        scope: TeamScope,
        workspace_root: str | Path | None = None,
    ) -> AgentTeamConfiguration:
        """校验并保存用户确认后的 Team 配置文档。

        参数:
            document: 前端提交的配置字典，不包含可信的作用域字段。
            scope: 用户在确认界面选择的保存作用域。
            workspace_root: workspace 作用域对应的根路径；system 作用域不需要该参数。

        返回:
            已完成静态校验、Agent profile 引用校验并写入文件的配置对象。

        异常:
            ValueError: 配置字段、图结构、作用域或节点 Agent 引用无效。
            FileExistsError: 同一作用域中已存在相同 Team ID。
        """

        payload = dict(document)
        payload["scope"] = scope
        configuration = AgentTeamConfiguration.model_validate(payload)
        self._validate_node_profiles(configuration, workspace_root)
        return self.save(configuration, workspace_root=workspace_root)

    def list_documents(
        self,
        *,
        scope: TeamScope,
        workspace_root: str | Path | None = None,
    ) -> list[AgentTeamConfiguration]:
        """列出该作用域实际拥有的配置，不把继承的 system 项混入编辑列表。"""

        return self._registry.list_scope(
            scope,
            workspace_root=workspace_root,
        )

    def update_confirmed(
        self,
        team_id: str,
        document: Mapping[str, Any],
        *,
        scope: TeamScope,
        workspace_root: str | Path | None = None,
    ) -> AgentTeamConfiguration:
        """校验并替换指定作用域已有 Team 配置，Team ID 保持不变。"""

        payload = dict(document)
        payload["scope"] = scope
        configuration = AgentTeamConfiguration.model_validate(payload)
        if configuration.team_id != team_id:
            raise ValueError("Team ID 不可变")
        self._validate_node_profiles(configuration, workspace_root)
        return self.update(configuration, workspace_root=workspace_root)

    def update(
        self,
        configuration: AgentTeamConfiguration,
        *,
        workspace_root: str | Path | None = None,
    ) -> AgentTeamConfiguration:
        """原子替换指定作用域现有 Team 文件，再更新进程内注册表。"""

        root = self._directory_for(configuration.scope, workspace_root)
        path = root / f"{configuration.team_id}.json"
        with ConfigurationFileStore.locked(path):
            if not path.exists():
                raise KeyError(configuration.team_id)
            ConfigurationFileStore.write_text_atomic(
                path,
                self._encode(configuration),
                root=root,
            )
            self._registry.register(configuration, workspace_root=workspace_root)
        return configuration

    def delete(
        self,
        team_id: str,
        *,
        scope: TeamScope,
        workspace_root: str | Path | None = None,
    ) -> None:
        """删除指定作用域的 Team 配置；同名继承项不受影响。"""

        root = self._directory_for(scope, workspace_root)
        path = root / f"{team_id}.json"
        with ConfigurationFileStore.locked(path):
            if not path.exists():
                raise KeyError(team_id)
            owned_ids = {
                configuration.team_id
                for configuration in self._registry.list_scope(
                    scope,
                    workspace_root=workspace_root,
                )
            }
            if team_id not in owned_ids:
                raise KeyError(team_id)
            ConfigurationFileStore.delete_file(path, root=root)
            self._registry.unregister(
                team_id,
                scope=scope,
                workspace_root=workspace_root,
            )

    def save(
        self,
        configuration: AgentTeamConfiguration,
        *,
        workspace_root: str | Path | None = None,
    ) -> AgentTeamConfiguration:
        """把已通过领域校验的配置写入对应作用域并更新缓存。

        本方法不重复解析节点 profile；直接调用方必须保证运行时引用已经校验。面向 API
        的保存入口应使用 :meth:`save_confirmed`。
        """

        root = self._directory_for(configuration.scope, workspace_root)
        self._create_configuration_file(configuration, directory=root)
        self._registry.register(configuration, workspace_root=workspace_root)
        return configuration

    @staticmethod
    def _directory_for(
        scope: str,
        workspace_root: str | Path | None,
    ) -> Path:
        """映射已验证的配置作用域到其固定文件目录。"""

        if scope == AgentTeamConfigurationRegistry.SYSTEM_SCOPE:
            return system_cosir.system_agent_team_config_dir()
        if workspace_root is None:
            raise ValueError("workspace_root is required for workspace Team configuration")
        return workspace_agent_team_config_dir(workspace_root)

    @staticmethod
    def _encode(configuration: AgentTeamConfiguration) -> str:
        """序列化已校验的领域配置，供原子文件写入复用。"""

        return (
            json.dumps(configuration_document(configuration), ensure_ascii=False, indent=2)
            + "\n"
        )

    @staticmethod
    def _load_directory_documents(
        directory: str | Path,
    ) -> dict[str, AgentTeamConfiguration]:
        """读取并解析一个配置目录中的 Team JSON 文件。

        单个配置文件无效时记录结构化日志并跳过，不影响同目录其他配置的加载；目录级
        文件系统异常向调用方抛出，由生命周期决定是否终止启动。
        """

        target = Path(directory)
        target.mkdir(parents=True, exist_ok=True)
        loaded: dict[str, AgentTeamConfiguration] = {}
        for path in sorted(target.glob("*.json")):
            try:
                document = json.loads(ConfigurationFileStore.read_text(path))
                configuration = AgentTeamConfiguration.model_validate(document)
            except Exception as exc:
                log.error(
                    "agent_team_config_invalid",
                    extra={
                        "msg": "Agent Team 配置无效，已跳过该文件",
                        "data": {"path": str(path), "error_type": type(exc).__name__},
                    },
                )
                continue
            loaded[configuration.team_id] = configuration
        return loaded

    @staticmethod
    def _create_configuration_file(
        configuration: AgentTeamConfiguration,
        *,
        directory: str | Path,
    ) -> Path:
        """原子创建一份已校验的 Team 配置文件，不覆盖已有文件。"""

        root = Path(directory)
        path = root / f"{configuration.team_id}.json"
        with ConfigurationFileStore.locked(path):
            if path.exists():
                raise FileExistsError(f"Team 配置已存在: {configuration.team_id}")
            ConfigurationFileStore._write_text_atomic(
                path,
                AgentTeamConfigurationService._encode(configuration),
                root=root,
            )
        return path

    @staticmethod
    def _validate_node_profiles(
        configuration: AgentTeamConfiguration,
        workspace_root: str | Path | None,
    ) -> None:
        """校验配置引用的 profile 存在且属于 CHILD 类型。"""

        # Agent registry 由生命周期先于配置 service 完成装配，但其模块又会间接装配
        # ToolSystem。延迟获取可以避免配置 service、ToolSystem 和 Team 工具形成导入环。
        from app.config.configuration import get_agent_registry

        profile_scope = "system" if configuration.scope == "system" else str(workspace_root or "")
        for node in configuration.nodes:
            profile = get_agent_registry().resolve(profile_scope, node.agent_id)
            if profile is None or profile.agent_type is not AgentProfileType.CHILD:
                raise ValueError(f"Team 节点引用的 child Agent 不可用: {node.agent_id}")


_SERVICE: AgentTeamConfigurationService | None = None


def set_agent_team_configuration_service(service: AgentTeamConfigurationService) -> None:
    """设置进程级 Team 配置服务。"""

    global _SERVICE
    _SERVICE = service


def get_agent_team_configuration_service() -> AgentTeamConfigurationService:
    """返回已初始化的进程级 Team 配置服务。

    异常:
        RuntimeError: 后端生命周期尚未注入配置服务时抛出。
    """

    if _SERVICE is None:
        raise RuntimeError("agent team configuration service has not been initialized")
    return _SERVICE
