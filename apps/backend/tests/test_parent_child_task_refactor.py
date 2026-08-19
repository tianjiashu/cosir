"""父子 task 委派重构契约测试（真实存储集成）。

本文件以真实 SQLite（临时库 + WAL/busy_timeout）+ 真实 service / crud 验证
docs/delegation-parent-child-task-refactor.md 规定的 8 项验收契约：

1. 上下文隔离：父 task 的 turn 列表不含子 turn，子 task 只含自己那一 turn。
2. 列表过滤：list_tasks_for_workspace 不含 delegation 子 task；list_child_tasks 能拿到。
3. 删除级联：删父 task 递归删子 task 及关联 turns/messages/events/delegations，无孤儿。
4. 并发：并行 N 个 delegate（N > 额度）额度被拒且无孤儿；N<=额度各自独立子 task。
5. 唯一索引：同一 delegation_id 重入 create_child_task 抛 IntegrityError。
6. 取消：取消父 turn 时子 task 的 child turn 被标记 cancelled。
7. child_task_id 落库：delegation 终态记录含正确 child_task_id。
8. 迁移：initialize_app_schema 自动补列与索引（审查修复的关键回归点）。

所有断言均针对业务契约行为，不依赖任何业务代码内部实现细节；测试只构造数据、
读取持久化结果并断言，绝不修改 app/ 下任何业务代码。
"""

from __future__ import annotations

import tempfile
import threading
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import text
from sqlalchemy.exc import IntegrityError

from app.config.configuration import build_agent_registry
from app.config.settings import Settings
from app.models import TaskRecord
from app.models.runtime_message import RuntimeMessage
from app.service.delegation.delegation_service import DelegationService
from app.service.depends import (
    get_runtime_event_service,
    initialize_service_dependencies,
    reset_service_dependencies,
)
from app.service.task.task_service import TaskService
from app.service.task.turn_service import TurnService
from app.storage.crud.delegation_crud import DelegationCrud
from app.storage.crud.turn_message_crud import TurnMessageCrud
from app.storage.crud.workspace_crud import WorkspaceCrud
from app.storage.engine_cache import _engine_cache
from app.storage.init_schema import initialize_app_schema
from app.storage.store_engines import close_storage


def _make_workspace(workspace_crud: WorkspaceCrud, name: str = "ws") -> str:
    """创建测试 workspace 并返回其标识（tasks 外键依赖）。"""

    record = workspace_crud.create(
        name=f"{name}_name", root_path=str(Path(tempfile.gettempdir()) / name)
    )
    return record.workspace_id


def _seed_minimal_provider_and_model() -> None:
    """为本组测试播种一行 deepseek 厂商 + 一行可用模型（不联网、不调 LLM）。

    参数:
        无。

    返回:
        无。

    异常:
        无（异常由调用方 fixture 捕获；正常路径下 DB 写入不会失败）。

    副作用:
        向 ``providers`` 与 ``models`` 表各插入一行；不输出 ``api_key`` 至日志。
    """

    from app.service.provider.provider_service import ProviderService
    from app.storage.crud.model_entry_crud import ModelEntryCrud

    provider = ProviderService().create_provider(
        name="DeepSeek 测试",
        provider_type="deepseek",
        api_key="sk-test-not-real",
    )
    ModelEntryCrud().create(
        provider_id=provider.provider_id,
        model_name="deepseek/deepseek-v4-flash",
        display_name="deepseek-v4-flash",
        max_context_window=1_000_000,
        supports_thinking=True,
    )


def _create_parent_task(task_service: TaskService, workspace_id: str) -> TaskRecord:
    """用真实 TaskService 创建一个人类用户任务（task_type='user'）。"""

    task = task_service.create_task(
        input_text="parent objective",
        status="open",
        agent_id="developer",
        workspace_id=workspace_id,
    )
    return task_service.get_task(task.task_id)


def _create_turn(
    turn_service: TurnService,
    *,
    task_id: str,
    agent_id: str = "developer",
) -> object:
    """用真实 TurnService 创建一条普通父 task 轮次（更新 latest_turn_id）。

    阶段 1.5 后必须传 ``model_name``，否则 ``create_turn`` 经
    ``ModelResolverService.resolve`` 抛 ``ModelNotConfiguredError``
    （``REASON_MODEL_NOT_SELECTED``）。
    """

    return turn_service.create_turn(
        task_id=task_id,
        input_text="parent work",
        agent_id=agent_id,
        model_name="deepseek/deepseek-v4-flash",
    )


