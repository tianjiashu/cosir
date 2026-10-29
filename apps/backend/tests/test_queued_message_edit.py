"""排队消息：排队投影的增量刷新，与原地编辑命令 ``edit_queued``。

消费投影只搬移排队条目的 parts、不重建，所以排队投影必须先于投递刷新、编辑后也要刷新；刷新只增改
不删除，条目的移除只归消费投影。编辑沿用原消息的身份、``delivery`` 与发起配置，消息已被并入时
返回不可重试的 ``MESSAGE_CONSUMED``。
"""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from app.assistant_transport.request import EditQueuedMessageRequest
from app.assistant_transport.service.conversation_task_state_service import (
    ConversationTaskStateService,
)
from app.assistant_transport.service.transport_assistant_service import (
    TransportAssistantService,
)
from app.core.task_actor import SessionConflictError
from app.core.workflow.message_queue.task_message import RunLaunch, TaskMessage, UserInputBody
from app.models.conversation_input import UserInputPresentation
from app.models.conversation_run_options import ConversationRunOptions
from app.models.conversation_run_record import ConversationRunRecord
from app.models.conversation_task_context import ConversationTaskContextRecord
from app.models.task_record import TaskRecord
from app.task_runtime.task_runtime_space_registry import task_runtime_spaces

pytestmark = pytest.mark.usefixtures("isolated_storage")

_TASK_ID = 7


def _queued(message_id: str, text: str, delivery: str = "immediate") -> dict[str, Any]:
    return {
        "id": message_id,
        "role": "user",
        "parts": [{"type": "text", "text": text}],
        "delivery": delivery,
    }


# ── 排队投影刷新 ──────────────────────────────────────────────────────────


def _task() -> TaskRecord:
    now = datetime(2026, 10, 27, tzinfo=UTC)
    return TaskRecord(
        id=_TASK_ID, workspace_id=3, title="t", created_at=now, updated_at=now,
        current_run_id=None,
    )


class _EmptyTaskSources:
    def get(
        self, _task_id: int, include_in_context: bool = True
    ) -> TaskRecord | list[ConversationTaskContextRecord]:
        return _task() if include_in_context else []

    def list_by_task(self, _task_id: int) -> list[ConversationRunRecord]:
        return []


@pytest.fixture
def states(monkeypatch: pytest.MonkeyPatch):
    """只替换 canonical 数据源与队列读取的真实 state service；记录发布的帧。"""

    monkeypatch.setattr(
        "app.task_runtime.task_runtime_space.get_task_service",
        lambda: SimpleNamespace(get_task=lambda _id: _task()),
    )
    monkeypatch.setattr(
        "app.task_runtime.task_runtime_space.get_workspace_service",
        lambda: SimpleNamespace(get_workspace=lambda _id: SimpleNamespace(id=3)),
    )
    ConversationTaskStateService.clear_process_state()
    service = ConversationTaskStateService.__new__(ConversationTaskStateService)
    sources = _EmptyTaskSources()
    service._task_source = sources
    service._run_source = sources
    service._context_source = sources
    queue: list[dict[str, Any]] = []
    frames: list[Any] = []
    monkeypatch.setattr(
        ConversationTaskStateService, "_queued_messages", staticmethod(lambda _id: list(queue))
    )
    service._publish = frames.append
    yield SimpleNamespace(service=service, queue=queue, frames=frames)
    ConversationTaskStateService.clear_process_state()


def test_refresh_does_nothing_before_the_snapshot_is_materialized(states) -> None:
    """没人读过的 Task 不为刷新去重建快照；之后的懒重建自会读到队列。"""

    states.queue.append(_queued("1", "hi"))
    states.service.refresh_queued_messages(_TASK_ID)

    assert states.frames == []
    assert not task_runtime_spaces.get_or_create(_TASK_ID).has_snapshot()


def test_refresh_replaces_in_place_appends_new_and_keeps_consumed_entries(states) -> None:
    """按身份原位替换、新条目追加；队列里已没有的条目保留，交给消费投影移除。"""

    states.queue.extend([_queued("1", "old"), _queued("2", "second")])
    states.service.get_state(_TASK_ID)
    # 「1」已被确认删除、消费投影还没到；「2」被编辑；「3」新入队。
    states.queue[:] = [_queued("2", "edited"), _queued("3", "third", "next_run")]

    states.service.refresh_queued_messages(_TASK_ID)

    queued = states.service.get_state(_TASK_ID)["queued_messages"]
    assert [(m["id"], m["parts"][0]["text"]) for m in queued] == [
        ("1", "old"), ("2", "edited"), ("3", "third"),
    ]
    [frame] = states.frames
    assert [mutation.path for mutation in frame.mutations] == [("queued_messages",)]


