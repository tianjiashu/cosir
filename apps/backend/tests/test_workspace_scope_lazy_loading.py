"""workspace 作用域配置「首次访问即装载」契约测试。

背景：曾经只在进程启动时遍历一次已登记 workspace 装载 ``.cosir/agents`` 与
``.cosir/agent-teams``，导致**启动之后才创建的 workspace** 在整个进程周期内解析不到自己的
子 Agent / Team（真实故障：启动后新建 workspace，Team 草稿引用的 child Agent 被判为不可用）。

本文件覆盖改造后的契约：

1. 运行期新增 workspace 目录与 JSON 后，无需重启后端即可首次访问装载到；
2. 每个作用域只装载一次（重复读取不重复读盘，并发首访只装载一次）；
3. 装载只合并不替换：system 作用域的内置 profile 不会被 workspace 装载清掉；
4. 删除 workspace 后 ``drop_scope`` 清掉已装载标记，同路径重建能读到新内容；
5. workspace 可见 Team 配置合并 system 继承项。
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path

import pytest
from app.agent_team.configuration_scope_source import AgentTeamConfigurationScopeSource
from app.agent_team.registry import AgentTeamConfigurationRegistry
from app.api.schemas.AgentConfigurationDocument import AgentConfigurationDocument
from app.config.configuration import build_agent_registry
from app.core.agents import agent_profile_source
from app.core.agents.agent_profile import AgentProfile, AgentProfileType
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.core.agents.agent_profile_source import AgentProfileScopeSource
from app.service.configuration.agent_configuration_service import AgentConfigurationService

_JOIN_TIMEOUT = 10.0


def _write_agent(workspace: Path, agent_id: str, *, role: str = "reader") -> Path:
    """在 workspace 的 ``.cosir/agents`` 下写入一份最小合法 CHILD 配置。"""

    directory = workspace / ".cosir" / "agents"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{agent_id}.json"
    path.write_text(
        json.dumps(
            {
                "agent_id": agent_id,
                "role": role,
                "description": f"Lazy loading probe {agent_id}",
                "system_prompt": "Read only the files needed and report evidence.",
                "allowed_tools": ["read_file"],
            }
        ),
        encoding="utf-8",
    )
    return path


def _file_agent_profile(agent_id: str, *, role: str) -> AgentProfile:
    """构造一个符合注册契约的 CHILD profile（``workflow=None`` 跳过工作流装配）。"""

    return AgentProfile(
        agent_id=agent_id,
        role=role,
        description=f"Probe {role}",
        allowed_tools=["read_file"],
        agent_type=AgentProfileType.CHILD,
        system_prompt="probe",
        workflow=None,  # type: ignore[arg-type]
    )


def _team_document(team_id: str, scope: str) -> dict[str, object]:
    """返回一份最小合法 Team 配置文档。"""

    return {
        "team_id": team_id,
        "name": f"Team {team_id}",
        "description": "Lazy loading probe team",
        "scope": scope,
        "start_node_id": "develop",
        "nodes": [
            {
                "node_id": "develop",
                "name": "开发",
                "agent_id": "general-assistant",
                "statuses": ["done", "blocked"],
            },
            {
                "node_id": "review",
                "name": "审查",
                "agent_id": "general-assistant",
                "statuses": ["passed", "needs_changes"],
            },
        ],
        "transitions": [
            {"from_node_id": "develop", "status": "done", "target_node_id": "review"},
            {"from_node_id": "develop", "status": "blocked", "target_node_id": "END"},
            {"from_node_id": "review", "status": "passed", "target_node_id": "END"},
            {"from_node_id": "review", "status": "needs_changes", "target_node_id": "develop"},
        ],
    }


def _write_team(workspace: Path, team_id: str, *, scope: str = "workspace") -> Path:
    """在 workspace 的 ``.cosir/agent-teams`` 下写入一份 Team 配置。"""

    directory = workspace / ".cosir" / "agent-teams"
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{team_id}.json"
    path.write_text(json.dumps(_team_document(team_id, scope)), encoding="utf-8")
    return path


def test_workspace_created_after_startup_resolves_without_restart(tmp_path: Path) -> None:
    """装载归属运行时：注册表装配之后才出现的 workspace 目录同样能被解析到。"""

    registry = AgentProfileRegistry(AgentProfileScopeSource())
    workspace = tmp_path / "workspace"

    # 模拟「后端启动时该 workspace 还不存在」：注册表已就绪，此时才出现配置。
    _write_agent(workspace, "backend-runtime-investigator")

    profile = registry.resolve(workspace, "backend-runtime-investigator")

    assert profile is not None
    assert profile.agent_id == "backend-runtime-investigator"
    assert profile.allowed_tools == ["read_file"]


def test_configuration_service_lists_disk_agent_on_first_read(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """配置中心首次读取即列出磁盘上的 workspace Agent。

    否则会出现「文件在手却管理不了」：列表为空，而同名新建被 409 拒绝、改/删返回 404。
    """

    workspace = tmp_path / "workspace"
    _write_agent(workspace, "workspace-reader")
    registry = AgentProfileRegistry(AgentProfileScopeSource())
    monkeypatch.setattr(
        "app.service.configuration.agent_configuration_service.get_agent_registry",
        lambda: registry,
    )

    documents = AgentConfigurationService(workspace_root=workspace).list_documents()

    assert [document.agent_id for document in documents] == ["workspace-reader"]
    assert documents[0].source == "user_file"


def test_lazy_load_merges_and_keeps_builtin_profiles(tmp_path: Path) -> None:
    """装载只合并不替换：system 内置 profile 不因 workspace 装载而消失。"""

    registry = build_agent_registry()
    workspace = tmp_path / "workspace"
    _write_agent(workspace, "workspace-reader")

    assert registry.resolve(workspace, "workspace-reader") is not None
    # 内置 CHILD 与主 Agent 仍在 system 作用域内可用（否则所有委派与 Run 都解析不到 Agent）。
    assert registry.resolve(workspace, "general-assistant") is not None
    assert registry.resolve(AgentProfileRegistry.SYSTEM_WORKSPACE, "main_agent") is not None


def test_system_baseline_wins_regardless_of_read_order(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """跨作用域同名 id 由 system 基线胜出，且结果不取决于哪个作用域先被读到。

    懒装载让「先注册者优先」的裁决依据变成读取顺序；若 workspace 先被读到，system 的用户
    配置文件会被判为冲突而整项丢弃——所以任何行经 workspace 作用域的注册/装载都必须先装载
    system 基线。
    """

    system_directory = tmp_path / "system-agents"
    system_directory.mkdir()
    (system_directory / "shared.json").write_text(
        json.dumps(
            {
                "agent_id": "shared",
                "role": "system-reader",
                "description": "System baseline probe",
                "system_prompt": "Read only the files needed and report evidence.",
                "allowed_tools": ["read_file"],
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setattr(
        agent_profile_source, "system_agent_config_dir", lambda: system_directory
    )
    workspace = tmp_path / "workspace"
    _write_agent(workspace, "shared", role="workspace-reader")

    # 顺序一：先注册 workspace 同名 profile —— 必须被拒绝，且 system 文件正常入库。
    registering = AgentProfileRegistry(AgentProfileScopeSource())
    assert registering.register(
        workspace, _file_agent_profile("shared", role="workspace-reader")
    ) is False
    assert registering.resolve_local(workspace, "shared") is None
    assert registering.resolve_local(
        AgentProfileRegistry.SYSTEM_WORKSPACE, "shared"
    ) is not None

    # 顺序二：workspace 目录内也存在同名文件 —— 解析仍命中 system 基线。
    resolving = AgentProfileRegistry(AgentProfileScopeSource())
    resolved = resolving.resolve(workspace, "shared")
    assert resolved is not None
    assert resolved.role == "system-reader"


def test_scope_is_loaded_only_once(tmp_path: Path) -> None:
    """同一作用域只装载一次：重复读取不再读盘。"""

    loads: list[str] = []

    class _CountingSource(AgentProfileScopeSource):
        def load(self, scope: str) -> list[AgentProfile]:
            loads.append(scope)
            return super().load(scope)

    registry = AgentProfileRegistry(_CountingSource())
    workspace = tmp_path / "workspace"
    _write_agent(workspace, "workspace-reader")

    for _ in range(3):
        assert registry.resolve(workspace, "workspace-reader") is not None

    assert loads.count(AgentProfileRegistry.normalize_workspace(workspace)) == 1


def test_concurrent_first_access_loads_scope_once(tmp_path: Path) -> None:
    """并发首次访问同一作用域只装载一次，且所有线程都能读到 profile。"""

    loads: list[str] = []
    loads_lock = threading.Lock()

    class _CountingSource(AgentProfileScopeSource):
        def load(self, scope: str) -> list[AgentProfile]:
            with loads_lock:
                loads.append(scope)
            return super().load(scope)

    registry = AgentProfileRegistry(_CountingSource())
    workspace = tmp_path / "workspace"
    _write_agent(workspace, "workspace-reader")
    barrier = threading.Barrier(8)
    results: list[bool] = []
    results_lock = threading.Lock()

    def worker() -> None:
        barrier.wait(timeout=_JOIN_TIMEOUT)
        profile = registry.resolve(workspace, "workspace-reader")
        with results_lock:
            results.append(profile is not None)

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=_JOIN_TIMEOUT)

    assert results == [True] * 8
    assert loads.count(AgentProfileRegistry.normalize_workspace(workspace)) == 1


def test_drop_scope_rereads_directory_after_recreate(tmp_path: Path) -> None:
    """workspace 删除后同路径重建：drop_scope 清掉装载标记，重建后的目录内容可被重新装载。"""

    workspace = tmp_path / "workspace"
    stale = _write_agent(workspace, "first-agent")
    registry = AgentProfileRegistry(AgentProfileScopeSource())
    assert registry.resolve(workspace, "first-agent") is not None

    # 进程外改动（旧文件删除、新文件写入）不会被已装载作用域感知——同路径重建的 workspace
    # 会复用同一作用域键，这正是必须由删除路径显式 drop_scope 的原因。
    stale.unlink()
    _write_agent(workspace, "second-agent")
    assert registry.resolve(workspace, "first-agent") is not None
    assert registry.resolve(workspace, "second-agent") is None

    registry.drop_scope(workspace)

    assert registry.resolve(workspace, "second-agent") is not None
    assert registry.resolve(workspace, "first-agent") is None


def test_team_configurations_load_lazily_and_inherit_system(tmp_path: Path) -> None:
    """Team 配置同样按需装载：workspace 独有可见，system 继承项在 workspace 可见性内。"""

    system_directory = tmp_path / "system-agent-teams"
    system_directory.mkdir()
    (system_directory / "system-team.json").write_text(
        json.dumps(_team_document("system-team", "system")),
        encoding="utf-8",
    )
    registry = AgentTeamConfigurationRegistry(AgentTeamConfigurationScopeSource())
    workspace = tmp_path / "workspace"
    _write_team(workspace, "workspace-team")

    visible = {configuration.team_id for configuration in registry.list_visible(workspace)}

    assert {"workspace-team", "system-team"} <= visible
    # 独立列举不混入继承项，供配置中心区分本地文件与继承配置。
    owned = registry.list_scope("workspace", workspace_root=workspace)
    assert [item.team_id for item in owned] == ["workspace-team"]


def test_team_source_logs_only_when_scope_path_is_broken(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Team 装载：目录不存在不记日志（正常无配置），路径不是目录必须留痕。"""

    registry = AgentTeamConfigurationRegistry(AgentTeamConfigurationScopeSource())
    missing_workspace = tmp_path / "missing-workspace"

    with caplog.at_level(logging.WARNING):
        assert registry.list_visible(missing_workspace) == []
    assert not [
        record
        for record in caplog.records
        if record.message == "team_configuration_scope_load_failed"
    ], "目录不存在属正常无配置，不应产生告警噪音"

    broken_workspace = tmp_path / "broken-workspace"
    (broken_workspace / ".cosir").mkdir(parents=True)
    (broken_workspace / ".cosir" / "agent-teams").write_text("", encoding="utf-8")

    with caplog.at_level(logging.WARNING):
        assert registry.list_visible(broken_workspace) == []

    assert any(
        record.message == "team_configuration_scope_load_failed" for record in caplog.records
    ), "配置路径不是目录必须留痕，否则该作用域会永远为空且无线索"


