"""``mark_context_changed`` 条目化（``ContextEntry``）改造的增量契约测试。

覆盖 :class:`test_context_usage_listener` 未覆盖的部分：turn 归属透传矩阵、快照对象
身份隔离、listener 异常下的 ``finally`` 覆盖、边界入参与 listener 排序守卫。占用统计
口径与 task 回写容错的主契约在 ``test_context_usage_listener`` 中覆盖，此处不重复。

共享替身与记录工厂统一从 :mod:`context_test_doubles` 引入，本文件不复制任何替身。
"""

from __future__ import annotations

from typing import Any

import pytest
from context_test_doubles import (
    RecordingListener,
    ThrowingListener,
    TurnScopedMemoryMessageStore,
    build_agent_profile,
    build_turn_record,
)

from app.core.context.context_listener.context_compress_listener import ContextCompressListener
from app.core.context.context_listener.context_listener import ContextListener
from app.core.context.context_listener.context_usage_compute_listener import (
    ContextUsageComputeListener,
)
from app.core.context.context_listener.listener_event import ContextEventType, ListenerEvent
from app.core.context.context_listener.listener_result import ListenerResult
from app.core.context.runtime_context_manager import RuntimeContextManager
from app.models import RuntimeMessage


def test_system_entry_turn_id_is_none_and_active_belongs_to_current_turn() -> None:
    """task 级 system 条目 turn_id 为 None，active 条目归属当前 turn。"""
    received: list[ListenerEvent] = []
    manager = RuntimeContextManager(
        task_id=1,
        agent_profile=build_agent_profile(main_agent=True),
        total_tokens=100,
        current_turn_id=55,
    )
    manager.add_change_listener(RecordingListener(received))
    manager.add_message(RuntimeMessage(role="user", content_text="hi"))

    entries = received[-1].entries
    assert entries[0].turn_id is None
    assert entries[1].turn_id == 55


def test_begin_turn_fresh_propagates_turn_ownership() -> None:
    """fresh 绑定 turn 后，事件条目应含「system(None) + history + active(当前 turn)」。"""
    received: list[ListenerEvent] = []
    store = TurnScopedMemoryMessageStore(
        {10: [RuntimeMessage(role="user", content_text="history")]}
    )
    manager = RuntimeContextManager(
        task_id=1,
        agent_profile=build_agent_profile(main_agent=True),
        total_tokens=100,
        store=store,
        current_turn_id=20,
    )
    manager.add_change_listener(RecordingListener(received))
    manager.load_history()
    manager.begin_turn(build_turn_record(turn_id=20, task_id=1), mode="fresh")
    manager.add_message(RuntimeMessage(role="user", content_text="active"))

    assert [entry.turn_id for entry in received[-1].entries] == [None, 10, 20]


def test_begin_turn_resume_propagates_turn_ownership() -> None:
    """resume 恢复的条目与后续增量都归属被恢复的 turn。"""
    received: list[ListenerEvent] = []
    current_turn_id = 9221
    store = TurnScopedMemoryMessageStore(
        {current_turn_id: [RuntimeMessage(role="user", content_text="persisted")]}
    )
    manager = RuntimeContextManager(
        task_id=9220,
        agent_profile=build_agent_profile(main_agent=True),
        store=store,
        current_turn_id=current_turn_id,
    )
    manager.add_change_listener(RecordingListener(received))
    manager.begin_turn(build_turn_record(turn_id=current_turn_id, task_id=9220), mode="resume")
    manager.add_message(RuntimeMessage(role="assistant", content_text="reply"))

    assert [entry.turn_id for entry in received[-1].entries] == [
        None,
        current_turn_id,
        current_turn_id,
    ]


def test_load_history_excludes_current_turn_from_history_entries() -> None:
    """load_history 必须排除当前 turn，避免把正在执行的轨迹重复计入历史区。"""
    received: list[ListenerEvent] = []
    store = TurnScopedMemoryMessageStore(
        {
            10: [RuntimeMessage(role="user", content_text="history-10")],
            11: [RuntimeMessage(role="assistant", content_text="history-11")],
        }
    )
    manager = RuntimeContextManager(
        task_id=1,
        agent_profile=build_agent_profile(main_agent=True),
        total_tokens=100,
        store=store,
        current_turn_id=11,
    )
    manager.add_change_listener(RecordingListener(received))
    manager.load_history()

    assert [entry.turn_id for entry in received[-1].entries] == [None, 10]