def test_refresh_publishes_nothing_when_the_projection_is_unchanged(states) -> None:
    states.queue.append(_queued("1", "hi"))
    states.service.get_state(_TASK_ID)

    states.service.refresh_queued_messages(_TASK_ID)

    assert states.frames == []


# ── edit_queued ──────────────────────────────────────────────────────────

_LAUNCH = RunLaunch(
    agent_id="child-agent",
    model_config_id=5,
    options=ConversationRunOptions(disabled_tool_groups=["联网工具"], reasoning_effort="high"),
)


def _queued_input(text: str = "old") -> TaskMessage:
    body = UserInputBody(
        input_text=text, launch=_LAUNCH, presentation=UserInputPresentation(display_text=text)
    )
    return TaskMessage.of(body, delivery="next_run", message_id=42)


def _request(text: str = "new") -> EditQueuedMessageRequest:
    return EditQueuedMessageRequest.model_validate(
        {"message": {"role": "user", "parts": [{"type": "text", "text": text}]}}
    )


class _Queue:
    def __init__(self, row: TaskMessage | None, *, update_result: bool = True) -> None:
        self.row = row
        self.update_result = update_result
        self.updated: list[tuple[int, int, TaskMessage]] = []

    def find(self, _task_id: int, _message_id: int) -> TaskMessage | None:
        return self.row

    def update(self, task_id: int, message_id: int, message: TaskMessage) -> bool:
        self.updated.append((task_id, message_id, message))
        return self.update_result


def _service(queue: _Queue) -> SimpleNamespace:
    prepared: list[dict[str, Any]] = []
    refreshed: list[int] = []

    def prepare(task_id, command, model_config_id, reasoning_effort, agent_id, *, reuse=None):
        prepared.append(
            {
                "model_config_id": model_config_id,
                "reasoning_effort": reasoning_effort,
                "agent_id": agent_id,
                "reuse": reuse,
            }
        )
        # 真实 ``prepare`` 按请求重组 launch（编辑请求不带禁用工具），这里模拟一份不同的。
        body = UserInputBody(
            input_text=command.display_text,
            launch=RunLaunch(model_config_id=model_config_id, agent_id=agent_id),
            presentation=UserInputPresentation(display_text=command.display_text),
        )
        return TaskMessage.of(body)

    service = TransportAssistantService.__new__(TransportAssistantService)
    service._queue = queue
    service._inputs = SimpleNamespace(prepare=prepare)
    service._states = SimpleNamespace(refresh_queued_messages=refreshed.append)
    return SimpleNamespace(service=service, prepared=prepared, refreshed=refreshed)


async def test_edit_keeps_identity_delivery_and_launch_and_refreshes_projection() -> None:
    queue = _Queue(_queued_input())
    harness = _service(queue)

    await harness.service.edit_queued(_TASK_ID, 42, _request("new"))

    [(task_id, message_id, edited)] = queue.updated
    assert (task_id, message_id) == (_TASK_ID, 42)
    assert edited.body.presentation.display_text == "new"
    assert edited.body.launch == _LAUNCH
    assert edited.delivery == "next_run"
    # 能力校验按原配置：消息最终在那个模型上运行；附件路径从原消息的展示绑定恢复。
    [call] = harness.prepared
    assert (call["model_config_id"], call["reasoning_effort"], call["agent_id"]) == (
        5, "high", "child-agent",
    )
    assert isinstance(call["reuse"], UserInputPresentation)
    assert call["reuse"].display_text == "old"
    assert harness.refreshed == [_TASK_ID]


@pytest.mark.parametrize(
    "queue",
    [_Queue(None), _Queue(_queued_input(), update_result=False)],
    ids=["already-consumed", "consumed-while-editing"],
)
async def test_edit_of_a_consumed_message_is_a_non_retryable_conflict(queue: _Queue) -> None:
    harness = _service(queue)

    with pytest.raises(SessionConflictError) as caught:
        await harness.service.edit_queued(_TASK_ID, 42, _request())

    assert caught.value.code == "MESSAGE_CONSUMED"
    assert caught.value.retryable is False
    assert harness.refreshed == []
