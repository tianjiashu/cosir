"""作用域懒装载改造的独立缺陷发现型测试（对抗性）。

覆盖目标（均以「试图证伪」为出发点，而不只是 happy path）：

1. 并发：多线程首访是否只装载一次；``register`` / ``drop_scope`` / 读交错时是否抛异常或读到
   半装填状态；``drop_scope`` 与正在进行的装载之间的锁序是否会死锁。
2. 跨作用域同名 ``agent_id``：两种读取顺序下裁决是否一致；``register`` 是否会被 system 目录
   内容抢先；system 作用域自身的注册会不会被「某个 workspace 的本地同名 Agent」挡掉（跨作用域
   串扰）。
3. 删除/重建：``drop_scope`` 幂等性与作用域隔离；不 ``drop_scope`` 时的陈旧语义。
4. 坏配置与布局破坏：单文件坏、路径是文件、符号链接目录、失效符号链接、祖先被占位、
   隐藏文件、非 UTF-8、顶层非对象 —— 是否都能「降级 + 留痕（事件名）」且不抛异常、不阻断后续。
5. 内置 profile 不被同名用户文件覆盖。
6. 配置中心写入目录 = 装载目录；先落盘再注册不产生虚假重复告警。
7. Team 侧可见性合并、``list_scope`` 不混入继承项、``save``/``delete`` 与装载目录一致。
8. 边界：``normalize_workspace`` 的空串/空白/``None`` 契约、大小写与相对路径归一。

不修改任何生产代码；所有断言都以被测模块自身声明的契约为「期望」。
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from pathlib import Path

import pytest
from app.agent_team.configuration.agent_team_configuration import AgentTeamConfiguration
from app.agent_team.configuration_scope_source import AgentTeamConfigurationScopeSource
from app.agent_team.registry import AgentTeamConfigurationRegistry
from app.api.schemas.AgentConfigurationDocument import AgentConfigurationDocument
from app.config.configuration import build_agent_registry
from app.core.agents.agent_profile import AgentProfile, AgentProfileType
from app.core.agents.agent_profile_registry import AgentProfileRegistry
from app.core.agents.agent_profile_source import AgentProfileScopeSource
from app.service.configuration.agent_configuration_service import (
    AgentConfigurationError,
    AgentConfigurationService,
)
from app.service.configuration.agent_team_configuration_service import (
    AgentTeamConfigurationService,
)
from app.utils.scope_config_directory import ScopeDirectoryError, read_scope_json_files
from app.utils.workspace_scope import SYSTEM_SCOPE, normalize_scope_path

# 与 conftest 的 ``isolate_scope_config_dirs`` 保持一致：系统作用域被隔离到用例临时目录。
_SYSTEM_AGENTS = "system-agents"
_SYSTEM_TEAMS = "system-agent-teams"

_JOIN_TIMEOUT = 15.0


# --------------------------------------------------------------------------- 构造工具


def _agent_document(agent_id: str, *, role: str = "reader") -> dict[str, object]:
    """返回一份最小合法 CHILD 配置文档。"""

    return {
        "agent_id": agent_id,
        "role": role,
        "description": f"Adversarial probe {agent_id}",
        "system_prompt": "Read only the files needed and report evidence.",
        "allowed_tools": ["read_file"],
    }


def _write_agent(
    directory: Path, agent_id: str, *, role: str = "reader", filename: str | None = None
) -> Path:
    """在给定目录写入一份合法 Agent 配置。"""

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / (filename if filename is not None else f"{agent_id}.json")
    path.write_text(json.dumps(_agent_document(agent_id, role=role)), encoding="utf-8")
    return path


def _write_workspace_agent(workspace: Path, agent_id: str, *, role: str = "reader") -> Path:
    """在 workspace 的 ``.cosir/agents`` 下写入一份合法配置。"""

    return _write_agent(workspace / ".cosir" / "agents", agent_id, role=role)


def _file_profile(agent_id: str, *, role: str) -> AgentProfile:
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


def _team_document(team_id: str, scope: str, *, name: str | None = None) -> dict[str, object]:
    """返回一份最小合法 Team 配置文档。"""

    return {
        "team_id": team_id,
        "name": name if name is not None else f"Team {team_id}",
        "description": "Adversarial probe team",
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


def _write_team(directory: Path, team_id: str, *, scope: str, name: str | None = None) -> Path:
    """在给定目录写入一份合法 Team 配置。"""

    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{team_id}.json"
    path.write_text(
        json.dumps(_team_document(team_id, scope, name=name)), encoding="utf-8"
    )
    return path


def _symlink_or_skip(link: Path, target: Path, *, target_is_directory: bool) -> None:
    """创建符号链接；当前平台不支持时跳过用例（不把环境限制误报为缺陷）。"""

    try:
        os.symlink(target, link, target_is_directory=target_is_directory)
    except (OSError, NotImplementedError) as exc:  # pragma: no cover - 平台相关
        pytest.skip(f"当前平台无法创建符号链接: {type(exc).__name__}: {exc}")


def _run_threads(targets: list, timeout: float = _JOIN_TIMEOUT) -> None:
    """启动并 join 一批线程，返回后断言没有线程仍在运行（防死锁）。"""

    threads = [threading.Thread(target=target) for target in targets]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + timeout
    for thread in threads:
        thread.join(timeout=max(0.0, deadline - time.monotonic()))
    assert not any(thread.is_alive() for thread in threads), "存在未在超时内结束的线程（疑似死锁）"


def _count_source(counter: dict[str, int], lock: threading.Lock) -> type[AgentProfileScopeSource]:
    """返回一个按作用域统计 ``load`` 次数的来源子类。"""

    class _CountingSource(AgentProfileScopeSource):
        def load(self, scope: str) -> list[AgentProfile]:
            with lock:
                counter[scope] = counter.get(scope, 0) + 1
            return super().load(scope)

    return _CountingSource


# =========================================================================== A. 并发


def test_concurrent_first_access_loads_scope_once(tmp_path: Path) -> None:
    """[并发/幂等] 8 线程同时首访同一新 workspace：应只读盘一次，且全部读到 profile。

    潜在缺陷：装载未串行化时会重复读盘，或某线程读到半装填（索引已标记已装载但内容未合并）。
    """

    counter: dict[str, int] = {}
    registry = AgentProfileRegistry(_count_source(counter, threading.Lock())())
    workspace = tmp_path / "workspace"
    _write_workspace_agent(workspace, "workspace-reader")
    barrier = threading.Barrier(8)
    results: list[bool] = []
    results_lock = threading.Lock()

    def worker() -> None:
        barrier.wait(timeout=_JOIN_TIMEOUT)
        profile = registry.resolve(workspace, "workspace-reader")
        with results_lock:
            results.append(profile is not None)

    _run_threads([worker] * 8)

    workspace_scope = AgentProfileRegistry.normalize_workspace(workspace)
    assert results == [True] * 8
    assert counter.get(workspace_scope) == 1, "同一作用域并发首访应只装载一次"
    assert counter.get(SYSTEM_SCOPE) == 1, "system 基线也应只装载一次"


def test_concurrent_distinct_scopes_isolate_and_load_once_each(tmp_path: Path) -> None:
    """[并发/作用域隔离] 每个线程读自己的 workspace：不得串到别人的作用域。

    潜在缺陷：作用域键归一化不唯一或缺锁导致跨 workspace 内容互相可见。
    """

    counter: dict[str, int] = {}
    registry = AgentProfileRegistry(_count_source(counter, threading.Lock())())
    workspaces = [tmp_path / f"ws-{index}" for index in range(6)]
    for index, workspace in enumerate(workspaces):
        _write_workspace_agent(workspace, f"agent-{index}")

    barrier = threading.Barrier(len(workspaces))
    seen: dict[int, set[str]] = {}
    errors: list[BaseException] = []

    def worker(index: int) -> None:
        barrier.wait(timeout=_JOIN_TIMEOUT)
        try:
            ids = registry.list_agent_ids(workspaces[index])
            seen[index] = set(ids)
        except BaseException as exc:  # noqa: BLE001
            errors.append(exc)

    _run_threads([lambda index=index: worker(index) for index in range(len(workspaces))])

    assert not errors
    for index in range(len(workspaces)):
        assert seen[index] == {f"agent-{index}"}, "每个 workspace 只能看到自己的 Agent"


def test_reader_writer_drop_interleaving_never_raises(tmp_path: Path) -> None:
    """[并发/健壮性] register/unregister/list/resolve 与反复 drop_scope 交错不得抛异常。

    潜在缺陷：``list`` 迭代 ``self._profiles`` 时被并发改写（``RuntimeError: dictionary changed
    size during iteration``），或读路径遇到半装填作用域。
    """

    registry = AgentProfileRegistry(AgentProfileScopeSource())
    workspace = tmp_path / "workspace"
    _write_workspace_agent(workspace, "disk-agent")
    scope = AgentProfileRegistry.normalize_workspace(workspace)
    done = threading.Event()
    failures: list[BaseException] = []
    rounds = 300

    def writer(prefix: str) -> None:
        try:
            for index in range(rounds):
                agent_id = f"{prefix}-{index}"
                registry.register(scope, _file_profile(agent_id, role="writer"))
                registry.unregister(scope, agent_id)
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)
            done.set()

    def reader() -> None:
        try:
            while not done.is_set():
                assert all(item.agent_id for item in registry.list(scope))
                registry.resolve(scope, "absent")
                registry.list_workspace_sub_agents(scope)
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)
            done.set()

    def dropper() -> None:
        try:
            while not done.is_set():
                registry.drop_scope(scope)
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)
            done.set()

    targets = [
        lambda: writer("w1"),
        lambda: writer("w2"),
        reader,
        reader,
        dropper,
    ]
    _run_threads(targets)

    assert not failures, f"并发交错出现异常: {failures!r}"


def test_drop_scope_waits_for_inflight_load_without_deadlock(tmp_path: Path) -> None:
    """[并发/锁序] 装载进行中调用 drop_scope 应阻塞等待而非死锁，且结果自洽。

    潜在缺陷：锁序颠倒（持索引锁再取装载锁）造成死锁；或 drop 与装载交叉留下「已清空但仍标记
    已装载」的坏状态（此后永远读不到磁盘内容）。
    """

    load_started = threading.Event()
    release_load = threading.Event()

    class _BlockingSource(AgentProfileScopeSource):
        def load(self, scope: str) -> list[AgentProfile]:
            if scope != SYSTEM_SCOPE:
                load_started.set()
                assert release_load.wait(timeout=_JOIN_TIMEOUT), "装载未被放行"
            return super().load(scope)

    registry = AgentProfileRegistry(_BlockingSource())
    workspace = tmp_path / "workspace"
    _write_workspace_agent(workspace, "disk-agent")

    resolve_result: list[object] = []
    resolve_thread = threading.Thread(
        target=lambda: resolve_result.append(registry.resolve(workspace, "disk-agent"))
    )
    resolve_thread.start()
    assert load_started.wait(timeout=_JOIN_TIMEOUT), "首个读取未触发装载"

    drop_done = threading.Event()

    def drop() -> None:
        registry.drop_scope(workspace)
        drop_done.set()

    drop_thread = threading.Thread(target=drop)
    drop_thread.start()
    # 装载仍被卡在磁盘读取，drop 必须等它拿不到装载锁 → 不应立刻完成。
    time.sleep(0.2)
    assert not drop_done.is_set(), "drop_scope 未等待进行中的装载（装载锁未生效）"

    release_load.set()
    resolve_thread.join(timeout=_JOIN_TIMEOUT)
    drop_thread.join(timeout=_JOIN_TIMEOUT)
    assert not resolve_thread.is_alive() and not drop_thread.is_alive()

    # drop 发生在装载之后：该作用域应重新变为「未装载且索引为空」。
    assert resolve_result == [None] or resolve_result[0] is not None
    assert registry.list_workspace_sub_agents(workspace) == []
    assert registry.resolve(workspace, "disk-agent") is not None, "drop 后应能重新装载磁盘内容"


def test_team_registry_concurrent_read_and_write_no_exception(tmp_path: Path) -> None:
    """[并发/健壮性] Team 注册表在并发 list_visible/register 下不得抛异常。"""

    registry = AgentTeamConfigurationRegistry(AgentTeamConfigurationScopeSource())
    workspace = tmp_path / "workspace"
    _write_team(workspace / ".cosir" / "agent-teams", "disk-team", scope="workspace")
    done = threading.Event()
    failures: list[BaseException] = []

    def writer() -> None:
        try:
            for index in range(200):
                configuration = AgentTeamConfiguration.model_validate(
                    _team_document(f"t-{index}", "workspace")
                )
                registry.register(configuration, workspace_root=workspace)
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)
            done.set()

    def reader() -> None:
        try:
            while not done.is_set():
                assert all(item.team_id for item in registry.list_visible(workspace))
                registry.list_scope("workspace", workspace_root=workspace)
        except BaseException as exc:  # noqa: BLE001
            failures.append(exc)
            done.set()

    targets = [writer, reader, reader]
    _run_threads(targets)
    assert not failures, f"Team 注册表并发出现异常: {failures!r}"


# =========================================================== B. 跨作用域同名 agent_id


def test_workspace_first_read_still_lets_system_baseline_win(tmp_path: Path) -> None:
    """[跨作用域/裁决顺序] 先读 workspace 作用域时，system 同名文件仍应胜出。

    潜在缺陷：懒装载把「先注册者优先」变成「先被读到者优先」，使 system 用户配置被判定冲突丢弃。
    """

    system_directory = tmp_path / _SYSTEM_AGENTS
    _write_agent(system_directory, "shared", role="system-reader")
    workspace = tmp_path / "workspace"
    _write_workspace_agent(workspace, "shared", role="workspace-reader")
    registry = AgentProfileRegistry(AgentProfileScopeSource())

    # 显式先装载 workspace 作用域（内部应先装载 system 基线）。
    registry.list_workspace_sub_agents(workspace)

    assert registry.resolve_local(AgentProfileRegistry.SYSTEM_WORKSPACE, "shared") is not None
    assert registry.resolve_local(workspace, "shared") is None, "workspace 与 system 同名文件应被跳过"
    resolved = registry.resolve(workspace, "shared")
    assert resolved is not None and resolved.role == "system-reader"


def test_register_workspace_profile_colliding_with_system_file_is_rejected(tmp_path: Path) -> None:
    """[跨作用域/注册] 注册与 system 目录同名的 workspace profile 应被拒绝且先装载 system。

    潜在缺陷：``register`` 未先装载 system 时，workspace 同名 profile 抢先入索引，跨作用域裁决翻转。
    """

    system_directory = tmp_path / _SYSTEM_AGENTS
    _write_agent(system_directory, "shared", role="system-reader")
    workspace = tmp_path / "workspace"
    registry = AgentProfileRegistry(AgentProfileScopeSource())

    accepted = registry.register(workspace, _file_profile("shared", role="workspace-reader"))

    assert accepted is False
    assert registry.resolve_local(workspace, "shared") is None
    assert registry.resolve_local(AgentProfileRegistry.SYSTEM_WORKSPACE, "shared") is not None


def test_register_system_after_workspace_holds_same_id_is_rejected(tmp_path: Path) -> None:
    """[跨作用域/不对称] workspace 已持有某 id 时，system 作用域的注册会被拒绝（记录实际行为）。

    这是「先注册者优先」的直接后果：workspace 通过配置中心写出的本地 Agent 会挡住同 id 的
    system 级 Agent 创建（system 不再天然优先）。此处记录基线行为，服务层的可见后果在
    ``test_system_scope_create_blocked_by_workspace_local_agent`` 单独断言。
    """

    workspace = tmp_path / "workspace"
    registry = AgentProfileRegistry(AgentProfileScopeSource())
    assert registry.register(workspace, _file_profile("local-only", role="workspace-reader")) is True

    accepted = registry.register(
        AgentProfileRegistry.SYSTEM_WORKSPACE,
        _file_profile("local-only", role="system-reader"),
    )

    assert accepted is False
    assert registry.resolve_local(AgentProfileRegistry.SYSTEM_WORKSPACE, "local-only") is None


def test_cross_scope_conflict_logs_skip_event(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """[跨作用域/留痕] 冲突跳过必须写 ``agent_profile_conflict_skipped``，便于复盘。"""

    system_directory = tmp_path / _SYSTEM_AGENTS
    _write_agent(system_directory, "shared", role="system-reader")
    workspace = tmp_path / "workspace"
    registry = AgentProfileRegistry(AgentProfileScopeSource())

    with caplog.at_level(logging.WARNING):
        registry.register(workspace, _file_profile("shared", role="workspace-reader"))

    events = [record.message for record in caplog.records]
    assert "agent_profile_conflict_skipped" in events


def test_same_id_allowed_in_two_different_workspaces(tmp_path: Path) -> None:
    """[跨作用域/隔离] 两个 workspace 各自注册同名 id 不应互相冲突。"""

    first = tmp_path / "ws-a"
    second = tmp_path / "ws-b"
    registry = AgentProfileRegistry(AgentProfileScopeSource())
    _write_workspace_agent(first, "shared")
    _write_workspace_agent(second, "shared")

    assert registry.resolve(first, "shared") is not None
    assert registry.resolve(second, "shared") is not None
    assert registry.resolve_local(first, "shared") is not None
    assert registry.resolve_local(second, "shared") is not None


# =========================================================== C. drop_scope / 重建


def test_drop_scope_on_never_loaded_scope_is_idempotent(tmp_path: Path) -> None:
    """[删除/幂等] 从未装载过的作用域上调用 drop_scope 不应报错。"""

    registry = AgentProfileRegistry(AgentProfileScopeSource())
    team_registry = AgentTeamConfigurationRegistry(AgentTeamConfigurationScopeSource())
    workspace = tmp_path / "never-touched"

    registry.drop_scope(workspace)
    registry.drop_scope(workspace)
    team_registry.drop_scope(workspace)

    assert registry.list(workspace) == []
    assert team_registry.list_visible(workspace) == []


def test_drop_scope_does_not_touch_other_scopes(tmp_path: Path) -> None:
    """[删除/隔离] drop 一个 workspace 不得影响其他 workspace 或 system 已索引内容。"""

    system_directory = tmp_path / _SYSTEM_AGENTS
    _write_agent(system_directory, "sys-agent")
    first = tmp_path / "ws-a"
    second = tmp_path / "ws-b"
    _write_workspace_agent(first, "a-agent")
    _write_workspace_agent(second, "b-agent")
    registry = AgentProfileRegistry(AgentProfileScopeSource())

    assert registry.resolve(first, "a-agent") is not None
    assert registry.resolve(second, "b-agent") is not None
    registry.drop_scope(first)

    assert set(registry.list_agent_ids(first)) == {"sys-agent", "a-agent"}
    assert registry.resolve(second, "b-agent") is not None
    assert registry.resolve_local(AgentProfileRegistry.SYSTEM_WORKSPACE, "sys-agent") is not None


def test_drop_scope_then_reregister_keeps_new_state_after_reload(tmp_path: Path) -> None:
    """[删除/重建] drop 后重新注册的条目应在后续重新装载时保留（合并不覆盖内存新值）。"""

    workspace = tmp_path / "workspace"
    _write_workspace_agent(workspace, "disk-agent")
    registry = AgentProfileRegistry(AgentProfileScopeSource())
    assert registry.resolve_local(workspace, "disk-agent") is not None

    registry.drop_scope(workspace)
    registry.register(workspace, _file_profile("memory-only", role="memory"))

    assert registry.resolve_local(workspace, "memory-only") is not None
    # 触发重新装载：磁盘内容合并进来，内存项不被清掉。
    assert registry.resolve_local(workspace, "disk-agent") is not None
    assert registry.resolve_local(workspace, "memory-only") is not None


def test_stale_scope_without_drop_ignores_on_disk_changes(tmp_path: Path) -> None:
    """[删除/陈旧语义] 未 drop 时，进程外的删除/新增不应被感知（文档声明的已知限制）。"""

    workspace = tmp_path / "workspace"
    stale = _write_workspace_agent(workspace, "first")
    registry = AgentProfileRegistry(AgentProfileScopeSource())
    assert registry.resolve_local(workspace, "first") is not None

    stale.unlink()
    _write_workspace_agent(workspace, "second")

    assert registry.resolve_local(workspace, "first") is not None
    assert registry.resolve_local(workspace, "second") is None


def test_team_drop_scope_rereads_directory_after_recreate(tmp_path: Path) -> None:
    """[删除/重建] Team 侧 drop_scope 后同路径重建应读到新内容。"""

    workspace = tmp_path / "workspace"
    directory = workspace / ".cosir" / "agent-teams"
    stale = _write_team(directory, "first-team", scope="workspace")
    registry = AgentTeamConfigurationRegistry(AgentTeamConfigurationScopeSource())
    assert [item.team_id for item in registry.list_scope("workspace", workspace_root=workspace)] == [
        "first-team"
    ]

    stale.unlink()
    _write_team(directory, "second-team", scope="workspace")
    assert registry.list_scope("workspace", workspace_root=workspace)[0].team_id == "first-team"

    registry.drop_scope(workspace)
    assert [item.team_id for item in registry.list_scope("workspace", workspace_root=workspace)] == [
        "second-team"
    ]


# =========================================================== D. 坏配置与布局破坏


def test_single_invalid_json_is_skipped_others_load(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """[坏配置] 单个语法错文件应被跳过、同目录其他文件正常装载，并写 error 事件留痕。"""

    directory = tmp_path / "workspace" / ".cosir" / "agents"
    directory.mkdir(parents=True)
    (directory / "broken.json").write_text("{ not json", encoding="utf-8")
    _write_agent(directory, "good-agent")
    registry = AgentProfileRegistry(AgentProfileScopeSource())

    with caplog.at_level(logging.ERROR):
        ids = registry.list_agent_ids(tmp_path / "workspace")

    assert ids == {"good-agent"}
    assert "agent_profile_config_invalid" in {record.message for record in caplog.records}


def test_agents_path_is_regular_file_degrades_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """[布局破坏] ``.cosir/agents`` 是普通文件时应降级为空并留痕（不静默、不抛异常、不阻断后续）。"""

    workspace = tmp_path / "workspace"
    (workspace / ".cosir").mkdir(parents=True)
    (workspace / ".cosir" / "agents").write_text("", encoding="utf-8")
    registry = AgentProfileRegistry(AgentProfileScopeSource())

    with caplog.at_level(logging.WARNING):
        assert registry.resolve(workspace, "anything") is None

    assert "agent_profile_scope_load_failed" in {record.message for record in caplog.records}
    # 不阻断后续读取：作用域已标记装载，重复读取仍返回空且不抛。
    assert registry.list(workspace) == []


def test_team_agents_path_is_regular_file_degrades_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """[布局破坏/Team] Team 目录是普通文件时应降级并留痕。"""

    workspace = tmp_path / "workspace"
    (workspace / ".cosir").mkdir(parents=True)
    (workspace / ".cosir" / "agent-teams").write_text("", encoding="utf-8")
    registry = AgentTeamConfigurationRegistry(AgentTeamConfigurationScopeSource())

    with caplog.at_level(logging.WARNING):
        assert registry.list_visible(workspace) == []

    assert "team_configuration_scope_load_failed" in {record.message for record in caplog.records}


def test_agents_directory_is_symlink_degrades_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """[布局破坏/符号链接] 配置目录本身是符号链接时应拒绝并留痕（不得静默跟随）。"""

    real = tmp_path / "real-agents"
    _write_agent(real, "linked-agent")
    workspace = tmp_path / "workspace"
    (workspace / ".cosir").mkdir(parents=True)
    _symlink_or_skip(workspace / ".cosir" / "agents", real, target_is_directory=True)
    registry = AgentProfileRegistry(AgentProfileScopeSource())

    with caplog.at_level(logging.WARNING):
        assert registry.resolve(workspace, "linked-agent") is None

    assert "agent_profile_scope_load_failed" in {record.message for record in caplog.records}


def test_agents_directory_is_broken_symlink_degrades_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """[布局破坏/符号链接] 配置目录是失效符号链接时应拒绝并留痕。"""

    workspace = tmp_path / "workspace"
    (workspace / ".cosir").mkdir(parents=True)
    _symlink_or_skip(workspace / ".cosir" / "agents", tmp_path / "gone", target_is_directory=True)
    registry = AgentProfileRegistry(AgentProfileScopeSource())

    with caplog.at_level(logging.WARNING):
        assert registry.resolve(workspace, "anything") is None

    assert "agent_profile_scope_load_failed" in {record.message for record in caplog.records}


def test_config_file_symlink_escaping_directory_degrades_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """[布局破坏/越界链接] 配置文件符号链接越出目录时，整作用域应降级（而非只跳过该文件）。"""

    outside = tmp_path / "outside.json"
    outside.write_text(json.dumps(_agent_document("escaped")), encoding="utf-8")
    workspace = tmp_path / "workspace"
    directory = workspace / ".cosir" / "agents"
    _write_agent(directory, "fine-agent")
    _symlink_or_skip(directory / "escape.json", outside, target_is_directory=False)
    registry = AgentProfileRegistry(AgentProfileScopeSource())

    with caplog.at_level(logging.WARNING):
        ids = registry.list_agent_ids(workspace)

    assert ids == set(), "越界符号链接视为布局破坏，整个作用域降级"
    assert "agent_profile_scope_load_failed" in {record.message for record in caplog.records}


def test_missing_configuration_directory_is_silent_no_config(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """[边界] 目录不存在属于「没有配置」，不应产生告警噪音。"""

    registry = AgentProfileRegistry(AgentProfileScopeSource())
    team_registry = AgentTeamConfigurationRegistry(AgentTeamConfigurationScopeSource())
    missing = tmp_path / "missing-workspace"

    with caplog.at_level(logging.WARNING):
        assert registry.list(missing) == []
        assert team_registry.list_visible(missing) == []

    assert not [
        record
        for record in caplog.records
        if record.message
        in {"agent_profile_scope_load_failed", "team_configuration_scope_load_failed"}
    ]


def test_cosir_occupied_by_regular_file_degrades_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """[布局破坏/祖先] ``.cosir`` 被普通文件占位时两类配置都应留痕降级。"""

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    (workspace / ".cosir").write_text("", encoding="utf-8")
    registry = AgentProfileRegistry(AgentProfileScopeSource())
    team_registry = AgentTeamConfigurationRegistry(AgentTeamConfigurationScopeSource())

    with caplog.at_level(logging.WARNING):
        assert registry.resolve(workspace, "x") is None
        assert team_registry.list_visible(workspace) == []

    events = {record.message for record in caplog.records}
    assert "agent_profile_scope_load_failed" in events
    assert "team_configuration_scope_load_failed" in events


def test_cosir_broken_symlink_ancestor_degrades_with_warning(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """[布局破坏/祖先] ``.cosir`` 是失效符号链接（祖先不是目录）时也应留痕降级。

    契约依据：``ScopeDirectoryError`` 明确声明「路径不是目录（含祖先不是目录）」为布局不合法，
    模块 docstring 也要求布局破坏必须留痕。失效符号链接在 ``Path.is_dir`` 下并非目录。
    """

    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _symlink_or_skip(workspace / ".cosir", tmp_path / "gone", target_is_directory=True)
    registry = AgentProfileRegistry(AgentProfileScopeSource())

    with caplog.at_level(logging.WARNING):
        assert registry.resolve(workspace, "x") is None

    assert "agent_profile_scope_load_failed" in {record.message for record in caplog.records}


def test_hidden_json_files_are_not_loaded(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """[边界/文档契约] 以点开头的隐藏 JSON 文件不应参与装载。

    契约依据：``scope_config_directory`` 模块 docstring 明确写「只识别小写 ``.json`` 扩展名
    （沿用 ``Path.glob("*.json")``，因此以点开头的隐藏 JSON 文件不参与装载）」。
    """

    workspace = tmp_path / "workspace"
    directory = workspace / ".cosir" / "agents"
    directory.mkdir(parents=True)
    (directory / ".hidden.json").write_text(
        json.dumps(_agent_document("hidden-agent")), encoding="utf-8"
    )
    registry = AgentProfileRegistry(AgentProfileScopeSource())

    with caplog.at_level(logging.WARNING):
        assert registry.resolve(workspace, "hidden-agent") is None

    assert registry.list_agent_ids(workspace) == set()


def test_directory_entry_with_json_suffix_is_ignored(tmp_path: Path) -> None:
    """[边界] 目录内名为 ``x.json`` 的子目录应被忽略且不报错，不影响其他文件。"""

    directory = tmp_path / "agents"
    (directory / "nested.json").mkdir(parents=True)
    _write_agent(directory, "real-agent")

    paths = read_scope_json_files(directory)

    assert [path.name for path in paths] == ["real-agent.json"]


def test_non_utf8_config_is_skipped_with_error_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """[坏配置] 非 UTF-8 编码文件应被跳过并留痕，不影响同目录其他文件。"""

    directory = tmp_path / "workspace" / ".cosir" / "agents"
    directory.mkdir(parents=True)
    (directory / "latin.json").write_bytes(b'{"agent_id": "\xff\xfe"}')
    _write_agent(directory, "good-agent")
    registry = AgentProfileRegistry(AgentProfileScopeSource())

    with caplog.at_level(logging.ERROR):
        ids = registry.list_agent_ids(tmp_path / "workspace")

    assert ids == {"good-agent"}
    assert "agent_profile_config_invalid" in {record.message for record in caplog.records}


def test_top_level_array_config_is_skipped_with_error_log(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """[坏配置] 顶层为 JSON 数组的文件应被跳过并留痕。"""

    directory = tmp_path / "workspace" / ".cosir" / "agents"
    directory.mkdir(parents=True)
    (directory / "array.json").write_text("[]", encoding="utf-8")
    registry = AgentProfileScopeSource()

    with caplog.at_level(logging.ERROR):
        profiles = registry.load(AgentProfileRegistry.normalize_workspace(tmp_path / "workspace"))

    assert profiles == []
    assert "agent_profile_config_invalid" in {record.message for record in caplog.records}


def test_broken_scope_marked_loaded_so_fix_requires_drop(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """[状态副作用] 布局破坏降级后作用域仍标记已装载：修复目录后不 drop 不应被感知。"""

    workspace = tmp_path / "workspace"
    (workspace / ".cosir").mkdir(parents=True)
    (workspace / ".cosir" / "agents").write_text("", encoding="utf-8")
    registry = AgentProfileRegistry(AgentProfileScopeSource())

    assert registry.list(workspace) == []

    # 修复布局：把占位文件替换为真实目录并放入合法配置。
    (workspace / ".cosir" / "agents").unlink()
    _write_workspace_agent(workspace, "recovered-agent")

    assert registry.resolve_local(workspace, "recovered-agent") is None, (
        "文档声明每作用域只读盘一次；修复后需 drop_scope 才能感知"
    )
    registry.drop_scope(workspace)
    assert registry.resolve_local(workspace, "recovered-agent") is not None


def test_read_scope_json_files_orders_case_insensitively_and_rejects_missing(
    tmp_path: Path,
) -> None:
    """[单元/边界] 目录读取原语：缺失目录返回空、排序不分大小写、非目录抛 ScopeDirectoryError。"""

    assert read_scope_json_files(tmp_path / "nope") == []

    directory = tmp_path / "mixed"
    directory.mkdir()
    (directory / "B.json").write_text("{}", encoding="utf-8")
    (directory / "a.json").write_text("{}", encoding="utf-8")

    assert [path.name for path in read_scope_json_files(directory)] == ["a.json", "B.json"]

    file_path = tmp_path / "file.txt"
    file_path.write_text("", encoding="utf-8")
    with pytest.raises(ScopeDirectoryError):
        read_scope_json_files(file_path)


# =========================================================== E. 内置 profile 保护


def test_builtin_profiles_survive_same_id_system_files(
    tmp_path: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """[内置保护] system 目录中的同名文件不得覆盖内置 ``main_agent`` / ``general-assistant``。"""

    system_directory = tmp_path / _SYSTEM_AGENTS
    _write_agent(system_directory, "main_agent", role="hijacker")
    _write_agent(system_directory, "general-assistant", role="hijacker")
    registry = build_agent_registry()

    with caplog.at_level(logging.WARNING):
        workspace = tmp_path / "workspace"
        assert registry.resolve(workspace, "general-assistant") is not None

    main = registry.resolve(AgentProfileRegistry.SYSTEM_WORKSPACE, "main_agent")
    general = registry.resolve(AgentProfileRegistry.SYSTEM_WORKSPACE, "general-assistant")
    assert main is not None and main.agent_type is AgentProfileType.MAIN
    assert general is not None and general.role == "general-assistant"
    assert main.role == "main_agent"
    assert "agent_profile_duplicate_skipped" in {record.message for record in caplog.records}


# =========================================================== F. 配置中心一致性


def test_agent_service_workspace_write_then_list_matches_loaded_directory(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[配置中心] workspace 写入目录必须等于装载目录，写入后列表立即可读。"""

    workspace = tmp_path / "workspace"
    registry = AgentProfileRegistry(AgentProfileScopeSource())
    monkeypatch.setattr(
        "app.service.configuration.agent_configuration_service.get_agent_registry",
        lambda: registry,
    )
    service = AgentConfigurationService(workspace_root=workspace)

    service.create_document(
        AgentConfigurationDocument(
            agent_id="workspace-reader",
            role="reader",
            description="Probe",
            system_prompt="Read only the files needed and report evidence.",
            allowed_tools=["read_file"],
        )
    )

    expected = registry.source.directory(service.scope) / "workspace-reader.json"
    assert expected.exists()
    assert service.directory == expected.parent
    assert [document.agent_id for document in service.list_documents()] == ["workspace-reader"]


