"""上下文占用 listener 的主 Agent 挂载与容错测试。"""

from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from typing import Any

import pytest
from context_test_doubles import (
    ClearingListener,
    MemoryMessageStore,
    MutableSubObjectListener,
    RecordingListener,
    TurnScopedMemoryMessageStore,
    build_agent_profile,
    build_task_record,
    build_turn_record,
    build_turn_record_for_task,
    build_workspace_record,
)

from app.core.context.context_entry import ContextEntry
from app.core.context.context_listener.context_usage_compute_listener import (
    ContextUsageComputeListener,
)
from app.core.context.context_listener.listener_event import ContextEventType, ListenerEvent
from app.core.context.runtime_context_manager import RuntimeContextManager
from app.models import RuntimeMessage


def test_listener_event_exposes_usage_and_total_tokens() -> None:
    """缺失 ``usage`` 或 ``total_tokens`` 初始化会破坏 usage listener。"""
    event = ListenerEvent(
        ContextEventType.ADD_MESSAGE,
        [ContextEntry(message=RuntimeMessage(role="user", content_text="hello"), turn_id=None)],
        3,
        100,
    )

    assert event.usage == 3
    assert event.total_tokens == 100
    assert event.entries[0].turn_id is None


def test_listener_event_carries_entries_with_turn_ownership() -> None:
    """事件载体必须是条目（含 turn 归属），而非裸消息列表。"""
    event = ListenerEvent(
        ContextEventType.ADD_MESSAGE,
        [
            ContextEntry(message=RuntimeMessage(role="system", content_text="sys"), turn_id=None),
            ContextEntry(message=RuntimeMessage(role="user", content_text="hi"), turn_id=42),
        ],
        0,
        100,
    )

    assert [entry.turn_id for entry in event.entries] == [None, 42]
    assert [entry.message.content_text for entry in event.entries] == ["sys", "hi"]


def test_mark_context_changed_passes_entries_with_turn_id() -> None:
    """manager 通知 listener 时必须携带条目及其 turn 归属。"""
    received: list[ListenerEvent] = []

    manager = RuntimeContextManager(
        task_id=77,
        agent_profile=build_agent_profile(main_agent=True),
        total_tokens=100,
        current_turn_id=88,
    )
    manager.add_change_listener(RecordingListener(received))
    manager.add_message(RuntimeMessage(role="user", content_text="hello"))

    assert len(received) == 1
    entries = received[0].entries
    assert [entry.turn_id for entry in entries] == [None, 88]
    assert [entry.message.role for entry in entries] == ["system", "user"]


def test_context_usage_listener_attaches_to_main_agent_and_emits_usage() -> None:
    """主 Agent 被错误过滤时不会产生 usage 事件和 task 回写。"""
    events: list[tuple[Any, Any]] = []
    updates: list[tuple[int, int]] = []
    manager = RuntimeContextManager(
        task_id=7,
        agent_profile=build_agent_profile(main_agent=True),
        total_tokens=100,
    ).add_change_listener(
        ContextUsageComputeListener(
            write_event=lambda event_type, payload: events.append((event_type, payload)),
            update_context_usage=lambda task_id, used: updates.append((task_id, used)),
            task_id=7,
        )
    )

    manager.add_message(RuntimeMessage(role="user", content_text="abcd"))

    assert len(events) == 1
    expected_usage = manager._system_entry.message.estimate_tokens() + 1
    assert events[0][1].used_tokens == expected_usage
    assert events[0][1].total_tokens == 100
    assert updates == [(7, expected_usage)]


def test_context_usage_listener_does_not_attach_to_child_agent() -> None:
    """子 Agent 挂载 usage listener 会把内部任务占用暴露给用户圆环。"""
    events: list[tuple[Any, Any]] = []
    updates: list[tuple[int, int]] = []
    manager = RuntimeContextManager(
        task_id=8,
        agent_profile=build_agent_profile(main_agent=False),
        total_tokens=100,
    ).add_change_listener(
        ContextUsageComputeListener(
            write_event=lambda event_type, payload: events.append((event_type, payload)),
            update_context_usage=lambda task_id, used: updates.append((task_id, used)),
            task_id=8,
        )
    )

    manager.add_message(RuntimeMessage(role="user", content_text="abcd"))

    assert len(manager._listeners) == 0
    assert events == []
    assert updates == []