def _create_child_turn(
    turn_service: TurnService,
    *,
    task_id: str,
    parent_turn_id: str,
    delegation_id: str,
    agent_id: str = "delegate_reviewer",
) -> object:
    """用真实 TurnService 创建一条委派子轮次（必须带父 turn 与 delegation 关联）。"""

    return turn_service.create_child_turn(
        task_id=task_id,
        input_text="child work",
        agent_id=agent_id,
        parent_turn_id=parent_turn_id,
        delegation_id=delegation_id,
    )


def _agent_registry():
    """返回进程内 agent registry（delegate_* 子 agent 必须已注册）。"""

    return build_agent_registry()


@pytest.fixture
def real_services(tmp_path: Path):
    """以临时 SQLite 初始化完整存储并返回真实 service 集合。

    参数:
        tmp_path: pytest 临时目录，作为隔离的 SQLite 数据库路径。

    返回:
        包含全部真实 service / crud 与 workspace_id 的字典。

    异常:
        无。

    副作用:
        初始化 storage 并在测试后关闭，避免跨测试连接泄漏。
    """

    Settings.override(
        DATABASE_FILE=tmp_path / "app.sqlite3",
        LOG_DATABASE_FILE=tmp_path / "logs.sqlite3",
        CHECKPOINT_FILE=tmp_path / "langgraph_checkpoints.sqlite",
    )
    initialize_service_dependencies()
    # 阶段 1.5 后 ``TurnService.create_turn`` 经 ``ModelResolverService.resolve``
    # 做 service 期预解析（设计 §6.4 两段式 ①），无 provider + model 行时抛
    # ``ModelNotConfiguredError``。本测试聚焦父子 task 委派编排，不关心模型解析
    # 细节，但需为解析器播种一行可用模型，使 ``_create_turn`` 不被 None 模型拒绝。
    _seed_minimal_provider_and_model()

    task_service = TaskService()
    turn_service = TurnService()
    delegation_service = DelegationService(
        delegation_crud=DelegationCrud(),
        runtime_event_service=get_runtime_event_service(),
    )
    workspace_crud = WorkspaceCrud()

    monkeypatch_map = {
        "app.service.depends.get_task_service": lambda: task_service,
        "app.service.depends.get_turn_service": lambda: turn_service,
        "app.service.depends.get_delegation_service": lambda: delegation_service,
        "app.config.configuration.get_agent_registry": lambda: _agent_registry(),
    }

    yield {
        "task_service": task_service,
        "turn_service": turn_service,
        "delegation_service": delegation_service,
        "workspace_crud": workspace_crud,
        "workspace_id": _make_workspace(workspace_crud),
        "_monkeypatch_map": monkeypatch_map,
    }

    reset_service_dependencies()
    close_storage()
    Settings.load()


def _apply_monkeypatch(monkeypatch, monkeypatch_map: dict[str, object]) -> None:
    """把真实 service 注入 service 依赖入口（按需调用）。"""

    for target, fake in monkeypatch_map.items():
        monkeypatch.setattr(target, fake)


# ---------------------------------------------------------------------------
# 契约 1：上下文隔离
# ---------------------------------------------------------------------------


