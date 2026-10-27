"""Conversation event 到 Transport snapshot projector 的回归测试。"""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from typing import Any

import pytest

from app.assistant_transport.projection.event_projection import plan_event
from app.assistant_transport.projection.user_input_projection import build_user_input_parts
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
from app.core.events import (
    AssistantPartClosedEvent,
    AssistantTextDeltaEvent,
    RunInitializedEvent,
    RunResultUpdatedEvent,
    RunStatusChangedEvent,
    ToolCallCreatedEvent,
    ToolCallsSettledEvent,
    ToolCallStatusChangedEvent,
)
from app.core.tool.schemas import ToolObservation
from app.core.tool.execute.tool_terminal_projection import terminal_event
from app.core.workflow.execution_state.conversation_run_usage_stats import ConversationRunUsageStats
from app.models.conversation_input import UserInputPresentation
from app.models.enums.conversation_run_status import ConversationRunStatus
from tests.transport_fixtures import consumed, queue_user_input


class InMemorySnapshotService:
    """测试用 snapshot owner，复用生产 mutation 语义但不触碰 SQLite。"""

    def __init__(self) -> None:
        self.states: dict[int, ConversationStateSnapshot] = {}

    def get_state(self, task_id: int) -> ConversationStateSnapshot:
        state = self.states.setdefault(task_id, empty_snapshot())
        return copy.deepcopy(state)

    def refresh_parent_delegation(self, _child_task_id: int, _run_id: int, _status: str) -> None:
        """单任务内存 owner 没有父委派卡片可同步；生产实现见 ConversationTaskStateService。"""
        return None

    def apply_planned(self, event: object) -> TransportFrame:
        """按生产口径投影一条事件：由事件自带 ``plan`` 产出 mutation 后落回进程内状态。"""

        task_id = event.task_id
        state = self.get_state(task_id)
        mutations = tuple(plan_event(event, copy.deepcopy(state)))
        for mutation in mutations:
            _apply_mutation(state, mutation)
        validate_snapshot(state)
        self.states[task_id] = copy.deepcopy(state)
        return TransportFrame(task_id=task_id, kind="mutation", mutations=mutations)


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


def _init_run(
    projector: ConversationEventProjector, context_window_total: int | None = None
) -> None:
    """只建立 Run 骨架；消息由用户输入投影追加。"""

    projector.process(
        RunInitializedEvent(task_id=1, run_id=1, context_window_total=context_window_total)
    )


def _append_user_input(
    projector: ConversationEventProjector, task_id: int = 1, run_id: int = 1
) -> None:
    """追加一条已消费用户输入，生成 user 消息与其后的 assistant 占位。

    生产口径：先投影排队消息，再发只带消息身份的消费事件（消息身份取 Run 号，便于断言）。
    """

    _consume(projector, run_id, UserInputPresentation(display_text="你好"), task_id, run_id)


def _consume(
    projector: ConversationEventProjector,
    message_id: int,
    presentation: UserInputPresentation,
    task_id: int = 1,
    run_id: int = 1,
) -> object:
    owner = projector._state_service
    queue_user_input(owner.states.setdefault(task_id, empty_snapshot()), message_id, presentation)
    return projector.process(consumed(task_id, run_id, message_id))


def _start(projector: ConversationEventProjector, context_window_total: int | None = None) -> None:
    """建立 Run 并消费首条用户输入：之后 ``messages[1]`` 即 assistant 气泡。"""

    _init_run(projector, context_window_total)
    _append_user_input(projector)


def _messages(state: ConversationStateSnapshot, run_id: int) -> list[dict[str, Any]]:
    """某 Run 的消息（扁平消息列表按 ``runId`` 过滤）。"""
    return [message for message in state["messages"] if message["runId"] == run_id]


def _run(state: ConversationStateSnapshot, run_id: int) -> dict[str, Any]:
    """某 Run 在 Task 视角 snapshot 里的生命周期视图：当前活动读 ``activity``，旧 Run 读它最后
    一条助手消息的 ``outcome``。"""
    activity = state["activity"]
    if activity is not None and activity["runId"] == run_id:
        return dict(activity)
    assistant = [m for m in _messages(state, run_id) if m["role"] == "assistant"]
    outcome = assistant[-1]["outcome"] if assistant else None
    assert outcome is not None, f"run {run_id} 既不是当前活动也没有结局"
    return dict(outcome)