def test_snapshot_entries_are_distinct_objects() -> None:
    """快照条目与其消息必须是深拷贝出的不同对象，而非 manager 内部对象的引用。"""
    received: list[ListenerEvent] = []
    manager = RuntimeContextManager(
        task_id=1,
        agent_profile=build_agent_profile(main_agent=True),
        total_tokens=100,
        current_turn_id=20,
    )
    manager.add_change_listener(RecordingListener(received))
    manager.add_message(RuntimeMessage(role="user", content_text="x"))

    original = manager._active_entries[0]
    snapshot_entry = received[-1].entries[1]
    assert snapshot_entry is not original
    assert snapshot_entry.message is not original.message


def test_listener_exception_still_overrides_used_tokens_via_finally() -> None:
    """listener 抛异常时，``used_tokens`` 仍被 ``result.usage`` 覆盖后向外传播。"""
    manager = RuntimeContextManager(
        task_id=1,
        agent_profile=build_agent_profile(main_agent=True),
        total_tokens=100,
        current_turn_id=20,
    )
    manager.add_change_listener(ThrowingListener(usage_value=123, exc=RuntimeError("boom")))
    with pytest.raises(RuntimeError):
        manager.add_message(RuntimeMessage(role="user", content_text="x"))

    assert manager.used_tokens == 123


def test_upsert_user_message_writer_failure_silent_when_allowed() -> None:
    """upsert 路径的 ``allow_write_event_failure=True`` 时 writer 失败应静默降级。"""
    writer_calls = {"n": 0}

    def write_event(event_type: Any, payload: Any) -> None:
        """首次调用成功，第二次起模拟 writer 不可用。

        参数:
            event_type: 事件类型。
            payload: 事件 payload。

        返回:
            无。

        异常:
            RuntimeError: 第二次及之后调用时模拟 writer 失败。

        副作用:
            累加调用次数。
        """
        writer_calls["n"] += 1
        if writer_calls["n"] > 1:
            raise RuntimeError("no writer")

    manager = RuntimeContextManager(
        task_id=1,
        agent_profile=build_agent_profile(main_agent=True),
        total_tokens=100,
        current_turn_id=20,
    ).add_change_listener(
        ContextUsageComputeListener(
            update_context_usage=lambda task_id, used: None,
            task_id=1,
        )
    )
    manager.add_message(RuntimeMessage(role="user", content_text="first"))
    manager.upsert_current_user_message(
        RuntimeMessage(role="user", content_text="first"),
        allow_write_event_failure=True,
    )

    users = [message for message in manager.load_message() if message.type == "human"]
    assert len(users) == 1
    # Context usage is now a canonical run statistic, not a RuntimeEvent side effect.


def test_entries_turn_id_none_when_no_bound_turn() -> None:
    """未绑定 turn（``current_turn_id=None``）时，条目 turn_id 全部为 None。"""
    received: list[ListenerEvent] = []
    manager = RuntimeContextManager(
        task_id=1,
        agent_profile=build_agent_profile(main_agent=True),
        total_tokens=100,
        current_turn_id=None,
    )
    manager.add_change_listener(RecordingListener(received))
    manager.add_message(RuntimeMessage(role="user", content_text="x"))

    assert all(entry.turn_id is None for entry in received[-1].entries)


def test_mark_context_changed_with_no_listeners_does_not_error() -> None:
    """无订阅者时通知上下文变化不报错，且 used_tokens 保持初值。"""
    manager = RuntimeContextManager(
        task_id=1,
        agent_profile=build_agent_profile(main_agent=True),
        total_tokens=100,
    )
    manager.mark_context_changed(ContextEventType.ADD_MESSAGE, manager._effective_entries())

    assert manager.used_tokens == 0
    assert [message.type for message in manager.load_message()] == ["system"]


