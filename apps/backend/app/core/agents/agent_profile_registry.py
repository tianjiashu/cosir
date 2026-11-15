"""进程内按配置作用域索引和解析 Agent profile；配置文件是唯一事实源，注册表只是按签名校验的副本。

单一职责：按「作用域 + ``agent_id``」解析与列举 Agent profile。每次读取先比对该作用域配置文件的签名
（``AgentProfileScopeSource.signature``），签名变了就整份重读这个作用域。配置页写入、手动编辑、Agent 自己
改了 ``.cosir/agents``，走的都是这一条路：写文件，下一次读取生效；注册表不接受内存写入。

不负责：配置文件读取与 JSON 字段校验（见 ``AgentProfileScopeSource`` 与
``AgentProfile.vaild_agent_profile``）、磁盘持久化、Run / context 运行态，以及工具实现。

作用域键：系统级 profile 使用哨兵 ``AgentProfileRegistry.SYSTEM_WORKSPACE``；workspace
profile 使用其根路径经 ``normalize_workspace`` 规范化后的字符串（口径见
``app.utils.workspace_scope``），同一 workspace 的不同写法（大小写、相对路径）必须归一到同一键。

为什么在读取时校验而不在启动期装载：workspace 可以在进程运行期间创建，配置也随时可能被改；校验收口在
索引读原语内部，调用方无需也不应显式装载。

日志：重读、冲突与重复都落盘，便于运行后复盘「为什么某个子 Agent 不见了」；日志是诊断旁路，写失败不影响
索引结果。
"""

from __future__ import annotations

import builtins
import os
import threading
from collections.abc import Hashable
from dataclasses import dataclass
from pathlib import Path

from app.config.logging.logger import log
from app.core.agents.agent_profile import (
    AgentProfile,
    AgentProfileType,
)
from app.core.agents.agent_profile_source import AgentProfileScopeSource
from app.utils.workspace_scope import SYSTEM_SCOPE as _SYSTEM_SCOPE
from app.utils.workspace_scope import normalize_scope_path


@dataclass(frozen=True)
class _ScopeSnapshot:
    """一个作用域某一刻的配置：读取前的签名，与按文件名顺序去重后的 profile。"""

    signature: Hashable
    profiles: dict[str, AgentProfile]