def test_context_isolation_parent_and_child_turns_separated(real_services, monkeypatch):
    # 测试目的：验证父子 task 模型下，父 task 的 turn 列表不含子 task 的 turn，
    # 子 task 只含自己那一 turn（上下文隔离）。可能发现的缺陷：上下文未隔离、
    # child turn 错误挂在父 task 下。
    _apply_monkeypatch(monkeypatch, real_services["_monkeypatch_map"])
    task_service: TaskService = real_services["task_service"]
    turn_service: TurnService = real_services["turn_service"]
    workspace_id = real_services["workspace_id"]

    parent = _create_parent_task(task_service, workspace_id)
    parent_turn = _create_turn(turn_service, task_id=parent.task_id, agent_id="developer")

    child = task_service.create_child_task(
        parent_task_id=parent.task_id,
        parent_turn_id=parent_turn.turn_id,
        delegation_id=f"del_{uuid4().hex}",
        workspace_id=workspace_id,
        agent_id="delegate_reviewer",
        input_text="child objective",
    )
    child_turn = _create_child_turn(
        turn_service,
        task_id=child.task_id,
        parent_turn_id=parent_turn.turn_id,
        delegation_id="del_x",
    )

    parent_turns = turn_service.list_turns_for_task(parent.task_id)
    child_turns = turn_service.list_turns_for_task(child.task_id)
    parent_turn_ids = [t.turn_id for t in parent_turns]
    child_turn_ids = [t.turn_id for t in child_turns]

    # 上下文隔离：父 task 的 turn 列表必须包含自己的父 turn，且绝不能包含子 task 的
    # child turn（TaskService.create_task 还会建一条首 turn，故父 task 至少 2 条，
    # 不能做严格相等断言，只能用包含/不包含断言边界）。
    assert parent_turn.turn_id in parent_turn_ids
    assert child_turn.turn_id not in parent_turn_ids
    # 子 task 只含自己的那一 turn。
    assert child_turn_ids == [child_turn.turn_id]
    # 反向确认：child turn 的 task_id 是子 task，而非父 task。
    assert child_turn.task_id == child.task_id
    assert child_turn.parent_turn_id == parent_turn.turn_id


# ---------------------------------------------------------------------------
# 契约 2：列表过滤
# ---------------------------------------------------------------------------


def test_list_tasks_for_workspace_excludes_delegation_children(real_services, monkeypatch):
    # 测试目的：验证 workspace 对话列表不含 delegation 子 task，子 task 只经
    # list_child_tasks 取得。可能发现的缺陷：子任务泄漏进侧边栏列表。
    _apply_monkeypatch(monkeypatch, real_services["_monkeypatch_map"])
    task_service: TaskService = real_services["task_service"]
    turn_service: TurnService = real_services["turn_service"]
    workspace_id = real_services["workspace_id"]

    parent = _create_parent_task(task_service, workspace_id)
    parent_turn = _create_turn(turn_service, task_id=parent.task_id, agent_id="developer")
    child = task_service.create_child_task(
        parent_task_id=parent.task_id,
        parent_turn_id=parent_turn.turn_id,
        delegation_id=f"del_{uuid4().hex}",
        workspace_id=workspace_id,
        agent_id="delegate_reviewer",
        input_text="child objective",
    )

    workspace_tasks = task_service.list_tasks_for_workspace(workspace_id)
    assert child.task_id not in {t.task_id for t in workspace_tasks}
    assert all(t.task_type == "user" for t in workspace_tasks)
    assert parent.task_id in {t.task_id for t in workspace_tasks}

    children = task_service.list_child_tasks(parent.task_id)
    assert [c.task_id for c in children] == [child.task_id]
    assert children[0].task_type == "delegation"
    assert children[0].is_child is True


# ---------------------------------------------------------------------------
# 契约 3：删除级联
# ---------------------------------------------------------------------------