def test_context_usage_is_isolated_between_tasks() -> None:
    """多个 task 的 usage 统计不能串线。"""
    task_1_events: list[tuple[Any, Any]] = []
    task_2_events: list[tuple[Any, Any]] = []
    updates: list[tuple[int, int]] = []
    manager_1 = RuntimeContextManager(
        task_id=31,
        agent_profile=build_agent_profile(main_agent=True),
        total_tokens=100,
    ).add_change_listener(
        ContextUsageComputeListener(
            write_event=lambda event_type, payload: task_1_events.append((event_type, payload)),
            update_context_usage=lambda task_id, used: updates.append((task_id, used)),
            task_id=31,
        )
    )
    manager_2 = RuntimeContextManager(
        task_id=32,
        agent_profile=build_agent_profile(main_agent=True),
        total_tokens=200,
    ).add_change_listener(
        ContextUsageComputeListener(
            write_event=lambda event_type, payload: task_2_events.append((event_type, payload)),
            update_context_usage=lambda task_id, used: updates.append((task_id, used)),
            task_id=32,
        )
    )

    manager_1.add_message(RuntimeMessage(role="user", content_text="abcd"))
    manager_2.add_message(RuntimeMessage(role="user", content_text="abcdefgh"))
    manager_1.add_message(RuntimeMessage(role="assistant", content_text="ijkl"))

    manager_1_base = manager_1._system_entry.message.estimate_tokens()
    manager_2_base = manager_2._system_entry.message.estimate_tokens()
    assert [payload.used_tokens for _, payload in task_1_events] == [
        manager_1_base + 1,
        manager_1_base + 2,
    ]
    assert [payload.total_tokens for _, payload in task_1_events] == [100, 100]
    assert [payload.used_tokens for _, payload in task_2_events] == [manager_2_base + 2]
    assert [payload.total_tokens for _, payload in task_2_events] == [200]
    assert updates == [
        (31, manager_1_base + 1),
        (32, manager_2_base + 2),
        (31, manager_1_base + 2),
    ]
    assert manager_1.used_tokens == manager_1_base + 2
    assert manager_2.used_tokens == manager_2_base + 2


def test_context_usage_update_failure_is_not_silenced() -> None:
    """task usage 持久化失败必须暴露给调用方。"""
    manager = RuntimeContextManager(
        task_id=33,
        agent_profile=build_agent_profile(main_agent=True),
        total_tokens=100,
    ).add_change_listener(
        ContextUsageComputeListener(
            write_event=lambda event_type, payload: None,
            update_context_usage=lambda task_id, used: (_ for _ in ()).throw(
                RuntimeError("database unavailable")
            ),
            task_id=33,
        )
    )

    try:
        manager.add_message(RuntimeMessage(role="user", content_text="abcd"))
    except RuntimeError as exc:
        assert str(exc) == "database unavailable"
    else:
        raise AssertionError("context usage update failure should propagate")


def test_runtime_write_event_failure_updates_usage_then_propagates() -> None:
    """运行期 usage 事件发送失败不能静默丢失。"""
    updates: list[tuple[int, int]] = []
    events: list[tuple[Any, Any]] = []
    writer_calls = 0

    def write_event(event_type: Any, payload: Any) -> None:
        """第一次写事件失败，后续恢复正常。

        参数:
            event_type: 事件类型。
            payload: 事件 payload。

        返回:
            无。

        异常:
            RuntimeError: 第一次调用时模拟 writer 失败。

        副作用:
            记录调用次数和成功写入的事件。
        """
        nonlocal writer_calls
        writer_calls += 1
        if writer_calls == 1:
            raise RuntimeError("writer failed")
        events.append((event_type, payload))

    manager = RuntimeContextManager(
        task_id=34,
        agent_profile=build_agent_profile(main_agent=True),
        total_tokens=100,
    ).add_change_listener(
        ContextUsageComputeListener(
            write_event=write_event,
            update_context_usage=lambda task_id, used: updates.append((task_id, used)),
            task_id=34,
        )
    )

    try:
        manager.add_message(RuntimeMessage(role="user", content_text="abcd"))
    except RuntimeError as exc:
        assert str(exc) == "writer failed"
    else:
        raise AssertionError("runtime context usage write_event failure should propagate")

    expected_usage = manager._system_entry.message.estimate_tokens() + 1
    assert manager.used_tokens == expected_usage
    manager.add_message(RuntimeMessage(role="assistant", content_text="efgh"))

    expected_usage_after_second_message = expected_usage + 1
    assert updates == [(34, expected_usage), (34, expected_usage_after_second_message)]
    assert manager.used_tokens == expected_usage_after_second_message
    assert events[0][1].used_tokens == expected_usage_after_second_message