def test_scope_with_file_as_cosir_parent_logs_and_degrades(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """``.cosir`` 被普通文件占位（祖先不是目录）时降级留痕，而不是静默当作「没有配置」。"""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".cosir").write_text("", encoding="utf-8")

    profile_registry = AgentProfileRegistry(AgentProfileScopeSource())
    team_registry = AgentTeamConfigurationRegistry(AgentTeamConfigurationScopeSource())
    with caplog.at_level(logging.WARNING):
        assert profile_registry.resolve(workspace, "any-agent") is None
        assert team_registry.list_visible(workspace) == []

    events = {record.message for record in caplog.records}
    assert "agent_profile_scope_load_failed" in events
    assert "team_configuration_scope_load_failed" in events


def test_creating_agent_does_not_report_false_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """先落盘再注册的写入路径不会被重新读回并报成「同作用域重复」。"""

    workspace = tmp_path / "workspace"
    (workspace / ".cosir" / "agents").mkdir(parents=True)
    registry = AgentProfileRegistry(AgentProfileScopeSource())
    monkeypatch.setattr(
        "app.service.configuration.agent_configuration_service.get_agent_registry",
        lambda: registry,
    )
    service = AgentConfigurationService(workspace_root=workspace)

    with caplog.at_level(logging.WARNING):
        service.create_document(
            AgentConfigurationDocument(
                agent_id="workspace-reader",
                role="reader",
                description="Probe",
                system_prompt="Read only the files needed and report evidence.",
                allowed_tools=["read_file"],
            )
        )
        assert [document.agent_id for document in service.list_documents()] == [
            "workspace-reader"
        ]

    assert not [
        record
        for record in caplog.records
        if record.message == "agent_profile_duplicate_skipped"
    ], "刚写入的配置文件不应在首次读取时被读回并报成重复"


def test_team_scope_uses_same_key_normalization_as_agent_scope(tmp_path: Path) -> None:
    """两类注册表对同一 workspace 路径得出同一作用域键，系统哨兵也同源。"""

    workspace = str(tmp_path / "workspace")

    assert AgentTeamConfigurationRegistry.normalize_scope(
        "workspace", workspace
    ) == AgentProfileRegistry.normalize_workspace(workspace)
    assert (
        AgentTeamConfigurationRegistry.normalize_scope("system")
        == AgentProfileRegistry.SYSTEM_WORKSPACE
    )


def test_sources_map_scopes_to_their_fixed_directories(tmp_path: Path) -> None:
    """目录映射按 `.cosir` 约定落到对应子目录，system 与 workspace 不互相串位。"""

    workspace = tmp_path / "workspace"

    assert AgentProfileScopeSource().directory(str(workspace)) == (
        workspace / ".cosir" / "agents"
    )
    assert AgentTeamConfigurationScopeSource().directory(str(workspace)) == (
        workspace / ".cosir" / "agent-teams"
    )
    assert AgentProfileScopeSource().directory(
        AgentProfileRegistry.SYSTEM_WORKSPACE
    ) != AgentProfileScopeSource().directory(str(workspace))
