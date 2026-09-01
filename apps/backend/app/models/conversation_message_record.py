"""会话消息事实值对象。"""

from dataclasses import dataclass
from datetime import datetime

from app.storage.model.conversation_message_model import ConversationMessageModel
from app.utils.datetime_utils import from_text


@dataclass(frozen=True)
class ConversationMessageRecord:
    """表示一条可重建对话的 canonical message fact。"""

    id: int
    task_id: int
    turn_id: int | None
    sequence: int
    role: str
    status: str
    end_reason: str | None
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, row: ConversationMessageModel) -> "ConversationMessageRecord":
        """从 ORM 行构造消息事实值对象。

        参数:
            row: ``conversation_messages`` 表 ORM 行。

        返回:
            对应的不可变消息记录。

        异常:
            ValueError: 如果数据库时间戳不是合法 ISO-8601 文本。

        副作用:
            无。
        """

        return cls(
            id=row.id,
            task_id=row.task_id,
            turn_id=row.turn_id,
            sequence=row.sequence,
            role=row.role,
            status=row.status,
            end_reason=row.end_reason,
            created_at=from_text(row.created_at),
            updated_at=from_text(row.updated_at),
        )
