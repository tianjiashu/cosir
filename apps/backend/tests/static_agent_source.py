"""测试用的 Agent 配置来源：给定作用域直接返回给定的 profile，其余作用域照常读文件。

注册表只认来源（没有内存写入），用例要「注册表里有这些 profile」时用它构造注册表。改 ``profiles`` 后签名随之
变化，注册表下一次读取就重读，与真实来源「改文件即生效」同一语义。
"""

from __future__ import annotations

from collections.abc import Hashable, Iterable
from pathlib import Path

from app.core.agents.agent_profile import AgentProfile
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.core.agents.agent_profile_source import AgentProfileScopeSource


class StaticAgentSource(AgentProfileScopeSource):
    """``profiles`` 以作用域为键（系统作用域用 ``AgentProfileRegistry.SYSTEM_WORKSPACE``）。"""

    def __init__(self, profiles: dict[str, list[AgentProfile]] | None = None) -> None:
        self.profiles: dict[str, list[AgentProfile]] = profiles or {}

    def add(self, workspace: str | Path, profile: AgentProfile) -> None:
        scope = AgentProfileRegistry.normalize_workspace(workspace)
        self.profiles.setdefault(scope, []).append(profile)

    def signature(self, scope: str) -> Hashable:
        if scope not in self.profiles:
            return super().signature(scope)
        return tuple(id(profile) for profile in self.profiles[scope])

    def load(self, scope: str) -> list[AgentProfile]:
        if scope not in self.profiles:
            return super().load(scope)
        return list(self.profiles[scope])


def registry_with(
    system: Iterable[AgentProfile] = (),
) -> tuple[AgentProfileRegistry, StaticAgentSource]:
    """系统作用域里有 ``system`` 这些 profile 的注册表，连同它的来源（用于再添加 workspace profile）。"""

    source = StaticAgentSource({AgentProfileRegistry.SYSTEM_WORKSPACE: list(system)})
    return AgentProfileRegistry(source), source
