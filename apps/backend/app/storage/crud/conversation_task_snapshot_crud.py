"""ConversationTaskSnapshot 单表持久化访问。"""

import json
from typing import Any, cast

from sqlalchemy import delete, select
from sqlalchemy.orm import Session

from app.assistant_transport.state.conversation_state_snapshot import ConversationStateSnapshot
from app.storage.model.conversation_task_snapshot_model import ConversationTaskSnapshotModel
from app.storage.store_engines import main_session_factory
from app.utils.datetime_utils import to_text, utc_now


class ConversationTaskSnapshotCrud:
    """提供 Task 快照的单表读写，不承载快照更新规则。"""

    def __init__(self) -> None:
        """绑定进程共享的主库 session 工厂。"""

        self._session_factory = main_session_factory()

    def get(self, task_id: int) -> ConversationStateSnapshot | None:
        """读取 Task 快照；不存在时返回 ``None``。"""

        with self._session_factory() as session:
            return self.get_in_session(session, task_id)

    def get_or_create(
        self,
        task_id: int,
        fallback: ConversationStateSnapshot,
    ) -> ConversationStateSnapshot:
        """读取 Task 快照；不存在时在一个事务中创建并返回 fallback。"""

        with self._session_factory.begin() as session:
            current = self.get_in_session(session, task_id)
            if current is None:
                self.upsert_in_session(session, task_id, fallback)
                return fallback
            return current

    @staticmethod
    def get_in_session(session: Session, task_id: int) -> ConversationStateSnapshot | None:
        """在调用方事务中读取并解析 Task 快照。"""

        row = session.execute(
            select(ConversationTaskSnapshotModel).where(
                ConversationTaskSnapshotModel.task_id == task_id
            )
        ).scalar_one_or_none()
        if row is None:
            return None
        value: Any = json.loads(row.state_json)
        if not isinstance(value, dict):
            raise ValueError(f"snapshot for task {task_id} must be a JSON object")
        return cast(ConversationStateSnapshot, value)

    @staticmethod
    def upsert_in_session(
        session: Session,
        task_id: int,
        state: ConversationStateSnapshot,
    ) -> None:
        """在调用方事务中插入或更新 Task 快照。"""

        row = session.execute(
            select(ConversationTaskSnapshotModel).where(
                ConversationTaskSnapshotModel.task_id == task_id
            )
        ).scalar_one_or_none()
        encoded = json.dumps(state, ensure_ascii=False, sort_keys=True)
        if row is None:
            session.add(
                ConversationTaskSnapshotModel(
                    task_id=task_id,
                    state_json=encoded,
                    schema_version=1,
                )
            )
        else:
            row.state_json = encoded
            row.updated_at = to_text(utc_now())
        session.flush()

    def delete_by_task_ids(self, task_ids: set[int]) -> None:
        """删除一批 Task 的快照。"""

        if not task_ids:
            return
        with self._session_factory.begin() as session:
            session.execute(
                delete(ConversationTaskSnapshotModel).where(
                    ConversationTaskSnapshotModel.task_id.in_(task_ids)
                )
            )
