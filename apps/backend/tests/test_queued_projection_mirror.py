"""排队投影镜像外部队列：撤回后条目随刷新消失；消费投影不依赖排队条目。

回归：排队投影曾「只增改不删」，删除排队消息后行已撤回、投影里却一直留着，界面上的删除与立即发送
随后都 409，Run 结束也接不到它。
"""

from __future__ import annotations

import pytest

from app.assistant_transport.projection.user_input_projection import plan_input_consumed
from app.assistant_transport.service.conversation_state_service import (
    ConversationStateService,
)
from app.assistant_transport.state.conversation_state_snapshot import empty_snapshot
from app.core.events import InputConsumedEvent
from app.core.workflow.message_queue.message_bodies.user_input import RunLaunch
from app.core.workflow.message_queue.session_message import SessionMessage, UserInputBody
from app.models.conversation_input import UserInputPresentation
from app.service import depends
from app.storage.store_engines import main_db_factory

pytestmark = pytest.mark.usefixtures("isolated_storage", "empty_tool_registry")


def _user(text: str) -> SessionMessage:
    return SessionMessage.of(
        UserInputBody(
            text=text,
            launch=RunLaunch(model_config_id="test/m1"),
            presentation=UserInputPresentation(text=text),
        ),
        delivery="next_run",
    )


@pytest.fixture
def session_id(tmp_path) -> int:
    workspace = depends.get_workspace_crud().create("ws", str(tmp_path))
    return depends.get_session_crud().create(workspace.id, "排队会话").id


def _queued_texts(service: ConversationStateService, session_id: int) -> list[str]:
    return [
        message["parts"][0]["text"] for message in service.get_state(session_id)["queued_messages"]
    ]


def test_withdrawn_message_leaves_the_projection_on_refresh(session_id: int) -> None:
    queue = depends.get_session_message_queue()
    service = ConversationStateService()
    service.get_state(session_id)
    first = queue.put(session_id, _user("第一条"))
    queue.put(session_id, _user("第二条"))
    service.refresh_queued_messages(session_id)
    assert _queued_texts(service, session_id) == ["第一条", "第二条"]

    with main_db_factory().begin() as db:
        assert queue.withdraw(db, session_id, first) is not None
    service.refresh_queued_messages(session_id)

    assert _queued_texts(service, session_id) == ["第二条"]


def test_consumption_builds_the_message_without_a_queued_entry() -> None:
    state = empty_snapshot()
    event = InputConsumedEvent(
        session_id=1,
        run_id=1,
        message_id=7,
        presentation=UserInputPresentation(text="你好"),
        turn=4,
    )

    mutations = plan_input_consumed(event, state)

    user = mutations[0].value
    assert (user["id"], user["role"], user["turn"]) == ("7", "user", 4)
    assert user["parts"] == [{"type": "text", "text": "你好", "status": "completed"}]
    assert mutations[-1].path == ("queued_messages",)
    assert mutations[-1].value == []