class AgentProfileRegistry:
    """系统级及各 workspace 的 Agent profile 副本，读取时按签名重读作用域。

    主 Agent 属于系统作用域（来源在读取系统作用域时生成它），子 Agent 一律来自配置文件（内置子 Agent
    也是系统级配置文件）。注册表不区分内置与否：「哪些子 Agent 不能删除」是配置中心的规则。

    重复与冲突：同一作用域内同 ``agent_id`` 保留先读到的（主 Agent 排在文件之前，同名文件顶替不了它）；
    workspace 与系统级同名时系统级生效，workspace 的那份在列举与解析中都不可见。冲突按读取时两个作用域
    当前的内容判定，所以两边各自改动后结果与重启后一致。

    失败策略：坏配置不影响运行。目录级问题与单文件无效都由 source 记日志后降级为空/跳过，读取路径不抛
    异常；签名没变就不重读、冲突也不重复告警，坏文件与冲突都不会每次读取都刷一遍日志。

    已知缺口：未校验配置文件名与 ``agent_id`` 是否一致——文件名只用于排序与日志定位，
    以 ``b.json`` 声明 ``agent_id: a`` 同样会被装载为 ``a``。

    并发：快照整体替换（不原地修改），读方拿到的总是某一刻完整的快照；重读由一把装载锁串行化，并发
    读到同一个变化只读一次盘。``AgentProfile`` 是不可变值对象，读路径返回共享实例。
    """

    # 哨兵值来自 app.utils.workspace_scope 的唯一来源，与其他作用域匹配方共用。
    SYSTEM_WORKSPACE = _SYSTEM_SCOPE

    def __init__(self, source: AgentProfileScopeSource) -> None:
        """绑定配置来源；不读文件。

        参数:
            source: 作用域 → 配置文件 → profile 列表与签名的来源。装配期显式注入；不接受缺省值，
                以免出现「注册表存在但配置永远不会被装载」的静默失效状态。
        """

        self._source = source
        self._load_lock = threading.Lock()
        self._snapshots: dict[str, _ScopeSnapshot] = {}
        # 已记过冲突告警时两边快照的签名：没变就不再记（见 :meth:`_local`）。
        self._reported_conflicts: dict[str, tuple[Hashable, Hashable]] = {}

    @property
    def source(self) -> AgentProfileScopeSource:
        """返回本注册表使用的配置来源（只读）。

        供配置写入方复用同一目录映射：写目录与装载目录必须来自同一个来源，否则保存后的配置
        可能落在读取方看不到的位置，表现为「保存成功但列表为空」。
        """

        return self._source

    @classmethod
    def normalize_workspace(cls, workspace: str | Path) -> str:
        """将 workspace 根路径归一化为 Registry 作用域键。

        参数:
            workspace: 系统作用域哨兵或 workspace 根路径。

        返回:
            系统哨兵 ``system`` 或绝对、规范化后的 workspace 路径。

        异常:
            ValueError: workspace 路径为空。
            TypeError: 传入 ``None`` 等非路径对象。用 ``os.fspath`` 而不是 ``str``：
                ``str(None)`` 会把「没有 workspace」伪装成名为 ``None`` 的路径键（口径同
                ``normalize_scope_path``）。
        """

        text = os.fspath(workspace)
        if text == cls.SYSTEM_WORKSPACE:
            return cls.SYSTEM_WORKSPACE
        text = text.strip()
        if not text:
            raise ValueError("workspace path must not be empty")
        return normalize_scope_path(text)

    def drop_scope(self, workspace: str | Path) -> None:
        """丢掉某个作用域的快照（workspace 被删除时调用，释放内存）；之后再读会按磁盘重建。"""

        scope = self.normalize_workspace(workspace)
        with self._load_lock:
            self._snapshots.pop(scope, None)
            self._reported_conflicts.pop(scope, None)

    def _snapshot(self, scope: str) -> _ScopeSnapshot:
        """该作用域当前的快照：签名没变就复用，变了就重读。

        签名在读盘之前取：读盘期间文件又被改了，下一次取到的签名对不上，会再读一次。
        """

        signature = self._source.signature(scope)
        current = self._snapshots.get(scope)
        if current is not None and current.signature == signature:
            return current
        with self._load_lock:
            current = self._snapshots.get(scope)
            if current is not None and current.signature == signature:
                return current
            profiles: dict[str, AgentProfile] = {}
            duplicates: list[str] = []
            for profile in self._source.load(scope):
                if profile.agent_id in profiles:
                    duplicates.append(profile.agent_id)
                    continue
                profiles[profile.agent_id] = profile
            snapshot = _ScopeSnapshot(signature, profiles)
            self._snapshots[scope] = snapshot
        log.info(
            "agent_profile_scope_loaded",
            extra={
                "msg": "Agent 配置作用域已按文件重读",
                "data": {"scope": scope, "profile_count": len(profiles)},
            },
        )
        for agent_id in duplicates:
            log.warning(
                "agent_profile_duplicate_skipped",
                extra={
                    "msg": "同一作用域内 Agent ID 重复，保留先读到的 profile",
                    "data": {"scope": scope, "agent_id": agent_id},
                },
            )
        return snapshot

    def _system(self) -> dict[str, AgentProfile]:
        return self._snapshot(self.SYSTEM_WORKSPACE).profiles

    def _local(self, scope: str) -> dict[str, AgentProfile]:
        """作用域自己生效的 profile；workspace 里与系统级同名的被系统级遮住，不算在内。

        冲突在读取时按两边当前的快照判定（每步都会走到这里），告警只在两边任一快照变了之后记一次。
        """

        if scope == self.SYSTEM_WORKSPACE:
            return self._system()
        system = self._snapshot(self.SYSTEM_WORKSPACE)
        local = self._snapshot(scope)
        shadowed = [agent_id for agent_id in local.profiles if agent_id in system.profiles]
        if shadowed:
            checked = (system.signature, local.signature)
            if self._reported_conflicts.get(scope) != checked:
                self._reported_conflicts[scope] = checked
                log.warning(
                    "agent_profile_conflict_skipped",
                    extra={
                        "msg": "workspace Agent ID 与系统级冲突，系统级生效",
                        "data": {"scope": scope, "agent_ids": shadowed},
                    },
                )
        return {
            agent_id: profile
            for agent_id, profile in local.profiles.items()
            if agent_id not in system.profiles
        }

    def resolve(self, workspace: str | Path, agent_id: str) -> AgentProfile | None:
        """按 workspace 优先、system 回退的规则解析 profile；未找到时返回 ``None``。"""

        scope = self.normalize_workspace(workspace)
        return self._local(scope).get(agent_id) or self._system().get(agent_id)

    def resolve_local(self, workspace: str | Path, agent_id: str) -> AgentProfile | None:
        """只解析指定作用域内的 Agent，不执行 system fallback。

        配置写入、更新和删除必须使用本方法区分 workspace 本地文件与 system 继承项，
        不得用带 fallback 的 :meth:`resolve` 代替。
        """

        return self._local(self.normalize_workspace(workspace)).get(agent_id)

    def list(self, workspace: str | Path) -> list[AgentProfile]:
        """列出指定 workspace 可见的 profile；系统作用域 profile 排在 workspace profile 前。"""

        scope = self.normalize_workspace(workspace)
        profiles = list(self._system().values())
        if scope != self.SYSTEM_WORKSPACE:
            profiles.extend(self._local(scope).values())
        return profiles

    def list_agent_ids(self, workspace: str | Path) -> set[str]:
        """列出指定 workspace 可见的所有 Agent ID。"""

        return {profile.agent_id for profile in self.list(workspace)}

    def child_agent_ids(self, workspace: str | Path) -> set[str]:
        """列出指定 workspace 可见的 CHILD Agent ID。"""

        return {
            profile.agent_id
            for profile in self.list(workspace)
            if profile.agent_type is AgentProfileType.CHILD
        }

    def list_system_sub_agents(self) -> builtins.list[AgentProfile]:
        """列出系统作用域内的 CHILD profile，不包含主 Agent。"""

        return [
            profile
            for profile in self._system().values()
            if profile.agent_type is AgentProfileType.CHILD
        ]

    def list_workspace_sub_agents(self, workspace: str | Path) -> builtins.list[AgentProfile]:
        """列出指定 workspace 作用域内生效的 profile；与 :meth:`list` 的区别是不合并 system 作用域，
        供配置中心区分本地文件与继承项。"""

        return list(self._local(self.normalize_workspace(workspace)).values())
