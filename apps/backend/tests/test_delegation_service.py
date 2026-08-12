"""delegation service acquire/creation tests."""

import threading
from collections.abc import Iterator
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

from app.config.settings import Settings
from app.models.delegation_record import DelegationRecord
from app.service.agent_runtime_event.runtime_event_service import RuntimeEventService
from app.service.delegation.delegation_acquire_result import REASON_CONCURRENCY_EXCEEDED
from app.service.depends import get_delegation_service, reset_service_dependencies
from app.storage.crud.delegation_crud import DelegationCrud
from app.storage.store_engines import close_storage, init_storage
from app.utils.datetime_utils import utc_now


@pytest.fixture
def isolated_storage(tmp_path: Path) -> Iterator[None]:
    """为单个测试初始化隔离 SQLite 存储。

    参数:
        tmp_path: pytest 提供的临时目录。

    返回:
        已初始化的隔离存储上下文。

    异常:
        OSError: 当临时 SQLite 存储无法初始化时抛出。

    副作用:
        临时覆盖进程级存储路径，并在测试结束后关闭存储、清理 service 单例并恢复配置。
    """

    original_database_file = Settings.DATABASE_FILE
    original_log_database_file = Settings.LOG_DATABASE_FILE
    original_checkpoint_file = Settings.CHECKPOINT_FILE
    reset_service_dependencies()
    close_storage()
    Settings.override(
        DATABASE_FILE=tmp_path / "app.sqlite3",
        LOG_DATABASE_FILE=tmp_path / "logs.sqlite3",
        CHECKPOINT_FILE=tmp_path / "checkpoints.sqlite3",
    )
    init_storage()
    try:
        yield
    finally:
        reset_service_dependencies()
        close_storage()
        Settings.override(
            DATABASE_FILE=original_database_file,
            LOG_DATABASE_FILE=original_log_database_file,
            CHECKPOINT_FILE=original_checkpoint_file,
        )


def test_try_create_pending_acquires_when_below_limit(isolated_storage: None) -> None:
    """验证活跃数未达上限时创建 pending 并发出 started 事件。

    参数:
        isolated_storage: 已初始化的隔离 SQLite 存储 fixture。

    返回:
        无。

    异常:
        AssertionError: 当 acquire 结果、数据库记录或 started 事件不符合预期时由 pytest 抛出。

    副作用:
        向隔离数据库写入一条 pending delegation 记录与一条 delegation_started 事件。
    """

    service = get_delegation_service()
    result = _try_acquire(service, max_concurrency=2)

    assert result.acquired is True
    assert result.delegation_id != ""
    assert result.reason == ""

    records = service.list_by_parent_turn("turn_parent")
    assert len(records) == 1
    assert records[0].delegation_id == result.delegation_id
    assert records[0].status == "pending"

    started = _started_events("turn_parent")
    assert len(started) == 1
    assert started[0]["payload"]["delegation_id"] == result.delegation_id


def test_try_create_pending_rejects_when_limit_reached(isolated_storage: None) -> None:
    """验证活跃数已达上限时拒绝 acquire 且不创建记录、不发出事件。

    参数:
        isolated_storage: 已初始化的隔离 SQLite 存储 fixture。

    返回:
        无。

    异常:
        AssertionError: 当拒绝结果、原因或残留记录不符合预期时由 pytest 抛出。

    副作用:
        向隔离数据库写入一条 pending delegation 记录（首次成功 acquire）。
    """

    service = get_delegation_service()
    first = _try_acquire(service, max_concurrency=1)
    assert first.acquired is True

    second = _try_acquire(service, max_concurrency=1)
    assert second.acquired is False
    assert second.delegation_id == ""
    assert second.reason == REASON_CONCURRENCY_EXCEEDED

    assert len(service.list_by_parent_turn("turn_parent")) == 1
    assert len(_started_events("turn_parent")) == 1


def test_try_create_pending_concurrent_acquire_only_one_succeeds(isolated_storage: None) -> None:
    """并发两个 acquire（max=1）时恰好一个成功，库中仅一条 active 记录。

    使用真实多线程（ThreadPoolExecutor）+ 栅栏（Barrier）同步碰撞，配合真实 SQLite
    （隔离存储 fixture），验证 count + insert 的原子性；断言「成功 1 + 拒绝 1」且库中
    仅 1 条记录。不做任何 mock 自证。

    参数:
        isolated_storage: 已初始化的隔离 SQLite 存储 fixture。

    返回:
        无。

    异常:
        AssertionError: 当并发 acquire 结果或库中记录数不符合预期时由 pytest 抛出。

    副作用:
        每轮向隔离数据库写入一条 pending delegation 记录与一条 delegation_started 事件。
    """

    service = get_delegation_service()
    for round_no in range(5):
        parent_turn_id = f"turn_parent_{round_no}"
        barrier = threading.Barrier(2)

        def acquire(
            idx: int,
            barrier: threading.Barrier = barrier,
            pturn: str = parent_turn_id,
            rno: int = round_no,
        ) -> bool:
            barrier.wait()
            return _try_acquire(
                service,
                parent_turn_id=pturn,
                prompt=f"review {rno}-{idx}",
                max_concurrency=1,
            ).acquired

        with ThreadPoolExecutor(max_workers=2) as pool:
            acquired = list(pool.map(acquire, range(2)))

        assert sorted(acquired) == [False, True], f"round {round_no}: acquired={acquired}"
        assert len(service.list_active_by_parent_turn(parent_turn_id)) == 1
        assert len(_started_events(parent_turn_id)) == 1


