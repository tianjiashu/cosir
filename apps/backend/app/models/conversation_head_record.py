"""会话版本头持久化值对象。"""

from dataclasses import dataclass
from datetime import datetime

from app.storage.model.conversation_head_model import ConversationHeadModel
from app.utils.datetime_utils import from_text


@dataclass(frozen=True)
class ConversationHeadRecord:
    """表示一个 task 的会话版本头。"""

    id: int
    task_id: int
    revision: int
    message_sequence: int
    created_at: datetime
    updated_at: datetime

    @classmethod
    def from_model(cls, row: ConversationHeadModel) -> "ConversationHeadRecord":
        """从 ORM 行构造会话版本头值对象。

        参数:
            row: ``conversation_heads`` 表 ORM 行。

        返回:
            对应的不可变版本头记录。

        异常:
            ValueError: 如果数据库时间戳不是合法 ISO-8601 文本。

        副作用:
            无。
        """

        return cls(
            id=row.id,
            task_id=row.task_id,
            revision=row.revision,
            message_sequence=row.message_sequence,
            created_at=from_text(row.created_at),
            updated_at=from_text(row.updated_at),
        )
