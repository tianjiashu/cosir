"""撤回在 context 上的存储语义（真实 SQLite）：定位最新用户输入、按 sequence 软删除。"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.core.context.context_store import ContextStore
from app.models.conversation_input import MessageSource, UserInputPresentation
from app.models.message_metadata import UserMessageMetadata
from app.service.depends import get_session_context_service
from app.storage.crud.run_crud import RunCrud
from app.storage.crud.session_crud import SessionCrud
from app.storage.crud.workspace_crud import WorkspaceCrud


def _user(text: str) -> UserMessageMetadata:
    return UserMessageMetadata(kind="user_input", presentation=UserInputPresentation(text=text))


def _turn_of(previous: list, metadata: object) -> int:
    """单个 Run 里的回合号：每条用户输入开一个回合（与写入方规则一致）。"""
    inputs = [
        m for _, m, _ in previous if isinstance(m, UserMessageMetadata) and m.kind == "user_input"
    ]
    opens = isinstance(metadata, UserMessageMetadata) and metadata.kind == "user_input"
    return max(1, len(inputs) + int(opens))


@pytest.fixture
def session(isolated_storage, tmp_path):
    workspace = WorkspaceCrud().create("withdraw", str(tmp_path))
    session = SessionCrud().create(workspace.id, "withdraw", tool_schemas=[])
    run = RunCrud().create(session.id, status="completed")
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
        store.append(
            session.id,
            run.id,
            message,
            sequence,
            metadata=metadata,
            message_id=message_id,
            turn=_turn_of(rows[: sequence - 1], metadata),
        )
    return SimpleNamespace(id=session.id, run_id=run.id)


def test_latest_user_input_skips_other_user_role_messages(session) -> None:
    latest = get_session_context_service().latest_user_input(session.id)

    assert latest is not None
    assert (latest.sequence, latest.message_id) == (3, 12)


def test_latest_input_from_an_agent_is_not_an_edit_target(session) -> None:
    """最新一轮由 Agent 消息开启时没有可编辑目标：越过它改更早的用户输入会把它那一轮一起撤回。"""
    ContextStore().append(
        session.id,
        session.run_id,
        {"role": "user", "content": "<agent_message/>"},
        6,
        metadata=UserMessageMetadata(
            kind="agent_message",
            presentation=UserInputPresentation(text="from parent"),
            source=MessageSource(session_id=1, name="parent"),
        ),
        message_id=14,
        turn=2,
    )

    assert get_session_context_service().latest_user_input(session.id) is None