def test_delete_parent_cascades_to_child_task_and_related_rows(real_services, monkeypatch):
    # 测试目的：验证删父 task 递归删子 task 及关联 turns/messages/events/
    # delegations，无孤儿。可能发现的缺陷：子 task / 关联行残留成孤儿。
    _apply_monkeypatch(monkeypatch, real_services["_monkeypatch_map"])
    task_service: TaskService = real_services["task_service"]
    turn_service: TurnService = real_services["turn_service"]
    delegation_service: DelegationService = real_services["delegation_service"]
    workspace_id = real_services["workspace_id"]

    parent = _create_parent_task(task_service, workspace_id)
    parent_turn = _create_turn(turn_service, task_id=parent.task_id, agent_id="developer")
    # 业务契约：delegation 记录唯一创建入口是 try_create_pending（先 acquire 额度）。
    # create_child_task 只建 task 不建 delegation 记录，故须先创建 delegation 记录。
    acquire = delegation_service.try_create_pending(
        task_id=parent.task_id,
        parent_turn_id=parent_turn.turn_id,
        parent_agent_id="developer",
        child_agent_id="delegate_reviewer",
        delegation_type="review",
        prompt="child objective",
        effective_tools=("read_file",),
        max_concurrency=Settings.DELEGATION_MAX_CONCURRENCY,
    )
    assert acquire.acquired is True
    delegation_id = acquire.delegation_id
    child = task_service.create_child_task(
        parent_task_id=parent.task_id,
        parent_turn_id=parent_turn.turn_id,
        delegation_id=delegation_id,
        workspace_id=workspace_id,
        agent_id="delegate_reviewer",
        input_text="child objective",
    )
    child_turn = _create_child_turn(
        turn_service,
        task_id=child.task_id,
        parent_turn_id=parent_turn.turn_id,
        delegation_id=delegation_id,
    )

    # 写入一条父 turn 与一条子 turn 的运行时事件，供级联删除校验。
    runtime_event_service = get_runtime_event_service()
    from app.models.enums.event_type import EventType
    from app.models.event.runtime_event import RuntimeEvent
    from app.models.payload.run_started_payload import RunStartedPayload

    for tid, tid_task in (
        (parent_turn.turn_id, parent.task_id),
        (child_turn.turn_id, child.task_id),
    ):
        runtime_event_service.save_event(
            RuntimeEvent(
                event_type=EventType.RUN_STARTED,
                task_id=tid_task,
                turn_id=tid,
                payload=RunStartedPayload(status="running", agent_id="developer"),
            )
        )

    # 标记 delegation 为 running 以验证其被级联删除。
    delegation_service.mark_child_started(
        delegation_id, child_turn.turn_id, child_task_id=child.task_id
    )

    task_service.delete_task(parent.task_id)

    # 父 task 已删除
    with pytest.raises(KeyError):
        task_service.get_task(parent.task_id)
    # 子 task 已递归删除
    with pytest.raises(KeyError):
        task_service.get_task(child.task_id)
    # 关联 turns 已删除（无孤儿）
    assert turn_service.list_turns_for_task(parent.task_id) == []
    assert turn_service.list_turns_for_task(child.task_id) == []
    # 关联 delegation 已删除（按 task_id 清理）
    assert delegation_service.list_by_parent_turn(parent_turn.turn_id) == []
    # 关联 runtime_events 已删除（无孤儿）
    assert runtime_event_service.list_by_task(parent.task_id) == []
    assert runtime_event_service.list_by_task(child.task_id) == []


# ---------------------------------------------------------------------------
# 契约 4：并发
# ---------------------------------------------------------------------------


def _run_one_delegation(
    delegation_service, task_service, parent, parent_turn, workspace_id, max_concurrency
):
    """执行一次「原子 acquire + 创建子 task」并返回 (acquired, child_task_id|None)。"""

    acquire = delegation_service.try_create_pending(
        task_id=parent.task_id,
        parent_turn_id=parent_turn.turn_id,
        parent_agent_id="developer",
        child_agent_id="delegate_reviewer",
        delegation_type="review",
        prompt="work",
        effective_tools=("read_file",),
        max_concurrency=max_concurrency,
    )
    if not acquire.acquired:
        return False, None
    child = task_service.create_child_task(
        parent_task_id=parent.task_id,
        parent_turn_id=parent_turn.turn_id,
        delegation_id=acquire.delegation_id,
        workspace_id=workspace_id,
        agent_id="delegate_reviewer",
        input_text="child objective",
    )
    return True, child.task_id