def test_empty_history_and_active_entries_snapshot() -> None:
    """清空 history/active 后通知，快照只剩 task 级 system 条目。"""
    received: list[ListenerEvent] = []
    manager = RuntimeContextManager(
        task_id=1,
        agent_profile=build_agent_profile(main_agent=True),
        total_tokens=100,
        current_turn_id=20,
    )
    manager.add_change_listener(RecordingListener(received))
    manager._history_entries = []
    manager._active_entries = []
    manager.mark_context_changed(ContextEventType.LOAD_HISTORY, manager._effective_entries())

    assert received[-1].entries == [manager._system_entry]


def test_mark_context_changed_with_truly_empty_entries() -> None:
    """传入空条目列表（非 ``_effective_entries`` 结果）时不抛错。"""
    manager = RuntimeContextManager(
        task_id=1,
        agent_profile=build_agent_profile(main_agent=True),
        total_tokens=100,
    )
    manager.mark_context_changed(ContextEventType.ADD_MESSAGE, [])

    assert manager.used_tokens == 0


def test_begin_turn_invalid_mode_raises() -> None:
    """begin_turn 传入非法 mode 必须抛 ValueError，避免静默走默认分支。"""
    manager = RuntimeContextManager(
        task_id=1,
        agent_profile=build_agent_profile(main_agent=True),
        total_tokens=100,
    )
    with pytest.raises(ValueError):
        # 负向用例：故意传入非法 mode，字面量类型检查必然报错，故单点豁免。
        manager.begin_turn(build_turn_record(task_id=1), mode="bogus")  # type: ignore[arg-type]


def test_begin_turn_wrong_task_raises() -> None:
    """begin_turn 绑定不属于本 task 的 turn 必须抛 ValueError。"""
    manager = RuntimeContextManager(
        task_id=1,
        agent_profile=build_agent_profile(main_agent=True),
        total_tokens=100,
    )
    with pytest.raises(ValueError):
        manager.begin_turn(build_turn_record(turn_id=5, task_id=999))


def test_listener_order_sorts_by_order() -> None:
    """多个 listener 必须严格按 ``order`` 升序收到条目化事件。"""
    order_seen: list[int] = []

    class _OrderedListener(ContextListener):
        """按构造时给定的 order 记录调用先后。"""

        main_agent_only = False
        order: int

        def __init__(self, order: int) -> None:
            """保存 order。

            参数:
                order: 该 listener 的排序权重。

            返回:
                无。

            异常:
                无。

            副作用:
                保存 order 值。
            """
            self.order = order

        def listen(self, event: ListenerEvent, result: ListenerResult) -> None:
            """记录自己的 order。

            参数:
                event: 监听事件（未使用）。
                result: 累计结果；本实现不修改它。

            返回:
                无。

            异常:
                无。

            副作用:
                向外部列表追加自己的 order。
            """
            order_seen.append(self.order)

    manager = RuntimeContextManager(
        task_id=2,
        agent_profile=build_agent_profile(main_agent=True),
        total_tokens=100,
        current_turn_id=20,
    )
    manager.add_change_listener(_OrderedListener(order=2))
    manager.add_change_listener(_OrderedListener(order=0))
    manager.add_change_listener(_OrderedListener(order=1))
    manager.add_message(RuntimeMessage(role="user", content_text="x"))

    assert order_seen == [0, 1, 2]


def test_compress_listener_coexists_with_usage_listener() -> None:
    """压缩占位 listener（order=0）与 usage listener 共存时，事件仍携带 turn 归属条目。"""
    received: list[ListenerEvent] = []
    manager = RuntimeContextManager(
        task_id=1,
        agent_profile=build_agent_profile(main_agent=True),
        total_tokens=100,
        current_turn_id=20,
    )
    manager.add_change_listener(ContextCompressListener())
    manager.add_change_listener(RecordingListener(received))
    manager.add_message(RuntimeMessage(role="user", content_text="x"))

    entries = received[-1].entries
    assert entries[0].turn_id is None
    assert entries[1].turn_id == 20