def test_create_pending_thin_wrapper_still_creates_and_emits(isolated_storage: None) -> None:
    """验证 create_pending 薄封装仍保持纯创建 + 发事件语义（回归护栏）。

    参数:
        isolated_storage: 已初始化的隔离 SQLite 存储 fixture。

    返回:
        无。

    异常:
        AssertionError: 当纯创建结果、数据库记录或 started 事件不符合预期时由 pytest 抛出。

    副作用:
        向隔离数据库写入一条 pending delegation 记录与一条 delegation_started 事件。
    """

    service = get_delegation_service()
    delegation_id = service.create_pending(
        task_id="task_1",
        parent_turn_id="turn_parent",
        parent_agent_id="developer",
        child_agent_id="delegate_reviewer",
        delegation_type="review",
        prompt="review",
        effective_tools=("read_file",),
    )

    assert delegation_id != ""
    records = service.list_by_parent_turn("turn_parent")
    assert len(records) == 1
    assert records[0].delegation_id == delegation_id
    assert records[0].status == "pending"

    started = _started_events("turn_parent")
    assert len(started) == 1
    assert started[0]["payload"]["delegation_id"] == delegation_id


def test_crud_create_pending_if_slot_available_slot_semantics(isolated_storage: None) -> None:
    """验证 CRUD 原子方法单测语义：额度未满返回 id、额度已满返回 None。

    参数:
        isolated_storage: 已初始化的隔离 SQLite 存储 fixture。

    返回:
        无。

    异常:
        AssertionError: 当原子方法的返回或残留记录不符合预期时由 pytest 抛出。

    副作用:
        向隔离数据库写入一条 pending delegation 记录。
    """

    crud = DelegationCrud()
    first = crud.create_pending_if_slot_available(_pending_record("del_1", "turn_parent"), 1)
    assert first == "del_1"
    second = crud.create_pending_if_slot_available(_pending_record("del_2", "turn_parent"), 1)
    assert second is None
    assert len(crud.list_by_parent_turn("turn_parent")) == 1


def _try_acquire(
    service,
    *,
    parent_turn_id: str = "turn_parent",
    prompt: str = "review",
    max_concurrency: int,
) -> object:
    """以固定委派参数调用一次 try_create_pending。

    参数:
        service: DelegationService 实例。
        parent_turn_id: 本次 acquire 使用的父 turn 标识。
        prompt: 本次 acquire 使用的任务文本。
        max_concurrency: 本次 acquire 使用的并发上限。

    返回:
        DelegationAcquireResult。

    异常:
        sqlalchemy.exc.SQLAlchemyError: 如果底层持久化失败。

    副作用:
        视额度情况向隔离数据库写入一条 pending delegation 记录。
    """

    return service.try_create_pending(
        task_id="task_1",
        parent_turn_id=parent_turn_id,
        parent_agent_id="developer",
        child_agent_id="delegate_reviewer",
        delegation_type="review",
        prompt=prompt,
        effective_tools=("read_file",),
        max_concurrency=max_concurrency,
    )


def _started_events(parent_turn_id: str) -> list[dict]:
    """查询某 parent turn 下已持久化的 delegation_started 事件。

    参数:
        parent_turn_id: 父 turn 标识。

    返回:
        event_type 为 ``delegation_started`` 的事件字典列表。

    异常:
        RuntimeError: 如果底层事件查询失败。

    副作用:
        读取 runtime_events 表。
    """

    return [
        event
        for event in RuntimeEventService().list_by_turn(parent_turn_id)
        if event["event_type"] == "delegation_started"
    ]


def _pending_record(delegation_id: str, parent_turn_id: str) -> DelegationRecord:
    """构造一条 pending delegation 测试记录。

    参数:
        delegation_id: 测试记录标识。
        parent_turn_id: 测试记录所属父 turn。

    返回:
        状态为 ``pending`` 的 DelegationRecord。

    异常:
        无。

    副作用:
        无。
    """

    now = utc_now()
    return DelegationRecord(
        delegation_id=delegation_id,
        task_id="task_1",
        parent_turn_id=parent_turn_id,
        child_turn_id="",
        parent_agent_id="developer",
        child_agent_id="delegate_reviewer",
        delegation_type="review",
        status="pending",
        prompt="review",
        summary="",
        error="",
        effective_tools=("read_file",),
        created_at=now,
        updated_at=now,
    )