def test_concurrent_delegation_over_concurrency_rejected_no_orphan(real_services, monkeypatch):
    # 测试目的：并行 N 个 delegate（N > DELEGATION_MAX_CONCURRENCY）时额度被拒返回
    # error 且不产生孤儿 task；并行 N<=额度时各自创建独立子 task。可能发现的缺陷：
    # 并发额度未原子裁决导致超额创建子 task（孤儿数据）。
    _apply_monkeypatch(monkeypatch, real_services["_monkeypatch_map"])
    from app.config.settings import Settings as S

    task_service: TaskService = real_services["task_service"]
    delegation_service: DelegationService = real_services["delegation_service"]
    turn_service: TurnService = real_services["turn_service"]
    workspace_id = real_services["workspace_id"]
    max_concurrency = S.DELEGATION_MAX_CONCURRENCY

    parent = _create_parent_task(task_service, workspace_id)
    parent_turn = _create_turn(turn_service, task_id=parent.task_id, agent_id="developer")

    n = max_concurrency + 3  # 明确超过额度

    results: list[tuple[bool, object | None]] = []
    lock = threading.Lock()

    def _worker():
        ok, cid = _run_one_delegation(
            delegation_service, task_service, parent, parent_turn, workspace_id, max_concurrency
        )
        with lock:
            results.append((ok, cid))

    threads = [threading.Thread(target=_worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    acquired = [r for r in results if r[0]]
    rejected = [r for r in results if not r[0]]

    # 额度恰好放行 max_concurrency 个，其余全部被拒。
    assert (
        len(acquired) == max_concurrency
    ), f"期望恰好 {max_concurrency} 个 acquire 成功，实际 {len(acquired)}"
    assert len(rejected) == n - max_concurrency

    # 被拒的调用绝不能产生孤儿子 task。
    orphan_leak = [r for r in rejected if r[1] is not None]
    assert not orphan_leak, "被拒的并发委派不应创建任何子 task"

    # 已放行的各自创建独立子 task，数量为 max_concurrency，且无重复 task_id。
    child_ids = [r[1] for r in acquired]
    assert len(set(child_ids)) == len(child_ids) == max_concurrency

    # workspace 列表里不应出现这些 delegation 子 task。
    ws_tasks = task_service.list_tasks_for_workspace(workspace_id)
    assert set(child_ids).isdisjoint({t.task_id for t in ws_tasks})


def test_concurrent_delegation_within_concurrency_creates_independent_children(
    real_services, monkeypatch
):
    # 测试目的：并行 N<=额度时各自创建独立子 task，互不干扰。可能发现的缺陷：
    # 并发下子 task 错挂到错误父 task 或 task_id 冲突。
    _apply_monkeypatch(monkeypatch, real_services["_monkeypatch_map"])
    from app.config.settings import Settings as S

    task_service: TaskService = real_services["task_service"]
    delegation_service: DelegationService = real_services["delegation_service"]
    turn_service: TurnService = real_services["turn_service"]
    workspace_id = real_services["workspace_id"]
    max_concurrency = S.DELEGATION_MAX_CONCURRENCY

    n = max_concurrency  # 不超过额度
    parent = _create_parent_task(task_service, workspace_id)
    parent_turn = _create_turn(turn_service, task_id=parent.task_id, agent_id="developer")

    results: list[tuple[bool, object | None]] = []
    lock = threading.Lock()

    def _worker():
        ok, cid = _run_one_delegation(
            delegation_service, task_service, parent, parent_turn, workspace_id, max_concurrency
        )
        with lock:
            results.append((ok, cid))

    threads = [threading.Thread(target=_worker) for _ in range(n)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    acquired = [r for r in results if r[0]]
    assert len(acquired) == n
    child_ids = [r[1] for r in acquired]
    assert len(set(child_ids)) == n
    # 每个子 task 都正确挂在父 task 下
    children = task_service.list_child_tasks(parent.task_id)
    assert sorted(c.task_id for c in children) == sorted(child_ids)


# ---------------------------------------------------------------------------
# 契约 5：唯一索引
# ---------------------------------------------------------------------------


def test_create_child_task_duplicate_delegation_id_raises_integrity_error(
    real_services, monkeypatch
):
    # 测试目的：同一 delegation_id 重入 create_child_task 应触发唯一索引冲突
    # 抛 IntegrityError（并发重入兜底）。可能发现的缺陷：唯一索引缺失导致重复子 task。
    _apply_monkeypatch(monkeypatch, real_services["_monkeypatch_map"])
    task_service: TaskService = real_services["task_service"]
    turn_service: TurnService = real_services["turn_service"]
    workspace_id = real_services["workspace_id"]
    parent = _create_parent_task(task_service, workspace_id)
    # 先创建一条合法 parent turn，否则 create_child_task 会先因 turns 外键失败，
    # 而非期望的 delegation_id 唯一索引冲突。
    parent_turn = _create_turn(turn_service, task_id=parent.task_id, agent_id="developer")
    delegation_id = f"del_{uuid4().hex}"

    first = task_service.create_child_task(
        parent_task_id=parent.task_id,
        parent_turn_id=parent_turn.turn_id,
        delegation_id=delegation_id,
        workspace_id=workspace_id,
        agent_id="delegate_reviewer",
        input_text="child objective",
    )
    assert first.delegation_id == delegation_id

    with pytest.raises(IntegrityError):
        task_service.create_child_task(
            parent_task_id=parent.task_id,
            parent_turn_id=parent_turn.turn_id,
            delegation_id=delegation_id,  # 重复：唯一索引冲突
            workspace_id=workspace_id,
            agent_id="delegate_reviewer",
            input_text="another child objective",
        )


# ---------------------------------------------------------------------------
# 契约 6：取消
# ---------------------------------------------------------------------------


def test_cancel_parent_turn_cancels_child_turn(real_services, monkeypatch):
    # 测试目的：取消父 turn 时子 task 的 child turn 被标记 cancelled。可能发现的
    # 缺陷：取消未级联到子 task 的 child turn。
    _apply_monkeypatch(monkeypatch, real_services["_monkeypatch_map"])
    from app.core.runtime.runner import AgentRuntime
    from app.core.runtime.turn_cancellation_registry import cancellation_registry

    task_service: TaskService = real_services["task_service"]
    turn_service: TurnService = real_services["turn_service"]
    delegation_service: DelegationService = real_services["delegation_service"]
    workspace_id = real_services["workspace_id"]

    parent = _create_parent_task(task_service, workspace_id)
    parent_turn = _create_turn(turn_service, task_id=parent.task_id, agent_id="developer")
    # 业务契约：delegation 记录唯一创建入口是 try_create_pending（先 acquire 额度）。
    # create_child_task 只建 task 不建 delegation 记录，故须先创建 delegation 记录。
    acquire = delegation_service.try_create_pending(
        task_id=parent.task_id,
        parent_turn_id=parent_turn.turn_id,
        parent_agent_id="developer",
        child_agent_id="delegate_reviewer",
        delegation_type="review",
        prompt="child objective",
        effective_tools=("read_file",),
        max_concurrency=Settings.DELEGATION_MAX_CONCURRENCY,
    )
    assert acquire.acquired is True
    delegation_id = acquire.delegation_id
    child = task_service.create_child_task(
        parent_task_id=parent.task_id,
        parent_turn_id=parent_turn.turn_id,
        delegation_id=delegation_id,
        workspace_id=workspace_id,
        agent_id="delegate_reviewer",
        input_text="child objective",
    )
    child_turn = _create_child_turn(
        turn_service,
        task_id=child.task_id,
        parent_turn_id=parent_turn.turn_id,
        delegation_id=delegation_id,
    )
    delegation_service.mark_child_started(
        delegation_id, child_turn.turn_id, child_task_id=child.task_id
    )

    # 构造最小 AgentRuntime 并注入真实 service 依赖入口。
    runtime = object.__new__(AgentRuntime)
    runtime._turn_service = turn_service
    runtime._runtime_event_service = get_runtime_event_service()
    runtime._mark_stable_file_changes = lambda _turn_id: None
    runtime._save_and_publish_runtime_event = lambda event: event

    try:
        cancelled_parent = runtime.cancel_turn(parent_turn.turn_id)
        assert cancelled_parent.status == "cancelled"

        # 子 task 的 child turn 被级联取消
        reloaded_child_turn = turn_service.get_turn(child_turn.turn_id)
        assert reloaded_child_turn.status == "cancelled"
        assert cancellation_registry.is_cancelled(child_turn.turn_id)
    finally:
        cancellation_registry.clear(parent_turn.turn_id)
        cancellation_registry.clear(child_turn.turn_id)


# ---------------------------------------------------------------------------
# 契约 7：child_task_id 落库
# ---------------------------------------------------------------------------


def test_delegation_terminal_record_persists_child_task_id(real_services, monkeypatch):
    # 测试目的：delegation 终态记录（completed）含正确的 child_task_id。可能发现的
    # 缺陷：child_task_id 未随终态落库，导致前端无法跳转子 task。
    _apply_monkeypatch(monkeypatch, real_services["_monkeypatch_map"])
    task_service: TaskService = real_services["task_service"]
    turn_service: TurnService = real_services["turn_service"]
    delegation_service: DelegationService = real_services["delegation_service"]
    workspace_id = real_services["workspace_id"]

    parent = _create_parent_task(task_service, workspace_id)
    parent_turn = _create_turn(turn_service, task_id=parent.task_id, agent_id="developer")
    # 业务契约：delegation 记录唯一创建入口是 try_create_pending（先 acquire 额度）。
    # create_child_task 只建 task 不建 delegation 记录，故须先创建 delegation 记录。
    acquire = delegation_service.try_create_pending(
        task_id=parent.task_id,
        parent_turn_id=parent_turn.turn_id,
        parent_agent_id="developer",
        child_agent_id="delegate_reviewer",
        delegation_type="review",
        prompt="child objective",
        effective_tools=("read_file",),
        max_concurrency=Settings.DELEGATION_MAX_CONCURRENCY,
    )
    assert acquire.acquired is True
    delegation_id = acquire.delegation_id
    child = task_service.create_child_task(
        parent_task_id=parent.task_id,
        parent_turn_id=parent_turn.turn_id,
        delegation_id=delegation_id,
        workspace_id=workspace_id,
        agent_id="delegate_reviewer",
        input_text="child objective",
    )
    child_turn = _create_child_turn(
        turn_service,
        task_id=child.task_id,
        parent_turn_id=parent_turn.turn_id,
        delegation_id=delegation_id,
    )
    delegation_service.mark_child_started(
        delegation_id, child_turn.turn_id, child_task_id=child.task_id
    )
    delegation_service.mark_completed(delegation_id, "child done", child_task_id=child.task_id)

    reloaded = delegation_service._delegation_crud.get(delegation_id)
    assert reloaded.status == "completed"
    assert reloaded.child_task_id == child.task_id
    assert reloaded.child_turn_id == child_turn.turn_id


# ---------------------------------------------------------------------------
# 契约 8：迁移（审查修复关键回归点）
# ---------------------------------------------------------------------------


def test_initialize_schema_migrates_child_task_columns_and_drops_legacy_columns(tmp_path: Path):
    # 测试目的：验证 initialize_app_schema 对存量库（旧 tasks 表含重构前遗留的孤儿列
    # input_text / last_message_preview / latest_turn_id，且无新列与索引）的行为：
    #   1) 自动补齐 task_type / parent_task_id / parent_turn_id / delegation_id 新列；
    #   2) 自动补齐 uq_tasks_delegation_id 与 idx_tasks_parent_task_id 索引；
    #   3) 移除已废弃且无人消费的孤儿列。
    # 可能发现的缺陷：迁移漏建列或索引，或遗漏清理孤儿列导致 NOT NULL 约束失败。
    db_file = tmp_path / "legacy.sqlite3"
    engine = _engine_cache.get(db_file)

    # 构造一个「旧版本」tasks 表：含重构前遗留的孤儿列，且缺失新列与索引。
    with engine.begin() as conn:
        conn.execute(text("DROP TABLE IF EXISTS tasks"))
        conn.execute(
            text(
                """
                CREATE TABLE tasks (
                    task_id TEXT PRIMARY KEY,
                    workspace_id TEXT NOT NULL,
                    agent_id TEXT NOT NULL,
                    input_text TEXT NOT NULL,
                    title TEXT NOT NULL,
                    last_message_preview TEXT NOT NULL,
                    latest_turn_id TEXT,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
                """
            )
        )

    # 执行迁移
    initialize_app_schema(engine)

    with engine.connect() as conn:
        columns = {row[1] for row in conn.execute(text("PRAGMA table_info(tasks)")).fetchall()}
        indexes = [row[1] for row in conn.execute(text("PRAGMA index_list(tasks)")).fetchall()]

    # 新列必须全部补齐
    for expected_col in (
        "task_type",
        "parent_task_id",
        "parent_turn_id",
        "delegation_id",
    ):
        assert expected_col in columns, f"迁移后缺失列 {expected_col}"

    # 孤儿列必须被移除
    for dropped_col in ("input_text", "last_message_preview", "latest_turn_id"):
        assert dropped_col not in columns, f"迁移后残留孤儿列 {dropped_col}"

    # 新索引必须补齐（唯一索引 + 普通索引）
    assert "uq_tasks_delegation_id" in indexes, "迁移后缺失唯一索引 uq_tasks_delegation_id"
    assert "idx_tasks_parent_task_id" in indexes, "迁移后缺失索引 idx_tasks_parent_task_id"

    # 唯一索引必须真正唯一：插入一条 delegation_id 后，重复插入应被拒。
    delegation_id = f"del_{uuid4().hex}"
    with engine.begin() as conn:
        conn.execute(
            text(
                "INSERT INTO tasks (task_id, workspace_id, agent_id, title, status, "
                "created_at, updated_at, task_type, parent_task_id, parent_turn_id, "
                "delegation_id) "
                "VALUES (:tid, 'ws', 'developer', 't', 'open', '0', '0', "
                "'delegation', 'pt', 'pt', :did)"
            ),
            {"tid": f"task_{uuid4().hex}", "did": delegation_id},
        )
        # 重复 delegation_id 必须触发唯一约束冲突
        with pytest.raises(IntegrityError):
            conn.execute(
                text(
                    "INSERT INTO tasks (task_id, workspace_id, agent_id, title, status, "
                    "created_at, updated_at, task_type, parent_task_id, parent_turn_id, "
                    "delegation_id) "
                    "VALUES (:tid, 'ws', 'developer', 't', 'open', '0', '0', "
                    "'delegation', 'pt', 'pt', :did)"
                ),
                {"tid": f"task_{uuid4().hex}", "did": delegation_id},
            )

    _engine_cache.dispose_path(db_file)


# ---------------------------------------------------------------------------
# 契约 1 补充：消息级上下文隔离（turn_message 维度）
# ---------------------------------------------------------------------------


def test_context_isolation_turn_messages_separated(real_services, monkeypatch):
    # 测试目的：验证父子 task 的 turn_message 在存储上以 turn 为边界隔离——
    # 父 turn 与子 turn 各自写入的消息，load_messages 只返回各自 turn 的消息，互不串台。
    # 可能发现的缺陷：消息轨迹未以 turn 为边界，导致父/子上下文污染。
    _apply_monkeypatch(monkeypatch, real_services["_monkeypatch_map"])
    task_service: TaskService = real_services["task_service"]
    turn_service: TurnService = real_services["turn_service"]
    turn_message_crud = TurnMessageCrud()
    workspace_id = real_services["workspace_id"]

    parent = _create_parent_task(task_service, workspace_id)
    parent_turn = _create_turn(turn_service, task_id=parent.task_id, agent_id="developer")

    child = task_service.create_child_task(
        parent_task_id=parent.task_id,
        parent_turn_id=parent_turn.turn_id,
        delegation_id=f"del_{uuid4().hex}",
        workspace_id=workspace_id,
        agent_id="delegate_reviewer",
        input_text="child objective",
    )
    child_turn = _create_child_turn(
        turn_service,
        task_id=child.task_id,
        parent_turn_id=parent_turn.turn_id,
        delegation_id="del_m",
    )

    parent_msg = RuntimeMessage(role="assistant", content_text="parent message")
    child_msg = RuntimeMessage(role="assistant", content_text="child message")
    turn_message_crud.append_message(parent_turn.turn_id, parent_msg, sequence=1)
    turn_message_crud.append_message(child_turn.turn_id, child_msg, sequence=1)

    parent_loaded = turn_message_crud.load_messages(parent_turn.turn_id)
    child_loaded = turn_message_crud.load_messages(child_turn.turn_id)

    # 父 turn 只返回父消息，子 turn 只返回子消息，消息级隔离成立。
    assert [m.content_text for m in parent_loaded] == ["parent message"]
    assert [m.content_text for m in child_loaded] == ["child message"]


# ---------------------------------------------------------------------------
# 契约 3 补充：删子 task 不波及父 task（反向级联不应发生）
# ---------------------------------------------------------------------------


def test_delete_child_task_does_not_delete_parent(real_services, monkeypatch):
    # 测试目的：验证删除子 task 只删其自身子树，不影响父 task（反向级联不应发生）。
    # 可能发现的缺陷：删除逻辑误把 parent_task_id 当作级联方向，删子时连带删父。
    _apply_monkeypatch(monkeypatch, real_services["_monkeypatch_map"])
    task_service: TaskService = real_services["task_service"]
    turn_service: TurnService = real_services["turn_service"]
    workspace_id = real_services["workspace_id"]

    parent = _create_parent_task(task_service, workspace_id)
    parent_turn = _create_turn(turn_service, task_id=parent.task_id, agent_id="developer")
    child = task_service.create_child_task(
        parent_task_id=parent.task_id,
        parent_turn_id=parent_turn.turn_id,
        delegation_id=f"del_{uuid4().hex}",
        workspace_id=workspace_id,
        agent_id="delegate_reviewer",
        input_text="child objective",
    )

    task_service.delete_task(child.task_id)

    # 父 task 仍然存在且可读取。
    assert task_service.get_task(parent.task_id).task_id == parent.task_id
    # 子 task 已删除。
    with pytest.raises(KeyError):
        task_service.get_task(child.task_id)
