"""``DelegationCrud.create_pending_if_slot_available`` 自增 id 回填契约测试。

回归覆盖 D1：原生 ``insert(...).values(**record.to_model_dict())`` 不会回填
``DelegationRecord.id``，若方法返回 ``record.id`` 则恒为 ``None``，导致整条委派
链路（mark_child_started → update_status(id,...)）失效。修复后方法应返回数据库
自增主键（``Result.lastrowid``），且满额路径返回 ``None`` 语义不变。

测试以 MagicMock 桩替代原生连接与 Result，仅验证返回值契约，不依赖真实 DB。
"""

from unittest.mock import MagicMock

from app.models.delegation_record import DelegationRecord
from app.storage.crud.delegation_crud import DelegationCrud


def _make_record() -> DelegationRecord:
    """构造一个最小可用的 pending delegation 值对象（id 显式为 None）。"""

    return DelegationRecord(
        id=None,
        task_id=1,
        parent_turn_id=1,
        child_turn_id=None,
        child_task_id=None,
        parent_agent_id="developer",
        child_agent_id="researcher",
        status="pending",
        prompt="do something",
        summary="",
        error="",
        effective_tools=("read_file",),
    )


def _make_crud_with_conn(active_count: int, lastrowid: int) -> tuple[DelegationCrud, MagicMock]:
    """构造 ``DelegationCrud`` 桩，注入可控的 engine.connect 行为。

    参数:
        active_count: 模拟当前活跃 delegation 数（用于额度判断）。
        lastrowid: 模拟 ``Result.lastrowid``（成功插入后的自增 id）。

    返回:
        (crud, conn) 元组；conn 暴露给调用方做断言。
    """

    crud = DelegationCrud.__new__(DelegationCrud)
    engine = MagicMock()
    conn = MagicMock()
    result = MagicMock()
    result.lastrowid = lastrowid
    conn.scalar.return_value = active_count
    conn.execute.return_value = result
    engine.connect.return_value.__enter__.return_value = conn
    engine.kw = {"bind": engine}
    crud._session_factory = MagicMock()
    crud._session_factory.kw = {"bind": engine}
    return crud, conn


def test_returns_database_id_not_record_id() -> None:
    """额度未满时返回 ``lastrowid``（真实自增 id），而非传入的 ``record.id``(None)。"""

    crud, _conn = _make_crud_with_conn(active_count=0, lastrowid=42)
    record = _make_record()

    created_id = crud.create_pending_if_slot_available(record, max_concurrency=4)

    assert created_id == 42
    assert created_id is not None
    assert created_id != record.id  # record.id 仍为 None，方法不得返回它


def test_returns_none_when_slot_full() -> None:
    """额度已满时返回 ``None`` 且不写入记录（insert 不被调用）。"""

    crud, conn = _make_crud_with_conn(active_count=4, lastrowid=0)
    record = _make_record()

    created_id = crud.create_pending_if_slot_available(record, max_concurrency=4)

    assert created_id is None
    conn.execute.assert_not_called()
