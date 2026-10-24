"""编辑已并入 context 的用户输入：校验编辑资格、移出目标及其后 context。

复用目标附件属于输入解析，归 ``UserInputService.prepare(reuse=...)``。

编辑 = 改 context + 接收消息：移出与编辑后的消息入队由调用方放进同一事务，任一步失败都不会
丢掉原消息；外部文件、工具副作用不回滚。
"""

from __future__ import annotations

from typing import cast

from sqlalchemy.orm import Session

from app.models.conversation_input import UserInputPresentation
from app.models.message_metadata import UserMessageMetadata
from app.storage.crud.conversation_task_context_crud import ConversationTaskContextCrud
from app.storage.store_engines import main_session_factory


def latest_user_input(task_id: int) -> tuple[int, UserInputPresentation] | None:
    """最新一条已投影的用户输入的身份与展示绑定；没有时返回 ``None``。"""
    with main_session_factory()() as session:
        return _latest_user_input(session, task_id)


def exclude_from(session: Session, task_id: int, message_id: int) -> None:
    """在调用方事务内把目标及其后 context 移出模型上下文与投影（观测页仍可查看）。

    异常:
        ValueError: 目标不是最新一条已投影的用户输入（读取后被并发改变）。
    """
    latest = _latest_user_input(session, task_id)
    if latest is None or latest[0] != message_id:
        raise ValueError("只能编辑最新用户消息")
    contexts = ConversationTaskContextCrud()
    target = next(
        row
        for row in contexts.get(task_id, include_in_context=False, session=session)
        if row.message_id == message_id
    )
    contexts.exclude_from_sequence(task_id, target.sequence, session)


def _latest_user_input(
    session: Session, task_id: int
) -> tuple[int, UserInputPresentation] | None:
    # 编辑资格按来源 kind 判断：其它 kind 的 user 消息（通知等）即使将来可投影也不可编辑。
    rows = ConversationTaskContextCrud().get(task_id, include_in_context=False, session=session)
    users = [
        row
        for row in rows
        if row.project_to_transport
        and row.message["role"] == "user"
        and cast(UserMessageMetadata, row.metadata).kind == "user_input"
    ]
    if not users:
        return None
    target = users[-1]
    presentation = cast(UserMessageMetadata, target.metadata).presentation
    return cast(int, target.message_id), cast(UserInputPresentation, presentation)
