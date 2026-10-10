"""Agent Team 配置的进程内注册表。

本模块只保存已经通过领域模型校验的 :class:`AgentTeamConfiguration`，提供 system/workspace
作用域下的解析、可见性合并，并在某个作用域**首次被读取时**把该作用域的配置文件装载进来。
它不读取 JSON 正文（由 :class:`AgentTeamConfigurationScopeSource` 负责）、不写文件、不校验
Agent profile，也不负责 TeamRun 生命周期。

为什么懒装载：workspace 可以在进程运行期间创建，只在启动期遍历一次已登记 workspace 会让
「启动之后才创建的 workspace」的 Team 配置在整个进程周期内不可见。装载收口在索引读原语内，
调用方无需也不应显式装载。
"""

from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path
from threading import Lock, RLock

from app.agent_team.configuration.agent_team_configuration import AgentTeamConfiguration
from app.agent_team.configuration_scope_source import AgentTeamConfigurationScopeSource
from app.config.logging.logger import log
from app.utils.workspace_scope import SYSTEM_SCOPE as _SYSTEM_SCOPE
from app.utils.workspace_scope import normalize_scope_path


class AgentTeamConfigurationRegistry:
    """在进程内按 system/workspace 作用域索引 Team 配置，并按需装载作用域。

    workspace 配置优先于 system 配置。装载把目录内容**合并**进该作用域（同 ``team_id`` 以目录
    内容为准，已即时注册的其他项不受影响）；配置中心保存/删除的单条写入走 :meth:`register` /
    :meth:`unregister`。装载只在该作用域首次被访问时发生，进程内不感知进程外的手工删改。

    失败策略：坏配置不影响运行。目录级读取失败与单文件无效由 source 记日志后降级，读取路径
    不抛异常；该作用域仍标记为已装载，避免每次读取重复读盘与刷日志。

    并发契约：索引由一把可重入锁保护，临界区内只做内存字典操作；装载由另一把
    :class:`~threading.Lock` 与已装载集合串行化，读盘全程不持有索引锁，锁序恒为
    「装载锁 → 索引锁」。
    """

    # 哨兵值来自 app.utils.workspace_scope 的唯一来源，避免与 Agent profile 注册表各写一份。
    SYSTEM_SCOPE = _SYSTEM_SCOPE

    def __init__(self, source: AgentTeamConfigurationScopeSource) -> None:
        """绑定配置文件来源，建立空索引。

        参数:
            source: 作用域 → 配置目录 → 配置映射的来源。装配期显式注入；不接受缺省值，以免
                出现「注册表存在但配置永远不会被装载」的静默失效状态。

        返回:
            无。

        异常:
            无。

        副作用:
            只保存引用并构造两把锁与空索引，不读取文件系统。
        """

        self._source = source
        self._lock = RLock()
        # 装载锁与索引锁分离：装载要做磁盘 IO，绝不能在持有索引锁时进行。
        self._load_lock = Lock()
        self._configs: dict[tuple[str, str], AgentTeamConfiguration] = {}
        self._loaded_scopes: set[str] = set()

    @property
    def source(self) -> AgentTeamConfigurationScopeSource:
        """返回本注册表使用的配置来源（只读）。

        供配置写入方复用同一目录映射：写目录与装载目录必须来自同一个来源，否则保存后的配置
        可能落在读取方看不到的位置，表现为「保存成功但列表为空」。
        """

        return self._source

    @classmethod
    def normalize_scope(
        cls,
        scope: str,
        workspace_root: str | Path | None = None,
    ) -> str:
        """把作用域归一化为 ``system`` 或归一化后的 workspace 路径。

        归一化口径与 Agent profile 注册表共用 ``app.utils.workspace_scope``，避免同一个
        workspace 在两类配置里得到不同键。

        参数:
            scope: ``system`` 或 workspace 作用域标识。
            workspace_root: workspace 作用域对应的根路径。

        返回:
            系统哨兵或归一化后的 workspace 作用域键。

        异常:
            ValueError: workspace 作用域缺少非空 workspace 根路径时抛出。
        """

        if scope == cls.SYSTEM_SCOPE:
            return cls.SYSTEM_SCOPE
        if workspace_root is None or not str(workspace_root).strip():
            raise ValueError("workspace_root is required for workspace Team configuration")
        return normalize_scope_path(workspace_root)

    def register(
        self,
        configuration: AgentTeamConfiguration,
        *,
        workspace_root: str | Path | None = None,
    ) -> None:
        """注册一份已经写入文件的配置。

        本方法不重复执行 Pydantic 校验，也不执行文件写入；调用方必须先完成配置持久化。
        同一作用域和 Team ID 的已有内存项会被新的配置替换。

        参数:
            configuration: 已校验的领域配置，其 ``scope`` 决定落入哪个作用域。
            workspace_root: workspace 作用域对应的根路径。

        返回:
            无。

        异常:
            ValueError: workspace 作用域缺少根路径时抛出。

        副作用:
            原地更新当前进程内索引，不触碰文件系统和数据库。
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

        参数:
            team_id: 要移除的 Team 标识。
            scope: ``system`` 或 workspace 作用域标识。
            workspace_root: workspace 作用域对应的根路径。

        返回:
            被移除的配置。

        异常:
            ValueError: workspace 作用域缺少根路径时抛出。
            KeyError: 该作用域不存在该 Team。

        副作用:
            原地更新当前进程内索引，不触碰文件系统和数据库。
        """

        normalized_scope = self.normalize_scope(scope, workspace_root)
        with self._lock:
            return self._configs.pop((normalized_scope, team_id))

    def drop_scope(self, workspace_root: str | Path) -> None:
        """卸载指定 workspace 作用域的索引与已装载标记。

        workspace 被删除时调用。懒装载「每个作用域只读盘一次」的前提是作用域身份稳定：
        workspace 删除后同一路径可能被重新创建，若保留标记就再也读不到新内容。

        参数:
            workspace_root: workspace 根路径。

        返回:
            无。

        异常:
            ValueError: workspace 根路径为空时抛出。

        副作用:
            持「装载锁 → 索引锁」清空该作用域配置并移除其已装载标记；不触碰文件系统与
            system 作用域。作用域不存在时幂等无操作。
        """

        scope = self.normalize_scope("workspace", workspace_root)
        with self._load_lock:
            with self._lock:
                self._configs = {
                    key: configuration
                    for key, configuration in self._configs.items()
                    if key[0] != scope
                }
            self._loaded_scopes.discard(scope)

    def list_scope(
        self,
        scope: str,
        *,
        workspace_root: str | Path | None = None,
    ) -> list[AgentTeamConfiguration]:
        """列出指定作用域实际拥有的配置，不合并继承的 system 项。

        参数:
            scope: ``system`` 或 workspace 作用域标识。
            workspace_root: workspace 作用域对应的根路径。

        返回:
            该作用域内的配置，按 ``team_id`` 排序。

        异常:
            ValueError: workspace 作用域缺少根路径时抛出。

        副作用:
            首次访问该作用域时读取一次配置文件（workspace 可见性还会连带装载 system 作用域
            的继承项）；此后仅读取内存索引。
        """

        normalized_scope = self.normalize_scope(scope, workspace_root)
        self._ensure_scope(normalized_scope)
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
        """按 workspace 优先、system 回退解析 Team 配置。

        参数:
            workspace_root: 当前 workspace 根路径。
            team_id: 要解析的 Team 标识。

        返回:
            workspace 或 system 中匹配的配置；未找到时返回 ``None``。

        异常:
            ValueError: workspace 根路径为空时抛出。

        副作用:
            首次访问该作用域时读取一次配置文件（workspace 可见性还会连带装载 system 作用域
            的继承项）；此后仅读取内存索引。
        """

        scope = self.normalize_scope("workspace", workspace_root)
        self._ensure_scope(scope)
        self._ensure_scope(self.SYSTEM_SCOPE)
        with self._lock:
            return self._configs.get((scope, team_id)) or self._configs.get(
                (self.SYSTEM_SCOPE, team_id)
            )

    def list_visible(self, workspace_root: str | Path) -> list[AgentTeamConfiguration]:
        """列出 workspace 可见配置，workspace 同名配置覆盖 system。

        参数:
            workspace_root: 当前 workspace 根路径。

        返回:
            合并后的可见配置，按 ``team_id`` 排序。

        异常:
            ValueError: workspace 根路径为空时抛出。

        副作用:
            首次访问该作用域时读取一次配置文件（workspace 可见性还会连带装载 system 作用域
            的继承项）；此后仅读取内存索引。
        """

        scope = self.normalize_scope("workspace", workspace_root)
        self._ensure_scope(scope)
        self._ensure_scope(self.SYSTEM_SCOPE)
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

    def team_summary(self, workspace_root: str | Path) -> str:
        """将指定 workspace 可见的 Team 配置投影为能力摘要。

        与 :meth:`AgentProfileRegistry.child_agent_summary` 同构，但 Team 摘要更完整：除标识、
        名称与用途外，还列出 Team 的节点清单与状态转移，供主 Agent 在调用 ``agent_team`` 工具前
        知悉可选 Team，并据此构造 ``node_goals``（键为各 ``node_id``）、理解节点流转。不暴露运行态。

        参数:
            workspace_root: 当前 workspace 根路径。

        返回:
            以 ``team_id | name | description`` 加节点/转移明细列出的 Team 目录；没有可见 Team 时
            返回 ``""``（整层不出现），避免向模型下发不存在的契约。

        异常:
            ValueError: workspace 根路径为空时由 :meth:`normalize_scope` 抛出。

        副作用:
            若该作用域尚未装载，则读取它的配置文件（连带装载 system 基线，见 :meth:`_ensure_scope`）；
            此后仅读取内存索引，不包含系统提示词正文。
        """

        teams = self.list_visible(workspace_root)
        if not teams:
            return "no agent teams available"
        blocks = []
        for configuration in teams:
            lines = [
                f"team_id: {configuration.team_id} | name: {configuration.name} | description: {configuration.description}",
                "  nodes:",
            ]
            lines.extend(
                f"    - node_id: {node.node_id} | name: {node.name} | agent_id: {node.agent_id} | statuses: {node.statuses}"
                for node in configuration.nodes
            )
            lines.append("  transitions:")
            lines.extend(
                f"    - {transition.from_node_id} (status={transition.status}) -> {transition.target_node_id}"
                for transition in configuration.transitions
            )
            blocks.append("\n".join(lines))
        return "Available agent teams:\n" + "\n\n".join(blocks)

    def _ensure_scope(self, scope: str) -> None:
        """确保指定作用域的 Team 配置已装载（每个作用域在本进程内只读盘一次）。

        参数:
            scope: 经 :meth:`normalize_scope` 归一化后的作用域键。

        返回:
            无。

        异常:
            无：装载失败由 ``AgentTeamConfigurationScopeSource`` 记日志并降级为空映射。

        副作用:
            首次调用时读盘并合并进该作用域索引；无论成功还是降级都标记为已装载。出锁后写
            ``team_configuration_scope_loaded`` info 日志（含作用域与装载数量），使
            「每个作用域只装载一次」可在运行期经日志验证。
        """

        with self._load_lock:
            if scope in self._loaded_scopes:
                return
            configurations = self._source.load(scope)
            self._merge_scope(scope, configurations)
            self._loaded_scopes.add(scope)
        log.info(
            "team_configuration_scope_loaded",
            extra={
                "msg": "Team 配置作用域已按需装载",
                "data": {"scope": scope, "team_count": len(configurations)},
            },
        )

    def _merge_scope(
        self,
        scope: str,
        configurations: Mapping[str, AgentTeamConfiguration],
    ) -> None:
        """把读到的配置合并进该作用域索引。

        合并（而非整批替换）与 Agent profile 注册表同语义：同一作用域里可能已有配置中心刚写入
        并即时注册的项，整批替换会在「先保存、后首次读取」的顺序下把它清掉。装载只在作用域首次
        被访问时发生，因此合并不会造成重复装载。

        参数:
            scope: 已归一化的作用域键。
            configurations: 以 ``team_id`` 为键的已校验配置对象。

        返回:
            无。

        异常:
            无。

        副作用:
            持索引锁原地写入该作用域；不触碰文件系统和数据库。调用方须已持有装载锁。
        """

        with self._lock:
            for team_id, configuration in configurations.items():
                self._configs[(scope, team_id)] = configuration


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