def test_excluded_message_is_persisted_but_not_counted_in_usage() -> None:
    """不进入内存上下文的消息应落库但计入 usage。

    删除 ``persist`` 参数后，``add_message`` 始终落库；``include_in_context=False``
    仅控制是否进入内存上下文与 usage 统计，不落库维度已不存在。本测试验证落库完整
    且被排除的消息不污染 usage 圆环。
    """
    events: list[tuple[Any, Any]] = []
    updates: list[tuple[int, int]] = []
    manager = RuntimeContextManager(
        task_id=9,
        agent_profile=build_agent_profile(main_agent=True),
        total_tokens=100,
        store=MemoryMessageStore(),
        current_turn_id=21,
    ).add_change_listener(
        ContextUsageComputeListener(
            write_event=lambda event_type, payload: events.append((event_type, payload)),
            update_context_usage=lambda task_id, used: updates.append((task_id, used)),
            task_id=9,
        )
    )

    manager.add_message(
        RuntimeMessage(role="user", content_text="abcd"),
        include_in_context=False,
    )

    assert len(manager.load_message()) == 1
    assert events == []
    assert updates == []
    assert manager.used_tokens == 0


def test_resume_turn_reuses_current_user_without_duplicate_and_refreshes_blocks() -> None:
    """恢复当前 turn 时应复用轨迹 user，而不是重复追加一条 user 消息。"""
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
    manager.begin_turn(build_turn_record(turn_id=current_turn_id, task_id=9220), mode="resume")
    manager.upsert_current_user_message(
        RuntimeMessage(
            role="user",
            content_text="persisted",
            content_blocks=[
                {"type": "text", "text": "persisted"},
                {"type": "image_url", "image_url": {"url": "data:image/png;base64,x"}},
            ],
        )
    )

    users = [message for message in manager.load_message() if message.type == "human"]
    assert len(users) == 1
    assert users[0].content[1]["type"] == "image_url"
    assert store.appended == []


