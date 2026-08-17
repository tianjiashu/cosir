"""``runtime_events`` 表主键契约回归测试。

背景：``RuntimeEvent`` 契约允许 ``turn_id=None``（无 turn 归属的事件），但旧表结构
把 ``(turn_id, sequence)`` 设为 NOT NULL 复合主键，``turn_id=None`` 的插入必然触发
``IntegrityError``，且 ``RuntimeEventCrud.save_event`` 用宽 ``except`` 吞异常导致事件
静默丢失（后端审查报告 #9「turn_id=None 持久化必炸被吞」）。

修复：主键改为单列 ``event_id``，``turn_id`` 降为可空普通列，``(turn_id, sequence)``
保留为唯一索引（SQLite 唯一索引不约束 NULL，故多个 None 事件可共存）。

本文件锁定该契约，防止回退；全部用例使用隔离临时库，不污染开发库。
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path
from typing import Any

import pytest
from sqlalchemy.exc import IntegrityError

from app.config.settings import Settings
from app.service.depends import reset_service_dependencies
from app.storage.crud.runtime_event_crud import RuntimeEventCrud
from app.storage.model.runtime_event_model import RuntimeEventModel
from app.storage.store_engines import close_storage, init_storage, main_session_factory
from app.utils.datetime_utils import utc_now


@pytest.fixture
def isolated_storage(tmp_path: Path) -> Iterator[None]:
    """为单个测试提供隔离的 SQLite 存储生命周期。

    参数:
        tmp_path: pytest 提供的当前测试临时目录。

    返回:
        已初始化的隔离存储上下文。

    异常:
        OSError: 如果临时 SQLite 存储无法初始化。

    副作用:
        临时覆盖进程级存储路径，并在测试结束时关闭存储和恢复原配置。
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


def _event_dict(event_id: str, *, turn_id: str | None, sequence: int = 0) -> dict[str, Any]:
    """构造一条最小化 runtime event 字典（契约字段齐备，turn_id 可空）。

    参数:
        event_id: 事件唯一标识。
        turn_id: 轮次标识，无 turn 归属时传 None。
        sequence: 事件序号，默认 0。

    返回:
        可直接传给 ``RuntimeEventCrud.save_event`` 的事件字典。
    """

    return {
        "event_id": event_id,
        "turn_id": turn_id,
        "sequence": sequence,
        "event_type": "RUN_STARTED",
        "task_id": "task-1",
        "payload": {},
        "created_at": utc_now(),
    }


def test_event_id_is_single_primary_key() -> None:
    """主键必须收敛为单列 event_id，防止回退到 (turn_id, sequence) 复合主键。

    参数:
        无。

    返回:
        无。

    异常:
        无。

    副作用:
        无（仅检查表结构元数据）。
    """

    assert [col.name for col in RuntimeEventModel.__table__.primary_key] == ["event_id"]
    assert RuntimeEventModel.__table__.c.turn_id.nullable is True


def test_none_turn_id_event_persists(isolated_storage) -> None:
    """turn_id=None 事件可正常落库并读回（原「存储必炸」场景回归）。

    参数:
        isolated_storage: 隔离 SQLite 存储 fixture。

    返回:
        无。

    异常:
        无。

    副作用:
        向隔离数据库写入一条 turn_id=None 的事件。
    """

    crud = RuntimeEventCrud()
    crud.save_event(_event_dict("evt-none-1", turn_id=None))

    events = crud.list_by_task("task-1")
    assert len(events) == 1
    assert events[0]["event_id"] == "evt-none-1"
    assert events[0]["turn_id"] is None


def test_multiple_none_turn_events_coexist(isolated_storage) -> None:
    """多个 turn_id=None 事件可共存（(NULL, 0) 不触发唯一索引冲突）。

    参数:
        isolated_storage: 隔离 SQLite 存储 fixture。

    返回:
        无。

    异常:
        无。

    副作用:
        向隔离数据库写入两条 turn_id=None 的事件。
    """

    crud = RuntimeEventCrud()
    crud.save_event(_event_dict("evt-none-a", turn_id=None))
    crud.save_event(_event_dict("evt-none-b", turn_id=None))

    events = crud.list_by_task("task-1")
    assert {e["event_id"] for e in events} == {"evt-none-a", "evt-none-b"}


def test_duplicate_event_id_rejected(isolated_storage) -> None:
    """event_id 主键唯一性仍生效：同 event_id 二次插入触发 IntegrityError。

    参数:
        isolated_storage: 隔离 SQLite 存储 fixture。

    返回:
        无。

    异常:
        sqlalchemy.exc.IntegrityError: 期望重复 event_id 的插入被拒绝。

    副作用:
        向隔离数据库写入一条事件，并尝试写入重复 event_id 的行（失败回滚）。
    """

    crud = RuntimeEventCrud()
    crud.save_event(_event_dict("evt-dup", turn_id="turn-1", sequence=1))
    with pytest.raises(IntegrityError), main_session_factory()() as session:
        session.add(
            RuntimeEventModel(
                turn_id="turn-2",
                sequence=1,
                event_id="evt-dup",
                event_type="RUN_STARTED",
                task_id="task-1",
                payload_json="{}",
                created_at=utc_now(),
            )
        )
        session.commit()


def test_same_turn_same_sequence_conflict(isolated_storage) -> None:
    """同 turn 内 sequence 唯一约束仍生效：(turn_id, sequence) 唯一索引保留。

    参数:
        isolated_storage: 隔离 SQLite 存储 fixture。

    返回:
        无。

    异常:
        sqlalchemy.exc.IntegrityError: 期望同 turn 同 sequence 的插入被拒绝。

    副作用:
        向隔离数据库写入一条事件，并尝试写入同 turn 同 sequence 的行（失败回滚）。
    """

    crud = RuntimeEventCrud()
    crud.save_event(_event_dict("evt-a", turn_id="turn-1", sequence=3))
    with pytest.raises(IntegrityError), main_session_factory()() as session:
        session.add(
            RuntimeEventModel(
                turn_id="turn-1",
                sequence=3,
                event_id="evt-b",
                event_type="RUN_STARTED",
                task_id="task-1",
                payload_json="{}",
                created_at=utc_now(),
            )
        )
        session.commit()


def test_next_sequence_unaffected_by_none_turn_events(isolated_storage) -> None:
    """None 事件不参与 turn 内 sequence 分配，正常 turn 的序列自 0 起。

    参数:
        isolated_storage: 隔离 SQLite 存储 fixture。

    返回:
        无。

    异常:
        无。

    副作用:
        向隔离数据库写入一条 None 事件和一条 turn-1 事件。
    """

    crud = RuntimeEventCrud()
    crud.save_event(_event_dict("evt-none", turn_id=None))

    sequence = crud.save_event_with_next_sequence(
        _event_dict("evt-turn1", turn_id="turn-1", sequence=0)
    )
    assert sequence == 0
