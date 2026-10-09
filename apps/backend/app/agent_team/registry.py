"""Agent Team 配置的进程内注册表。

本模块只保存已经通过领域模型校验的 :class:`AgentTeamConfiguration`，提供 system/workspace
作用域下的解析和可见性合并。它不读取 JSON、不写文件、不校验 Agent profile，也不负责
TeamRun 生命周期；文件事实由配置 service 管理，运行事实由 Team coordinator 管理。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from threading import RLock

from app.agent_team.configuration.agent_team_configuration import AgentTeamConfiguration


class AgentTeamConfigurationRegistry:
    """在进程内索引 system/workspace Team 配置。

    workspace 配置优先于 system 配置。同一作用域的批量加载采用整批替换，调用方不会看到
    半批次状态；单个注册用于保存新配置后的内存事实更新。所有读写都由同一把可重入锁保护。
    """

    SYSTEM_SCOPE = "system"

    def __init__(self) -> None:
        """创建空的 Team 配置索引，不读取外部文件。"""

        self._lock = RLock()
        self._configs: dict[tuple[str, str], AgentTeamConfiguration] = {}

    @classmethod
    def normalize_scope(
        cls,
        scope: str,
        workspace_root: str | Path | None = None,
    ) -> str:
        """把作用域归一化为 ``system`` 或绝对 workspace 路径。

        异常:
            ValueError: workspace 作用域缺少非空 workspace 根路径时抛出。
        """

        if scope == cls.SYSTEM_SCOPE:
            return scope
        if workspace_root is None or not str(workspace_root).strip():
            raise ValueError("workspace_root is required for workspace Team configuration")
        return str(Path(workspace_root).expanduser().resolve())

    def replace_scope(
        self,
        scope: str,
        configurations: Mapping[str, AgentTeamConfiguration],
    ) -> None:
        """原子替换一个作用域的全部配置。

        参数:
            scope: ``system`` 或 workspace 根路径。
            configurations: 以 ``team_id`` 为键的已校验配置对象。

        异常:
            ValueError: workspace 作用域路径为空时抛出。

        副作用:
            只更新当前进程内索引，不触碰文件系统和数据库。
        """

        normalized_scope = self.normalize_scope(
            scope,
            None if scope == self.SYSTEM_SCOPE else scope,
        )
        replacement = dict(configurations)
        with self._lock:
            self._configs = {
                key: configuration
                for key, configuration in self._configs.items()
                if key[0] != normalized_scope
            }
            self._configs.update(
                {
                    (normalized_scope, team_id): configuration
                    for team_id, configuration in replacement.items()
                }
            )

    def register(
        self,
        configuration: AgentTeamConfiguration,
        *,
        workspace_root: str | Path | None = None,
    ) -> None:
        """注册一份已经写入文件的配置。

        本方法不重复执行 Pydantic 校验，也不执行文件写入；调用方必须先完成配置持久化。
        同一作用域和 Team ID 的已有内存项会被新的配置替换。
        """

        scope = self.normalize_scope(configuration.scope, workspace_root)
        with self._lock:
            self._configs[(scope, configuration.team_id)] = configuration

    def unregister(
        self,
        team_id: str,
        *,
        scope: str,
        workspace_root: str | Path | None = None,
    ) -> AgentTeamConfiguration:
        """从指定作用域移除 Team，并返回被移除配置。

        删除按配置所有权作用域执行，避免删除 workspace 覆盖时误删同名 system 配置。
        """

        normalized_scope = self.normalize_scope(scope, workspace_root)
        with self._lock:
            return self._configs.pop((normalized_scope, team_id))

    def list_scope(
        self,
        scope: str,
        *,
        workspace_root: str | Path | None = None,
    ) -> list[AgentTeamConfiguration]:
        """列出指定作用域实际拥有的配置，不合并继承的 system 项。"""

        normalized_scope = self.normalize_scope(scope, workspace_root)
        with self._lock:
            return sorted(
                (
                    configuration
                    for (config_scope, _), configuration in self._configs.items()
                    if config_scope == normalized_scope
                ),
                key=lambda item: item.team_id,
            )

    def resolve(
        self,
        workspace_root: str | Path,
        team_id: str,
    ) -> AgentTeamConfiguration | None:
        """按 workspace 优先、system 回退解析 Team 配置。"""

        scope = self.normalize_scope("workspace", workspace_root)
        with self._lock:
            return self._configs.get((scope, team_id)) or self._configs.get(
                (self.SYSTEM_SCOPE, team_id)
            )

    def list_visible(self, workspace_root: str | Path) -> list[AgentTeamConfiguration]:
        """列出 workspace 可见配置，workspace 同名配置覆盖 system。"""

        scope = self.normalize_scope("workspace", workspace_root)
        with self._lock:
            merged = {
                team_id: configuration
                for (config_scope, team_id), configuration in self._configs.items()
                if config_scope == self.SYSTEM_SCOPE
            }
            merged.update(
                {
                    team_id: configuration
                    for (config_scope, team_id), configuration in self._configs.items()
                    if config_scope == scope
                }
            )
            return sorted(merged.values(), key=lambda item: item.team_id)


_REGISTRY: AgentTeamConfigurationRegistry | None = None


def set_agent_team_registry(registry: AgentTeamConfigurationRegistry) -> None:
    """设置进程级 Agent Team 配置注册表。"""

    global _REGISTRY
    _REGISTRY = registry


def get_agent_team_registry() -> AgentTeamConfigurationRegistry:
    """返回已初始化的进程级 Agent Team 配置注册表。

    异常:
        RuntimeError: 后端生命周期尚未注入注册表时抛出。
    """

    if _REGISTRY is None:
        raise RuntimeError("agent team registry has not been initialized")
    return _REGISTRY