def test_run_initialized_and_user_input(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    event_projector, snapshots = projector
    _init_run(event_projector)
    assert _messages(snapshots.states[1], 1) == []
    _append_user_input(event_projector)
    state = snapshots.states[1]
    assert state["activity"]["runId"] == 1
    # Run 由调度 actor 直接以 running 创建，没有待认领态。
    assert state["activity"]["status"] == "running"
    assert [(m["id"], m["role"], m["runId"], m["outcome"]) for m in state["messages"]] == [
        ("1", "user", 1, None),
        ("assistant-1", "assistant", 1, None),
    ]
    assert state["messages"][0]["parts"][0]["text"] == "你好"
    assert state["messages"][1]["parts"] == []


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
            "title": "审查前端",
            "role": "Reviewer",
            "status": "running",
        },
    )

    change = event_projector.process(event)
    part = _messages(snapshots.states[1], 1)[1]["parts"][0]

    assert change is not None
    assert part["display_data"] == {
        "kind": "delegation-result",
        "child_agent_id": "delegate_reviewer",
        "child_task_id": 22,
        "title": "审查前端",
        "role": "Reviewer",
        "status": "running",
    }


def test_user_input_projects_ordered_text_image_and_file_parts(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    event_projector, snapshots = projector
    _init_run(event_projector)
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

    image_id = "a" * 64
    _consume(
        event_projector,
        1,
        UserInputPresentation(
            display_text=f"请看[[cosir-file:readme]]和这张图[[cosir-image:{image_id}]]",
            image_paths=[f"attachments/{image_id}.png"],
            attachments=[
                {
                    "id": "readme",
                    "name": "README.md",
                    "content_type": "text/markdown",
                    "path": "attachments/readme.md",
                }
            ],
        ),
    )

    user_parts = _messages(snapshots.states[1], 1)[0]["parts"]
    assert user_parts == parts
    assert all(part.get("path") is None for part in user_parts if isinstance(part, dict))


def test_user_input_supports_attachment_only_and_duplicate_event(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    event_projector, snapshots = projector
    _init_run(event_projector)
    queue_user_input(
        snapshots.states[1],
        1,
        UserInputPresentation(
            display_text="[[cosir-file:only-file]]",
            attachments=[
                {
                    "id": "only-file",
                    "name": "a.txt",
                    "content_type": "text/plain",
                    "path": "attachments/only-file.txt",
                }
            ],
        ),
    )
    event = consumed(1, 1, 1)

    first = event_projector.process(event)
    # 同一事件重复投递：按 event_id 去重，不再产出帧。
    second = event_projector.process(event)

    assert first is not None
    assert second is None
    assert _messages(snapshots.states[1], 1)[0]["parts"] == [
        {
            "type": "file",
            "file": "cosir-local-file:only-file",
            "name": "a.txt",
            "contentType": "text/plain",
        }
    ]


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
        [{
                        "id": "readme",
                        "name": "README.md",
                        "content_type": "text/markdown",
                        "path": "attachments/readme.md",
                    }],
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
    parts = _messages(snapshots.states[1], 1)[1]["parts"]
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
    assert _messages(snapshots.states[1], 1)[1]["parts"][0]["text"] == "一次"


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
    parts = _messages(snapshots.states[1], 1)[1]["parts"]
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


def test_completed_run_rejects_reviving_status_event() -> None:
    """completed 不可逆：迟到事件不能把已结束的 Run 拉回 active。

    failed 可经 ``RunTransition.RESUME`` 合法续跑，不在此列。
    """

    snapshots = InMemorySnapshotService()
    event_projector = ConversationEventProjector(snapshots)
    _start(event_projector)
    event_projector.process(
        RunStatusChangedEvent(task_id=1, run_id=1, status=ConversationRunStatus.COMPLETED)
    )
    event_projector.process(
        RunStatusChangedEvent(task_id=1, run_id=1, status=ConversationRunStatus.RUNNING)
    )

    assert _run(snapshots.states[1], 1)["status"] == "completed"


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

    parts = _messages(snapshots.states[1], 1)[1]["parts"]
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
    assert _messages(snapshots.states[1], 1)[1]["parts"] == []
    assert "tool_call_part_missing_for_status_change" in {
        record.message for record in caplog.records
    }


def test_run_status_usage_is_projected(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    event_projector, snapshots = projector
    _start(event_projector, context_window_total=100)
    event_projector.process(RunStatusChangedEvent(task_id=1, run_id=1, status=ConversationRunStatus.RUNNING))
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
    assert state["activity"]["runId"] == 1
    assert _run(state, 1)["contextWindowTotal"] == 100
    assert _run(state, 1)["status"] == "completed"


def test_terminal_usage_is_projected(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    event_projector, snapshots = projector
    _start(event_projector)
    event_projector.process(RunStatusChangedEvent(task_id=1, run_id=1, status=ConversationRunStatus.RUNNING))
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
    _start(event_projector, context_window_total=100)
    event_projector.process(RunStatusChangedEvent(task_id=1, run_id=1, status=ConversationRunStatus.RUNNING))
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
    # 上下文窗口只属于当前活动；旧 Run 的结局不带窗口上限。
    assert _run(state, 2)["contextWindowTotal"] is None
    assert "context_window_total" not in state


def test_late_previous_run_events_cannot_overwrite_current_run(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    event_projector, snapshots = projector
    _start(event_projector)
    event_projector.process(RunStatusChangedEvent(task_id=1, run_id=1, status=ConversationRunStatus.RUNNING))
    event_projector.process(
        RunStatusChangedEvent(task_id=1, run_id=1, status=ConversationRunStatus.COMPLETED)
    )
    event_projector.process(RunInitializedEvent(task_id=1, run_id=2))
    event_projector.process(
        RunStatusChangedEvent(task_id=1, run_id=1, status=ConversationRunStatus.FAILED)
    )
    state = snapshots.states[1]
    assert _run(state, 2)["status"] == "running"
    assert _run(state, 1)["status"] == "completed"
    assert _run(state, 1)["usage"] is None


def test_structured_result_updates_completed_run_without_status_transition(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    """支线失败原因和用量可在 completed 后更新，已有主线状态保持不变。"""

    event_projector, snapshots = projector
    _start(event_projector)
    event_projector.process(
        RunStatusChangedEvent(task_id=1, run_id=1, status=ConversationRunStatus.RUNNING)
    )
    event_projector.process(
        RunStatusChangedEvent(task_id=1, run_id=1, status=ConversationRunStatus.COMPLETED)
    )
    event_projector.process(RunInitializedEvent(task_id=1, run_id=2))
    change = event_projector.process(
        RunResultUpdatedEvent(
            task_id=1,
            run_id=1,
            end_reason="structured_output_failed",
            usage_stats=ConversationRunUsageStats(input_tokens=4, output_tokens=2, total_tokens=6),
        )
    )
    assert change is not None and change.mutations
    assert snapshots.states[1]["activity"]["runId"] == 2
    assert _run(snapshots.states[1], 1)["status"] == "completed"
    assert _run(snapshots.states[1], 1)["endReason"] == "structured_output_failed"
    assert _run(snapshots.states[1], 1)["usage"]["total_tokens"] == 6


def test_historical_run_status_event_updates_its_own_run(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    event_projector, snapshots = projector
    _start(event_projector)
    event_projector.process(RunStatusChangedEvent(task_id=1, run_id=1, status=ConversationRunStatus.RUNNING))
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
    assert _run(state, 2)["status"] == "running"


def test_unknown_run_event_raises_and_is_not_projected(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    """事件引用的 Run 既不是当前活动、也没有已写出的结局时丢弃（留痕），不写入任何 mutation。"""

    event_projector, snapshots = projector
    change = event_projector.process(
        RunStatusChangedEvent(
            task_id=1,
            run_id=404,
            status=ConversationRunStatus.COMPLETED,
            usage_stats=ConversationRunUsageStats(input_tokens=1, output_tokens=1, total_tokens=2),
        )
    )
    assert change is not None and change.mutations == ()
    assert snapshots.states.get(1, empty_snapshot()) == empty_snapshot()


def test_late_run_initialization_cannot_rewind_terminal_newer_run(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    event_projector, snapshots = projector
    _start(event_projector)
    event_projector.process(RunStatusChangedEvent(task_id=1, run_id=1, status=ConversationRunStatus.RUNNING))
    event_projector.process(
        RunStatusChangedEvent(task_id=1, run_id=1, status=ConversationRunStatus.COMPLETED)
    )
    event_projector.process(RunInitializedEvent(task_id=1, run_id=2))
    event_projector.process(RunStatusChangedEvent(task_id=1, run_id=2, status=ConversationRunStatus.RUNNING))
    event_projector.process(
        RunStatusChangedEvent(task_id=1, run_id=2, status=ConversationRunStatus.COMPLETED)
    )
    event_projector.process(RunInitializedEvent(task_id=1, run_id=1))
    assert _run(snapshots.states[1], 2)["status"] == "completed"


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
        _append_user_input(event_projector, task_id=task_id, run_id=task_id)
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
    parts = _messages(snapshots.states[1], 1)[1]["parts"]
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
    parts = _messages(snapshots.states[1], 1)[1]["parts"]
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
        lambda: consumed(1, 1, 1),
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
    # 终态由实时与结算两条路径各投一次（同经 ``terminal_event`` 构造），重放必须不改快照。
    _assert_reapply_changes_nothing(
        event_projector,
        snapshots,
        lambda: terminal_event(
            ToolObservation(
                tool_name="read_file",
                status="success",
                content="hello",
                display_data={"kind": "read-file-meta", "path": "a.txt"},
            ),
            task_id=1,
            run_id=1,
            step_id="step-1",
            tool_call_id="t1",
        ),
    )
    _assert_reapply_changes_nothing(
        event_projector,
        snapshots,
        lambda: ToolCallsSettledEvent(
            task_id=1, run_id=1, status="cancelled", reason="runtime_failed"
        ),
    )




# ---------------------------------------------------------------------------
# Task 视角：activity、消息级结局（outcome）与旧 Run 的迟到事件
# ---------------------------------------------------------------------------


def _finish(
    projector: ConversationEventProjector,
    run_id: int,
    status: ConversationRunStatus,
    total_tokens: int | None = None,
) -> None:
    usage = (
        None
        if total_tokens is None
        else ConversationRunUsageStats(input_tokens=1, output_tokens=1, total_tokens=total_tokens)
    )
    projector.process(
        RunStatusChangedEvent(task_id=1, run_id=run_id, status=status, usage_stats=usage)
    )


def test_empty_task_has_no_activity() -> None:
    """A3：Task 还没有任何 Run 时 activity 为 null。"""

    assert empty_snapshot()["activity"] is None


def test_activity_follows_latest_run(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    """A3：activity 是最新 Run 的摘要；新 Run 初始化后切换到它。"""

    event_projector, snapshots = projector
    _start(event_projector, context_window_total=100)
    _finish(event_projector, 1, ConversationRunStatus.COMPLETED, total_tokens=9)
    event_projector.process(RunInitializedEvent(task_id=1, run_id=2, context_window_total=64))

    activity = snapshots.states[1]["activity"]
    assert (activity["runId"], activity["status"], activity["usage"]) == (2, "running", None)
    assert activity["contextWindowTotal"] == 64


def test_terminal_status_writes_outcome_on_last_assistant_message(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    """A2：运行中助手消息没有 outcome；Run 终态时写到该 Run 最后一条助手消息，user 消息不带。"""

    event_projector, snapshots = projector
    _start(event_projector, context_window_total=100)
    _consume(event_projector, 2, UserInputPresentation(display_text="追问"))
    assert all(m["outcome"] is None for m in snapshots.states[1]["messages"])

    _finish(event_projector, 1, ConversationRunStatus.COMPLETED, total_tokens=7)

    outcomes = [(m["id"], m["outcome"]) for m in snapshots.states[1]["messages"]]
    assert [message_id for message_id, outcome in outcomes if outcome is not None] == [
        "assistant-2"
    ]
    outcome = snapshots.states[1]["messages"][-1]["outcome"]
    assert outcome["status"] == "completed"
    assert outcome["usage"]["total_tokens"] == 7
    # 窗口上限与用量同属这个 Run：取自它的 activity，不随后续 Run 变化。
    assert outcome["contextWindowTotal"] == 100


def test_resume_clears_outcome(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    """A2：取消后恢复（cancelled → running）清空结局，重新进入终态时再写。"""

    event_projector, snapshots = projector
    _start(event_projector)
    _finish(event_projector, 1, ConversationRunStatus.CANCELLED)
    assert snapshots.states[1]["messages"][-1]["outcome"]["status"] == "cancelled"

    _finish(event_projector, 1, ConversationRunStatus.RUNNING)
    assert snapshots.states[1]["messages"][-1]["outcome"] is None
    assert snapshots.states[1]["activity"]["status"] == "running"

    _finish(event_projector, 1, ConversationRunStatus.FAILED)
    assert snapshots.states[1]["messages"][-1]["outcome"]["status"] == "failed"


def test_late_events_of_previous_run_only_update_its_outcome(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    """A5：两个 Run 依次结束后回放第一个 Run 的状态 / 结果事件。

    activity 不变，只更新第一个 Run 的 outcome。
    """

    event_projector, snapshots = projector
    _start(event_projector)
    _finish(event_projector, 1, ConversationRunStatus.COMPLETED, total_tokens=5)
    event_projector.process(RunInitializedEvent(task_id=1, run_id=2))
    _append_user_input(event_projector, run_id=2)
    _finish(event_projector, 2, ConversationRunStatus.COMPLETED, total_tokens=11)
    activity_before = copy.deepcopy(snapshots.states[1]["activity"])

    event_projector.process(
        RunStatusChangedEvent(task_id=1, run_id=1, status=ConversationRunStatus.COMPLETED)
    )
    event_projector.process(
        RunResultUpdatedEvent(
            task_id=1,
            run_id=1,
            end_reason="structured_output_failed",
            usage_stats=ConversationRunUsageStats(input_tokens=3, output_tokens=3, total_tokens=6),
        )
    )

    state = snapshots.states[1]
    assert state["activity"] == activity_before
    first_outcome = _messages(state, 1)[-1]["outcome"]
    assert first_outcome["endReason"] == "structured_output_failed"
    assert first_outcome["usage"]["total_tokens"] == 6
    assert _messages(state, 2)[-1]["outcome"]["usage"]["total_tokens"] == 11


def test_run_events_never_write_top_level_error(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    """A6：Run 的失败只进 activity / outcome，run 事件投影不写顶层 error（它只承载命令被拒）。"""

    event_projector, snapshots = projector
    _start(event_projector)
    failure = {"code": "model_network_error", "message": "无法连接模型服务"}
    change = event_projector.process(
        RunStatusChangedEvent(
            task_id=1, run_id=1, status=ConversationRunStatus.FAILED, error=failure
        )
    )

    assert change is not None
    assert all(mutation.path != ("error",) for mutation in change.mutations)
    state = snapshots.states[1]
    assert "error" not in state
    assert state["activity"]["error"] == failure
    assert state["messages"][-1]["outcome"]["error"] == failure


def test_first_output_of_run_without_user_input_creates_leading_reply(
    projector: tuple[ConversationEventProjector, InMemorySnapshotService],
) -> None:
    """被结果消息唤醒新开、没有用户输入的 Run 第一次输出时建出助手消息。

    该消息 id 为 ``assistant-run-{runId}``。
    """

    event_projector, snapshots = projector
    _start(event_projector)
    _finish(event_projector, 1, ConversationRunStatus.COMPLETED)
    event_projector.process(RunInitializedEvent(task_id=1, run_id=2))

    event_projector.process(AssistantTextDeltaEvent(task_id=1, run_id=2, part="text", delta="结"))
    event_projector.process(AssistantTextDeltaEvent(task_id=1, run_id=2, part="text", delta="论"))
    event_projector.process(
        ToolCallCreatedEvent(task_id=1, run_id=2, tool_call_id="call-9", tool_name="read_file")
    )

    [reply] = _messages(snapshots.states[1], 2)
    assert (reply["id"], reply["role"], reply["outcome"]) == ("assistant-run-2", "assistant", None)
    assert reply["parts"][0]["text"] == "结论"
    assert reply["parts"][1]["toolCallId"] == "call-9"
