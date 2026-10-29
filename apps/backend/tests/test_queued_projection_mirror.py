"""排队投影镜像外部队列：撤回后条目随刷新消失；消费投影不依赖排队条目。

回归：排队投影曾「只增改不删」，删除排队消息后行已撤回、投影里却一直留着，界面上的删除与立即发送
随后都 409，Run 结束也接不到它。
"""

from __future__ import annotations

import pytest

from app.assistant_transport.projection.user_input_projection import plan_user_input_consumed
from app.assistant_transport.service.conversation_task_state_service import (
    ConversationTaskStateService,
)
from app.assistant_transport.state.conversation_state_snapshot import empty_snapshot
from app.core.events import UserInputConsumedEvent
from app.core.workflow.message_queue.task_message import TaskMessage, UserInputBody
from app.models.conversation_input import UserInputPresentation
from app.core.workflow.message_queue.message_bodies.user_input import RunLaunch
from app.service import depends
from app.storage.store_engines import main_session_factory

pytestmark = pytest.mark.usefixtures("isolated_storage")


def _user(text: str) -> TaskMessage:
    return TaskMessage.of(
        UserInputBody(
            input_text=text,
            launch=RunLaunch(model_config_id=1),
            presentation=UserInputPresentation(display_text=text),
        ),
        delivery="next_run",
    )


@pytest.fixture
def task_id(tmp_path) -> int:
    workspace = depends.get_workspace_crud().create("ws", str(tmp_path))
    return depends.get_task_crud().create(workspace.id, "排队任务").id


def _queued_texts(service: ConversationTaskStateService, task_id: int) -> list[str]:
    return [
        message["parts"][0]["text"] for message in service.get_state(task_id)["queued_messages"]
    ]


def test_withdrawn_message_leaves_the_projection_on_refresh(task_id: int) -> None:
    queue = depends.get_task_message_queue()
    service = ConversationTaskStateService()
    service.get_state(task_id)
    first = queue.put(task_id, _user("第一条"))
    queue.put(task_id, _user("第二条"))
    service.refresh_queued_messages(task_id)
    assert _queued_texts(service, task_id) == ["第一条", "第二条"]

    with main_session_factory().begin() as session:
        assert queue.withdraw(session, task_id, first) is not None
    service.refresh_queued_messages(task_id)

    assert _queued_texts(service, task_id) == ["第二条"]


def test_consumption_builds_the_message_without_a_queued_entry() -> None:
    state = empty_snapshot()
    event = UserInputConsumedEvent(
        task_id=1, run_id=1, message_id=7, presentation=UserInputPresentation(display_text="你好")
    )

    mutations = plan_user_input_consumed(event, state)

    user = mutations[0].value
    assert (user["id"], user["role"], user["runId"]) == ("7", "user", 1)
    assert user["parts"] == [{"type": "text", "text": "你好", "status": "completed"}]
    assert mutations[-1].path == ("queued_messages",)
    assert mutations[-1].value == []
