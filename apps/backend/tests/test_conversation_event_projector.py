"""Conversation event 到 Transport snapshot projector 的回归测试。"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from typing import Any

import pytest

from app.assistant_transport.event import (
    AssistantPartClosedEvent,
    AssistantTextDeltaEvent,
    ContextUsageUpdatedEvent,
    RunInitializedEvent,
    RunStatusChangedEvent,
    ToolCallCreatedEvent,
    ToolCallRuntimeUpdateEvent,
    ToolCallsSettledEvent,
    ToolCallStatusChangedEvent,
    UserInputAppendedEvent,
    build_user_input_parts,
)
from app.assistant_transport.service.conversation_event_projector import (
    ConversationEventProjector,
)
from app.assistant_transport.state.conversation_state_mutation import ConversationStateMutation
from app.assistant_transport.state.conversation_state_snapshot import (
    ConversationStateSnapshot,
    empty_snapshot,
    validate_snapshot,
)
from app.assistant_transport.stream import TransportFrame
from app.config.constant import Constant
from app.core.workflows.conversation_run_usage_stats import ConversationRunUsageStats
from app.models.enums.conversation_run_status import ConversationRunStatus


class InMemorySnapshotService:
    """测试用 snapshot owner，复用生产 mutation 语义但不触碰 SQLite。"""

    def __init__(self) -> None:
        self.states: dict[int, ConversationStateSnapshot] = {}

    def get_state(self, task_id: int) -> ConversationStateSnapshot:
        state = self.states.setdefault(task_id, empty_snapshot())
        return copy.deepcopy(state)

    def apply_planned(self, event: object) -> TransportFrame:
        """按生产口径投影一条事件：由事件自带 ``plan`` 产出 mutation 后落回进程内状态。"""

        task_id = event.task_id
        state = self.get_state(task_id)
        mutations = tuple(event.plan(copy.deepcopy(state)))
        for mutation in mutations:
            _apply_mutation(state, mutation)
        validate_snapshot(state)
        self.states[task_id] = copy.deepcopy(state)
        return TransportFrame(
            task_id=task_id,
            kind="mutation",
            mutations=mutations,
            source_run_id=getattr(event, "run_id", None),
            current_run_id=state["current_run_id"],
            current_run_status=_run(state, state["current_run_id"])["status"]
            if state["current_run_id"] is not None
            else None,
        )


def _apply_mutation(state: ConversationStateSnapshot, mutation: ConversationStateMutation) -> None:
    """在测试内应用生产 snapshot mutation。"""

    parent: Any = state
    if not mutation.path:
        parent.clear()
        parent.update(copy.deepcopy(mutation.value))
        return
    for key in mutation.path[:-1]:
        parent = parent[key]
    key = mutation.path[-1]
    if mutation.kind == "set":
        if isinstance(parent, list) and key == len(parent):
            parent.append(copy.deepcopy(mutation.value))
        else:
            parent[key] = copy.deepcopy(mutation.value)
    else:
        parent[key] += mutation.value


@pytest.fixture
def projector() -> tuple[ConversationEventProjector, InMemorySnapshotService]:
    snapshots = InMemorySnapshotService()
    return ConversationEventProjector(snapshots), snapshots


def _start(projector: ConversationEventProjector) -> None:
    projector.process(RunInitializedEvent(task_id=1, run_id=1))


def _run(state: ConversationStateSnapshot, run_id: int) -> dict[str, Any]:
    return next(run for run in state["runs"] if run["runId"] == run_id)


def test_run_initialized_and_user_input(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    event_projector, snapshots = projector
    _start(event_projector)
    event_projector.process(
        UserInputAppendedEvent(
            task_id=1,
            run_id=1,
            parts=[{"type": "text", "text": "你好", "status": "completed"}],
        )
    )
    state = snapshots.states[1]
    current = _run(state, 1)
    assert state["current_run_id"] == 1
    assert current["status"] == "pending"
    assert current["messages"][0]["parts"][0]["text"] == "你好"
    assert current["messages"][1]["parts"] == []


def test_delegation_status_event_projects_display_data_to_target_tool(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    event_projector, snapshots = projector
    _start(event_projector)
    event_projector.process(
        ToolCallCreatedEvent(
            task_id=1,
            run_id=1,
            tool_call_id="delegate-1",
            tool_name="delegate_task",
        )
    )
    event = ToolCallStatusChangedEvent(
        task_id=1,
        run_id=1,
        tool_call_id="delegate-1",
        status="completed",
        display_data={
            "kind": "delegation-result",
            "child_agent_id": "delegate_reviewer",
            "child_task_id": 22,
            "child_run_id": 220,
            "title": "审查前端",
            "role": "Reviewer",
            "status": "running",
        },
    )

    change = event_projector.process(event)
    part = _run(snapshots.states[1], 1)["messages"][1]["parts"][0]

    assert change is not None
    assert part["display_data"] == {
        "kind": "delegation-result",
        "child_agent_id": "delegate_reviewer",
        "child_task_id": 22,
        "child_run_id": 220,
        "title": "审查前端",
        "role": "Reviewer",
        "status": "running",
    }


def test_user_input_projects_ordered_text_image_and_file_parts(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    event_projector, snapshots = projector
    _start(event_projector)
    parts = [
        {"type": "text", "text": "请看", "status": "completed"},
        {
            "type": "file",
            "file": "cosir-local-file:readme",
            "name": "README.md",
            "contentType": "text/markdown",
        },
        {"type": "text", "text": "和这张图", "status": "completed"},
        {"type": "image", "image": "cosir-attachment://" + "a" * 64},
    ]

    event_projector.process(UserInputAppendedEvent(task_id=1, run_id=1, parts=parts))

    user_parts = _run(snapshots.states[1], 1)["messages"][0]["parts"]
    assert user_parts == parts
    assert all(part.get("path") is None for part in user_parts if isinstance(part, dict))


def test_user_input_supports_attachment_only_and_duplicate_event(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    event_projector, snapshots = projector
    _start(event_projector)
    event = UserInputAppendedEvent(
        task_id=1,
        run_id=1,
        parts=[
            {
                "type": "file",
                "file": "cosir-local-file:only-file",
                "name": "a.txt",
                "contentType": "text/plain",
            }
        ],
    )

    first = event_projector.process(event)
    second = event_projector.process(event)

    assert first is not None
    assert second is None
    assert _run(snapshots.states[1], 1)["messages"][0]["parts"] == event.parts


def test_build_user_input_parts_preserves_mixed_composer_order() -> None:
    """既有 display_text marker 必须恢复文本、图片、普通文件的原始交错顺序。"""

    image_id = "a" * 64
    parts = build_user_input_parts(
        f"前文\n[[cosir-image:{image_id}]]\n中间\n[[cosir-file:readme]]\n后文",
        [f"attachments/{image_id}.png"],
        [
            {
                "id": "readme",
                "name": "README.md",
                "content_type": "text/markdown",
            }
        ],
    )

    assert [part["type"] for part in parts] == ["text", "image", "text", "file", "text"]
    assert parts[1] == {"type": "image", "image": f"cosir-attachment://{image_id}"}
    assert parts[3] == {
        "type": "file",
        "file": "cosir-local-file:readme",
        "name": "README.md",
        "contentType": "text/markdown",
    }


def test_build_user_input_parts_deduplicates_repeated_attachment_markers() -> None:
    """重复提交同一 marker 时，canonical user parts 仍只保留一个附件。"""

    image_id = "b" * 64
    parts = build_user_input_parts(
        f"前[[cosir-image:{image_id}]]中[[cosir-image:{image_id}]]后 "
        "[[cosir-file:readme]][[cosir-file:readme]]",
        [f"attachments/{image_id}.png"],
        [{"id": "readme", "name": "README.md", "content_type": "text/markdown"}],
    )

    assert [part["type"] for part in parts] == ["text", "image", "text", "text", "file"]
    assert sum(part["type"] == "image" for part in parts) == 1
    assert sum(part["type"] == "file" for part in parts) == 1


def test_build_user_input_parts_rejects_unsafe_file_attachment_id() -> None:
    """node_helper 直接被调用时也不能把不安全 id 投影为 file locator。"""

    with pytest.raises(ValueError, match="malformed"):
        build_user_input_parts(
            "[[cosir-file:../../secret]]",
            [],
            [
                {
                    "id": "../../secret",
                    "name": "secret",
                    "content_type": "text/plain",
                }
            ],
        )


def test_assistant_text_and_reasoning_parts(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    event_projector, snapshots = projector
    _start(event_projector)
    event_projector.process(
        AssistantTextDeltaEvent(task_id=1, run_id=1, part="reasoning", delta="先思考")
    )
    event_projector.process(
        AssistantTextDeltaEvent(task_id=1, run_id=1, part="reasoning", delta="一下")
    )
    event_projector.process(AssistantPartClosedEvent(task_id=1, run_id=1, part="reasoning"))
    event_projector.process(AssistantTextDeltaEvent(task_id=1, run_id=1, part="text", delta="答案"))
    parts = _run(snapshots.states[1], 1)["messages"][1]["parts"]
    assert [part["type"] for part in parts] == ["reasoning", "text"]
    assert parts[0]["text"] == "先思考一下"
    assert parts[0]["status"] == "completed"
    assert parts[1]["text"] == "答案"


def test_duplicate_text_delta_is_not_appended_twice(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    event_projector, snapshots = projector
    _start(event_projector)
    event = AssistantTextDeltaEvent(task_id=1, run_id=1, part="text", delta="一次")
    first = event_projector.process(event)
    second = event_projector.process(event)
    assert first is not None and first.mutations
    assert second is None
    assert _run(snapshots.states[1], 1)["messages"][1]["parts"][0]["text"] == "一次"


def test_tool_lifecycle_and_settlement(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    event_projector, snapshots = projector
    _start(event_projector)
    event_projector.process(
        ToolCallCreatedEvent(
            task_id=1,
            run_id=1,
            tool_call_id="call-1",
            tool_name="read_file",
            presentation={
                "verb": "读取文件",
                "icon": "eye",
                "surface": "trace",
                "expandable": False,
                "expand_layout": "none",
                "default_open": False,
            },
        )
    )
    event_projector.process(
        ToolCallStatusChangedEvent(
            task_id=1,
            run_id=1,
            tool_call_id="call-1",
            status="running",
            args={"path": "a.py"},
        )
    )
    event_projector.process(
        ToolCallStatusChangedEvent(
            task_id=1,
            run_id=1,
            tool_call_id="call-1",
            status="completed",
            display_data={"kind": "read-file-meta", "path": "a.py", "total_lines": 10},
        )
    )
    event_projector.process(
        ToolCallCreatedEvent(task_id=1, run_id=1, tool_call_id="call-2", tool_name="write_file")
    )
    event_projector.process(
        ToolCallsSettledEvent(task_id=1, run_id=1, status="failed", reason="runtime_failed")
    )
    parts = _run(snapshots.states[1], 1)["messages"][1]["parts"]
    assert parts[0]["status"] == "completed"
    assert parts[0]["presentation"]["expandable"] is False
    assert parts[0]["display_data"] == {"kind": "read-file-meta", "path": "a.py", "total_lines": 10}
    assert parts[1]["status"] == "failed"
    assert parts[1]["error"] == "执行异常"


def test_cancelled_run_resume_status_is_projected(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    """续跑恢复（cancelled -> running）必须被投影，否则 snapshot 会永久停在终态。"""

    event_projector, snapshots = projector
    _start(event_projector)
    event_projector.process(
        RunStatusChangedEvent(
            task_id=1,
            run_id=1,
            status=ConversationRunStatus.CANCELLED,
            end_reason="user_cancelled",
        )
    )
    assert _run(snapshots.states[1], 1)["status"] == "cancelled"

    event_projector.process(
        RunStatusChangedEvent(task_id=1, run_id=1, status=ConversationRunStatus.RUNNING)
    )

    resumed = _run(snapshots.states[1], 1)
    assert resumed["status"] == "running"
    assert resumed["endReason"] is None


def test_completed_and_failed_runs_reject_reviving_status_event() -> None:
    """completed / failed 仍不可逆：迟到事件不能把已结束的 Run 拉回 active。"""

    for terminal in (ConversationRunStatus.COMPLETED, ConversationRunStatus.FAILED):
        snapshots = InMemorySnapshotService()
        event_projector = ConversationEventProjector(snapshots)
        _start(event_projector)
        event_projector.process(RunStatusChangedEvent(task_id=1, run_id=1, status=terminal))
        event_projector.process(
            RunStatusChangedEvent(task_id=1, run_id=1, status=ConversationRunStatus.RUNNING)
        )

        assert _run(snapshots.states[1], 1)["status"] == terminal.value


def test_tool_part_is_created_after_run_resume(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    """回归：取消后续跑时，新的 tool-call 创建事件必须落成 part 并被状态事件正常迁移。"""

    event_projector, snapshots = projector
    _start(event_projector)
    event_projector.process(
        RunStatusChangedEvent(
            task_id=1,
            run_id=1,
            status=ConversationRunStatus.CANCELLED,
            end_reason="user_cancelled",
        )
    )
    event_projector.process(
        RunStatusChangedEvent(task_id=1, run_id=1, status=ConversationRunStatus.RUNNING)
    )

    event_projector.process(
        ToolCallCreatedEvent(task_id=1, run_id=1, tool_call_id="call-9", tool_name="read_file")
    )
    event_projector.process(
        ToolCallStatusChangedEvent(
            task_id=1,
            run_id=1,
            tool_call_id="call-9",
            status="running",
            args={"path": "a.py"},
        )
    )

    parts = _run(snapshots.states[1], 1)["messages"][1]["parts"]
    assert [part["toolCallId"] for part in parts] == ["call-9"]
    assert parts[0]["status"] == "running"
    assert parts[0]["args"] == {"path": "a.py"}


def test_tool_status_change_without_part_is_skipped(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """part 缺失属于展示事实不完整：跳过投影并记 warning，不得中断整个 Run。"""

    event_projector, snapshots = projector
    _start(event_projector)

    with caplog.at_level("WARNING"):
        change = event_projector.process(
            ToolCallStatusChangedEvent(
                task_id=1,
                run_id=1,
                tool_call_id="missing-call",
                status="running",
            )
        )

    assert change is not None
    assert change.mutations == ()
    assert _run(snapshots.states[1], 1)["messages"][1]["parts"] == []
    assert "tool_call_part_missing_for_status_change" in {
        record.message for record in caplog.records
    }


def test_run_status_usage_and_context_usage(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    event_projector, snapshots = projector
    _start(event_projector)
    event_projector.process(
        ContextUsageUpdatedEvent(
            task_id=1,
            run_id=1,
            ratio=0.42,
            used_tokens=42,
            context_window_tokens=100,
        )
    )
    event_projector.process(
        RunStatusChangedEvent(
            task_id=1,
            run_id=1,
            status=ConversationRunStatus.COMPLETED,
            usage_stats=ConversationRunUsageStats(
                input_tokens=10, output_tokens=4, total_tokens=14
            ),
        )
    )
    state = snapshots.states[1]
    assert _run(state, 1)["usage"]["total_tokens"] == 14
    assert state["current_run_id"] == 1
    assert state["context_usage_ratio"] == 0.42
    assert state["context_usage_used"] == 42
    assert state["context_window_total"] == 100
    assert _run(state, 1)["status"] == "completed"


def test_terminal_usage_is_projected(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    event_projector, snapshots = projector
    _start(event_projector)
    event_projector.process(
        RunStatusChangedEvent(
            task_id=1,
            run_id=1,
            status=ConversationRunStatus.COMPLETED,
            usage_stats=ConversationRunUsageStats(
                input_tokens=100, output_tokens=50, total_tokens=150
            ),
        )
    )
    assert _run(snapshots.states[1], 1)["usage"]["total_tokens"] == 150


def test_new_run_clears_previous_run_usage(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    event_projector, snapshots = projector
    _start(event_projector)
    event_projector.process(
        RunStatusChangedEvent(
            task_id=1,
            run_id=1,
            status=ConversationRunStatus.COMPLETED,
            usage_stats=ConversationRunUsageStats(
                input_tokens=10, output_tokens=4, total_tokens=14
            ),
        )
    )
    event_projector.process(RunInitializedEvent(task_id=1, run_id=2))
    state = snapshots.states[1]
    assert _run(state, 1)["usage"]["total_tokens"] == 14
    assert _run(state, 2)["usage"] is None
    assert state["context_usage_used"] is None
    assert state["context_window_total"] is None


def test_late_previous_run_events_cannot_overwrite_current_run(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    event_projector, snapshots = projector
    _start(event_projector)
    event_projector.process(
        RunStatusChangedEvent(task_id=1, run_id=1, status=ConversationRunStatus.COMPLETED)
    )
    event_projector.process(RunInitializedEvent(task_id=1, run_id=2))
    event_projector.process(
        RunStatusChangedEvent(task_id=1, run_id=1, status=ConversationRunStatus.FAILED)
    )
    state = snapshots.states[1]
    assert _run(state, 2)["status"] == "pending"
    assert _run(state, 1)["status"] == "completed"
    assert _run(state, 1)["usage"] is None


def test_historical_run_status_event_updates_its_own_run(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    event_projector, snapshots = projector
    _start(event_projector)
    event_projector.process(
        RunStatusChangedEvent(
            task_id=1,
            run_id=1,
            status=ConversationRunStatus.COMPLETED,
        )
    )
    event_projector.process(RunInitializedEvent(task_id=1, run_id=2))
    change = event_projector.process(
        RunStatusChangedEvent(
            task_id=1,
            run_id=1,
            status=ConversationRunStatus.COMPLETED,
            end_reason="late_failure",
            usage_stats=ConversationRunUsageStats(input_tokens=4, output_tokens=2, total_tokens=6),
        )
    )
    assert change is not None and change.mutations
    state = snapshots.states[1]
    assert _run(state, 1)["status"] == "completed"
    assert _run(state, 1)["endReason"] == "late_failure"
    assert _run(state, 1)["usage"]["total_tokens"] == 6
    assert _run(state, 2)["status"] == "pending"


def test_unknown_run_event_raises_and_is_not_projected(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    """事件引用快照里不存在的 Run 时按契约 fail-fast，不写入任何 mutation。"""

    event_projector, snapshots = projector
    with pytest.raises(KeyError, match="run 404 not found"):
        event_projector.process(
            RunStatusChangedEvent(
                task_id=1,
                run_id=404,
                status=ConversationRunStatus.COMPLETED,
                usage_stats=ConversationRunUsageStats(
                    input_tokens=1, output_tokens=1, total_tokens=2
                ),
            )
        )
    assert snapshots.states.get(1, empty_snapshot()) == empty_snapshot()


def test_late_run_initialization_cannot_rewind_terminal_newer_run(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    event_projector, snapshots = projector
    _start(event_projector)
    event_projector.process(
        RunStatusChangedEvent(task_id=1, run_id=1, status=ConversationRunStatus.COMPLETED)
    )
    event_projector.process(RunInitializedEvent(task_id=1, run_id=2))
    event_projector.process(
        RunStatusChangedEvent(task_id=1, run_id=2, status=ConversationRunStatus.COMPLETED)
    )
    event_projector.process(RunInitializedEvent(task_id=1, run_id=1))
    assert _run(snapshots.states[1], 2)["status"] == "completed"


def test_unknown_context_measurement_clears_stale_absolute_values(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    event_projector, snapshots = projector
    _start(event_projector)
    event_projector.process(
        ContextUsageUpdatedEvent(
            task_id=1,
            run_id=1,
            ratio=0.9,
            used_tokens=90,
            context_window_tokens=100,
        )
    )
    event_projector.process(ContextUsageUpdatedEvent(task_id=1, run_id=1, ratio=0.0))
    state = snapshots.states[1]
    assert state["context_usage_ratio"] == 0.0
    assert state["context_usage_used"] is None
    assert state["context_window_total"] is None


def test_context_reprojection_replaces_task_usage(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    event_projector, snapshots = projector
    _start(event_projector)
    event_projector.process(
        ContextUsageUpdatedEvent(
            task_id=1,
            run_id=1,
            ratio=0.9,
            used_tokens=90,
            context_window_tokens=100,
        )
    )
    event_projector.process(
        ContextUsageUpdatedEvent(
            task_id=1,
            run_id=1,
            ratio=0.4,
            used_tokens=40,
            context_window_tokens=100,
            reproject=True,
        )
    )
    state = snapshots.states[1]
    assert state["context_usage_ratio"] == 0.4
    assert state["context_usage_used"] == 40


def test_unknown_event_is_ignored(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    event_projector, snapshots = projector
    assert event_projector.process({"type": "future_event", "task_id": 1, "run_id": 1}) is None
    assert snapshots.states == {}


def test_model_chunk_event_becomes_assistant_transport_update() -> None:
    """验证 model chunk → event → mutation frame 的闭环。"""

    snapshots = InMemorySnapshotService()
    event_projector = ConversationEventProjector(snapshots)
    _start(event_projector)
    event = AssistantTextDeltaEvent(task_id=1, run_id=1, part="text", delta="你好")
    change = event_projector.process(event)
    assert change is not None and change.mutations


def _projector_with_window(
    limit: int,
    monkeypatch: pytest.MonkeyPatch,
) -> tuple[ConversationEventProjector, InMemorySnapshotService]:
    """按指定容量构造投影器（容量来源是 ``Constant.Transport``，此处临时覆盖）。"""

    monkeypatch.setattr(Constant.Transport, "EVENT_DEDUP_WINDOW", limit)
    snapshots = InMemorySnapshotService()
    return ConversationEventProjector(snapshots), snapshots


def _assert_reapply_changes_nothing(
    event_projector: ConversationEventProjector,
    snapshots: InMemorySnapshotService,
    factory: Callable[[], object],
) -> None:
    """同一业务事实用新 event_id 再投一次后，快照必须与首次完全一致。

    这证明该事件的 ``plan`` 自幂等：去重窗口淘汰后即使重复投递漏网，也不会改坏快照。
    唯一的例外是 ``RunInitializedEvent(replace_existing=True)``，见对应用例。
    """

    event_projector.process(factory())
    before = copy.deepcopy(snapshots.states[1])
    event_projector.process(factory())
    assert snapshots.states[1] == before


def test_dedup_window_is_bounded_by_configured_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """去重窗口是全局有界 FIFO：条目数不随事件数增长。"""

    event_projector, _ = _projector_with_window(4, monkeypatch)
    _start(event_projector)
    for _ in range(10):
        event_projector.process(
            AssistantTextDeltaEvent(task_id=1, run_id=1, part="text", delta="x")
        )
    assert event_projector._dedup_window.size == 4


def test_dedup_window_is_not_keyed_by_task(monkeypatch: pytest.MonkeyPatch) -> None:
    """窗口不按 task 分桶：task 数量增加也不会突破容量上界。"""

    event_projector, snapshots = _projector_with_window(3, monkeypatch)
    for task_id in range(1, 6):
        event_projector.process(RunInitializedEvent(task_id=task_id, run_id=task_id))
        event_projector.process(
            AssistantTextDeltaEvent(task_id=task_id, run_id=task_id, part="text", delta="x")
        )
    assert event_projector._dedup_window.size == 3
    assert len(snapshots.states) == 5


def test_duplicate_delta_event_is_dropped(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    """同一 event_id 的增量事件只被投影一次。"""

    event_projector, snapshots = projector
    _start(event_projector)
    event = AssistantTextDeltaEvent(task_id=1, run_id=1, part="text", delta="abc")
    assert event_projector.process(event) is not None
    assert event_projector.process(event) is None
    parts = _run(snapshots.states[1], 1)["messages"][1]["parts"]
    assert [part["text"] for part in parts] == ["abc"]


def test_evicted_delta_is_no_longer_deduplicated(monkeypatch: pytest.MonkeyPatch) -> None:
    """超出窗口的旧 event_id 不再去重——这是有界化**记录在案**的取舍。"""

    event_projector, snapshots = _projector_with_window(2, monkeypatch)
    _start(event_projector)
    events = [
        AssistantTextDeltaEvent(task_id=1, run_id=1, part="text", delta=delta)
        for delta in ("a", "b", "c")
    ]
    for event in events:
        event_projector.process(event)
    assert event_projector._dedup_window.size == 2
    # 最早的 a 已被淘汰：重放它会再次被应用（后果是文本重复，而非崩溃）。
    assert event_projector.process(events[0]) is not None
    parts = _run(snapshots.states[1], 1)["messages"][1]["parts"]
    assert [part["text"] for part in parts] == ["abca"]


def test_duplicate_delta_drop_is_logged(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """重复投递被丢弃时留下可观测日志（回答「该机制是否真被触发」）。"""

    event_projector, _ = projector
    _start(event_projector)
    event = AssistantTextDeltaEvent(task_id=1, run_id=1, part="text", delta="a")
    event_projector.process(event)
    with caplog.at_level(logging.DEBUG):
        event_projector.process(event)
    assert "conversation_event_duplicate_dropped" in caplog.text


def test_window_eviction_is_logged(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """窗口淘汰最旧 event_id 时记录容量预警。"""

    event_projector, _ = _projector_with_window(1, monkeypatch)
    _start(event_projector)
    with caplog.at_level(logging.WARNING):
        event_projector.process(
            AssistantTextDeltaEvent(task_id=1, run_id=1, part="text", delta="a")
        )
        event_projector.process(
            AssistantTextDeltaEvent(task_id=1, run_id=1, part="text", delta="b")
        )
    assert "conversation_event_dedup_window_evicted" in caplog.text


def test_idempotent_run_and_message_events_reapply_without_changing_snapshot(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    """run / message / usage 级事件的 ``plan`` 自幂等：重复投递不会改变快照。"""

    event_projector, snapshots = projector
    _start(event_projector)
    event_projector.process(
        AssistantTextDeltaEvent(task_id=1, run_id=1, part="text", delta="你好")
    )
    _assert_reapply_changes_nothing(
        event_projector, snapshots, lambda: RunInitializedEvent(task_id=1, run_id=1)
    )
    _assert_reapply_changes_nothing(
        event_projector,
        snapshots,
        lambda: UserInputAppendedEvent(
            task_id=1,
            run_id=1,
            parts=[{"type": "text", "text": "你好", "status": "completed"}],
        ),
    )
    _assert_reapply_changes_nothing(
        event_projector,
        snapshots,
        lambda: RunStatusChangedEvent(
            task_id=1, run_id=1, status=ConversationRunStatus.RUNNING
        ),
    )
    _assert_reapply_changes_nothing(
        event_projector,
        snapshots,
        lambda: AssistantPartClosedEvent(task_id=1, run_id=1, part="text"),
    )
    _assert_reapply_changes_nothing(
        event_projector,
        snapshots,
        lambda: ContextUsageUpdatedEvent(
            task_id=1, run_id=1, ratio=0.5, used_tokens=10, context_window_tokens=20
        ),
    )


def test_idempotent_tool_events_reapply_without_changing_snapshot(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    """工具级事件的 ``plan`` 自带幂等（存在性检查 / seq 守卫 / 迁移矩阵）。"""

    event_projector, snapshots = projector
    _start(event_projector)
    _assert_reapply_changes_nothing(
        event_projector,
        snapshots,
        lambda: ToolCallCreatedEvent(
            task_id=1, run_id=1, tool_call_id="t1", tool_name="read_file"
        ),
    )
    _assert_reapply_changes_nothing(
        event_projector,
        snapshots,
        lambda: ToolCallStatusChangedEvent(
            task_id=1, run_id=1, tool_call_id="t1", status="running", args={"path": "a.txt"}
        ),
    )
    _assert_reapply_changes_nothing(
        event_projector,
        snapshots,
        lambda: ToolCallsSettledEvent(
            task_id=1, run_id=1, status="cancelled", reason="runtime_failed"
        ),
    )


def test_run_initialized_replace_existing_reapplication_resets_run(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    """记录危害：``replace_existing=True`` 重复应用会清空 run，故去重必须覆盖全部事件。

    去重窗口淘汰后，极旧事件的重复投递会漏网。对绝大多数事件这不构成问题（plan 自幂等），
    但本事件是唯一例外——它整体 ``set`` 会把 run 重置回 ``pending`` 并清空 messages。因此
    去重不能按「事件是否幂等」收窄，必须对所有事件生效。
    """

    event_projector, snapshots = projector
    _start(event_projector)
    reset = RunInitializedEvent(task_id=1, run_id=1, replace_existing=True)

    assert event_projector.process(reset) is not None
    # 同一 event_id 的重复投递必须被去重，否则 run 会被再次重置。
    assert event_projector.process(reset) is None

    event_projector.process(
        AssistantTextDeltaEvent(task_id=1, run_id=1, part="text", delta="已产出的回答")
    )
    assert _run(snapshots.states[1], 1)["messages"][1]["parts"] != []

    # 换新 event_id、同一业务事实：模拟「去重窗口淘汰后漏网」的那一次重复投递。
    event_projector.process(RunInitializedEvent(task_id=1, run_id=1, replace_existing=True))

    assert _run(snapshots.states[1], 1)["messages"][1]["parts"] == []
