"""``DelegationCrud.fail_active_delegations`` 原子恢复契约测试。

回归覆盖 D5：旧实现先 ``list_pending_or_running()`` 快照再逐条 ``mark_failed``，存在
TOCTOU（已在循环外推进为 ``succeeded`` 的记录被误覆盖）与中途崩溃残留活跃记录永久占用
并发额度的风险。新实现用单条 ``UPDATE ... WHERE status IN (active) [AND parent_turn_id=?]
RETURNING id`` 原子置 failed。

本测试以 MagicMock 桩替代 session，验证：
- 生成的 UPDATE 语句必须带 ``status IN (active)`` 条件（保证只动活跃记录）；
- 单 turn 恢复带 ``parent_turn_id`` 过滤，全局恢复不带该过滤；
- 恢复路径不调用 ``list_pending_or_running`` 快照（无 TOCTOU）；
- 返回值直接来自 ``RETURNING id`` 的结果集（不依赖快照）。
"""

from unittest.mock import MagicMock

from app.storage.crud.delegation_crud import (
    ACTIVE_DELEGATION_STATUSES,
    DelegationCrud,
)


def _make_crud_with_session(returning_ids: list[int]) -> tuple[DelegationCrud, MagicMock]:
    """构造 ``DelegationCrud`` 桩，注入可控的 session。

    参数:
        returning_ids: 模拟 ``UPDATE ... RETURNING id`` 返回的主键列表。

    返回:
        (crud, session) 元组；session 暴露给调用方断言执行语句。
    """

    crud = DelegationCrud.__new__(DelegationCrud)
    session = MagicMock()
    result = MagicMock()
    # .all() 返回 [(id,), ...] 形式，对应 RETURNING 单列
    result.all.return_value = [(rid,) for rid in returning_ids]
    session.execute.return_value = result
    session_factory = MagicMock()
    session_factory.begin.return_value.__enter__.return_value = session
    crud._session_factory = session_factory
    return crud, session


def _compiled_text(session: MagicMock) -> str:
    """取本次执行的唯一 UPDATE 语句编译文本。"""

    session.execute.assert_called_once()
    return str(session.execute.call_args[0][0])


def test_scoped_recovery_targets_active_and_parent_turn() -> None:
    """单 turn 恢复：UPDATE 必须带 ``status IN (active)`` 与 ``parent_turn_id`` 过滤。"""

    crud, session = _make_crud_with_session(returning_ids=[7, 8])

    affected = crud.fail_active_delegations(error="recovery", parent_turn_id=1)

    assert affected == [7, 8]
    text = _compiled_text(session)
    assert "status" in text
    assert "IN" in text
    # SQLAlchemy 用绑定参数占位（如 `__[POSTCOMPILE_status_1]`）承载 IN 列表，
    # 编译文本不含状态字面量，故校验占位存在而非逐字面量。
    assert "__[POSTCOMPILE_status_1]" in text or ":status" in text
    assert "parent_turn_id" in text


def test_global_recovery_has_no_parent_turn_filter() -> None:
    """全局恢复（parent_turn_id=None）：UPDATE 不带 parent_turn_id 过滤，仅按活跃状态。"""

    crud, session = _make_crud_with_session(returning_ids=[3])

    affected = crud.fail_active_delegations(error="recovery")

    assert affected == [3]
    text = _compiled_text(session)
    assert "status" in text
    assert "parent_turn_id" not in text


def test_no_snapshot_query_used() -> None:
    """恢复路径不得调用 ``list_pending_or_running`` 快照，保证原子、无 TOCTOU。"""

    crud, _session = _make_crud_with_session(returning_ids=[1, 2])
    spy = MagicMock(side_effect=AssertionError("list_pending_or_running 不应被调用"))
    crud.list_pending_or_running = spy  # type: ignore[assignment]

    crud.fail_active_delegations(error="recovery")
    spy.assert_not_called()


def test_no_active_records_returns_empty() -> None:
    """无活跃记录时返回空列表，且不产生被改动的 id。"""

    crud, session = _make_crud_with_session(returning_ids=[])

    affected = crud.fail_active_delegations(error="recovery", parent_turn_id=99)

    assert affected == []
    session.execute.assert_called_once()
