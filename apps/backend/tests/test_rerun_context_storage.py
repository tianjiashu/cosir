"""编辑重跑在 context 上的存储语义（真实 SQLite）：定位最新用户输入、按 sequence 软删除。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.context.context_store import ContextStore
from app.models.conversation_input import UserInputPresentation
from app.models.message_metadata import UserMessageMetadata
from app.service.depends import get_conversation_task_context_service
from app.storage.crud.conversation_task_context_crud import ConversationTaskContextCrud
from app.storage.crud.task_crud import TaskCrud
from app.storage.crud.workspace_crud import WorkspaceCrud
from app.storage.store_engines import main_session_factory


def _user(text: str) -> UserMessageMetadata:
    return UserMessageMetadata(
        kind="user_input", presentation=UserInputPresentation(display_text=text)
    )


@pytest.fixture
def task(isolated_storage, tmp_path):
    workspace = WorkspaceCrud().create("rerun", str(tmp_path))
    task = TaskCrud().create(workspace.id, "rerun", tool_schemas=[])
    store = ContextStore()
    rows = [
        ({"role": "user", "content": "first"}, _user("first"), 11),
        ({"role": "assistant", "content": "a1"}, None, None),
        ({"role": "user", "content": "second"}, _user("second"), 12),
        ({"role": "assistant", "content": "a2"}, None, None),
        # 通知同样是 user 角色，但不是用户输入，不能被当作编辑目标。
        ({"role": "user", "content": "<system_notice/>"}, UserMessageMetadata(kind="notice"), 13),
    ]
    for sequence, (message, metadata, message_id) in enumerate(rows, start=1):
        store.append(task.id, None, message, sequence, metadata=metadata, message_id=message_id)
    return SimpleNamespace(id=task.id)


def test_latest_user_input_skips_other_user_role_messages(task) -> None:
    latest = get_conversation_task_context_service().latest_user_input(task.id)

    assert latest is not None
    assert (latest.sequence, latest.message_id) == (3, 12)


def test_exclude_from_soft_deletes_target_and_later_rows(task) -> None:
    context = get_conversation_task_context_service()

    with main_session_factory().begin() as session:
        context.exclude_from(task.id, 3, session)

    crud = ConversationTaskContextCrud()
    assert [row.sequence for row in crud.get(task.id, include_in_context=True)] == [1, 2]
    # 行保留供观测页查看，但不进模型上下文、也不投影到前端。
    excluded = [row for row in crud.get(task.id, include_in_context=False) if row.sequence >= 3]
    assert [row.sequence for row in excluded] == [3, 4, 5]
    assert not any(row.project_to_transport for row in excluded)
    # 被移出的输入不再是编辑目标，最新用户输入回退到更早那条。
    assert context.latest_user_input(task.id).message_id == 11