def test_ensure_get_context_usage_is_isolated_for_concurrent_tasks(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """并发创建不同 task 的 manager 时 usage 事件和回写必须按 task 隔离。

    ``ensure_get_runtime_context_manager`` 内部硬编码使用生产回写实现
    （``_update_task_context_usage``），没有注入点，故此处 monkeypatch 该模块级
    名字才能观测到回写；否则本地 ``updates`` 列表永远为空，断言恒不成立。
    """
    events: list[tuple[int, int, int]] = []
    updates: list[tuple[int, int]] = []

    def _record_update(task_id: int, used: int) -> None:
        """记录 task 回写调用（list.append 是线程安全的，适配并发场景）。"""

        updates.append((task_id, used))

    monkeypatch.setattr(
        "app.core.context.runtime_context_manager._update_task_context_usage",
        _record_update,
    )

    def create_and_write(task_id: int, turn_id: int, content: str) -> tuple[int, int]:
        manager = RuntimeContextManager.ensure_get_runtime_context_manager(
            agent_profile=build_agent_profile(main_agent=True),
            write_event=lambda event_type, payload: events.append(
                (task_id, payload.used_tokens, payload.total_tokens)
            ),
            current_workspace=build_workspace_record(),
            current_task=build_task_record(task_id=task_id),
            store=MemoryMessageStore(),
            turn=build_turn_record_for_task(task_id=task_id, turn_id=turn_id),
        )
        manager.add_message(RuntimeMessage(role="user", content_text=content))
        return manager.task_id, manager.used_tokens

    with ThreadPoolExecutor(max_workers=2) as executor:
        list(
            executor.map(
                lambda args: create_and_write(*args),
                [(9101, 101, "abcd"), (9102, 102, "abcdefgh")],
            )
        )

    assert {task_id for task_id, _, _ in events} == {9101, 9102}
    assert {task_id for task_id, _ in updates} == {9101, 9102}


def test_context_usage_load_history_survives_unavailable_stream_writer(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """load_history 阶段 writer 不可用时不应阻断 manager 创建。

    task 回写同理：生产默认实现 ``_update_task_context_usage`` 在 storage 不可用
    时应降级为日志而非抛出，否则 manager 创建失败会直接阻断整个 turn。此处
    monkeypatch 该回写以观测降级后是否仍完成了上下文加载。
    """
    updates: list[tuple[int, int]] = []

    def _record_update(task_id: int, used: int) -> None:
        """记录 task 回写调用。"""

        updates.append((task_id, used))

    monkeypatch.setattr(
        "app.core.context.runtime_context_manager._update_task_context_usage",
        _record_update,
    )

    manager = RuntimeContextManager.ensure_get_runtime_context_manager(
        agent_profile=build_agent_profile(main_agent=True),
        write_event=lambda event_type, payload: (_ for _ in ()).throw(RuntimeError("no writer")),
        current_workspace=build_workspace_record(),
        current_task=build_task_record(task_id=9001),
        store=MemoryMessageStore([RuntimeMessage(role="user", content_text="abcd")]),
        turn=build_turn_record(turn_id=42, task_id=9001),
    )

    assert manager.used_tokens == manager._system_entry.message.estimate_tokens() + 1
    assert updates == [(9001, manager.used_tokens)]


def test_manager_has_no_messages_projection() -> None:
    """RuntimeContextManager 不应再保存冗余的 messages 列表。"""
    manager = RuntimeContextManager(
        task_id=1,
        agent_profile=build_agent_profile(main_agent=True),
        total_tokens=100,
    )

    assert not hasattr(manager, "messages")
    assert [message.type for message in manager.load_message()] == ["system"]


def test_load_message_reads_system_history_and_active_entries() -> None:
    """模型读取出口应从 system、history 和 active entries 组装完整上下文。"""
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
    manager.load_history()
    manager.add_message(RuntimeMessage(role="user", content_text="active"))

    contents = [message.content for message in manager.load_message()]
    assert contents[1:] == ["history", "active"]


def test_context_listener_snapshot_does_not_replace_manager_context() -> None:
    """listener 修改事件快照时不应修改 manager 的上下文 entries。"""
    manager = RuntimeContextManager(
        task_id=1,
        agent_profile=build_agent_profile(main_agent=True),
        total_tokens=100,
    )

    manager.add_change_listener(ClearingListener())
    manager.add_message(RuntimeMessage(role="user", content_text="original"))

    assert [message.content for message in manager.load_message()][-1] == "original"


def test_listener_cannot_pollute_message_sub_objects_through_snapshot() -> None:
    """listener 篡改快照的可变子对象不得回灌 manager 内部条目。

    ``RuntimeMessage`` 是 frozen dataclass，但 ``metadata`` / ``content_blocks`` 是可变
    容器，只有深拷贝能隔离。本用例固化「深拷贝收口在 :meth:`mark_context_changed` 内部」
    这一契约：若有人把深拷贝退化成 ``list(entries)`` 浅拷贝，条目消息仍是同一对象，
    ``metadata`` 会被污染，本用例必须失败。
    """
    manager = RuntimeContextManager(
        task_id=1,
        agent_profile=build_agent_profile(main_agent=True),
        total_tokens=100,
        current_turn_id=20,
    )
    manager.add_change_listener(MutableSubObjectListener())
    manager.add_message(
        RuntimeMessage(
            role="user",
            content_text="original",
            metadata={"tool_call_id": "call-1"},
            content_blocks=[{"type": "text", "text": "original"}],
        )
    )

    user_entry = manager._active_entries[0]
    assert user_entry.message.metadata == {"tool_call_id": "call-1"}
    assert user_entry.message.content_blocks == [{"type": "text", "text": "original"}]