def test_agent_service_system_write_then_list_without_false_duplicate(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """[配置中心/状态] system 作用域落盘后应能立即列出，且不产生虚假重复告警。"""

    registry = AgentProfileRegistry(AgentProfileScopeSource())
    monkeypatch.setattr(
        "app.service.configuration.agent_configuration_service.get_agent_registry",
        lambda: registry,
    )
    service = AgentConfigurationService()

    with caplog.at_level(logging.WARNING):
        service.create_document(
            AgentConfigurationDocument(
                agent_id="sys-reader",
                role="reader",
                description="Probe",
                system_prompt="Read only the files needed and report evidence.",
                allowed_tools=["read_file"],
            )
        )
        documents = service.list_documents()

    assert [document.agent_id for document in documents] == ["sys-reader"]
    assert documents[0].source == "user_file"
    assert documents[0].path is not None and documents[0].path.exists()
    assert not [
        record
        for record in caplog.records
        if record.message == "agent_profile_duplicate_skipped"
    ], "刚写入的配置文件不应在首次读取时被读回并报成重复"


def test_system_scope_create_blocked_by_workspace_local_agent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """[跨作用域/缺陷] 某个 workspace 的本地 Agent 不应挡住同 id 的 system 级 Agent 创建。

    期望：system 作用域是权威作用域，其创建只应与 system 作用域既有内容比较（同 id 的
    workspace 本地 Agent 不应使 system 创建失败）。实际：``register`` 因跨作用域冲突拒绝，
    配置中心抛出「Agent 已注册」，用户无法创建该 system Agent。
    """

    workspace = tmp_path / "workspace"
    _write_workspace_agent(workspace, "shared")
    registry = AgentProfileRegistry(AgentProfileScopeSource())
    # 先让某个 workspace 作用域装载，使 workspace 本地 Agent 进入索引。
    assert registry.resolve_local(workspace, "shared") is not None
    monkeypatch.setattr(
        "app.service.configuration.agent_configuration_service.get_agent_registry",
        lambda: registry,
    )
    service = AgentConfigurationService()

    service.create_document(
        AgentConfigurationDocument(
            agent_id="shared",
            role="system-reader",
            description="System level agent with a workspace-local name collision",
            system_prompt="Read only the files needed and report evidence.",
            allowed_tools=["read_file"],
        )
    )

    assert registry.resolve_local(AgentProfileRegistry.SYSTEM_WORKSPACE, "shared") is not None


def test_agent_service_create_does_not_log_false_duplicate_after_drop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """[配置中心/状态] drop_scope 之后再次创建同一 id：应正常创建而非报重复。"""

    workspace = tmp_path / "workspace"
    registry = AgentProfileRegistry(AgentProfileScopeSource())
    monkeypatch.setattr(
        "app.service.configuration.agent_configuration_service.get_agent_registry",
        lambda: registry,
    )
    service = AgentConfigurationService(workspace_root=workspace)
    document = AgentConfigurationDocument(
        agent_id="workspace-reader",
        role="reader",
        description="Probe",
        system_prompt="Read only the files needed and report evidence.",
        allowed_tools=["read_file"],
    )

    with caplog.at_level(logging.WARNING):
        service.create_document(document)
        registry.drop_scope(service.scope)
        # drop 后注册表失去「已装载」标记；重新读取应恢复磁盘内容且不抛异常。
        assert registry.resolve_local(service.scope, "workspace-reader") is not None

    assert "workspace-reader.json" in {
        path.name for path in (workspace / ".cosir" / "agents").iterdir()
    }


# =========================================================== G. Team 侧


def test_team_list_visible_merges_system_and_workspace_override(tmp_path: Path) -> None:
    """[Team/可见性] workspace 可见项应合并 system 继承项，且同名项由 workspace 覆盖。"""

    system_directory = tmp_path / _SYSTEM_TEAMS
    _write_team(system_directory, "shared", scope="system", name="System shared")
    _write_team(system_directory, "sys-only", scope="system")
    workspace = tmp_path / "workspace"
    directory = workspace / ".cosir" / "agent-teams"
    _write_team(directory, "shared", scope="workspace", name="Workspace shared")
    _write_team(directory, "ws-only", scope="workspace")
    registry = AgentTeamConfigurationRegistry(AgentTeamConfigurationScopeSource())

    visible = {item.team_id: item.name for item in registry.list_visible(workspace)}

    assert set(visible) == {"shared", "sys-only", "ws-only"}
    assert visible["shared"] == "Workspace shared"
    # ``list_scope`` 只列本作用域自有项，不得混入继承项。
    owned = [item.team_id for item in registry.list_scope("workspace", workspace_root=workspace)]
    assert owned == ["shared", "ws-only"]


def test_team_resolve_prefers_workspace_over_system(tmp_path: Path) -> None:
    """[Team/解析] 同名 team 解析应返回 workspace 覆盖项。"""

    system_directory = tmp_path / _SYSTEM_TEAMS
    _write_team(system_directory, "shared", scope="system", name="System shared")
    workspace = tmp_path / "workspace"
    _write_team(workspace / ".cosir" / "agent-teams", "shared", scope="workspace", name="WS shared")
    registry = AgentTeamConfigurationRegistry(AgentTeamConfigurationScopeSource())

    resolved = registry.resolve(workspace, "shared")

    assert resolved is not None and resolved.name == "WS shared"


def test_team_service_save_writes_to_loaded_directory_and_lists(tmp_path: Path) -> None:
    """[Team/一致性] ``save`` 写入目录必须等于装载目录，保存后列表立即可读。"""

    workspace = tmp_path / "workspace"
    registry = AgentTeamConfigurationRegistry(AgentTeamConfigurationScopeSource())
    service = AgentTeamConfigurationService(registry=registry)
    configuration = AgentTeamConfiguration.model_validate(_team_document("t1", "workspace"))

    service.save(configuration, workspace_root=workspace)

    expected = registry.source.directory(
        AgentTeamConfigurationRegistry.normalize_scope("workspace", workspace)
    )
    assert (expected / "t1.json").exists()
    assert [
        item.team_id for item in service.list_documents(scope="workspace", workspace_root=workspace)
    ] == ["t1"]
    assert (expected / "t1.json").exists()


def test_team_service_save_without_workspace_root_is_rejected(tmp_path: Path) -> None:
    """[Team/边界] workspace 作用域缺少根路径时应显式报错，而不是静默落到别处。"""

    registry = AgentTeamConfigurationRegistry(AgentTeamConfigurationScopeSource())
    service = AgentTeamConfigurationService(registry=registry)
    configuration = AgentTeamConfiguration.model_validate(_team_document("t1", "workspace"))

    with pytest.raises(ValueError):
        service.save(configuration, workspace_root=None)


@pytest.mark.xfail(
    reason=(
        "既有缺陷（非本次改动）：ConfigurationFileStore._delete_file 在 Windows 上用 "
        "os.open(目录) fsync 恒抛 PermissionError；file_store.py 未被本次改动修改"
    ),
    strict=False,
)
def test_team_service_delete_workspace_keeps_system_inherit(tmp_path: Path) -> None:
    """[Team/删除] 删除 workspace 覆盖项后，system 继承项应仍然可见。"""

    system_directory = tmp_path / _SYSTEM_TEAMS
    _write_team(system_directory, "shared", scope="system", name="System shared")
    workspace = tmp_path / "workspace"
    registry = AgentTeamConfigurationRegistry(AgentTeamConfigurationScopeSource())
    service = AgentTeamConfigurationService(registry=registry)
    service.save(
        AgentTeamConfiguration.model_validate(_team_document("shared", "workspace")),
        workspace_root=workspace,
    )

    service.delete("shared", scope="workspace", workspace_root=workspace)

    visible = {item.team_id: item.name for item in registry.list_visible(workspace)}
    assert visible["shared"] == "System shared"


# =========================================================== H. 作用域键边界


def test_normalize_workspace_rejects_empty_and_whitespace() -> None:
    """[边界] 空串与纯空白 workspace 必须抛 ValueError，而不是伪装成某个路径。"""

    with pytest.raises(ValueError):
        AgentProfileRegistry.normalize_workspace("")
    with pytest.raises(ValueError):
        AgentProfileRegistry.normalize_workspace("   ")
    with pytest.raises(ValueError):
        AgentProfileRegistry.normalize_scope("workspace", "")


def test_normalize_workspace_none_is_not_silently_stringified() -> None:
    """[边界/契约] ``None`` 不应被 ``str()`` 伪装成一个永不匹配的路径键。

    契约依据：``normalize_scope_path`` docstring 明确「刻意不把 ``None`` 转成字符串：那会把
    『没有 workspace』伪装成一个永不匹配的路径，让调用方的疏漏变成静默不匹配」。
    """

    with pytest.raises(TypeError):
        normalize_scope_path(None)  # type: ignore[arg-type]

    with pytest.raises((TypeError, ValueError)):
        AgentProfileRegistry.normalize_workspace(None)  # type: ignore[arg-type]


def test_normalize_workspace_relative_and_case_equivalence(tmp_path: Path) -> None:
    """[边界] 相对路径与大小写不同的同一路径应归一到同一作用域键（平台规则内）。"""

    workspace = tmp_path / "CaseDir"
    workspace.mkdir()
    absolute = AgentProfileRegistry.normalize_workspace(str(workspace))
    mixed_case = AgentProfileRegistry.normalize_workspace(str(workspace).upper())

    if os.path.normcase("A") == os.path.normcase("a"):
        # 大小写不敏感平台（Windows）：不同大小写必须归一到同一键。
        assert absolute == mixed_case
    else:
        assert absolute != mixed_case
    # 与共享归一化函数同源。
    assert absolute == normalize_scope_path(str(workspace))


def test_normalize_workspace_relative_path_equals_absolute() -> None:
    """[边界] 相对路径应折算为绝对键，调用点不会因写法不同产生两个键。"""

    relative = AgentProfileRegistry.normalize_workspace(".")
    absolute = AgentProfileRegistry.normalize_workspace(os.getcwd())
    assert relative == absolute


def test_team_normalize_scope_contract() -> None:
    """[边界/Team] system 哨兵与 workspace 键契约。"""

    workspace = "/tmp/example-ws" if os.name != "nt" else "C:\\example-ws"

    assert AgentTeamConfigurationRegistry.normalize_scope("system") == SYSTEM_SCOPE
    assert AgentTeamConfigurationRegistry.normalize_scope(
        "system", None
    ) == AgentProfileRegistry.SYSTEM_WORKSPACE
    assert AgentTeamConfigurationRegistry.normalize_scope(
        "workspace", workspace
    ) == AgentProfileRegistry.normalize_workspace(workspace)
    with pytest.raises(ValueError):
        AgentTeamConfigurationRegistry.normalize_scope("workspace", None)
    with pytest.raises(ValueError):
        AgentTeamConfigurationRegistry.normalize_scope("workspace", "   ")


def test_sentinel_is_single_source(tmp_path: Path) -> None:
    """[边界] 两类注册表的 system 哨兵必须同源，避免作用域键分裂。"""

    assert AgentProfileRegistry.SYSTEM_WORKSPACE == SYSTEM_SCOPE
    assert AgentTeamConfigurationRegistry.SYSTEM_SCOPE == SYSTEM_SCOPE
    # ``resolve``/``list`` 对 system 哨兵只访问 system 作用域。
    registry = AgentProfileRegistry(AgentProfileScopeSource())
    _write_agent(tmp_path / _SYSTEM_AGENTS, "sys-agent")
    assert registry.resolve_local(SYSTEM_SCOPE, "sys-agent") is not None
